"""Emit Tree-sitter `grammar.js` from the typed EBNF AST."""

from __future__ import annotations

import re
from dataclasses import dataclass

from arkts_gen.preprocess.ebnf_ast import (
    Alternation,
    CharClass,
    Group,
    NonTerminal,
    Optional,
    ProductionAST,
    Repeat,
    Repeat1,
    Sequence,
    Terminal,
    Tilde,
    _collect_refs,
)

# Token-production seeds: roots of the lexical grammar that must become
# single tree-sitter tokens. Reachability closure is computed from these.
TOKEN_SEEDS = {
    "identifier",
    "IntegerLiteral",
    "FloatLiteral",
    "BigIntLiteral",
    "BooleanLiteral",
    "StringLiteral",
    "MultilineStringLiteral",
    "NullLiteral",
    "UndefinedLiteral",
    "CharLiteral",
    "BackticksContentCharacter",
    "LineContinuation",
    "escapeSequence",
}

# Lexical token overrides (data; rationale recorded in scanner_needs.json):
#  - BackticksContentCharacter -> hidden chunk token `_template_chars`.
#    Spec's content char allows `$` and `{`, making `${...}` interpolations
#    ambiguous against plain content. `$` is excluded from the chunk; the
#    grammar offers a bare `"$"` alternative, and tree-sitter's
#    longest-match lexer picks `"${"` over `"$"` — disambiguation without
#    lookahead (which tree-sitter regexes do not support).
_TEMPLATE_ESCAPE = (
    r"\\(?:['\"bfnrtv0\\]"
    r"|x(?:[0-9a-fA-F])(?:[0-9a-fA-F])"
    r"|u(?:[0-9a-fA-F])(?:[0-9a-fA-F])(?:[0-9a-fA-F])(?:[0-9a-fA-F])"
    r"|u\{(?:(?:[0-9a-fA-F]))+\}"
    r"|[^1-9xu\r\n])"
)
_TEMPLATE_LINE_CONTINUATION = r"(?:\\(?:[\r\n\u2028\u2029])+)"
_TOKEN_OVERRIDES = {
    "BackticksContentCharacter": {
        "js_name": "_template_chars",
        "regex": (r"(?:[^`\\\r\n$]|" + _TEMPLATE_ESCAPE + "|" + _TEMPLATE_LINE_CONTINUATION + ")+"),
    },
}

# Emit as string literals (not a regex) so keyword extraction & lexical precedence beat identifier regexes.
_STRING_TOKEN_OVERRIDES = {
    "BooleanLiteral": 'choice("true", "false")',
}

# Spec 2.11: "A semicolon must be used in some cases to separate syntax
# productions written in a single line" — i.e. statements, declarations and
# members accept an optional ';' terminator in addition to the line
# separator. The Grammar Summary omits these trailing ';'s, but the fork's
# parser accepts them (corpus evidence: the dominant parse-error cause).
# Every listed production gets `seq(body, optional(";"))`.
_TERMINATOR_SUFFIX = {
    # declarations used as statements (blocks, top level, namespaces)
    "variableDeclarations",
    "constantDeclarations",
    # type and module-level declarations
    "classDeclaration",
    "interfaceDeclaration",
    "enumDeclaration",
    "typeAlias",
    "namespaceDeclaration",
    "annotationDeclaration",
    "functionDeclaration",
    "explicitFunctionOverload",
    "ambientDeclaration",
    # members
    "classMember",
    "interfaceMember",
    "enumMember",
    "annotationField",
    "ambientClassMember",
    "ambientFunctionDeclaration",
    "ambientMethodDeclaration",
    "ambientFieldDeclaration",
    "ambientInterfaceMember",
    # statements with an optional terminator (throw only; return/break/
    # continue carry ASI terminators via _SYNTACTIC_OVERRIDES — JS
    # restricted productions, fork-matching)
    "throwStatement",
    # directives
    "importDirective",
    "exportDirective",
}

# The spec defines MultilineStringLiteral (no interpolation) and
# stringInterpolation (with) as separate productions, but over the same
# surface syntax: a template with zero interpolations matches both. They are
# merged into one structural rule; stringInterpolation references alias to
# it. A flat token is impossible here: `${` boundaries need grammar-level
# disambiguation (see _TOKEN_OVERRIDES note).
_TEMPLATE_RULE = (
    "    multiline_string_literal: $ => seq(\n"
    '      "`",\n'
    "      repeat(choice(\n"
    "        $._template_chars,\n"
    "        $.embedded_expression,\n"
    '        "$",\n'
    "      )),\n"
    '      "`",\n'
    "    ),"
)
_MERGED_ALIASES = {"stringInterpolation": "multiline_string_literal"}

# Flattening fixes: fragments appended to a production's flattened regex
# (Grammar Summary omissions confirmed by the corpus oracle).
# identifierPart omits `_` — spec 2.6 allows underscores inside identifiers
# (every real identifier like `add_arrow` split into two tokens without it).
_FLATTEN_APPEND = {
    "identifierPart": "|_",
    # identifierStart omits `$` — ArkUI resource references (`$r(...)`,
    # `$rawfile(...)`) start with it (corpus evidence: ArkUI code)
    "identifierStart": r"|\$",
}

