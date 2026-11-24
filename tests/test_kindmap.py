"""Tests for `node_type_to_syntaxkind.json` mapping, conditional predicates, and CLI validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OVERRIDES = REPO / "kind_map_overrides.json"


def _iter(b, tree):
    return b.iter_nodes(tree.root_node)


def _kind(b, src, node_type, index=0):
    """Fork kind of the index-th node of `node_type` in `src`.

    Returns (node, kind_or_None); asserts the node exists and that the
    parse has no errors.
    """
    tree = b.parse(src)
    assert not b.has_errors(tree), f"unexpected parse errors in {src!r}"
    hits = [n for n in _iter(b, tree) if n.type == node_type]
    assert len(hits) > index, f"no {node_type} in parse of {src!r}"
    return hits[index], b.to_syntaxkind(hits[index])


def _assert_kind(b, src, node_type, kind, int_, index=0):
    node, kd = _kind(b, src, node_type, index)
    assert kd == {"kind": kind, "int": int_}, (node_type, src, kd)
    return node


def test_struct_declaration_maps_to_structdeclaration(binding):
    _assert_kind(binding, "struct S{}", "class_declaration", "StructDeclaration", 265)


def test_class_declaration_maps_to_classdeclaration(binding):
    _assert_kind(binding, "class C{}", "class_declaration", "ClassDeclaration", 264)


def test_static_get_accessor(binding):
    _assert_kind(
        binding, "class C { static get x(): number { return 1 } }", "class_accessor_declaration", "GetAccessor", 177
    )


def test_static_set_accessor(binding):
    _assert_kind(binding, "class C { static set x(v: number) {} }", "class_accessor_declaration", "SetAccessor", 178)


def test_abstract_get_accessor(binding):
    _assert_kind(binding, "class C { abstract get x(): number }", "class_accessor_declaration", "GetAccessor", 177)


def test_plain_get_and_set_unchanged(binding):
    # no modifiers: the predicate must still resolve via the `get`/`set`
    # keyword child (children[0] form)
    _assert_kind(binding, "class C { get x(): number { return 1 } }", "class_accessor_declaration", "GetAccessor", 177)
    _assert_kind(binding, "class C { set x(v: number) {} }", "class_accessor_declaration", "SetAccessor", 178)


def test_ambient_static_get_accessor(binding):
    # declare class: accessor lives under ambient_class_accessor_declaration
    _assert_kind(
        binding, "declare class C { static get x(): number }", "ambient_class_accessor_declaration", "GetAccessor", 177
    )


def test_component_call_with_trailing_lambda(binding):
    _assert_kind(binding, 'Column() { Text("x") }', "function_call_expression", "EtsComponentExpression", 220)


def test_plain_call_is_callexpression(binding):
    _assert_kind(binding, 'Text("x")', "function_call_expression", "CallExpression", 213)


def test_import_selective_bindings(binding):
    _assert_kind(binding, 'import { a } from "x"', "selective_bindings", "NamedImports", 278)
    _assert_kind(binding, 'import { a } from "x"', "name_binding", "ImportSpecifier", 279)


def test_export_selective_bindings(binding):
    _assert_kind(binding, "export { a }", "selective_bindings", "NamedExports", 282)
    _assert_kind(binding, "export { a }", "name_binding", "ExportSpecifier", 284)


def test_export_default_is_exportassignment(binding):
    _assert_kind(binding, "export default 42", "single_export_directive", "ExportAssignment", 280)


def test_export_name_as_default_is_exportdeclaration(binding):
    # THE deviation case: `export { a as default }` is a named export with
    # an alias, NOT an `export default` assignment ("default" is the LAST
    # child here, not children[1]).
    node, kd = _kind(binding, "export { a as default }", "single_export_directive")
    assert kd == {"kind": "ExportDeclaration", "int": 281}, kd
    assert kd["kind"] != "ExportAssignment"
    # the shape the predicate keys on: "default" is a trailing alias
    # child (before the closing brace), not the token right after "export"
    assert node.children[1].type != "default"
    assert node.children[-2].type == "default"


def test_export_directive_maps_to_exportdeclaration(binding):
    _assert_kind(binding, "export { a }", "export_directive", "ExportDeclaration", 281)


def test_object_spread_is_spreadassignment(binding):
    _assert_kind(binding, "let r = { ...o }", "spread_expression", "SpreadAssignment", 307)


def test_call_spread_is_spreadelement(binding):
    _assert_kind(binding, "f(...args)", "spread_expression", "SpreadElement", 231)


def test_void_expression(binding):
    node, kd = _kind(binding, "let x = void 0", "unary_expression")
    assert kd == {"kind": "VoidExpression", "int": 223}, kd
    assert node.children[0].type == "void"


def test_labeled_statement(binding):
    _assert_kind(binding, "foo: while(1){}", "loop_statement", "LabeledStatement", 257)


def test_plain_while_maps_to_none(binding):
    # negative control: a loop_statement without a label must resolve to
    # the None-default sentinel (the loop child maps by name instead)
    node, kd = _kind(binding, "while(1){}", "loop_statement")
    assert node.children[0].type == "while_statement"
    assert kd is None, kd


def test_module_declaration(binding):
    _assert_kind(binding, 'declare module "m" {}', "module_declaration", "ModuleDeclaration", 270)


def test_plain_file_is_sourcefile(binding):
    _assert_kind(binding, "let a = 1", "module_declaration", "SourceFile", 314)


def test_boolean_literal_mapping_data(binding):
    table = binding.kind_table()
    cond = table["boolean_literal"]["conditional"]
    assert {k: cond[0][k] for k in ("kind", "int")} == {"kind": "TrueKeyword", "int": 111}
    assert {k: cond[1][k] for k in ("kind", "int")} == {"kind": "FalseKeyword", "int": 96}


def test_boolean_keyword_parse_observed(binding):
    tree = binding.parse("let t = true, f = false")
    assert not binding.has_errors(tree)
    hits = [n for n in _iter(binding, tree) if n.type == "boolean_literal"]
    assert [n.text for n in hits] == [b"true", b"false"]
    kinds = [binding.to_syntaxkind(n) for n in hits]
    assert kinds == [{"kind": "TrueKeyword", "int": 111}, {"kind": "FalseKeyword", "int": 96}], kinds


def test_template_expression(binding):
    _assert_kind(binding, "let t = `a${b}c`", "multiline_string_literal", "TemplateExpression", 229)


def test_no_substitution_template(binding):
    _assert_kind(binding, "let t = `plain`", "multiline_string_literal", "NoSubstitutionTemplateLiteral", 14)


def test_postfix_unary(binding):
    node, kd = _kind(binding, "let p = 0; p++;", "unary_expression")
    assert kd == {"kind": "PostfixUnaryExpression", "int": 226}, kd
    assert node.children[-1].type == "++"


def test_prefix_unary(binding):
    node, kd = _kind(binding, "let p = 0; ++p;", "unary_expression")
    assert kd == {"kind": "PrefixUnaryExpression", "int": 225}, kd
    assert node.children[0].type == "++"


def test_rename_function_type(binding):
    _assert_kind(binding, "let f: (x: number) => void", "function_type", "FunctionType", 184)


def test_rename_function_type_with_receiver(binding):
    _assert_kind(binding, "let g: (this: T, x: number) => void", "function_type_with_receiver", "FunctionType", 184)


def test_rename_keyof_type(binding):
    _assert_kind(binding, "let k: keyof T", "keyof_type", "TypeOperator", 198)


def test_rename_type_parameter(binding):
    _assert_kind(binding, "function f<T>(x: T) {}", "type_parameter", "TypeParameter", 167)


def _run_kindmap(overrides_path: Path, out_path: Path):
    types_ts = REPO / "third_party_typescript" / "src" / "compiler" / "types.ts"
    if not types_ts.exists():
        pytest.skip(f"missing {types_ts}")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "arkts_gen.mapping.build_kind_map",
            "--types-ts",
            str(types_ts),
            "--overrides",
            str(overrides_path),
            "--out",
            str(out_path),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _base_overrides() -> dict:
    return json.loads(OVERRIDES.read_text(encoding="utf-8"))


def _write_overrides(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return p


def _assert_exit2_clean(proc, out_path, *message_parts):
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}\nstdout: {proc.stdout}"
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stdout, proc.stdout
    for part in message_parts:
        assert part in proc.stderr, f"{part!r} missing from: {proc.stderr}"
    # a validation error must not leave a (half-)written output behind
    assert not out_path.exists(), "output written despite exit 2"


def test_overrides_invalid_json(tmp_path):
    p = _write_overrides(tmp_path, "invalid-json", "{not json")
    out = tmp_path / "nested" / "map.json"
    proc = _run_kindmap(p, out)
    _assert_exit2_clean(proc, out, "is not valid JSON", str(p))


def test_overrides_top_level_not_object(tmp_path):
    p = _write_overrides(tmp_path, "top-level-list", "[]")
    out = tmp_path / "out" / "map.json"
    proc = _run_kindmap(p, out)
    _assert_exit2_clean(proc, out, "is not a JSON object", str(p))


def _probe(tmp_path, name, mutate, *message_parts):
    data = _base_overrides()
    mutate(data)
    p = _write_overrides(tmp_path, name, data)
    out = tmp_path / "out" / f"{name}.json"
    proc = _run_kindmap(p, out)
    _assert_exit2_clean(proc, out, *message_parts)
    return proc


def test_overrides_rename_as_list(tmp_path):
    _probe(tmp_path, "rename-as-list", lambda d: d.update(rename=[]), "'rename' is not an object")


def test_overrides_rename_target_not_string(tmp_path):
    proc = _probe(
        tmp_path,
        "rename-target-non-string",
        lambda d: d["rename"].update({"unary_expression": 123}),
        "'unary_expression'",
        "target is not a string",
    )
    assert "123" in proc.stderr


def test_overrides_conditional_as_list(tmp_path):
    _probe(tmp_path, "conditional-as-list", lambda d: d.update(conditional=[]), "'conditional' is not an object")


def test_overrides_conditional_entries_not_list(tmp_path):
    proc = _probe(
        tmp_path,
        "conditional-entries-not-list",
        lambda d: d["conditional"].update({"boolean_literal": "TrueKeyword"}),
        "'boolean_literal'",
        "is not a list",
    )
    assert "TrueKeyword" in proc.stderr


def test_overrides_conditional_entry_not_object(tmp_path):
    _probe(
        tmp_path,
        "conditional-entry-not-object",
        lambda d: d["conditional"]["boolean_literal"].append("x"),
        "'boolean_literal'",
        "entry is not an object",
    )


def test_overrides_conditional_entry_missing_kind(tmp_path):
    _probe(
        tmp_path,
        "conditional-entry-missing-kind",
        lambda d: d["conditional"]["boolean_literal"][0].pop("kind"),
        "missing required string 'kind'",
    )


def test_overrides_categories_as_list(tmp_path):
    _probe(
        tmp_path,
        "categories-as-list",
        lambda d: d.update(forbidden_categories=[]),
        "'forbidden_categories' is not an object",
    )


def test_overrides_bad_rename_target(tmp_path):
    # a bogus rename target must be named in the error, not silently
    # fall through to unmapped
    proc = _probe(
        tmp_path,
        "bad-rename-target",
        lambda d: d["rename"].update({"probe_node": "NoSuchKind"}),
        "rename kinds not in SyntaxKind enum",
        "NoSuchKind",
        "probe_node",
    )
    assert "NoSuchKind" in proc.stderr


def test_overrides_duplicate_overlap(tmp_path):
    # a kind that is both mapped (via rename) and forbidden (via a
    # category) is a data bug: the conditional table and the forbidden
    # categories disagree
    data = _base_overrides()
    assert data["rename"], "repo overrides carry no renames to overlap"
    mapped_kind = next(iter(data["rename"].values()))
    data["forbidden_categories"]["probe_category"] = {
        "basis": "probe: duplicates a mapped kind",
        "kinds": {mapped_kind: "probe evidence"},
    }
    p = _write_overrides(tmp_path, "overlap", data)
    out = tmp_path / "out" / "map.json"
    proc = _run_kindmap(p, out)
    _assert_exit2_clean(proc, out, "both mapped and forbidden", mapped_kind)
