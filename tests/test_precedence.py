"""Tests for expression operator precedence, associativity, and leading-dot floats."""

from __future__ import annotations

import pytest

# wrapper node types that merely re-expose a single child; descending
# through them yields the operator node (additive_expression, ...).
_WRAPPERS = frozenset({"expression", "binary_expression", "primary_expression", "rhs_expression", "lhs_expression"})


def _init_core(binding, expr_src):
    """Parse `let r = <expr>` and return the core of the initializer."""
    prefix = "let r = "
    tree = binding.parse(prefix + expr_src)
    assert not binding.has_errors(tree), f"unexpected parse errors in {expr_src!r}"
    init = [n for n in binding.iter_nodes(tree.root_node) if n.type == "initializer"]
    assert init, f"no initializer in {expr_src!r}"
    core = init[0].children[-1]  # the expression after '='
    return _unwrap(binding, core)


def _unwrap(binding, node):
    while node.type in _WRAPPERS and len(node.children) == 1:
        node = node.children[0]
    return node


def _operands(binding, node):
    """(left, operator, right) of an operator node's non-wrapper children."""
    kids = node.children
    tokens = ("!", "-", "~", "+", "*", "**", "<", "&&", "||", "??", "|", "&", "^", "===", "typeof", "void", "as")
    ops = []
    for c in kids:
        if c.type in tokens:
            ops.append(c)
        elif c.type == "assignment_operator":
            # `=` (and friends) arrive wrapped in an operator node
            ops.append(c.children[0])
    assert ops, f"no operator token in {node.type}: {kids}"
    left = _unwrap(binding, kids[0])
    right = _unwrap(binding, kids[-1])
    return left, ops[0], right


def test_not_binds_tighter_than_and(binding):
    core = _init_core(binding, "!a && b")
    assert core.type == "conditional_and_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "&&"
    assert left.type == "unary_expression", left.type
    assert left.children[0].type == "!"
    assert left.start_byte == 8 and left.text == b"!a"
    assert right.text == b"b"


def test_unary_binds_tighter_than_additive(binding):
    core = _init_core(binding, "-a + b")
    assert core.type == "additive_expression"
    left, op, _ = _operands(binding, core)
    assert op.type == "+"
    assert left.type == "unary_expression", left.type
    assert left.children[0].type == "-"
    assert left.text == b"-a"


def test_typeof_binds_under_equality(binding):
    core = _init_core(binding, 'typeof y === "s"')
    assert core.type == "equality_expression"
    left, op, _ = _operands(binding, core)
    assert op.type == "==="
    assert left.type == "type_of_expression", left.type
    assert left.text == b"typeof y"


def test_void_unary_in_additive(binding):
    core = _init_core(binding, "void a + b")
    assert core.type == "additive_expression"
    left, op, _ = _operands(binding, core)
    assert op.type == "+"
    assert left.type == "unary_expression", left.type
    assert left.children[0].type == "void"


def test_multiplicative_does_not_bind_over_exponent(binding):
    # a * b ** c  =>  a * (b ** c): the exponentiation nests as the right
    # operand of the multiplicative node
    core = _init_core(binding, "a * b ** c")
    assert core.type == "multiplicative_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "*"
    assert left.text == b"a"
    assert right.type == "exponentiation_expression", right.type
    assert right.start_byte == len("let r = ") + 4  # starts at 'b'
    assert right.text == b"b ** c"


def test_exponentiation_is_right_associative(binding):
    core = _init_core(binding, "a ** b ** c")
    assert core.type == "exponentiation_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "**"
    assert left.text == b"a"
    assert right.type == "exponentiation_expression", right.type
    assert right.text == b"b ** c"
    assert right.start_byte == len("let r = ") + 5  # nested node starts at b


def test_and_binds_tighter_than_or(binding):
    # a | b & c  =>  a | (b & c); assert via span: the nested node starts
    # at 'b', not at 'a' (which would mean (a|b) & c)
    core = _init_core(binding, "a | b & c")
    assert core.type == "bitwise_and_logical_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "|"
    assert left.text == b"a"
    assert right.type == "bitwise_and_logical_expression", right.type
    assert right.text == b"b & c"
    assert right.start_byte == len("let r = ") + 4  # starts at 'b'


