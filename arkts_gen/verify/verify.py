#!/usr/bin/env python3
"""Phase 5 — the four-oracle verification harness.

Usage:
    python verify.py                      # all oracles, default paths
    python verify.py --skip-fork          # skip the fork oracle (slow pass)
    python verify.py --dirs cases         # subset of corpus dirs

Oracles:
  1. conflict    — `tree-sitter generate` exits 0 (all conflicts declared).
  2. corpus      — parse the fork's test corpus with our grammar; per-file
                   success into corpus_report.json.
  3. fork        — the fork's own parser (ts.createSourceFile) accepts a
                   file => our grammar must too; divergences into
                   oracle_diff.json, categorized by likely cause.
  4. coverage    — fork SyntaxKind enum (types.ts) vs our node types into
                   coverage_diff.json.

Summary printed + verify_summary.json. Exit codes:
  0  every oracle that ran is green (conflict ok; zero fork-accepted
     files failing on valid ArkTS). Coverage itself is gated upstream by
     build_kind_map's residual check, not by verify's exit.
  2  divergence queue non-empty (a REPORT — the pipeline continues), a
     corpus config error (no files found), or the conflict oracle failed
     (tree-sitter generate red).
  1  harness failure — an oracle could not run at all (e.g. the fork
     oracle: node missing, fork lib missing, fork crash). Real failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
GRAMMAR_DIR = REPO / "arkts_grammar"
BUILD_TEST = REPO / "build" / "test"

# ----------------------------------------------------------------------
# Oracle 1: conflict
# ----------------------------------------------------------------------


def oracle_conflict() -> dict:
    proc = subprocess.run(
        ["tree-sitter", "generate"],
        cwd=GRAMMAR_DIR,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    declared = []
    cf = REPO / "build" / "grammar" / "emit_conflicts.json"
    if cf.exists():
        declared = json.loads(cf.read_text(encoding="utf-8"))
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "generate_exit": proc.returncode,
        "declared_conflicts": len(declared),
        "stderr_tail": proc.stderr.strip().splitlines()[-5:],
    }


# ----------------------------------------------------------------------
# Oracle 2 + 3: corpus + fork
# ----------------------------------------------------------------------


def collect_corpus_files(corpus_root: Path, dirs: list[str]) -> list[Path]:
    """ArkTS corpus: .ets + .ts files. Infra files (.js, .d.ts, .tsx) are
    test machinery / ambient declaration files, not ArkTS sources — they are
    excluded from the must-parse set and counted separately."""
    exts = {".ts", ".ets"}
    files: list[Path] = []
    for d in dirs:
        base = corpus_root / d
        if not base.exists():
            continue
        files.extend(
            p for p in base.rglob("*") if p.suffix.lower() in exts and p.is_file() and not p.name.endswith(".d.ts")
        )
    return sorted(set(files))


def collect_es2panda_files(root: Path) -> list[Path]:
    """Production-parser corpus: every .ets file under the es2panda repo's
    test trees. These are valid ArkTS by the production compiler's own
    standard — parse-only oracle (no fork cross-check: the two compilers
    are different authorities)."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.ets") if p.is_file())


