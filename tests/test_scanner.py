"""Tests for scanner line separators and automatic semicolon insertion (§2.11)."""

from __future__ import annotations

import pytest


def _statements(binding, src):
    tree = binding.parse(src)
    stmts = [c for c in tree.root_node.children if c.type == "statement"]
    return tree, stmts


def _comments(binding, tree):
    return [c for c in binding.iter_nodes(tree.root_node) if c.type == "comment"]


def _clean(binding, src, n_statements):
    tree, stmts = _statements(binding, src)
    assert not binding.has_errors(tree), f"unexpected errors in {src!r}"
    assert len(stmts) == n_statements, f"{src!r}: expected {n_statements} statements, got {len(stmts)}"
    return tree, stmts


@pytest.mark.parametrize(
    "src",
    [
        "let a = 1\rlet b = 2\r",
        "let a = 1\r\nlet b = 2\r\n",
        "let a = 1\r\rlet b = 2\r",
        "let a = 1\u2028\u2028let b = 2\u2028",
        "let a = 1\rlet b = 2\u2028",
    ],
)
def test_separator_produces_two_statements(binding, src):
    _, stmts = _clean(binding, src, 2)
    assert stmts[0].text == b"let a = 1"
    assert stmts[1].text == b"let b = 2"


def test_cr_at_eof_single_statement(binding):
    _, stmts = _clean(binding, "let a = 1\r", 1)
    assert stmts[0].text == b"let a = 1"


def test_only_separators_is_clean_empty(binding):
    _clean(binding, "\r\n\u2028\u2029", 0)


def test_nbsp_blank_before_newline(binding):
    _, stmts = _clean(binding, "foo()\u00a0\nbar()", 2)
    assert stmts[0].text == b"foo()" and stmts[1].text == b"bar()"


def test_bom_blank_before_newline(binding):
    _, stmts = _clean(binding, "foo()\ufeff\nbar()", 2)
    assert stmts[0].text == b"foo()" and stmts[1].text == b"bar()"


def test_newline_then_nbsp(binding):
    _, stmts = _clean(binding, "foo()\n\u00a0bar()", 2)
    assert stmts[0].text == b"foo()" and stmts[1].text == b"bar()"


def test_nbsp_alone_is_not_a_separator(binding):
    tree, stmts = _statements(binding, "foo()\u00a0bar()")
    assert binding.has_errors(tree), "NBSP without a separator must NOT trigger ASI"
    assert len(stmts) == 1
    assert stmts[0].text == "foo()\u00a0bar()".encode()


@pytest.mark.parametrize("term", ["\r", "\u2028", "\n"])
def test_line_comment_terminated_by_separator(binding, term):
    src = f"// c{term}let x = 1\n"
    tree, stmts = _statements(binding, src)
    assert not binding.has_errors(tree), f"errors for sep {term!r}"
    comments = _comments(binding, tree)
    assert len(comments) == 1, comments
    assert comments[0].text == b"// c"
    assert len(stmts) == 1 and stmts[0].text == b"let x = 1"


def test_line_comment_backslash_lf_continuation(binding):
    src = "let a = 1 // c\\\nstill comment\nlet b = 2"
    tree, stmts = _statements(binding, src)
    assert not binding.has_errors(tree)
    comments = _comments(binding, tree)
    assert len(comments) == 1, comments
    assert comments[0].text == b"// c\\\nstill comment"
    assert len(stmts) == 2
    assert stmts[0].text == b"let a = 1"
    assert stmts[1].text == b"let b = 2"


@pytest.mark.parametrize("inner", ["\r", "\u2028"])
def test_block_comment_contains_separator(binding, inner):
    src = f"let a = 1 /* a{inner}b */ let b = 2"
    tree, stmts = _statements(binding, src)
    assert not binding.has_errors(tree), f"errors for inner {inner!r}"
    comments = _comments(binding, tree)
    assert len(comments) == 1, comments
    assert comments[0].text == f"/* a{inner}b */".encode()
    assert len(stmts) == 2
    assert stmts[0].text == b"let a = 1" and stmts[1].text == b"let b = 2"


def test_restricted_return(binding):
    src = "function f() { return\n1 }"
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    returns = [n for n in binding.iter_nodes(tree.root_node) if n.type == "return_statement"]
    assert len(returns) == 1
    assert returns[0].text == b"return"
    exprs = [n for n in binding.iter_nodes(tree.root_node) if n.type == "expression_statement"]
    assert any(e.text == b"1" for e in exprs)


def test_restricted_throw_spans_lines(binding):
    src = "throw\nx;"
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    throws = [n for n in binding.iter_nodes(tree.root_node) if n.type == "throw_statement"]
    assert len(throws) == 1
    assert throws[0].start_point.row == 0 and throws[0].end_point.row == 1
    assert b"throw\nx" in throws[0].text


def test_chain_call_continuation(binding):
    src = 'struct P { build() { Text($r("app.string.m"))\n      .fontSize(30) } }'
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    calls = [n for n in binding.iter_nodes(tree.root_node) if n.type == "method_call_expression"]
    assert len(calls) == 1, [n.text for n in calls]
    call = calls[0]
    assert call.start_point.row == 0 and call.end_point.row == 1
    assert b'Text($r("app.string.m"))' in call.text
    types = [c.type for c in call.children]
    assert types == ["object_reference", ".", "identifier", "call_arguments"]
    assert call.children[2].text == b"fontSize"


def test_return_newline_paren_known_divergence(binding):
    # Deliberate tradeoff: paren continuation wins over restricted-production ASI for ArkUI chains.
    src = "function f() { return\n(x) }"
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    returns = [n for n in binding.iter_nodes(tree.root_node) if n.type == "return_statement"]
    assert len(returns) == 1
    ret = returns[0]
    assert ret.start_point.row == 0 and ret.end_point.row == 1
    parens = [n for n in binding.iter_nodes(ret) if n.type == "parenthesized_expression"]
    assert len(parens) == 1 and parens[0].text == b"(x)"


def test_interface_comma_and_semicolon_separators(binding):
    src = "interface Foo {\n  a: number,\n  b: string;\n  c?: boolean,\n}\n"
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    members = [n for n in binding.iter_nodes(tree.root_node) if n.type == "interface_member"]
    assert len(members) == 3


@pytest.mark.parametrize(
    "src",
    [
        "let choice: typeof option;",
        "interface I { new (s: number): I }",
        "const C = class { x: number = 1 };",
        'const s = "ok" as const;',
    ],
)
def test_reserved_keywords_rejected_in_forbidden_positions(binding, src):
    assert binding.has_errors(binding.parse(src))


def test_void_and_finally_remain_valid(binding):
    assert not binding.has_errors(binding.parse("let f: (x: number) => void;"))
    assert not binding.has_errors(binding.parse("fetchData().finally();"))