# Corpus-oracle fixes for Grammar Summary omissions (spec prose defines these;
# the fork's parser and real ArkTS code use them). Each is documented in
# scanner_needs.json / emit_report.json.
_SYNTACTIC_OVERRIDES = {
    # real syntax is `import { A, B as C } from "m"` — the Grammar Summary's
    # selectiveBindings omits the surrounding braces (also used by export
    # directives, same fix)
    "selectiveBindings": (
        "seq(\n"
        '      "{",\n'
        "      $.name_binding,\n"
        '      repeat(seq(",", $.name_binding)),\n'
        '      optional(","),\n'
        '      "}",\n'
        "    )"
    ),
    # statements declare variables via `let`/`const` keyword forms; the
    # Grammar Summary's constantOrVariableDeclaration omits the keywords
    # (only the plural top-level forms have them), so `let` inside any
    # block failed to parse
    # the fork's parser accepts `const x: T;` without an initializer (the
    # missing initializer is a semantic error, not a syntax error); the
    # Grammar Summary marks the initializer as required
    "constantDeclaration": (
        "seq(\n"
        '      field("name", $.identifier),\n'
        '      optional(seq(":", field("type", $.type))),\n'
        "      optional($.initializer),\n"
        "    )"
    ),
    # spec form kept (typed default `a: T = e`, optional `a?: T`) plus the
    # untyped-default form `a = e` that real ArkTS and the fork parser
    # accept (corpus evidence: arkts-no-* -ok test files)
    "optionalParameter": (
        "choice(\n"
        '      seq(field("name", $.identifier), ":", field("type", $.type),\n'
        '           "=", field("default", $.expression)),\n'
        '      seq(field("name", $.identifier), "?", ":", field("type", $.type)),\n'
        '      seq(field("name", $.identifier), "=", field("default", $.expression)),\n'
        "    )"
    ),
    # ---- ambient (.d.ets) fixes; Grammar Summary omissions confirmed by
    # parsing the fork's .d.ets corpus (tests/dets, SDK-style files) ----
    # `import lazy from '...'` — es2panda rewinds the lazy keyword unless
    # followed by `{` or a default identifier; offering `lazy` as a binding
    # name lets the GLR fork resolve by the next token
    "defaultBinding": (
        'choice(\n      seq(optional("type"), field("name", $.identifier)),\n      field("name", "lazy"),\n    )'
    ),
    # real files put decorators before `export` (`@Styles export declare
    # ...`); the Summary only allows export-then-annotation
    "topDeclaration": (
        "choice(\n"
        '      seq(optional(seq("export", optional("default"))),\n'
        "           repeat($.annotation_usage), TOP_DECL_BODY),\n"
        "      seq(repeat($.annotation_usage),\n"
        '           optional(seq("export", optional("default"))), TOP_DECL_BODY),\n'
        "    )".replace(
            "TOP_DECL_BODY",
            "choice(\n"
            "        $.type_declaration, $.variable_declarations,\n"
            "        $.constant_declarations, $.function_declaration,\n"
            "        $.explicit_function_overload, $.namespace_declaration,\n"
            "        $.ambient_declaration, $.annotation_declaration,\n"
            "        $.accessor_declaration, $.function_with_receiver_declaration,\n"
            "      )",
        )
    ),
    # Summary writes `'class' | 'struct' identifier ...` — missing parens,
    # so the class form matches the bare keyword and everything after fails
    # Summary writes `'const' | 'let' list ';'` — missing parens; `var` is
    # missing but used by real ambient files
    "ambientConstantOrVariableDeclaration": (
        "seq(\n"
        '      choice("const", "let", "var"),\n'
        "      $.ambient_constant_or_variable_list,\n"
        '      optional(";"),\n'
        "    )"
    ),
    # ambient methods carry type parameters (`static from<T>(...)`)
    "ambientMethodDeclaration": (
        "seq(\n"
        "      repeat($.ambient_method_modifier),\n"
        '      field("name", choice($.identifier, "get", "set")),\n'
        "      optional($.type_parameters),\n"
        "      $.signature,\n"
        ""
        "    )"
    ),
    # namespace elements are implicitly ambient (no `declare`), may carry
    # decorators, and `ambientConstantDeclaration` is the spec's dangling
    # reference (erratum) — the defined form is
    # ambientConstantOrVariableDeclaration
    "ambientNamespaceElementDeclaration": (
        "seq(\n"
        "      repeat($.annotation_usage),\n"
        '      optional("export"),\n'
        "      choice($.ambient_constant_or_variable_declaration,\n"
        "             $.ambient_function_declaration,\n"
        "             $.explicit_function_overload,\n"
        "             $.ambient_class_declaration,\n"
        "             $.ambient_interface_declaration,\n"
        "             $.ambient_namespace_declaration,\n"
        "             $.ambient_accessor_declaration,\n"
        "             $.enum_declaration, $.type_alias),\n"
        "    )"
    ),
    # ambient class members carry decorators (`@State count: number;`) and
    # may omit the type (`private toggle;`)
    "ambientClassMember": (
        "seq(\n"
        "      repeat($.annotation_usage),\n"
        "      optional($.ambient_access_modifier),\n"
        "      choice($.ambient_field_declaration, $.ambient_constructor_declaration,\n"
        "             $.ambient_method_declaration, $.ambient_class_accessor_declaration,\n"
        "             $.ambient_indexer_declaration, $.ambient_call_signature_declaration,\n"
        "             $.ambient_iterable_declaration),\n"
        ""
        "    )"
    ),
    "ambientFieldDeclaration": (
        "seq(\n"
        "      repeat($.ambient_field_modifier),\n"
        '      field("name", choice($.identifier, "get", "set")),\n'
        '      optional("?"),\n'
        '      optional(seq(":", field("type", $.type))),\n'
        ""
        "    )"
    ),
    # TS-style ambient modules: `declare module "m" { members }` — the
    # Summary's moduleDeclaration has no braced-body form (corpus: dets
    # baselines use it)
    "moduleDeclaration": (
        "seq(\n"
        "      optional(seq($.module_header,\n"
        '                    optional(seq("{",\n'
        "                                 repeat(choice($.ambient_namespace_element,\n"
        "                                              $.export_directive)),\n"
        '                                 "}")))),\n'
        "      repeat(choice($.import_directive, $.top_declaration,\n"
        "                    $.statement, $.export_directive,\n"
        '                    seq($.module_header, "{",\n'
        "                        repeat(choice($.ambient_namespace_element,\n"
        "                                     $.export_directive)),\n"
        '                        "}"))),\n'
        "    )"
    ),
    # real files use both `export declare ...` and `declare export ...`;
    # ambientAnnotationDeclaration re-requires `declare` (spec bug) — the
    # outer declare already consumed it
    "ambientDeclaration": (
        "seq(\n"
        '      "declare",\n'
        '      optional("export"),\n'
        "      choice($.ambient_constant_or_variable_declaration,\n"
        "             $.ambient_function_declaration,\n"
        "             $.explicit_function_overload,\n"
        "             $.ambient_class_declaration,\n"
        "             $.ambient_interface_declaration,\n"
        "             $.ambient_namespace_declaration,\n"
        "             $.annotation_declaration,\n"
        "             $.ambient_accessor_declaration,\n"
        "             $.enum_declaration, $.type_alias),\n"
        "    )"
    ),
    # `get`/`set` accessor keywords collide with methods NAMED get/set
    # (`get(key: K): V`). Accessor and method forms are structurally
    # distinct (accessor: get/set + name + parens; method: name + parens),
    # so accepting get/set as method names disambiguates by the next token
    # (corpus evidence: @arkts.collections.d.ets, Map interface)
    "classMethodDeclaration": (
        "seq(\n"
        "      repeat($.method_modifier),\n"
        "      optional($.type_parameters),\n"
        '      field("name", choice($.identifier, "get", "set")),\n'
        "      $.signature, optional($.block),\n"
        "    )"
    ),
    "interfaceMethodDeclaration": (
        "choice(\n"
        '      seq(field("name", choice($.identifier, "get", "set")),\n'
        "           $.signature),\n"
        "      $.interface_default_method_declaration,\n"
        "    )"
    ),
    "ambientInterfaceMethodDeclaration": (
        "seq(\n"
        '      optional("default"),\n'
        '      field("name", choice($.identifier, "get", "set")),\n'
        "      $.signature,\n"
        "    )"
    ),
    # class members stack decorators (`@Builder @BuilderParam ...`,
    # `@State @Watch ...`) — production parser evidence (es2panda tests)
    "classMember": (
        "seq(\n"
        "      repeat($.annotation_usage),\n"
        "      optional($.access_modifier),\n"
        "      choice($.constructor_declaration, $.explicit_constructor_overload,\n"
        "             $.class_field_declaration, $.class_method_declaration,\n"
        "             $.explicit_class_method_overload, $.class_accessor_declaration),\n"
        "    )"
    ),
    # fields may omit the type entirely (`@State empty;`) — production
    # parser evidence (es2panda tests)
    "classFieldDeclaration": (
        "seq(\n"
        "      repeat($.field_modifier),\n"
        '      field("name", choice($.identifier, "get", "set")),\n'
        "      choice(\n"
        '        seq(optional("?"), ":", field("type", $.type),\n'
        "             optional($.initializer)),\n"
        '        seq(optional("?"), optional($.initializer)),\n'
        '        seq("!", ":", field("type", $.type)),\n'
        "      ),\n"
        "    )"
    ),
    # `import lazy { x } from '...'` — HarmonyOS lazy imports (production
    # parser: es2panda statementParser.cpp KEYW_LAZY handling); the Summary
    # only knows the `type` modifier
    "importDirective": (
        'seq(\n      "import", optional(choice("type", "lazy")),\n      $.bindings, "from", $.import_path,\n    )'
    ),
    # ambient function params may omit types (`declare function f(a, b);`)
    # — production parser evidence (es2panda tests)
    "requiredParameter": (
        'seq(\n      field("name", $.identifier),\n      optional(seq(":", field("type", $.type))),\n    )'
    ),
    # ambient classes accept modifiers (`export declare abstract class`)
    "ambientClassDeclaration": (
        "seq(\n"
        '      repeat(choice("abstract", "final")),\n'
        '      choice("class", "struct"),\n'
        '      field("name", $.identifier),\n'
        "      optional($.type_parameters),\n"
        "      optional($.class_extends_clause),\n"
        "      optional($.implements_clause),\n"
        '      "{", repeat($.ambient_class_member), "}",\n'
        "    )"
    ),
    # JS restricted productions (fork-matching): the terminator's external
    # ASI scanner runs before the internal lexer, so at a line separator the
    # zero-width ASI token wins and the optional label/expression never
    # continues across the newline (`return\n1` = empty return + statement,
    # exactly what ts.createSourceFile produces)
    "returnStatement": (
        'seq(\n      "return", optional($.expression),\n      choice(";", $._automatic_semicolon_insertion),\n    )'
    ),
    "breakStatement": (
        "seq(\n"
        '      "break", optional(field("label", $.identifier)),\n'
        '      choice(";", $._automatic_semicolon_insertion),\n'
        "    )"
    ),
    "continueStatement": (
        "seq(\n"
        '      "continue", optional(field("label", $.identifier)),\n'
        '      choice(";", $._automatic_semicolon_insertion),\n'
        "    )"
    ),
    "constantOrVariableDeclaration": (
        "seq(\n"
        "      optional($.annotation_usage),\n"
        "      choice($.variable_declarations, $.constant_declarations),\n"
        "    )"
    ),
    # Use rhs_expression for when_false so `a ? b : c = d` parses as `a ? b : (c = d)`
    # instead of reducing `(a ? b : c)` before `=`.
    "ternaryConditionalExpression": (
        "seq(\n"
        '      field("condition", $.expression),\n'
        '      "?",\n'
        '      field("when_true", $.expression),\n'
        '      ":",\n'
        '      field("when_false", $.rhs_expression),\n'
        "    )"
    ),
}

