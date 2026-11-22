"""Core EBNF extraction and parsing for the ArkTS spec (Phase 0).

Grammar productions live inside gray boxes (one per page) in the "Grammar
Summary" chapter. We locate that chapter via the PDF table of contents,
extract the gray-box text with PyMuPDF, then parse it with a token-stream
EBNF parser. Precedence is derived from the nonterminal hierarchy in
Phase 2; the Operator Precedence table is not extracted (redundant).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# Gray fill used for grammar boxes in this spec.
_GRAY_FILL = (0.9490200281143188, 0.9490200281143188, 0.9490200281143188)


# ---------------------------------------------------------------------------
# PDF structure: locate chapters via TOC, extract gray-box text
# ---------------------------------------------------------------------------


def _toc(doc) -> list[tuple[int, str, int]]:
    return doc.get_toc()  # [(level, title, page_1based), ...]


def _find_chapter_pages(doc, title_substr: str) -> tuple[int, int]:
    """Return (start_page_1based, end_page_1based) for a top-level chapter."""
    toc = _toc(doc)
    start = None
    for i, (lvl, title, page) in enumerate(toc):
        if lvl == 1 and title_substr.lower() in title.lower():
            start = page
            # end = page before next top-level chapter
            end = doc.page_count
            for lvl2, _, p2 in toc[i + 1 :]:
                if lvl2 == 1:
                    end = p2 - 1
                    break
            return start, end
    sys.exit(f"chapter '{title_substr}' not found in PDF table of contents")


def _gray_box_rects(page) -> list[pymupdf.Rect]:
    """Return every gray-fill drawing rect on a page (grammar boxes and
    code-sample boxes share this fill; the EBNF parser rejects non-grammar
    text, so all boxes can be scanned)."""
    rects: list[pymupdf.Rect] = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None:
            continue
        # compare with tolerance (floats)
        if len(fill) == len(_GRAY_FILL) and all(abs(a - b) < 1e-3 for a, b in zip(fill, _GRAY_FILL, strict=True)):
            rects.append(d["rect"])
    return rects


def _box_text(page, rect) -> str:
    """Extract text inside a rect via dict mode (recovers glyphs that
    get_textbox mangles, e.g. backtick -> U+FFFD)."""
    d = page.get_text("dict", clip=rect)
    out: list[str] = []
    for blk in d.get("blocks", []):
        for line in blk.get("lines", []):
            s = "".join(sp["text"] for sp in line.get("spans", []))
            if s.strip():
                out.append(s)
    return "\n".join(out)


def _iter_box_texts(doc, page_range: range):
    """Yield (page_no_1based, text) for every gray box in the page range."""
    for pno in page_range:
        page = doc[pno]
        for rect in _gray_box_rects(page):
            txt = _box_text(page, rect)
            if txt.strip():
                yield pno + 1, txt


def extract_grammar_box_text(pdf_path: Path) -> tuple[str, list[int]]:
    """Extract grammar EBNF text from gray boxes in the Grammar Summary chapter.

    Returns (text, list_of_pdf_pages_used).
    """
    doc = pymupdf.open(str(pdf_path))
    start, end = _find_chapter_pages(doc, "Grammar Summary")
    chunks: list[str] = []
    used: list[int] = []
    for page_no, txt in _iter_box_texts(doc, range(start - 1, end)):
        chunks.append(txt)
        used.append(page_no)
    return "\n".join(chunks), used


def find_prose_definitions(pdf_path: Path, names: list[str]) -> tuple[dict[str, Production], list[str]]:
    """Fallback: find definitions of `names` in gray boxes OUTSIDE the
    Grammar Summary chapter (productions defined in prose chapters but
    missing from the Summary).

    Returns (found: name -> Production, conflicts: log messages).
    """
    doc = pymupdf.open(str(pdf_path))
    start, end = _find_chapter_pages(doc, "Grammar Summary")
    summary_pages = set(range(start - 1, end))
    wanted = set(names)
    found: dict[str, Production] = {}
    conflicts: list[str] = []
    for page_no, txt in _iter_box_texts(doc, range(doc.page_count)):
        if page_no - 1 in summary_pages:
            continue
        prods, _ = parse_productions(txt)
        for p in prods:
            if p.name not in wanted:
                continue
            if p.name in found:
                conflicts.append(
                    f"{p.name}: defined again on page {page_no} "
                    f"(first: page {found[p.name].line or '?'}) — keeping first"
                )
                continue
            p.line = page_no
            found[p.name] = p
    return found, conflicts


# ---------------------------------------------------------------------------
# Artifacts: stray apostrophes, page-break markers
# ---------------------------------------------------------------------------

_PAGEBREAK_MARKERS = (
    "(continues on next page)",
    "(continued from previous page)",
)


def _strip_pagebreak_markers(line: str) -> str:
    s = line.strip()
    if s in _PAGEBREAK_MARKERS:
        return ""
    return line


# PDF line-wrap artifact characters (visible-space marker + continuation
# arrow). They appear unquoted inside productions, e.g.
# `... |␣\n˓→ exportDirective` which means `... | exportDirective`.
_PDF_ARTIFACT_CHARS = "␣˓→"


def _strip_pdf_artifacts(line: str) -> str:
    """Remove PDF line-wrap artifact characters (unquoted context only)."""
    out: list[str] = []
    in_quote = False
    for ch in line:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif not in_quote and ch in _PDF_ARTIFACT_CHARS:
            continue
        else:
            out.append(ch)
    return "".join(out)


def _normalize_quotes(line: str) -> str:
    """Normalize quoting on a grammar line (single left-to-right pass).

    - `...` backtick-quoted terminals become '...' (the spec quotes the
      `**=` operator with backticks in one production).
    - Apostrophes with no same-line closing `'` are dropped (PDF artifacts).
      `\\'` is honored as an escape (so `'c\\''` and `'\\''` survive).
    - `[...]` char-class contents are copied verbatim (e.g. `~['\\\r\n]`).
    """
    out: list[str] = []
    i = 0
    n = len(line)
    in_bracket = 0
    while i < n:
        c = line[i]
        if in_bracket:
            out.append(c)
            if c == "]":
                in_bracket -= 1
            i += 1
            continue
        if c == "[":
            in_bracket += 1
            out.append(c)
            i += 1
            continue
        if c == "`":
            j = i + 1
            while j < n and line[j] != "`":
                j += 1
            if j < n:
                out.append("'")
                out.append(line[i + 1 : j])
                out.append("'")
                i = j + 1
            else:
                out.append(c)  # unmatched backtick: leave, parser errors on it
                i += 1
            continue
        if c == "'":
            j = i + 1
            closed = False
            while j < n:
                if line[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if line[j] == "'":
                    closed = True
                    break
                j += 1
            if closed:
                out.append(line[i : j + 1])
                i = j + 1
            else:
                i += 1  # stray apostrophe: drop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _clean_grammar_text(text: str) -> str:
    """Strip page-break markers, PDF artifacts, and stray apostrophes."""
    out_lines = []
    for ln in text.splitlines():
        ln = _strip_pagebreak_markers(ln)
        ln = _strip_pdf_artifacts(ln)
        ln = _normalize_quotes(ln)
        out_lines.append(ln)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Tokenizer + token-stream EBNF parser
# ---------------------------------------------------------------------------


@dataclass
class Tok:
    kind: str  # 'ident' | 'term' | 'charclass' | 'tilde' | 'punct'
    val: str


_PUNCT = set(":;|()?*+,")


def tokenize_ebnf(text: str) -> list[Tok]:
    toks: list[Tok] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "'":
            # terminal with \' escapes
            j = i + 1
            buf: list[str] = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j : j + 2])
                    j += 2
                    continue
                if text[j] == "'":
                    break
                buf.append(text[j])
                j += 1
            toks.append(Tok("term", "".join(buf)))
            i = j + 1  # past closing ' (or EOF)
        elif c == "[":
            j = i + 1
            while j < n and text[j] != "]":
                j += 1
            toks.append(Tok("charclass", text[i : j + 1]))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(Tok("ident", text[i:j]))
            i = j
        elif c == "~":
            toks.append(Tok("tilde", "~"))
            i += 1
        elif c in _PUNCT:
            toks.append(Tok("punct", c))
            i += 1
        else:
            # any other char (e.g. '.', '=', '<') as a punct token; only
            # appears in prose -> helps reject non-grammar chunks
            toks.append(Tok("punct", c))
            i += 1
    return toks


def _render_body(toks: list[Tok]) -> str:
    parts: list[str] = []
    for t in toks:
        if t.kind == "term":
            parts.append("'" + t.val.strip() + "'")
        elif t.kind in ("charclass", "ident"):
            parts.append(t.val)
        elif t.kind == "tilde":
            parts.append("~")
        else:
            parts.append(t.val)
    s = " ".join(parts)
    s = re.sub(r"\s+([?*+])", r"\1", s)  # postfix: no space before ? * +
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Production model + parser
# ---------------------------------------------------------------------------


@dataclass
class Production:
    name: str
    body: str
    raw_body: str
    line: int


@dataclass
class GrammarReport:
    production_count: int
    productions: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    undefined_references: list[str] = field(default_factory=list)
    terminals: list[str] = field(default_factory=list)
    suspicious_terminals: list[str] = field(default_factory=list)
    unterminated: list[str] = field(default_factory=list)
    externals: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    prose_sourced: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def classify_undefined(refs: list[str]) -> tuple[list[str], list[str]]:
    """Split undefined refs into (externals, unresolved).

    Convention: ALL_UPPERCASE names (e.g. UNICODE_CLASS_LL) are external
    lexical tokens implemented in Phase 4. Anything else is unresolved —
    reported and ignored downstream (spec does not describe it).
    """
    external_re = re.compile(r"^[A-Z][A-Z0-9_]*$")
    externals = sorted(r for r in refs if external_re.match(r))
    unresolved = sorted(r for r in refs if not external_re.match(r))
    return externals, unresolved


def parse_productions(grammar_text: str) -> tuple[list[Production], GrammarReport]:
    """Parse cleaned grammar text into Production objects.

    A production is `IDENT : <body> ;`. The header is only valid if the
    previous significant token was `;` (or start of stream); this rejects
    prose fragments like `.. BigIntLiteral: IntegerLiteral 'n';` that share
    the page with the grammar.
    """
    text = _clean_grammar_text(grammar_text)
    toks = tokenize_ebnf(text)
    prods: list[Production] = []
    report = GrammarReport(production_count=0)
    defined: set[str] = set()
    seen_order: list[str] = []
    refs: set[str] = set()
    terminals: set[str] = set()
    suspicious: set[str] = set()

    i = 0
    N = len(toks)
    prev_was_semi = True  # start of stream acts like "just after ;"
    while i < N:
        t = toks[i]
        is_header = t.kind == "ident" and i + 1 < N and toks[i + 1].kind == "punct" and toks[i + 1].val == ":"
        if is_header and prev_was_semi:
            name = t.val
            j = i + 2
            body_toks: list[Tok] = []
            while j < N and not (toks[j].kind == "punct" and toks[j].val == ";"):
                body_toks.append(toks[j])
                j += 1
            terminated = j < N and toks[j].val == ";"
            body = _render_body(body_toks)
            prods.append(Production(name=name, body=body, raw_body=body, line=0))
            if name in defined:
                report.duplicates.append(name)
            defined.add(name)
            seen_order.append(name)
            if not terminated:
                report.unterminated.append(name)
            for ref in extract_references(body):
                refs.add(ref)
            for term in extract_terminals(body):
                terminals.add(term)
                if _is_suspicious_terminal(term):
                    suspicious.add(term)
            i = j + 1 if terminated else j
            prev_was_semi = True
            continue
        # not a valid header start: just track separator state
        prev_was_semi = t.kind == "punct" and t.val == ";"
        i += 1

    report.production_count = len(prods)
    report.productions = seen_order
    report.undefined_references = sorted(r for r in refs if r not in defined)
    report.terminals = sorted(terminals)
    report.suspicious_terminals = sorted(suspicious)
    return prods, report


# ---------------------------------------------------------------------------
# Terminal / reference extraction (operate on a rendered body string)
# ---------------------------------------------------------------------------


def extract_references(body: str) -> list[str]:
    """Unquoted identifiers in a rendered body = nonterminal references.

    Uses the tokenizer (single left-to-right scan, quote- and
    bracket-aware) so terminals and char classes are excluded correctly.
    """
    return [t.val for t in tokenize_ebnf(body) if t.kind == "ident"]


def extract_terminals(body: str) -> list[str]:
    """Quoted '...' terminals in a rendered body (tokenizer-based)."""
    return [f"'{t.val}'" for t in tokenize_ebnf(body) if t.kind == "term"]


def _is_suspicious_terminal(term: str) -> bool:
    inner = term[1:-1] if len(term) >= 2 else term
    if not inner:
        return False
    return inner != inner.strip()


# ---------------------------------------------------------------------------
# EBNF serialization
# ---------------------------------------------------------------------------

_HEADER = (
    "// ArkTS Grammar Summary\n"
    "// Source: {source}\n"
    "// Productions: {count}\n"
    "// Auto-generated; do not edit by hand.\n\n"
)


def serialize_ebnf(prods: list[Production], source: str) -> str:
    out = [_HEADER.format(source=source, count=len(prods))]
    for p in prods:
        body = p.body
        out.append(f"{p.name}:")
        out.append(f"    {body}" if body else "    ")
        out.append(";")
        out.append("")
    return "\n".join(out)
