#!/usr/bin/env python3
"""Preprocess stage — Spec PDF -> validated EBNF -> typed AST (grammar_ast.json).

Mid-stage artifacts (grammar.ebnf, report.json, ast_report.json) are written
only when --debug-dir is passed or when extraction/AST errors occur.

Usage:
    uv run python -m arkts_gen.preprocess.run \
        --input ArkTS-Sta_Specification_en.pdf \
        --ast-out grammar_ast.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from arkts_gen.preprocess.ebnf import (
    classify_undefined,
    extract_grammar_box_text,
    find_prose_definitions,
    parse_productions,
    serialize_ebnf,
)
from arkts_gen.preprocess.ebnf_ast import ast_to_json, build_ast

REPO = Path(__file__).resolve().parent.parent.parent
BUILD_PREPROCESS = REPO / "build" / "preprocess"


def _dump_debug(debug_dir: Path, ebnf_text: str, extract_report: dict, ast_report: dict | None = None) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "grammar.ebnf").write_text(ebnf_text, encoding="utf-8")
    (debug_dir / "extract_report.json").write_text(
        json.dumps(extract_report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if ast_report:
        (debug_dir / "ast_report.json").write_text(
            json.dumps(ast_report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def run_preprocess(
    spec_pdf: Path,
    ast_out: Path = BUILD_PREPROCESS / "grammar_ast.json",
    debug: bool = False,
) -> int:
    src_label = spec_pdf.name
    print(f"[preprocess] reading spec: {spec_pdf}")

    # 1. Extract Grammar Summary + prose-chapter fallback
    grammar_text, used_pages = extract_grammar_box_text(spec_pdf)
    print(f"[preprocess] gray-box pages: {used_pages[0]}..{used_pages[-1]} ({len(used_pages)} pages)")
    prods, report = parse_productions(grammar_text)

    conflicts: list[str] = []
    if report.undefined_references:
        found, box_conflicts = find_prose_definitions(spec_pdf, report.undefined_references)
        conflicts = box_conflicts
        if found:
            prods = prods + [found[n] for n in sorted(found)]
            merged = serialize_ebnf(prods, source=src_label)
            merged_body = "\n".join(ln for ln in merged.splitlines() if not ln.lstrip().startswith("//"))
            prods, report = parse_productions(merged_body)
            report.prose_sourced = sorted(found)

    externals, unresolved = classify_undefined(report.undefined_references)
    report.externals = externals
    report.unresolved = unresolved
    report.conflicts = conflicts
    ebnf_text = serialize_ebnf(prods, source=src_label)

    extract_dict = asdict(report)
    extract_dict["source"] = src_label
    extract_dict["method"] = "pdf-gray-box (summary + prose fallback)"

    print(
        f"[preprocess] productions={report.production_count} "
        f"unterminated={len(report.unterminated)} duplicates={len(report.duplicates)} "
        f"externals={len(externals)} unresolved={len(unresolved)}"
    )

    if report.unterminated or report.duplicates:
        _dump_debug(BUILD_PREPROCESS, ebnf_text, extract_dict)
        print(f"[preprocess] ERROR: structural EBNF errors; debug artifacts written to {BUILD_PREPROCESS}")
        return 2

    # 2. Build typed AST
    asts, ast_report = build_ast(prods)
    ast_report["source"] = src_label
    print(
        f"[preprocess] ast productions={ast_report['production_count']} parse_errors={len(ast_report['parse_errors'])}"
    )

    if ast_report["parse_errors"]:
        _dump_debug(BUILD_PREPROCESS, ebnf_text, extract_dict, ast_report)
        print(f"[preprocess] ERROR: AST parse errors; debug artifacts written to {BUILD_PREPROCESS}")
        return 2

    ast_out.parent.mkdir(parents=True, exist_ok=True)
    ast_out.write_text(
        json.dumps(ast_to_json(asts), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[preprocess] wrote AST: {ast_out}")

    if debug:
        _dump_debug(BUILD_PREPROCESS, ebnf_text, extract_dict, ast_report)
        print(f"[preprocess] wrote debug artifacts: {BUILD_PREPROCESS}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Preprocess ArkTS spec PDF into typed grammar AST.")
    ap.add_argument("--input", default=REPO / "ArkTS-Sta_Specification_en.pdf", type=Path, help="spec PDF")
    ap.add_argument("--ast-out", default=BUILD_PREPROCESS / "grammar_ast.json", type=Path, help="output AST JSON")
    ap.add_argument("--debug", action="store_true", help="write mid-stage debug outputs to build/preprocess/")
    args = ap.parse_args()
    return run_preprocess(args.input, args.ast_out, args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