# New productions defined in spec prose but missing from the Grammar Summary.
_EXTRA_RULES = {
    # spec 9.9.2 Explicit Constructor Call: `super(...)` / `this(...)` as a
    # statement (a compile-time error if used as an expression)
    "explicit_constructor_call": (
        "    explicit_constructor_call: $ => choice(\n"
        '      seq("super", $.call_arguments, optional(";")),\n'
        '      seq("this", $.call_arguments, optional(";")),\n'
        "    ),"
    ),
}

# Extra alternatives appended to a production's choice (corpus-driven).
_EXTRA_ALTERNATIVES = {
    # empty statements (`;;`) and explicit constructor calls
    # (`super(...)` / `this(...)`) — production parser evidence (es2panda)
    "statement": ["$.explicit_constructor_call", '";"'],
    # JS `void` operator (`void 0`) — both production parsers accept it;
    # without this, `void` silently parses as an identifier and the operand
    # becomes a stray statement (silent misparse, no ERROR nodes).
    # Extra alternatives bypass emit_node's field pass, so the operand's
    # field label is written inline — it matches the spec'd unary forms'
    # `operand` label (the field heuristics use the rule-name key
    # "expression" for those). unaryExpression has NO rule-level prec
    # (its alternatives split prefix/postfix levels via
    # _POSTFIX_PREFIX_PRECEDENCE and an outer prec would mask those values),
    # so this alternative carries the prefix value itself: `void a + b`
    # must reduce to `(void a) + b`.
    "unaryExpression": ['prec.left(26, seq("void", field("operand", $.expression)))'],
}

