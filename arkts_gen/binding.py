"""Python binding for the generated ArkTS parser (Phase 7).

Loads build/libarkts.so (see build_lib.py), wraps py-tree-sitter, and adds
helpers used by tooling:

  parse()             source text or file -> Tree
  iter_nodes()        walk a tree (or subtree) top-down
  has_errors()        ERROR/MISSING nodes present?
  to_syntaxkind()     node type -> fork SyntaxKind name + int (Phase 6 table,
                      conditional entries resolved by structural predicate)
"""

from __future__ import annotations

import ctypes
import json
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Language, Node, Parser, Tree

REPO = Path(__file__).resolve().parent.parent
LIB_PATH = REPO / "build" / "libarkts.so"
KIND_MAP_PATH = REPO / "build" / "mapping" / "node_type_to_syntaxkind.json"

_language: Language | None = None
_parser: Parser | None = None
_kind_table: dict | None = None


# ---------------------------------------------------------------------------
# Language / parser setup
# ---------------------------------------------------------------------------


def language(lib_path: Path = LIB_PATH) -> Language:
    """Load (and cache) the ArkTS language from the shared library."""
    global _language
    if _language is None:
        lib = ctypes.CDLL(str(lib_path))
        fn = lib.tree_sitter_arkts
        fn.restype = ctypes.c_void_p
        ptr = fn()
        if not ptr:
            raise RuntimeError(f"tree_sitter_arkts() returned NULL from {lib_path}")
        capsule_new = ctypes.pythonapi.PyCapsule_New
        capsule_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        capsule_new.restype = ctypes.py_object
        capsule: object = capsule_new(ptr, b"tree_sitter.Language", None)
        _language = Language(capsule)
    return _language


def parser(lib_path: Path = LIB_PATH) -> Parser:
    """A configured Parser (new instance each call; cheap)."""
    p = Parser(language(lib_path))
    return p


def parse(source: str | bytes, lib_path: Path = LIB_PATH) -> Tree:
    """Parse source text (str or bytes) into a Tree."""
    if isinstance(source, str):
        source = source.encode("utf-8")
    return parser(lib_path).parse(source)


def parse_file(path: Path | str, lib_path: Path = LIB_PATH) -> Tree:
    p = Path(path)
    return parse(p.read_bytes(), lib_path)


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------


def iter_nodes(node: Node) -> Iterator[Node]:
    """Top-down walk over all named and anonymous nodes."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def has_errors(tree_or_node: Tree | Node) -> bool:
    root = tree_or_node.root_node if isinstance(tree_or_node, Tree) else tree_or_node
    return bool(root.has_error)


def error_nodes(tree_or_node: Tree | Node) -> list[Node]:
    root = tree_or_node.root_node if isinstance(tree_or_node, Tree) else tree_or_node
    return [n for n in iter_nodes(root) if n.type == "ERROR" or n.is_missing]


# ---------------------------------------------------------------------------
# Fork SyntaxKind mapping (Phase 6 table + structural predicates)
# ---------------------------------------------------------------------------


def kind_table() -> dict:
    global _kind_table
    if _kind_table is None:
        _kind_table = json.loads(KIND_MAP_PATH.read_text(encoding="utf-8"))["table"]
    return _kind_table


def to_syntaxkind(node: Node) -> dict | None:
    """Fork SyntaxKind {kind, int} for a node, or None if unmapped.

    Conditional entries are resolved by structural predicates; unknown
    conditional shapes fall back to the entry whose `when` says default.
    """
    entry = kind_table().get(node.type)
    if entry is None:
        return None
    if "kind" in entry:
        return {"kind": entry["kind"], "int": entry["int"]}
    for c in entry["conditional"]:
        if c["kind"] is not None and _conditional_matches(node, c["kind"]):
            return {"kind": c["kind"], "int": c["int"]}
    # fall back to the last (default) entry; a None default = no mapping
    c = entry["conditional"][-1]
    if c["kind"] is None:
        return None
    return {"kind": c["kind"], "int": c["int"]}


def _directive_context(node: Node) -> str | None:
    """ "import"/"export" if a directive rule wraps `node`, else None.

    Walks up the parent chain (py-tree-sitter exposes node.parent) to the
    nearest directive rule. Grammar (arkts_grammar/grammar.js):
    import side is `bindings -> import_directive`; export side wraps
    `selective_bindings` directly in selective_export_directive /
    export_type_directive / re_export_directive (inside export_directive).
    """
    export_rules = frozenset(
        {
            "export_directive",
            "single_export_directive",
            "selective_export_directive",
            "export_type_directive",
            "re_export_directive",
        }
    )
    parent = node.parent
    while parent is not None:
        if parent.type == "import_directive":
            return "import"
        if parent.type in export_rules:
            return "export"
        parent = parent.parent
    return None


def _conditional_matches(node: Node, kind: str | None) -> bool:
    if kind == "NamedImports":
        return node.type == "selective_bindings" and _directive_context(node) == "import"
    if kind == "NamedExports":
        return node.type == "selective_bindings" and _directive_context(node) == "export"
    if kind == "ImportSpecifier":
        return node.type == "name_binding" and _directive_context(node) == "import"
    if kind == "ExportSpecifier":
        return node.type == "name_binding" and _directive_context(node) == "export"
    if kind == "ExportAssignment":
        # `export default ...`: the "default" token immediately follows
        # "export" (children[1]); `export { a as default }` has "default" as
        # its LAST child and is NOT this form
        return node.type == "single_export_directive" and len(node.children) > 1 and node.children[1].type == "default"
    if kind == "SpreadAssignment":
        return (
            node.type == "spread_expression"
            and node.parent is not None
            and node.parent.type == "record_literal_element"
        )
    if kind == "StructDeclaration":
        return any(c.type == "struct" for c in node.children)
    if kind == "ClassDeclaration":
        return any(c.type == "class" for c in node.children)
    if kind == "EtsComponentExpression":
        call_args = next((c for c in node.children if c.type == "call_arguments"), None)
        return call_args is not None and any(c.type == "trailing_lambda" for c in call_args.children)
    if kind == "CallExpression":
        call_args = next((c for c in node.children if c.type == "call_arguments"), None)
        return call_args is not None and not any(c.type == "trailing_lambda" for c in call_args.children)
    if kind == "GetAccessor":
        # modifiers (`static`, `abstract`, `final`, `override`, `native`) may
        # precede the "get" keyword, so check any child, not children[0]
        return any(c.type == "get" for c in node.children)
    if kind == "SetAccessor":
        return any(c.type == "set" for c in node.children)
    if kind == "PostfixUnaryExpression":
        return bool(node.children) and node.children[-1].type in ("++", "--")
    if kind == "PrefixUnaryExpression":
        return bool(node.children) and node.children[0].type in ("++", "--")
    if kind == "TrueKeyword":
        return node.text == b"true"
    if kind == "FalseKeyword":
        return node.text == b"false"
    if kind == "VoidExpression":
        return bool(node.children) and node.children[0].type == "void"
    if kind == "LabeledStatement":
        # loop_statement with a leading `identifier ':'` label group
        return (
            bool(node.children)
            and node.children[0].type == "identifier"
            and len(node.children) > 1
            and node.children[1].type == ":"
        )
    if kind == "ModuleDeclaration":
        return any(c.type == "module_header" for c in node.children)
    if kind == "SourceFile":
        return not any(c.type == "module_header" for c in node.children)
    if kind == "TemplateExpression":
        return any(c.type == "embedded_expression" for c in node.children)
    if kind == "NoSubstitutionTemplateLiteral":
        return not any(c.type == "embedded_expression" for c in node.children)
    return False
