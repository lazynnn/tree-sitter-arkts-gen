"""Build the Tree-sitter node type -> fork `SyntaxKind` mapping table (`build/mapping/node_type_to_syntaxkind.json`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arkts_gen.mapping.kinds import extract_syntax_kinds, load_node_types

REPO = Path(__file__).resolve().parent.parent.parent

# enum marker alias lines that kinds.py deliberately does not extract as
# kinds, but that are legitimately listed in forbidden_categories
ALLOWED_MARKER_NAMES = frozenset({"FirstStatement", "LastStatement", "FirstTypeNode", "LastTypeNode"})


def run_kind_map(
    types_ts: Path,
    node_types: Path = REPO / "arkts_grammar/src/node-types.json",
    overrides_path: Path = REPO / "kind_map_overrides.json",
    out_path: Path = REPO / "build" / "mapping" / "node_type_to_syntaxkind.json",
) -> int:
    kinds = extract_syntax_kinds(types_ts)
    our_types = load_node_types(node_types)
    overrides: dict = {}
    if overrides_path.exists():
        # malformed overrides data (e.g. a bad hand edit) must produce a
        # clean exit-2 message naming the file, never a traceback
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[kindmap] ERROR: overrides file {overrides_path} is not valid JSON: {e}", file=sys.stderr)
            return 2
    if not isinstance(overrides, dict):
        print(f"[kindmap] ERROR: overrides file {overrides_path} is not a JSON object", file=sys.stderr)
        return 2

    # validate override shapes upfront - malformed data must produce a
    # clean exit-2 message naming the offending key, never a traceback
    rename: dict[str, str] = overrides.get("rename", {})
    if not isinstance(rename, dict):
        print("[kindmap] ERROR: 'rename' is not an object", file=sys.stderr)
        return 2
    for node, target in rename.items():
        if not isinstance(target, str):
            print(f"[kindmap] ERROR: rename[{node!r}] target is not a string: {target!r}", file=sys.stderr)
            return 2
    conditional: dict[str, list] = overrides.get("conditional", {})
    if not isinstance(conditional, dict):
        print("[kindmap] ERROR: 'conditional' is not an object", file=sys.stderr)
        return 2
    for node, entries in conditional.items():
        if not isinstance(entries, list):
            print(f"[kindmap] ERROR: conditional[{node!r}] is not a list: {entries!r}", file=sys.stderr)
            return 2
        for c in entries:
            if not isinstance(c, dict):
                print(f"[kindmap] ERROR: conditional[{node!r}] entry is not an object: {c!r}", file=sys.stderr)
                return 2
            if not isinstance(c.get("kind"), str):
                print(
                    f"[kindmap] ERROR: conditional[{node!r}] entry is missing required string 'kind': {c!r}",
                    file=sys.stderr,
                )
                return 2
    if not isinstance(overrides.get("forbidden_categories", {}), dict):
        print("[kindmap] ERROR: 'forbidden_categories' is not an object", file=sys.stderr)
        return 2
    # every rename target must be a real enum kind — a bad target would
    # otherwise silently fall through to unmapped (unlike conditional)
    bad_rename = sorted(f"{target!r} (for {node!r})" for node, target in rename.items() if target not in kinds)
    if bad_rename:
        print(f"[kindmap] ERROR: rename kinds not in SyntaxKind enum: {'; '.join(bad_rename)}", file=sys.stderr)
        return 2
    # forbidden kinds are grouped by evidence category; every category
    # counts as "no mapping on purpose"
    forbidden: list[str] = []
    forbidden_detail: dict[str, dict] = {}
    for cat, spec in overrides.get("forbidden_categories", {}).items():
        if not isinstance(spec, dict):
            print(f"[kindmap] ERROR: forbidden category {cat!r} value is not an object", file=sys.stderr)
            return 2
        cat_kinds = spec.get("kinds", {})
        if not isinstance(cat_kinds, dict):
            print(
                f"[kindmap] ERROR: forbidden category {cat!r} has malformed 'kinds' (expected object)", file=sys.stderr
            )
            return 2
        forbidden.extend(cat_kinds)
        forbidden_detail[cat] = spec

    kind_lower = {k.lower(): k for k in kinds}

    # forbidden names must be real enum kinds (or documented markers)
    bad_forbidden = sorted(set(forbidden) - set(kinds) - ALLOWED_MARKER_NAMES)
    if bad_forbidden:
        print(
            f"[kindmap] ERROR: forbidden kinds not in SyntaxKind enum "
            f"(nor documented markers): {', '.join(bad_forbidden)}",
            file=sys.stderr,
        )
        return 2

    table: dict[str, dict] = {}
    unmapped_ours: list[str] = []

    for t in sorted(our_types):
        if t in conditional:
            entries = []
            for c in conditional[t]:
                k = c["kind"]
                if k == "None":
                    # explicit "no mapping for this case" sentinel
                    entries.append({"kind": None, "int": None, "when": c.get("when", "")})
                    continue
                if k not in kinds:
                    print(f"[kindmap] ERROR: conditional kind {k!r} (for {t}) not in SyntaxKind enum", file=sys.stderr)
                    return 2
                entries.append({"kind": k, "int": kinds[k], "when": c.get("when", "")})
            table[t] = {"conditional": entries}
            continue
        target = rename.get(t)
        if target is None:
            target = kind_lower.get(t.replace("_", ""))
        if target is not None and target in kinds:
            table[t] = {"kind": target, "int": kinds[target]}
        else:
            unmapped_ours.append(t)

    # coverage: fork node kinds (the parse-tree node section starts at
    # QualifiedName; every kind from there on is a node, wrapper, or
    # synthesized AST shape) that are neither mapped nor forbidden
    mapped_kinds = set()
    for v in table.values():
        if "kind" in v:
            mapped_kinds.add(v["kind"])
        else:
            mapped_kinds.update(e["kind"] for e in v["conditional"])
    node_section_start = kinds["QualifiedName"]
    residual = sorted(
        k for k, i in kinds.items() if i >= node_section_start and k not in mapped_kinds and k not in forbidden
    )

    # a kind that is both mapped and forbidden is a data bug (the
    # conditional table and the forbidden categories disagree)
    overlap = sorted(mapped_kinds & set(forbidden))
    if overlap:
        print(f"[kindmap] ERROR: kinds both mapped and forbidden: {', '.join(overlap)}", file=sys.stderr)
        return 2

    out = {
        "source": {
            "types_ts": str(types_ts.relative_to(REPO)) if types_ts.is_relative_to(REPO) else str(types_ts),
            "node_types": str(node_types.relative_to(REPO)) if node_types.is_relative_to(REPO) else str(node_types),
        },
        "fork_kind_count": len(kinds),
        "our_node_type_count": len(our_types),
        "mapped": len(table),
        "table": table,
        "unmapped_node_types": unmapped_ours,
        "forbidden": forbidden,
        "forbidden_categories": {
            cat: {
                "basis": spec.get("basis", ""),
                "count": len(spec.get("kinds", {})),
            }
            for cat, spec in forbidden_detail.items()
        },
        "residual_nodeish_kinds": residual,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"[kindmap] fork kinds: {len(kinds)}  our node types: {len(our_types)}")
    print(f"[kindmap] mapped: {len(table)}  unmapped ours: {len(unmapped_ours)}  forbidden: {len(forbidden)}")
    for cat, spec in forbidden_detail.items():
        print(f"[kindmap]   {cat}: {len(spec.get('kinds', {}))}")
    print(f"[kindmap] residual nodeish kinds (neither mapped nor forbidden): {len(residual)}")
    for k in residual[:30]:
        print(f"[kindmap]   {k}")
    if len(residual) > 30:
        print(f"[kindmap]   ... and {len(residual) - 30} more")
    print(f"[kindmap] wrote {out_path}")
    return 2 if residual else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build node type -> SyntaxKind map.")
    ap.add_argument("--types-ts", required=True, type=Path, help="path to fork compiler types.ts")
    ap.add_argument("--node-types", default=REPO / "arkts_grammar/src/node-types.json", type=Path)
    ap.add_argument("--overrides", default=REPO / "kind_map_overrides.json", type=Path)
    ap.add_argument("--out", default=REPO / "build" / "mapping" / "node_type_to_syntaxkind.json", type=Path)
    args = ap.parse_args()
    return run_kind_map(args.types_ts, args.node_types, args.overrides, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