# Spec 2.11: "Declarations and statements are usually terminated by a line
# separator." Line separators are invisible to the grammar (whitespace is an
# extra), so statement termination needs an external, zero-width token
# implemented in src/scanner.c (same approach as tree-sitter-javascript).
_ASI_TOKEN = "_automatic_semicolon_insertion"

# External lexical refs -> regex unicode property escapes.
UNICODE_PROPS = {
    "UNICODE_CLASS_LU": "Lu",
    "UNICODE_CLASS_LL": "Ll",
    "UNICODE_CLASS_LT": "Lt",
    "UNICODE_CLASS_LM": "Lm",
    "UNICODE_CLASS_LO": "Lo",
    "UNICODE_CLASS_ND": "Nd",
}

# Operator precedence & associativity per spec §7.1.1 (×2 scale so & > ^ > | fit cleanly).
# Higher value binds tighter; postfix/prefix unary ops use _POSTFIX_PREFIX_PRECEDENCE instead.
_PRECEDENCE = {
    "ensureNotNullishExpression": (28, "left"),  # postfix `expr!` binds tighter than prefix ops (26)
    "typeOfExpression": (26, "left"),
    "awaitExpression": (26, "left"),
    "exponentiationExpression": (24, "right"),
    "multiplicativeExpression": (22, "left"),
    "additiveExpression": (20, "left"),
    # castExpression / instanceOfExpression deliberately carry NO static
    # precedence (see the arrayType note below): their `[`-lookahead decision
    # against array_type is genuinely ambiguous (`x as T[]` vs
    # `type N = obj['x']`) and stays a declared GLR conflict resolved per
    # input. A static prec on the cast reduce would always beat the
    # un-prec'd array_type shift and break every `expr as T[]`.
    "shiftExpression": (16, "left"),
    "relationalExpression": (14, "left"),
    "equalityExpression": (12, "left"),
    "conditionalAndExpression": (4, "left"),
    "conditionalOrExpression": (3, "left"),
    "nullishCoalescingExpression": (2, "left"),
    "ternaryConditionalExpression": (1, "right"),
    "assignmentExpression": (0, "right"),
    # Below lhsExpression (0) so `a = b = c` reduces lhs_expression first (right-assoc).
    "rhsExpression": (-1, "left"),
    # arrayType has no static prec so `[` after a type stays a GLR conflict (`T[]` vs `obj['x']`).
}

# §7.1.1 has three separate bitwise levels, but the Grammar Summary defines
# ONE production (bitwiseAndLogicalExpression). It is emitted as a single
# rule (one node type; the kind mapping is a flat rename) whose alternatives
# each carry their own precedence, keyed by the operator terminal.
_ALTERNATIVE_PRECEDENCE = {
    "bitwiseAndLogicalExpression": {
        "&": (10, "left"),
        "^": (8, "left"),
        "|": (6, "left"),
    },
}

# unaryExpression merges prefix (26) and postfix ++/-- (28); key by position since `++`
# appears in both. Omitted from _PRECEDENCE so an outer wrapper doesn't mask these.
_POSTFIX_PREFIX_PRECEDENCE = {
    "unaryExpression": {
        "prefix": (26, "left"),
        "postfix": (28, "left"),
    },
}

