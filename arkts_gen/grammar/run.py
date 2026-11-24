"""Grammar stage: `grammar_ast.json` -> `grammar.js` + `tree-sitter generate` -> `build/libarkts.so`.

Debug reports are written to `build/grammar/` when `--debug` is set or on failure.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from arkts_gen.grammar.emitter import _TOKEN_OVERRIDES, GrammarEmitter, build_default_fields
from arkts_gen.preprocess.ebnf_ast import ProductionAST, ast_from_json

REPO = Path(__file__).resolve().parent.parent.parent
BUILD_GRAMMAR = REPO / "build" / "grammar"

_CONFLICT_SUGGESTION_RE = re.compile(r"Add a conflict for these rules:\s*(.+?)\s*$", re.MULTILINE)
_RULE_NAME_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")

_TREE_SITTER_JSON = {
    "grammars": [
        {
            "name": "arkts",
            "camelcase": "Arkts",
            "scope": "source.arkts",
            "path": ".",
            "file-types": ["ets", "d.ets"],
        }
    ],
    "metadata": {
        "version": "0.1.0",
        "license": "MIT",
        "description": "ArkTS grammar, generated from the ArkTS specification",
    },
}

_SYNTACTIC_OVERRIDES = {
    "MultilineStringLiteral": "structural template rule with `_template_chars` chunks and embedded expressions",
    "expressionStatement": "terminator required per spec 2.11 via `_automatic_semicolon_insertion`",
}
_MERGED = {"stringInterpolation": "multiline_string_literal"}


def _run_generate(grammar_dir: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["tree-sitter", "generate"],
        cwd=grammar_dir,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def _extract_suggested_conflicts(output: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for m in _CONFLICT_SUGGESTION_RE.finditer(output):
        names = _RULE_NAME_RE.findall(m.group(1))
        if names:
            groups.append(sorted(set(names)))
    return groups


def _extract_unnecessary_conflicts(output: str) -> list[list[str]]:
    groups: list[list[str]] = []
    in_block = False
    for line in output.splitlines():
        if "unnecessary conflicts:" in line:
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped.startswith("`"):
            names = _RULE_NAME_RE.findall(line)
            if names:
                groups.append(sorted(set(names)))
        else:
            in_block = False
    return groups


def _write_tree_sitter_json(grammar_dir: Path) -> None:
    out = grammar_dir / "tree-sitter.json"
    if out.exists() and json.loads(out.read_text(encoding="utf-8")) == _TREE_SITTER_JSON:
        return
    out.write_text(json.dumps(_TREE_SITTER_JSON, indent=2) + "\n", encoding="utf-8")
    print(f"[grammar] wrote {out}")


def _build_scanner_needs(asts: list[ProductionAST]) -> dict:
    emitter = GrammarEmitter(asts)
    productions: dict[str, str] = {}
    for a in asts:
        if a.name in _TOKEN_OVERRIDES:
            productions[a.name] = "token-override"
        elif a.name in _SYNTACTIC_OVERRIDES:
            productions[a.name] = "syntactic-override"
        elif a.name in _MERGED:
            productions[a.name] = "merged"
        elif a.name in emitter.inline_only:
            productions[a.name] = "token-inline"
        elif a.name in emitter.token_rules:
            productions[a.name] = "token-rule"
        else:
            productions[a.name] = "structural"
    return {
        "production_count": len(productions),
        "external_tokens": ["_automatic_semicolon_insertion"],
        "productions": productions,
    }


def compile_lib(grammar_dir: Path, lib_out: Path) -> int:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        print("[grammar] ERROR: no C compiler found (cc/gcc/clang)", file=sys.stderr)
        return 1
    src = [grammar_dir / "src" / "parser.c", grammar_dir / "src" / "scanner.c"]
    for s in src:
        if not s.exists():
            print(f"[grammar] ERROR: missing {s} — run `tree-sitter generate` first", file=sys.stderr)
            return 1
    lib_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [cc, "-shared", "-fPIC", "-O2", "-I", str(grammar_dir / "src"), *(str(s) for s in src), "-o", str(lib_out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    print(f"[grammar] wrote {lib_out}")
    return 0


def run_grammar(
    ast_in: Path = REPO / "build" / "preprocess" / "grammar_ast.json",
    grammar_js_out: Path = REPO / "arkts_grammar" / "grammar.js",
    lib_out: Path = REPO / "build" / "libarkts.so",
    conflicts_file: Path = BUILD_GRAMMAR / "emit_conflicts.json",
    fields_file: Path = REPO / "fields.json",
    max_conflict_rounds: int = 300,
    debug: bool = False,
) -> int:
    asts = ast_from_json(json.loads(ast_in.read_text(encoding="utf-8")))
    conflicts: list[list[str]] = (
        json.loads(conflicts_file.read_text(encoding="utf-8")) if conflicts_file.exists() else []
    )
    fields = json.loads(fields_file.read_text(encoding="utf-8")) if fields_file.exists() else None

    def emit(conflict_list: list[list[str]]):
        nonlocal fields
        if fields is None:
            seed_emitter = GrammarEmitter(asts, conflicts=conflict_list)
            fields = build_default_fields(seed_emitter)
            fields_file.parent.mkdir(parents=True, exist_ok=True)
            fields_file.write_text(json.dumps(fields, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"[grammar] seeded fields manifest: {len(fields)} productions -> {fields_file}")
        emitter = GrammarEmitter(asts, conflicts=conflict_list, fields=fields)
        grammar_js, report = emitter.build()
        grammar_js_out.parent.mkdir(parents=True, exist_ok=True)
        grammar_js_out.write_text(grammar_js, encoding="utf-8")
        _write_tree_sitter_json(grammar_js_out.parent)
        return report

    report = emit(conflicts)
    print(f"[grammar] wrote {grammar_js_out} ({report.rule_count} rules)")

    ok, output = _run_generate(grammar_js_out.parent)
    unnecessary = _extract_unnecessary_conflicts(output)
    if unnecessary:
        known = {tuple(c) for c in conflicts}
        drop = {tuple(u) for u in unnecessary if tuple(u) in known}
        if drop:
            conflicts = [c for c in conflicts if tuple(c) not in drop]
            report = emit(conflicts)
            ok, output = _run_generate(grammar_js_out.parent)

    rounds = 0
    while not ok and rounds < max_conflict_rounds:
        suggestions = _extract_suggested_conflicts(output)
        known = {tuple(c) for c in conflicts}
        fresh = [s for s in suggestions if tuple(s) not in known]
        if not fresh:
            break
        for s in fresh:
            conflicts.append(s)
            print(f"[grammar] + conflict: {s}")
        report = emit(conflicts)
        rounds += 1
        ok, output = _run_generate(grammar_js_out.parent)

    conflicts_file.parent.mkdir(parents=True, exist_ok=True)
    conflicts_file.write_text(json.dumps(conflicts, indent=2) + "\n", encoding="utf-8")

    if not ok:
        _dump_debug(BUILD_GRAMMAR, asts, report, ast_in.name, conflicts, ok, rounds)
        print(f"[grammar] ERROR: tree-sitter generate failed after {rounds} round(s); debug -> {BUILD_GRAMMAR}")
        print("\n".join(output.strip().splitlines()[-12:]))
        return 2

    print(f"[grammar] tree-sitter generate OK after {rounds} round(s); {len(conflicts)} conflict(s)")

    if debug:
        _dump_debug(BUILD_GRAMMAR, asts, report, ast_in.name, conflicts, ok, rounds)
        print(f"[grammar] wrote debug artifacts: {BUILD_GRAMMAR}")

    return compile_lib(grammar_js_out.parent, lib_out)


def _dump_debug(
    debug_dir: Path,
    asts: list[ProductionAST],
    report,
    input_name: str,
    conflicts: list[list[str]],
    ok: bool,
    rounds: int,
) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    report_dict = asdict(report)
    report_dict.update(
        {
            "input": input_name,
            "grammar_name": "arkts",
            "conflicts": conflicts,
            "generate_ok": ok,
            "conflict_rounds": rounds,
        }
    )
    (debug_dir / "emit_report.json").write_text(
        json.dumps(report_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (debug_dir / "scanner_needs.json").write_text(
        json.dumps(_build_scanner_needs(asts), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit grammar.js, run tree-sitter generate, and build libarkts.so.")
    ap.add_argument(
        "--input",
        default=REPO / "build" / "preprocess" / "grammar_ast.json",
        type=Path,
        help="input AST JSON",
    )
    ap.add_argument("--out", default=REPO / "arkts_grammar" / "grammar.js", type=Path, help="output grammar.js")
    ap.add_argument("--lib-out", default=REPO / "build" / "libarkts.so", type=Path, help="output shared library")
    ap.add_argument("--conflicts-file", default=BUILD_GRAMMAR / "emit_conflicts.json", type=Path, help="conflicts JSON")
    ap.add_argument("--fields-file", default=REPO / "fields.json", type=Path, help="fields JSON")
    ap.add_argument("--max-conflict-rounds", type=int, default=300)
    ap.add_argument("--debug", action="store_true", help="write debug reports to build/grammar/")
    ap.add_argument("--build-only", action="store_true", help="only compile parser.c + scanner.c -> libarkts.so")
    args = ap.parse_args()

    if args.build_only:
        return compile_lib(args.out.parent, args.lib_out)
    return run_grammar(
        args.input,
        args.out,
        args.lib_out,
        args.conflicts_file,
        args.fields_file,
        args.max_conflict_rounds,
        args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