def oracle_corpus(files: list[Path], tmp: Path) -> dict:
    if not files:
        return {"total": 0, "parsed_by_cli": 0, "successful": 0, "failed": 0, "per_file": {}}
    paths_file = tmp / "corpus_paths.txt"
    paths_file.write_text("\n".join(str(p) for p in files), encoding="utf-8")
    proc = subprocess.run(
        ["tree-sitter", "parse", "-q", "--json-summary", "--paths", str(paths_file)],
        cwd=GRAMMAR_DIR,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    out = proc.stdout
    start = out.find("{")
    data = json.loads(out[start:]) if start >= 0 else {}
    summaries = data.get("parse_summaries", [])
    per_file = {s["file"]: s.get("successful", False) for s in summaries}
    ok = sum(1 for v in per_file.values() if v)
    return {
        "total": len(files),
        "parsed_by_cli": len(summaries),
        "successful": ok,
        "failed": len(summaries) - ok,
        "per_file": per_file,
    }


def oracle_fork(files: list[Path], tmp: Path, fork_lib: Path) -> dict:
    if not files:
        return {"total": 0}
    paths_file = tmp / "fork_paths.txt"
    paths_file.write_text("\n".join(str(p) for p in files), encoding="utf-8")
    out_file = tmp / "fork_results.json"
    # remove any stale results BEFORE the run: a node shim that exits 0
    # without producing work must not leave a previous run's results
    # readable as current (silent-green hole)
    if out_file.exists():
        out_file.unlink()
    lib = fork_lib
    if not lib.exists():
        return {"total": len(files), "error": f"fork lib not found: {lib}"}
    try:
        proc = subprocess.run(
            ["node", str(Path(__file__).resolve().parent / "fork_oracle.js"), str(lib), str(paths_file), str(out_file)],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    except FileNotFoundError:
        return {"total": len(files), "error": "node executable not found on PATH — the fork oracle cannot run"}
    except subprocess.TimeoutExpired:
        return {"total": len(files), "error": "fork oracle timed out after 3600s"}
    if proc.returncode != 0:
        return {"total": len(files), "error": proc.stderr[-500:]}
    if not out_file.exists():
        # exit 0 but no results file: a silent no-op shim (or a fork
        # oracle bug) — the fork oracle did not produce results
        return {"total": len(files), "error": f"fork oracle exited 0 but did not produce results at {out_file}"}
    try:
        results = json.loads(out_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"total": len(files), "error": f"fork results file {out_file} is not valid JSON: {e}"}
    return {
        "total": len(files),
        "accepted": sum(1 for r in results.values() if r.get("ok")),
        "per_file": {k: v.get("ok", False) for k, v in results.items()},
    }


def _categorize_divergence(src_path: Path) -> str:
    """Best-effort cause for a file the fork accepts but we fail."""
    # json-summary reports the first ERROR position only in text mode; with
    # -q we just have the flag. Use content heuristics for categorization.
    try:
        text = src_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    if re.search(r"=\s*/[^/*]", text) or re.search(r"return\s+/", text):
        return "regex-literal (absent from ArkTS spec)"
    # JSX-shaped only: a self-closing element tag, an element tag with a
    # matching close tag, or a fragment. Generic/cast text like
    # `x as Foo<Bar>` must NOT match (angle brackets but no element
    # shape). Line comments are stripped first: `/// <reference ... />`
    # directives are self-close-shaped but not JSX; a fragment opener must
    # not be glued to an identifier (`foo<>()` is empty type arguments).
    stripped = "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())
    if (
        re.search(r"<[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?/>", stripped)
        or (
            re.search(r"<[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?>", stripped)
            and re.search(r"</[A-Za-z][A-Za-z0-9]*\s*>", stripped)
        )
        or re.search(r"(?<![A-Za-z0-9_$.'\"])<>", stripped)
        or "</>" in stripped
    ):
        return "jsx (absent from ArkTS spec)"
    if re.search(r"^\s*@[A-Za-z]", text, re.MULTILINE) and "interface" not in text:
        return "decorators (check form)"
    if re.search(r"enum\s", text):
        return "enum (check form)"
    return "other (needs inspection)"


def diff_oracles(corpus: dict, fork: dict) -> dict:
    ours = corpus.get("per_file", {})
    theirs = fork.get("per_file", {})
    fork_ok_we_fail: dict[str, str] = {}
    we_ok_fork_fail: list[str] = []
    for f, fork_ok in theirs.items():
        our_ok = ours.get(f)
        if our_ok is None:
            continue
        if fork_ok and not our_ok:
            fork_ok_we_fail[f] = _categorize_divergence(Path(f))
        elif our_ok and not fork_ok:
            we_ok_fork_fail.append(f)
    cats: dict[str, int] = {}
    for c in fork_ok_we_fail.values():
        cats[c] = cats.get(c, 0) + 1
    return {
        "fork_accepted": sum(1 for v in theirs.values() if v),
        "fork_accepted_we_fail": len(fork_ok_we_fail),
        "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        "divergent_files": dict(sorted(fork_ok_we_fail.items())),
        "we_ok_fork_fails": len(we_ok_fork_fail),
        "we_ok_fork_fail_files": sorted(we_ok_fork_fail)[:200],
    }


# ----------------------------------------------------------------------
# Oracle 4: SyntaxKind coverage
# ----------------------------------------------------------------------


def oracle_coverage() -> dict:
    """Coverage via the Phase 6 mapping table: every nodeish fork kind must
    be mapped (flat or conditional) or explicitly forbidden."""
    km = json.loads((REPO / "build" / "mapping" / "node_type_to_syntaxkind.json").read_text(encoding="utf-8"))
    return {
        "fork_kind_count": km["fork_kind_count"],
        "our_node_type_count": km["our_node_type_count"],
        "mapped": km["mapped"],
        "forbidden": len(km["forbidden"]),
        "residual_nodeish_kinds": km["residual_nodeish_kinds"],
        "unmapped_node_types_informational": len(km["unmapped_node_types"]),
    }


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def run_verify(
    corpus_root: Path | None = None,
    fork_lib: Path | None = None,
    dirs: list[str] | None = None,
    skip_corpus: bool = False,
    es2panda_root: Path | None = None,
    skip_fork: bool = False,
    skip_coverage: bool = False,
) -> int:
    if dirs is None:
        dirs = ["cases", "arkTSTest"]

    BUILD_TEST.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verify_") as tmpdir:
        return _run_verify_in_tmp(
            tmp=Path(tmpdir),
            corpus_root=corpus_root,
            fork_lib=fork_lib,
            dirs=dirs,
            skip_corpus=skip_corpus,
            es2panda_root=es2panda_root,
            skip_fork=skip_fork,
            skip_coverage=skip_coverage,
        )


def _run_verify_in_tmp(
    tmp: Path,
    corpus_root: Path | None,
    fork_lib: Path | None,
    dirs: list[str],
    skip_corpus: bool,
    es2panda_root: Path | None,
    skip_fork: bool,
    skip_coverage: bool,
) -> int:

    summary: dict = {}

    print("[verify] oracle 1: conflict")
    summary["conflict"] = oracle_conflict()
    print(
        f"[verify]   generate ok={summary['conflict']['ok']}, "
        f"declared conflicts={summary['conflict']['declared_conflicts']}"
    )

    corpus: dict = {}
    if corpus_root is not None and (not skip_corpus or not skip_fork):
        files = collect_corpus_files(corpus_root, dirs)
        print(f"[verify] corpus: {len(files)} files under {corpus_root} ({' '.join(dirs)})")
        if not files:
            missing = [d for d in dirs if not (corpus_root / d).exists()]
            print(
                f"[verify] ERROR: no corpus files found under {corpus_root} for dirs: {' '.join(dirs)}", file=sys.stderr
            )
            if missing:
                print(f"[verify]   missing dirs: {', '.join(missing)}", file=sys.stderr)
            # the pipeline's continue-message points at
            # verify_summary.json — leave a (minimal) one behind so the
            # pointer is never stale or missing
            summary["error"] = f"no corpus files found under {corpus_root} for dirs: {' '.join(dirs)}"
            summary["missing_dirs"] = missing
            (BUILD_TEST / "verify_summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            return 2
        if not skip_corpus:
            print("[verify] oracle 2: corpus parse (this can take minutes)")
            corpus = oracle_corpus(files, tmp)
            (BUILD_TEST / "corpus_report.json").write_text(
                json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"[verify]   successful={corpus['successful']}/{corpus['total']}  failed={corpus['failed']}")
        if not skip_fork:
            print("[verify] oracle 3: fork parser acceptance")
            resolved_lib = fork_lib or (corpus_root.parent / "lib" / "typescript.js")
            fork = oracle_fork(files, tmp, resolved_lib)
            if "error" in fork:
                print(f"[verify]   fork oracle error: {fork['error']}")
                print(
                    "[verify] FAIL: the fork oracle did not run — the corpus is unverified against it", file=sys.stderr
                )
                summary["fork"] = fork
            elif skip_corpus:
                # corpus skipped: our per-file parse status is unknown, so
                # a fork-vs-ours diff is vacuous — warn loudly, record it,
                # and do NOT gate the exit on it (and do not overwrite the
                # real oracle_diff.json with empty data)
                print(
                    "[verify]   WARNING: corpus was skipped (--skip-corpus) "
                    "— the fork diff is VACUOUS: our parse status is "
                    "unknown for every file, so it does not gate the exit "
                    "code"
                )
                summary["fork_diff"] = {
                    "fork_accepted": fork["accepted"],
                    "fork_accepted_we_fail": 0,
                    "categories": {},
                    "divergent_files": 0,
                    "we_ok_fork_fails": 0,
                    "we_ok_fork_fail_files": [],
                    "vacuous": True,
                    "note": "corpus skipped: our parse status is unknown "
                    "for every file; this diff is vacuous and does "
                    "not gate the exit code",
                }
            else:
                diff = diff_oracles(corpus, fork)
                (BUILD_TEST / "oracle_diff.json").write_text(
                    json.dumps(diff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                summary["fork_diff"] = {k: v for k, v in diff.items() if k != "divergent_files"}
                summary["fork_diff"]["divergent_files"] = len(diff["divergent_files"])
                print(
                    f"[verify]   fork accepted={diff['fork_accepted']}, "
                    f"we fail on {diff['fork_accepted_we_fail']} of them"
                )
                for c, n in diff["categories"].items():
                    print(f"[verify]     {n:6d}  {c}")

    if not skip_corpus and es2panda_root is not None:
        es2panda_files = collect_es2panda_files(es2panda_root)
        if es2panda_files:
            print(f"[verify] oracle 2b: es2panda corpus ({len(es2panda_files)} production test files)")
            es2 = oracle_corpus(es2panda_files, tmp)
            (BUILD_TEST / "es2panda_report.json").write_text(
                json.dumps(es2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            summary["es2panda"] = {
                "total": es2["total"],
                "successful": es2["successful"],
                "failed": es2["failed"],
            }
            print(f"[verify]   es2panda: {es2['successful']}/{es2['total']}")
        else:
            print(f"[verify] es2panda corpus not found at {es2panda_root}")

    if not skip_coverage:
        print("[verify] oracle 4: SyntaxKind coverage")
        cov = oracle_coverage()
        (BUILD_TEST / "coverage_diff.json").write_text(
            json.dumps(cov, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        summary["coverage"] = cov
        print(
            f"[verify]   kinds={cov['fork_kind_count']} "
            f"ours={cov['our_node_type_count']} mapped={cov['mapped']} "
            f"forbidden={cov['forbidden']} "
            f"residual={len(cov['residual_nodeish_kinds'])}"
        )

    (BUILD_TEST / "verify_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[verify] wrote {BUILD_TEST / 'verify_summary.json'}")

    ok = summary["conflict"]["ok"]
    if "fork" in summary:
        # the fork oracle itself failed to run (node missing, lib missing,
        # crash): a harness failure, not a divergence report
        print("[verify] exit 1: fork oracle failed to run — verification incomplete", file=sys.stderr)
        return 1
    fork_diff = summary.get("fork_diff")
    if fork_diff is not None and not fork_diff.get("vacuous"):
        # divergence only for causes NOT known-absent-by-design is blocking;
        # for now report and let the queue be worked
        ok = ok and fork_diff["fork_accepted_we_fail"] == 0
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 5 four-oracle harness.")
    ap.add_argument("--corpus-root", default=None, type=Path, help="Path to third_party_typescript/tests (optional)")
    ap.add_argument(
        "--fork-lib",
        default=None,
        type=Path,
        help="Path to fork lib/typescript.js (default: <corpus-root>/../lib/typescript.js)",
    )
    ap.add_argument("--dirs", default=["cases", "arkTSTest"], nargs="+", help="corpus dirs under --corpus-root")
    ap.add_argument("--skip-corpus", action="store_true")
    ap.add_argument("--es2panda-root", default=None, type=Path, help="Path to arkcompiler_ets_frontend (optional)")
    ap.add_argument("--skip-fork", action="store_true")
    ap.add_argument("--skip-coverage", action="store_true")
    args = ap.parse_args()
    return run_verify(
        corpus_root=args.corpus_root,
        fork_lib=args.fork_lib,
        dirs=args.dirs,
        skip_corpus=args.skip_corpus,
        es2panda_root=args.es2panda_root,
        skip_fork=args.skip_fork,
        skip_coverage=args.skip_coverage,
    )


if __name__ == "__main__":
    raise SystemExit(main())