# binaryExpression's alternatives — the productions whose two expression
# operands the field heuristics label left/right. Deliberately NOT derived
# from _PRECEDENCE: cast/instanceOf operands keep their `type` field, and
# ternary/assignment/unary have their own field branches.
_BINARY_OPERAND_FIELD_RULES = frozenset(
    {
        "multiplicativeExpression",
        "exponentiationExpression",
        "additiveExpression",
        "shiftExpression",
        "relationalExpression",
        "equalityExpression",
        "bitwiseAndLogicalExpression",
        "conditionalAndExpression",
        "conditionalOrExpression",
    }
)

_GRAMMAR_NAME = "arkts"

# Regex chars needing escape outside a char class (Rust/JS regex subset).
_REGEX_SPECIAL = set(r"\.^$*+?{}[]|()/")

_COMMENT_RULE = (
    "comment: $ => token(choice(\n"
    "      seq('//', /(\\\\(.|\\r?\\n)|[^\\\\\\n\\r\\u2028\\u2029])*/),\n"
    "      seq('/*', /[^*]*\\*+([^/*][^*]*\\*+)*/, '/'),\n"
    "    ))"
)


def to_snake(name: str) -> str:
    """camelCase -> snake_case (tree-sitter node naming convention)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _ebnf_unescape(value: str) -> str:
    """Resolve EBNF terminal escapes: \\' -> ', \\\\ -> \\, and
    \\uXXXX -> the actual character."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        if value[i] == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in ("'", "\\"):
                out.append(nxt)
                i += 2
                continue
            if nxt == "u" and i + 5 < n and _is_hex(value[i + 2 : i + 6]):
                out.append(chr(int(value[i + 2 : i + 6], 16)))
                i += 6
                continue
        out.append(value[i])
        i += 1
    return "".join(out)


def _is_hex(s: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in s)


def _regex_escape(text: str) -> str:
    """Escape a literal string for use in a regex (outside char classes)."""
    return "".join("\\" + c if c in _REGEX_SPECIAL else c for c in text)


def _cc_body(cc: str) -> str:
    """Char-class inner text, EBNF-unescaped (\\' -> ') and regex-ready."""
    inner = cc[1:-1] if cc.startswith("[") and cc.endswith("]") else cc
    return inner.replace("\\'", "'")


@dataclass
class EmitReport:
    token_rules: list[str]
    inlined_lexical: list[str]
    dropped_refs: list[str]
    precedence: dict[str, int]
    rule_count: int


