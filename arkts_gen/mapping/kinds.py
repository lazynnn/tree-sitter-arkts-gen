"""Fork `SyntaxKind` extraction and `node_type_to_syntaxkind.json` mapping helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path


def extract_syntax_kinds(types_ts: Path) -> dict[str, int]:
    """SyntaxKind enum entries with their fork-order integer values.

    Line-based parse of the enum block only: real entries are `Name,` or
    `Name = N,`; `Marker = OtherKind` alias lines (FirstStatement =
    VariableStatement, ...) are NOT kinds and are skipped. Auto-indexed in
    file order, with explicit `= N` values resetting the counter.
    """
    text = types_ts.read_text(encoding="utf-8")
    start = text.index("export const enum SyntaxKind {")
    end = text.index("\n}", start)
    kinds: dict[str, int] = {}
    idx = 0
    for line in text[start:end].splitlines()[1:]:
        s = line.strip()
        if not s or s.startswith(("//", "/*", "*")):
            continue
        s = re.sub(r"//.*$", "", s).strip().rstrip(",")
        if not s:
            continue
        m = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)(?:\s*=\s*(.+))?", s)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if value is not None and not re.fullmatch(r"-?\d+", value.strip()):
            continue  # Marker = OtherKind alias, not a kind
        if value is not None:
            idx = int(value)
        if name in kinds:
            continue
        kinds[name] = idx
        idx += 1
    return kinds


def load_node_types(node_types_json: Path) -> set[str]:
    """Named node types of the generated grammar."""

    data = json.loads(node_types_json.read_text(encoding="utf-8"))
    return {n["type"] for n in data if isinstance(n, dict) and n.get("named")}