def test_full_bitwise_chain_deepest_is_and(binding):
    # a | b ^ c & d  =>  a | (b ^ (c & d)): c & d is the deepest pair
    core = _init_core(binding, "a | b ^ c & d")
    assert core.type == "bitwise_and_logical_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "|"
    assert left.text == b"a"
    assert right.text == b"b ^ c & d"
    _, xor_op, and_side = _operands(binding, right)
    assert xor_op.type == "^"
    assert and_side.text == b"c & d", and_side.text
    _, inner_op, _ = _operands(binding, and_side)
    assert inner_op.type == "&"


def test_nullish_binds_tighter_than_or(binding):
    # a || b ?? c  =>  (a || b) ?? c: the `??` is the OUTER operator
    core = _init_core(binding, "a || b ?? c")
    assert core.type == "nullish_coalescing_expression"
    left, op, right = _operands(binding, core)
    assert op.type == "??"
    assert left.type == "conditional_or_expression", left.type
    assert left.text == b"a || b"
    assert right.text == b"c"


def test_cast_is_left_chained(binding):
    # a as T as U  =>  (a as T) as U
    core = _init_core(binding, "a as T as U")
    assert core.type == "cast_expression"
    assert core.text == b"a as T as U"
    left = _unwrap(binding, core.children[0])
    assert left.type == "cast_expression", left.type
    assert left.text == b"a as T"
    assert left.start_byte == len("let r = ")  # starts at 'a'
    assert core.children[1].type == "as"


def test_cast_vs_relational_observed(binding):
    # OBSERVED (per campaign): `x < y as boolean` parses as (x < y) as
    # boolean — the cast WRAPS the relational, not the other way round.
    # Encoded here so any change to that shape is a visible test delta.
    core = _init_core(binding, "x < y as boolean")
    assert core.type == "cast_expression"
    assert core.text == b"x < y as boolean"
    left = _unwrap(binding, core.children[0])
    assert left.type == "relational_expression", left.type
    assert left.text == b"x < y"
    _, rel_op, _ = _operands(binding, left)
    assert rel_op.type == "<"
    assert core.children[1].type == "as"
    assert core.children[-1].text == b"boolean"


def _ternary_parts(binding, node):
    """(when_true, when_false) of a ternary_conditional_expression."""
    kids = node.children
    q = [i for i, c in enumerate(kids) if c.type == "?"]
    colon = [i for i, c in enumerate(kids) if c.type == ":"]
    assert q and colon, f"not a ternary: {kids}"
    when_true = _unwrap(binding, kids[q[0] + 1])
    when_false = _unwrap(binding, kids[colon[-1] + 1])
    return when_true, when_false


def test_ternary_is_right_associative(binding):
    # a ? b : c ? d : e  =>  a ? b : (c ? d : e)
    core = _init_core(binding, "a ? b : c ? d : e")
    assert core.type == "ternary_conditional_expression"
    when_true, when_false = _ternary_parts(binding, core)
    assert when_true.text == b"b"
    assert when_false.type == "ternary_conditional_expression", when_false.type
    assert when_false.text == b"c ? d : e"
    assert when_false.start_byte == len("let r = ") + 8  # nested starts at c


def test_assignment_is_right_associative(binding):
    core = _init_core(binding, "a = b = c")
    assert core.type == "assignment_expression"
    lhs, op, rhs = _operands(binding, core)
    assert op.text == b"="
    assert lhs.text == b"a"
    assert rhs.type == "assignment_expression", rhs.type
    assert rhs.text == b"b = c"
    assert rhs.start_byte == len("let r = ") + 4


def test_ternary_outer_assignment_in_when_false(binding):
    # a ? b : c = d  =>  a ? b : (c = d): the ternary is the OUTER node
    core = _init_core(binding, "a ? b : c = d")
    assert core.type == "ternary_conditional_expression", core.type
    when_true, when_false = _ternary_parts(binding, core)
    assert when_true.text == b"b"
    assert when_false.type == "assignment_expression", when_false.type
    assert when_false.text == b"c = d"


def test_assignment_inside_when_true(binding):
    # a ? b = c : d  =>  a ? (b = c) : d
    core = _init_core(binding, "a ? b = c : d")
    assert core.type == "ternary_conditional_expression"
    when_true, when_false = _ternary_parts(binding, core)
    assert when_true.type == "assignment_expression", when_true.type
    assert when_true.text == b"b = c"
    assert when_false.text == b"d"