class GrammarEmitter:
    def __init__(
        self,
        asts: list[ProductionAST],
        conflicts: list[list[str]] | None = None,
        fields: dict[str, dict[str, str]] | None = None,
    ):
        self.conflicts = conflicts or []
        self.fields = fields or {}
        self.asts = asts
        self.by_name = {a.name: a for a in asts}
        self.refs_of = self._reference_map()
        self.token_rules: set[str] = set()
        self.inline_only: set[str] = set()
        self.dropped: list[str] = []
        self.prec: dict[str, int] = {}
        self._classify()
        # Explicit §7.1.1 table (see _PRECEDENCE); only rules the spec
        # actually defines are kept (and listed in the emit report).
        self.prec = {n: p for n, p in _PRECEDENCE.items() if n in self.by_name}

    # -- classification ----------------------------------------------------

    def _reference_map(self) -> dict[str, set[str]]:
        refs = {}
        for a in self.asts:
            refs[a.name] = {r for r in _collect_refs([a]) if r in self.by_name}
        return refs

    def _classify(self) -> None:
        """Compute the lexical closure from TOKEN_SEEDS; split into named
        token rules (referenced from syntactic productions) and inline-only
        members."""
        closure: set[str] = set()
        stack = [s for s in TOKEN_SEEDS if s in self.by_name]
        while stack:
            n = stack.pop()
            if n in closure:
                continue
            closure.add(n)
            stack.extend(self.refs_of.get(n, ()))
        syntactic = [a.name for a in self.asts if a.name not in closure]
        used_from_syntactic: set[str] = set()
        for n in syntactic:
            used_from_syntactic |= self.refs_of[n] & closure
        self.token_rules = used_from_syntactic
        self.inline_only = closure - used_from_syntactic

    # -- lexical flattening (node -> regex fragment) -----------------------

    def flatten(self, node) -> str:
        if isinstance(node, Terminal):
            return _regex_escape(_ebnf_unescape(node.value))
        if isinstance(node, CharClass):
            return "[" + _cc_body(node.value) + "]"
        if isinstance(node, Tilde):
            return "[^" + _cc_body(node.child.value) + "]"
        if isinstance(node, NonTerminal):
            if node.name in UNICODE_PROPS:
                return "\\p{" + UNICODE_PROPS[node.name] + "}"
            if node.name in _TOKEN_OVERRIDES:
                return _TOKEN_OVERRIDES[node.name]["regex"]
            target = self.by_name[node.name]
            frag = self.flatten(target.body)
            if node.name in _FLATTEN_APPEND:
                # append INSIDE the group: ((frag)|x) — an outer-level
                # alternation would escape the surrounding concatenation
                frag = "(" + frag + _FLATTEN_APPEND[node.name] + ")"
            return frag
        if isinstance(node, Optional):
            return "(?:" + self.flatten(node.child) + ")?"
        if isinstance(node, Repeat):
            return "(?:" + self.flatten(node.child) + ")*"
        if isinstance(node, Repeat1):
            return "(?:" + self.flatten(node.child) + ")+"
        if isinstance(node, Alternation):
            parts = [self._flatten_seq_content(s) for s in node.alternatives]
            return "(?:" + "|".join(parts) + ")"
        if isinstance(node, Sequence):
            return self._flatten_seq_content(node)
        if isinstance(node, Group):
            return self.flatten(node.body)
        raise TypeError(f"cannot flatten {type(node)!r}")

    def _flatten_seq_content(self, seq: Sequence) -> str:
        # each item is self-grouping where needed; concat is safe because
        # opt/rep/rep1/alt wrap themselves in (?:...)
        return "".join(self.flatten(it) for it in seq.items)

    # -- structural emit (node -> tree-sitter DSL string) ------------------

    @staticmethod
    def _matches_empty(node) -> bool:
        """True if the node's language includes the empty string."""
        if isinstance(node, (Repeat, Optional)):
            return True
        if isinstance(node, Group):
            return GrammarEmitter._matches_empty(node.body)
        if isinstance(node, Alternation):
            return any(GrammarEmitter._matches_empty(s) for s in node.alternatives)
        if isinstance(node, Sequence):
            return all(GrammarEmitter._matches_empty(it) for it in node.items)
        return False

    def emit_node(self, node, in_repeat: bool = False, fields: dict | None = None):
        """Return DSL string for a syntactic node, or None to drop
        (unresolved reference). `in_repeat` marks that the node sits inside
        a repeat, where empty-matching wrappers collapse. `fields` is the
        production-level field manifest applied to top-level sequence items
        (keys: item index as string, or child rule name in snake_case)."""
        if isinstance(node, Terminal):
            return _js_string(_ebnf_unescape(node.value))
        if isinstance(node, NonTerminal):
            if node.name in _TOKEN_OVERRIDES:
                return "$." + _TOKEN_OVERRIDES[node.name]["js_name"]
            if node.name in _MERGED_ALIASES:
                return "$." + _MERGED_ALIASES[node.name]
            if node.name in self.by_name:
                target = self.by_name[node.name]
                if self._matches_empty(target.body) and node.name != "moduleDeclaration":
                    body = target.body
                    # unwrap singleton wrappers: alt(seq(X)) -> X
                    if isinstance(body, Alternation) and len(body.alternatives) == 1:
                        body = body.alternatives[0]
                    if isinstance(body, Sequence) and len(body.items) == 1:
                        body = body.items[0]
                    # named rules matching the empty string are forbidden by
                    # tree-sitter; inline their body at the reference site.
                    # Inside a repeat, a bare repeat(X)/optional(X) collapses
                    # to X (the outer repeat already iterates).
                    if in_repeat and isinstance(body, (Repeat, Optional)):
                        return self.emit_node(body.child)
                    return self.emit_node(body)
                return "$." + to_snake(node.name)
            # unresolved spec erratum: drop, record
            self.dropped.append(node.name)
            return None
        if isinstance(node, (CharClass, Tilde)):
            return "token(/" + self.flatten(node) + "/)"
        if isinstance(node, Optional):
            inner = self.emit_node(node.child, fields=fields)
            return None if inner is None else f"optional({inner})"
        if isinstance(node, Repeat):
            child = node.child
            if isinstance(child, Group):
                child = child.body
            if isinstance(child, Alternation):
                alts = []
                for s in child.alternatives:
                    e = self._emit_repeat_alt(s)
                    if e is not None:
                        alts.append(e)
                if not alts:
                    return None
                if len(alts) == 1:
                    return f"repeat({alts[0]})"
                inner = "choice(\n" + ",\n".join("      " + a for a in alts) + ",\n    )"
                return f"repeat({inner})"
            if isinstance(child, (Repeat, Optional)):
                child = child.child  # repeat(repeat(x)) == repeat(x)
            inner = self.emit_node(child, fields=fields)
            return None if inner is None else f"repeat({inner})"
        if isinstance(node, Repeat1):
            inner = self.emit_node(node.child, fields=fields)
            return None if inner is None else f"repeat1({inner})"
        if isinstance(node, Group):
            return self.emit_node(node.body, fields=fields)
        if isinstance(node, Alternation):
            alts = []
            for s in node.alternatives:
                e = self.emit_node(s, fields=fields)
                if e is not None:
                    alts.append(e)
            if not alts:
                return None
            if len(alts) == 1:
                return alts[0]
            return "choice(\n" + ",\n".join("      " + a for a in alts) + ",\n    )"
        if isinstance(node, Sequence):
            items = []
            # in_repeat only applies when this sequence is the whole
            # repeated unit (single item); multi-item sequences must keep
            # their inner empty-matching wrappers intact
            child_in_repeat = in_repeat and len(node.items) == 1
            for idx, it in enumerate(node.items):
                if fields and isinstance(it, (Group, Optional, Repeat, Repeat1)):
                    # index keys are top-level only; rule-name keys apply
                    # recursively into wrappers and groups
                    name_map = {k: v for k, v in fields.items() if not k.isdigit()}
                    e = self.emit_node(it, fields=name_map)
                else:
                    e = self.emit_node(it, child_in_repeat)
                if e is not None:
                    if fields:
                        fname = fields.get(str(idx))
                        if fname is None:
                            fname = fields.get(_wrapped_rule_name(it))
                        if fname:
                            e = f'field("{fname}", {e})'
                    items.append(e)
            if not items:
                return None
            if len(items) == 1:
                return items[0]
            return "seq(\n" + ",\n".join("      " + i for i in items) + ",\n    )"
        raise TypeError(f"cannot emit {type(node)!r}")

    def _emit_repeat_alt(self, seq: Sequence):
        """Emit one alternative of a choice inside a repeat. A single-item
        alternative that is Repeat(X) or Optional(X) collapses to X (the
        outer repeat already iterates and covers the empty case)."""
        if len(seq.items) == 1 and isinstance(seq.items[0], (Repeat, Optional)):
            return self.emit_node(seq.items[0].child)
        return self.emit_node(seq, in_repeat=True)

    # -- rule emission ------------------------------------------------------

    def _rule_for(self, a: ProductionAST) -> str | None:
        if a.name == "MultilineStringLiteral":
            return _TEMPLATE_RULE
        if a.name in _MERGED_ALIASES:
            return None  # merged into its alias target
        if a.name in _SYNTACTIC_OVERRIDES:
            name = to_snake(a.name)
            body = _SYNTACTIC_OVERRIDES[a.name]
            # _TERMINATOR_SUFFIX applies to overrides exactly as to normal
            # rules — one mechanism, no hand-carried optional(";") (this
            # trap fired twice: ambientClassMember, classMember)
            if a.name in _TERMINATOR_SUFFIX:
                body = "seq(\n      " + body + ',\n      optional(";"),\n    )'
            # _PRECEDENCE applies to overrides exactly as to normal rules,
            # same wrap order (terminator suffix first, then prec) — the
            # ternary override is the only prec-carrying override today;
            # without this, the override body would silently lose its
            # (1, right) wrapper and with it the whole ternary level.
            if a.name in self.prec:
                value, assoc = self.prec[a.name]
                body = f"prec.{assoc}({value}, {body})"
            return f"    {name}: $ => {body},"
        if a.name == "expressionStatement":
            # spec 2.11: statements are terminated by a line separator or a
            # semicolon; the line-separator case is the external ASI token
            return (
                "    expression_statement: $ => seq(\n"
                "      $.expression,\n"
                '      choice(";", $._automatic_semicolon_insertion),\n'
                "    ),"
            )
        if a.name in _TOKEN_OVERRIDES:
            ov = _TOKEN_OVERRIDES[a.name]
            return f"    {ov['js_name']}: $ => token(/{ov['regex']}/),"
        name = to_snake(a.name)
        if a.name in self.token_rules:
            if a.name in _STRING_TOKEN_OVERRIDES:
                return f"    {name}: $ => {_STRING_TOKEN_OVERRIDES[a.name]},"
            return f"    {name}: $ => token(/" + self.flatten(a.body) + "/),"
        if a.name in self.inline_only:
            return None  # folded into consumers' regexes
        if a.name != "moduleDeclaration" and self._matches_empty(a.body):
            return None  # inlined at reference sites; no named rule
        if a.name in _ALTERNATIVE_PRECEDENCE:
            # One spec production spanning several spec levels: emit each
            # alternative with its own prec (see _ALTERNATIVE_PRECEDENCE);
            # the rule and node type stay single (flat kind-mapping rename).
            ops = _ALTERNATIVE_PRECEDENCE[a.name]
            alts = []
            for seq in a.body.alternatives:
                op = next(_ebnf_unescape(t.value) for t in seq.items if isinstance(t, Terminal))
                value, assoc = ops[op]
                e = self.emit_node(seq, fields=self.fields.get(a.name))
                if e is not None:
                    alts.append(f"prec.{assoc}({value}, {e})")
            if not alts:
                return None
            if len(alts) == 1:
                body = alts[0]
            else:
                body = "choice(\n" + ",\n".join("      " + x for x in alts) + ",\n    )"
        elif a.name in _POSTFIX_PREFIX_PRECEDENCE:
            # Same per-alternative prec emission as above, but keyed on
            # operator POSITION because the operator terminals collide —
            # see _POSTFIX_PREFIX_PRECEDENCE.
            ops = _POSTFIX_PREFIX_PRECEDENCE[a.name]
            alts = []
            for seq in a.body.alternatives:
                kind = "prefix" if isinstance(seq.items[0], Terminal) else "postfix"
                value, assoc = ops[kind]
                e = self.emit_node(seq, fields=self.fields.get(a.name))
                if e is not None:
                    alts.append(f"prec.{assoc}({value}, {e})")
            if not alts:
                return None
            body = "choice(\n" + ",\n".join("      " + x for x in alts) + ",\n    )"
        else:
            body = self.emit_node(a.body, fields=self.fields.get(a.name))
        if body is None:
            return None
        # Wrap order: terminator suffix -> extra alternatives -> outermost prec().
        if a.name in _TERMINATOR_SUFFIX:
            body = "seq(\n      " + body + ',\n      optional(";"),\n    )'
        if a.name in _EXTRA_ALTERNATIVES:
            extras = ", ".join(_EXTRA_ALTERNATIVES[a.name])
            body = f"choice(\n      {body},\n      {extras},\n    )"
        if a.name in self.prec:
            value, assoc = self.prec[a.name]
            body = f"prec.{assoc}({value}, {body})"
        return f"    {name}: $ => {body},"

    def build(self) -> tuple[str, EmitReport]:
        rules: list[str] = []
        # start symbol first: tree-sitter uses the first rule as the goal
        order = sorted(self.asts, key=lambda a: (a.name != "moduleDeclaration",))
        for a in order:
            r = self._rule_for(a)
            if r:
                rules.append(r)
        rules.extend(_EXTRA_RULES.values())
        rules.append("    " + _COMMENT_RULE + ",")

        conflict_lines = [
            "  externals: $ => [",
            f"    $.{_ASI_TOKEN},",
            "  ],\n",
        ]
        if self.conflicts:
            conflict_lines.append("  conflicts: $ => [")
            for group in self.conflicts:
                refs = ", ".join("$." + r for r in group)
                conflict_lines.append(f"    [{refs}],")
            conflict_lines.append("  ],\n")

        text = (
            "module.exports = grammar({\n"
            f"  name: '{_GRAMMAR_NAME}',\n\n"
            "  word: $ => $.identifier,\n\n"
            # Whitespace extras are exactly the spec's lexical input
            # elements: §2.3 white spaces (U+0020, U+0009, U+000B, U+000C,
            # U+00A0 NBSP, U+FEFF ZWNBSP) plus §2.4 line separators
            # (U+000A LF, U+000D CR, U+2028 LS, U+2029 PS — "often
            # handled as white spaces"). `\s` supplies the ASCII members
            # (\t\n\v\f\r space); tree-sitter does NOT expand `\s` to
            # Unicode White_Space, so the rest must be listed explicitly —
            # otherwise the parser cannot skip them between tokens.
            "  extras: $ => [/[\\s\\u00a0\\ufeff\\u2028\\u2029]/, $.comment],\n\n"
            + "\n".join(conflict_lines)
            + "  rules: {\n"
            + "\n".join(rules)
            + "\n  },\n"
            "});\n"
        )
        precedence = {n: value for n, (value, _) in self.prec.items()}
        # per-alternative levels are reported per operator so every
        # spec level is visible (bitwiseAndLogicalExpression.& etc.)
        precedence.update(
            {f"{rule}.{op}": value for rule, ops in _ALTERNATIVE_PRECEDENCE.items() for op, (value, _) in ops.items()}
        )
        precedence.update(
            {
                f"{rule}.{kind}": value
                for rule, kinds in _POSTFIX_PREFIX_PRECEDENCE.items()
                for kind, (value, _) in kinds.items()
            }
        )
        report = EmitReport(
            token_rules=sorted(to_snake(n) for n in self.token_rules),
            inlined_lexical=sorted(to_snake(n) for n in self.inline_only),
            dropped_refs=sorted(set(self.dropped)),
            precedence=precedence,
            rule_count=len(rules),
        )
        return text, report


