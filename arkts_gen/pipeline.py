"""In-process pipeline orchestrator: spec PDF -> AST -> grammar + libarkts.so -> kind map -> verify."""

from __future__ import annotations

import argparse
from pathlib import Path

from arkts_gen.grammar.run import run_grammar
from arkts_gen.mapping.build_kind_map import run_kind_map
from arkts_gen.preprocess.run import run_preprocess
from arkts_gen.verify.verify import run_verify

REPO = Path(__file__).resolve().parent.parent


def run_pipeline(
    spec: Path,
    types_ts: Path | None = None,
    corpus_root: Path | None = None,
    es2panda_root: Path | None = None,
    skip_verify: bool = False,
    debug: bool = False,
) -> int:
    ast_out = REPO / "build" / "preprocess" / "grammar_ast.json"

    print("[pipeline] stage 1: preprocess")
    rc = run_preprocess(spec_pdf=spec, ast_out=ast_out, debug=debug)
    if rc != 0:
        print(f"[pipeline] FAILED at step preprocess (exit {rc})")
        return rc

    print("[pipeline] stage 2: grammar")
    rc = run_grammar(
        ast_in=ast_out,
        grammar_js_out=REPO / "arkts_grammar" / "grammar.js",
        lib_out=REPO / "build" / "libarkts.so",
        conflicts_file=REPO / "build" / "grammar" / "emit_conflicts.json",
        fields_file=REPO / "fields.json",
        max_conflict_rounds=300,
        debug=debug,
    )
    if rc != 0:
        print(f"[pipeline] FAILED at step grammar (exit {rc})")
        return rc

    if types_ts is not None:
        print("[pipeline] stage 3: kind-map")
        rc = run_kind_map(types_ts=types_ts)
        if rc != 0:
            print(f"[pipeline] FAILED at step kind-map (exit {rc})")
            return rc
    else:
        print("[pipeline] skipping kind-map (no --types-ts provided)")

    if skip_verify:
        print("[pipeline] skipping verify")
    else:
        print("[pipeline] stage 4: verify")
        rc = run_verify(corpus_root=corpus_root, es2panda_root=es2panda_root)
        if rc == 2:
            print("[pipeline] verify reports divergences (exit 2) — see build/test/verify_summary.json; continuing")
        elif rc != 0:
            print(f"[pipeline] FAILED at step verify (exit {rc})")
            return rc

    print("[pipeline] done: build/libarkts.so")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Full pipeline: spec -> parser lib.")
    ap.add_argument("--spec", default=REPO / "ArkTS-Sta_Specification_en.pdf", type=Path)
    ap.add_argument("--types-ts", type=Path, default=None, help="path to fork compiler types.ts for kind-map")
    ap.add_argument("--corpus-root", type=Path, default=None, help="optional third_party_typescript/tests for verify")
    ap.add_argument("--es2panda-root", type=Path, default=None, help="optional arkcompiler_ets_frontend for verify")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--debug", action="store_true", help="write mid-stage debug outputs to build/")
    args = ap.parse_args()
    return run_pipeline(
        spec=args.spec,
        types_ts=args.types_ts,
        corpus_root=args.corpus_root,
        es2panda_root=args.es2panda_root,
        skip_verify=args.skip_verify,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