def test_prefix_not_wraps_postfix(binding):
    # !a++  =>  !(a++); asserted by span order: the inner (postfix) node
    # starts at 'a' and ends after '++', the outer starts at '!'
    core = _init_core(binding, "!a++")
    assert core.type == "unary_expression"
    assert core.children[0].type == "!"
    assert core.start_byte == len("let r = ")
    inner = _unwrap(binding, core.children[-1])
    assert inner.type == "unary_expression", inner.type
    assert inner.text == b"a++"
    assert inner.start_byte == len("let r = ") + 1  # starts at 'a' ...
    assert inner.end_byte == len("let r = ") + 4  # ... ends after '++'
    assert inner.children[-1].type == "++"
    assert inner.children[0].text == b"a"


@pytest.mark.parametrize(
    "op,outer_type",
    [
        ("-", "unary_expression"),
        ("~", "unary_expression"),
        # OBSERVED deviation from the inventory: the outer node for
        # `typeof a++` is type_of_expression (its own rule), not
        # unary_expression; the postfix unary still nests inside it.
        ("typeof", "type_of_expression"),
    ],
)
def test_prefix_operators_wrap_postfix(binding, op, outer_type):
    core = _init_core(binding, f"{op} a++")
    assert core.type == outer_type, core.type
    assert core.children[0].type == op
    assert core.start_byte == len("let r = ")
    op_tok = core.children[0]
    inner = _unwrap(binding, core.children[-1])
    assert inner.type == "unary_expression", inner.type
    assert inner.text == b"a++"
    # span order: the postfix operand starts right after the prefix token
    # (plus the source's single space for `typeof`) and ends after `++`
    # the source has exactly one space between the prefix token and `a`
    assert inner.start_byte == op_tok.end_byte + 1
    assert inner.end_byte == op_tok.end_byte + 4


def test_standalone_postfix_and_prefix(binding):
    src = "let p = 0; p++; ++p;"
    tree = binding.parse(src)
    assert not binding.has_errors(tree)
    unary = [n for n in binding.iter_nodes(tree.root_node) if n.type == "unary_expression"]
    assert len(unary) == 2, [n.text for n in unary]
    postfix, prefix = unary  # source order
    assert postfix.text == b"p++" and postfix.children[-1].type == "++"
    assert prefix.text == b"++p" and prefix.children[0].type == "++"
    assert binding.to_syntaxkind(postfix) == {"kind": "PostfixUnaryExpression", "int": 226}
    assert binding.to_syntaxkind(prefix) == {"kind": "PrefixUnaryExpression", "int": 225}
    # binding conditional order: the `++` position decides prefix/postfix
    assert postfix.start_byte < prefix.start_byte


def test_not_wraps_ensure_not_nullish(binding):
    # !a!  =>  !(a!)
    core = _init_core(binding, "!a!")
    assert core.type == "unary_expression"
    assert core.children[0].type == "!"
    inner = _unwrap(binding, core.children[-1])
    assert inner.type == "ensure_not_nullish_expression", inner.type
    assert inner.text == b"a!"
    assert inner.children[-1].type == "!"


def test_leading_dot_float_in_expression(binding):
    core = _init_core(binding, ".5 + 1")
    assert core.type == "additive_expression"
    left, op, _ = _operands(binding, core)
    assert op.type == "+"
    assert left.text == b".5"
    assert left.type in ("float_literal", "literal"), left.type


def test_leading_dot_float_after_newline(binding):
    # post-newline `.digit`: the scanner must treat `.5` as a float
    # literal statement, not glue it onto the previous call as a member
    # access
    src = "foo()\n.5"
    tree = binding.parse(src)
    assert not binding.has_errors(tree), "leading-dot float glued to previous line"
    stmts = [c for c in tree.root_node.children if c.type == "statement"]
    assert len(stmts) == 2, [c.text for c in stmts]
    assert stmts[0].text == b"foo()"
    floats = [n for n in binding.iter_nodes(tree.root_node) if n.type == "float_literal"]
    assert floats and floats[0].text == b".5"