def _walk_nonterminals(node) -> list[str]:
    """All NonTerminal names in a subtree, any depth."""
    if isinstance(node, NonTerminal):
        return [node.name]
    if isinstance(node, (Tilde, Optional, Repeat, Repeat1)):
        return _walk_nonterminals(node.child)
    if isinstance(node, Group):
        return _walk_nonterminals(node.body)
    if isinstance(node, Sequence):
        out: list[str] = []
        for it in node.items:
            out += _walk_nonterminals(it)
        return out
    if isinstance(node, Alternation):
        out = []
        for s in node.alternatives:
            out += _walk_nonterminals(s)
        return out
    return []


def build_default_fields(emitter: GrammarEmitter) -> dict[str, dict[str, str]]:
    """Heuristic field manifest: every production whose children include
    identifier / type / *members / parameters gets labels, plus left/right
    on binary operator productions, operand on unary, and
    condition/when_true/when_false on the ternary. The manifest is data —
    edit fields.json to override; heuristics only seed it.
    """

    def seqs_of(body) -> list[Sequence]:
        if isinstance(body, Alternation):
            return body.alternatives
        return [body]

    manifest: dict[str, dict[str, str]] = {}
    for a in emitter.asts:
        if a.name in emitter.token_rules or a.name in emitter.inline_only:
            continue
        if a.name == "moduleDeclaration":
            continue
        entry: dict[str, str] = {}
        for seq in seqs_of(a.body):
            expr_idx = [i for i, it in enumerate(seq.items) if isinstance(it, NonTerminal)]
            if a.name == "ternaryConditionalExpression":
                for i, fname in zip(expr_idx, ["condition", "when_true", "when_false"], strict=True):
                    entry[str(i)] = fname
                continue
            if a.name in ("unaryExpression", "typeOfExpression", "awaitExpression"):
                # rule-name key, NOT index keys: the operator terminals must
                # stay unlabeled - only the expression operand is "operand".
                # Index keys would label the operators too (they apply to
                # every alternative, including the prefix forms). typeof and
                # await take the same rule-name key: their single expression
                # child is the operand, mirroring the fork's own AST label.
                entry["expression"] = "operand"
                continue
            if a.name in _BINARY_OPERAND_FIELD_RULES and len(expr_idx) >= 2:
                entry[str(expr_idx[0])] = "left"
                entry[str(expr_idx[-1])] = "right"
            for name in _walk_nonterminals(seq):
                s = to_snake(name)
                if s == "identifier":
                    entry.setdefault("identifier", "name")
                elif s == "type":
                    entry.setdefault("type", "type")
                elif s.endswith("members"):
                    entry.setdefault(s, "members")
                elif s == "parameters":
                    entry.setdefault("parameters", "parameters")
        if entry:
            manifest[a.name] = entry
    return manifest


def _wrapped_rule_name(item) -> str | None:
    """Rule name (snake) of a wrapper item's direct nonterminal child:
    Optional/Repeat/Repeat1/Group over a single NonTerminal."""
    node = item
    for _ in range(4):
        if isinstance(node, NonTerminal):
            return to_snake(node.name)
        if isinstance(node, (Optional, Repeat, Repeat1, Tilde)):
            node = node.child
        else:
            # Group contents are labeled by the recursive (inner) pass;
            # wrapping again here would nest field() inside field()
            return None
    return None


def _js_string(value: str) -> str:
    """JS string literal for a terminal's matched text."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
