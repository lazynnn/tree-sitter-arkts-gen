"""Phase 1 — parse grammar.ebnf production bodies into a typed AST.

Node types:
  Terminal(value)          '...' quoted terminal
  NonTerminal(name)        bare identifier reference
  CharClass(value)         [...] character class
  Tilde(child)             ~X negation prefix (applies to char classes)
  Group(body)              ( alternation )
  Optional(child)          X?
  Repeat(child)            X*   zero or more
  Repeat1(child)           X+   one or more
  Sequence(items)          item item ...
  Alternation(alternatives) seq | seq | ...

External tokens (ALL_UPPERCASE undefined refs, e.g. UNICODE_CLASS_LL) are
marked `external: true` on the NonTerminal node. Unresolved refs (anything
else undefined) are reported and left in place — spec does not describe
them, so downstream phases ignore them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from arkts_gen.preprocess.ebnf import Production, tokenize_ebnf


class EbnfAstError(Exception):
    """Raised when a production body does not parse as EBNF."""


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


@dataclass
class Terminal:
    value: str


@dataclass
class NonTerminal:
    name: str
    external: bool = False


@dataclass
class CharClass:
    value: str


@dataclass
class Tilde:
    child: Node


@dataclass
class Group:
    body: Alternation


@dataclass
class Optional:
    child: Node


@dataclass
class Repeat:
    child: Node


@dataclass
class Repeat1:
    child: Node


@dataclass
class Sequence:
    items: list[Node] = field(default_factory=list)


@dataclass
class Alternation:
    alternatives: list[Sequence] = field(default_factory=list)


@dataclass
class ProductionAST:
    name: str
    body: Alternation


Node = Terminal | NonTerminal | CharClass | Tilde | Group | Optional | Repeat | Repeat1


# ---------------------------------------------------------------------------
# Recursive-descent parser over the EBNF token stream
# ---------------------------------------------------------------------------


def parse_body(body: str, name: str = "<unnamed>") -> Alternation:
    """Parse a rendered production body string into an Alternation AST."""
    toks = tokenize_ebnf(body)
    if not toks:
        raise EbnfAstError(f"{name}: empty body")
    alt, i = _alternation(toks, i=0, name=name)
    if i != len(toks):
        raise EbnfAstError(f"{name}: unexpected trailing token {toks[i].val!r}")
    return alt


def _alternation(toks, i: int, name: str) -> tuple[Alternation, int]:
    alternatives: list[Sequence] = []
    seq, i = _sequence(toks, i, name)
    alternatives.append(seq)
    while i < len(toks) and toks[i].kind == "punct" and toks[i].val == "|":
        seq, i = _sequence(toks, i + 1, name)
        alternatives.append(seq)
    return Alternation(alternatives), i


def _sequence(toks, i: int, name: str) -> tuple[Sequence, int]:
    items: list[Node] = []
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct" and t.val in ("|", ")"):
            break
        item, i = _item(toks, i, name)
        items.append(item)
    if not items:
        raise EbnfAstError(f"{name}: empty sequence in body")
    return Sequence(items), i


def _item(toks, i: int, name: str) -> tuple[Node, int]:
    primary, i = _primary(toks, i, name)
    if i < len(toks) and toks[i].kind == "punct" and toks[i].val in ("?", "*", "+"):
        op = toks[i].val
        i += 1
        if op == "?":
            return Optional(primary), i
        if op == "*":
            return Repeat(primary), i
        return Repeat1(primary), i
    return primary, i


def _primary(toks, i: int, name: str) -> tuple[Node, int]:
    if i >= len(toks):
        raise EbnfAstError(f"{name}: unexpected end of body")
    t = toks[i]
    if t.kind == "term":
        return Terminal(t.val), i + 1
    if t.kind == "charclass":
        return CharClass(t.val), i + 1
    if t.kind == "ident":
        return NonTerminal(t.val), i + 1
    if t.kind == "tilde":
        child, i = _primary(toks, i + 1, name)
        return Tilde(child), i
    if t.kind == "punct" and t.val == "(":
        alt, i = _alternation(toks, i + 1, name)
        if i >= len(toks) or toks[i].val != ")":
            raise EbnfAstError(f"{name}: unclosed group")
        return Group(alt), i + 1
    raise EbnfAstError(f"{name}: unexpected token {t.val!r} ({t.kind})")


# ---------------------------------------------------------------------------
# Grammar-level: parse all productions, mark externals
# ---------------------------------------------------------------------------


def build_ast(prods: list[Production]) -> tuple[list[ProductionAST], dict]:
    """Parse Production bodies into ProductionAST list + summary report dict."""
    asts: list[ProductionAST] = []
    errors: list[dict] = []
    for p in prods:
        try:
            asts.append(ProductionAST(name=p.name, body=parse_body(p.body, p.name)))
        except EbnfAstError as exc:
            errors.append({"production": p.name, "error": str(exc)})

    defined = {a.name for a in asts}
    refs = _collect_refs(asts)
    undefined = sorted(refs - defined)

    external_re = re.compile(r"^[A-Z][A-Z0-9_]*$")
    externals = sorted(r for r in undefined if external_re.match(r))
    unresolved = sorted(r for r in undefined if not external_re.match(r))

    for a in asts:
        _mark_externals(a.body, set(externals))

    report = {
        "production_count": len(asts),
        "parse_errors": errors,
        "externals": externals,
        "unresolved": unresolved,
        "references": len(refs),
    }
    return asts, report


def _collect_refs(asts: list[ProductionAST]) -> set[str]:
    refs: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, NonTerminal):
            refs.add(node.name)
        elif isinstance(node, (Tilde, Optional, Repeat, Repeat1)):
            walk(node.child)
        elif isinstance(node, Group):
            walk(node.body)
        elif isinstance(node, Sequence):
            for it in node.items:
                walk(it)
        elif isinstance(node, Alternation):
            for seq in node.alternatives:
                walk(seq)

    for a in asts:
        walk(a.body)
    return refs


def _mark_externals(alt: Alternation, externals: set[str]) -> None:
    def walk(node) -> None:
        if isinstance(node, NonTerminal):
            if node.name in externals:
                node.external = True
        elif isinstance(node, (Tilde, Optional, Repeat, Repeat1)):
            walk(node.child)
        elif isinstance(node, Group):
            walk(node.body)
        elif isinstance(node, Sequence):
            for it in node.items:
                walk(it)
        elif isinstance(node, Alternation):
            for seq in node.alternatives:
                walk(seq)

    walk(alt)


# ---------------------------------------------------------------------------
# JSON serialization (tagged dicts)
# ---------------------------------------------------------------------------


def node_to_json(node) -> dict:
    if isinstance(node, Terminal):
        return {"t": "term", "v": node.value}
    if isinstance(node, NonTerminal):
        d = {"t": "nt", "v": node.name}
        if node.external:
            d["external"] = True
        return d
    if isinstance(node, CharClass):
        return {"t": "cc", "v": node.value}
    if isinstance(node, Tilde):
        return {"t": "tilde", "c": node_to_json(node.child)}
    if isinstance(node, Group):
        return {"t": "grp", "c": node_to_json(node.body)}
    if isinstance(node, Optional):
        return {"t": "opt", "c": node_to_json(node.child)}
    if isinstance(node, Repeat):
        return {"t": "rep", "c": node_to_json(node.child)}
    if isinstance(node, Repeat1):
        return {"t": "rep1", "c": node_to_json(node.child)}
    if isinstance(node, Sequence):
        return {"t": "seq", "items": [node_to_json(it) for it in node.items]}
    if isinstance(node, Alternation):
        return {"t": "alt", "alts": [node_to_json(s) for s in node.alternatives]}
    raise TypeError(f"unknown node type: {type(node)!r}")


def ast_to_json(asts: list[ProductionAST]) -> dict:
    return {"productions": [{"name": a.name, "body": node_to_json(a.body)} for a in asts]}


def node_from_json(d: dict):
    t = d["t"]
    if t == "term":
        return Terminal(value=d["v"])
    if t == "nt":
        return NonTerminal(name=d["v"], external=bool(d.get("external", False)))
    if t == "cc":
        return CharClass(value=d["v"])
    if t == "tilde":
        return Tilde(child=node_from_json(d["c"]))
    if t == "grp":
        return Group(body=node_from_json(d["c"]))
    if t == "opt":
        return Optional(child=node_from_json(d["c"]))
    if t == "rep":
        return Repeat(child=node_from_json(d["c"]))
    if t == "rep1":
        return Repeat1(child=node_from_json(d["c"]))
    if t == "seq":
        return Sequence(items=[node_from_json(it) for it in d["items"]])
    if t == "alt":
        return Alternation(alternatives=[node_from_json(s) for s in d["alts"]])
    raise ValueError(f"unknown AST node tag: {t!r}")


def ast_from_json(data: dict) -> list[ProductionAST]:
    return [ProductionAST(name=p["name"], body=node_from_json(p["body"])) for p in data["productions"]]
