"""E2E corpus test runner for `tree-sitter-arkts` using repo-relative keys (`ts:<rel>`, `ets:<rel>`)."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from arkts_gen.verify.verify import _categorize_divergence

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_GRAMMAR_DIR = REPO / "arkts_grammar"
DEFAULT_FORK_ORACLE = Path(__file__).resolve().parent / "fork_oracle.js"
DEFAULT_FORK_RESULTS = REPO / "build" / "test" / "fork_corpus_results.json"
DEFAULT_ARKTS_TS_DIRS = ["arkTSTest", "dets", "issues_cookbook_tests", "projects"]


def ensure_fresh_cli_cache(grammar_dir: Path) -> None:
    """Remove cached ~/.cache/tree-sitter/lib/arkts.so if grammar sources are newer."""
    cache_so = Path.home() / ".cache" / "tree-sitter" / "lib" / "arkts.so"
    if not cache_so.exists():
        return
    sources = [
        grammar_dir / "src" / "parser.c",
        grammar_dir / "src" / "scanner.c",
        grammar_dir / "grammar.js",
    ]
    cache_mtime = cache_so.stat().st_mtime
    if any(s.exists() and s.stat().st_mtime > cache_mtime for s in sources):
        cache_so.unlink()


def collect_repo_files(
    ts_repo: Path | None,
    ets_repo: Path | None,
    ts_dirs: list[str] | None = None,
) -> dict[str, Path]:
    """Collect corpus files mapped by repo-relative keys (`ts:<rel>`, `ets:<rel>`)."""
    files: dict[str, Path] = {}
    if ts_repo is not None:
        ts_root = ts_repo.resolve()
        tests_root = ts_root / "tests"
        if ts_dirs == ["ALL"]:
            search_roots = [tests_root] if tests_root.exists() else [ts_root]
        else:
            dirs = DEFAULT_ARKTS_TS_DIRS if ts_dirs is None else ts_dirs
            search_roots = [tests_root / d for d in dirs if (tests_root / d).exists()]
        exts = {".ts", ".ets"}
        for base in search_roots:
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in exts and not p.name.endswith(".d.ts"):
                    rel = p.relative_to(ts_root).as_posix()
                    files[f"ts:{rel}"] = p

    if ets_repo is not None:
        ets_root = ets_repo.resolve()
        if ets_root.exists():
            for p in sorted(ets_root.rglob("*.ets")):
                if p.is_file():
                    rel = p.relative_to(ets_root).as_posix()
                    files[f"ets:{rel}"] = p

    return dict(sorted(files.items()))


def _extract_json_summary(stdout: str) -> dict:
    """Extract the trailing JSON summary object from `tree-sitter parse --json-summary`."""
    idx = stdout.rfind("\n{")
    if idx != -1:
        return json.loads(stdout[idx + 1 :])
    start = stdout.find("{")
    if start != -1:
        return json.loads(stdout[start:])
    raise ValueError("No JSON summary found in tree-sitter parse output")


def run_tree_sitter_cli(
    files: dict[str, Path],
    grammar_dir: Path,
    timeout: int = 3600,
) -> dict[str, bool]:
    """Run `tree-sitter parse -q --json-summary --paths` and return {repo_key: ok}."""
    if not files:
        return {}
    ensure_fresh_cli_cache(grammar_dir)
    abs_to_key = {str(p): k for k, p in files.items()}

    with tempfile.TemporaryDirectory(prefix="ts_e2e_") as tmpdir:
        paths_file = Path(tmpdir) / "paths.txt"
        paths_file.write_text("\n".join(str(p) for p in files.values()), encoding="utf-8")
        proc = subprocess.run(
            ["tree-sitter", "parse", "-q", "--json-summary", "--paths", str(paths_file)],
            cwd=grammar_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    data = _extract_json_summary(proc.stdout)
    summaries = data.get("parse_summaries", [])
    if not summaries:
        raise RuntimeError(
            f"tree-sitter parse produced 0 summaries (rc={proc.returncode}): {proc.stderr.strip()[:500]}"
        )

    results: dict[str, bool] = {}
    for entry in summaries:
        key = abs_to_key.get(entry["file"])
        if key is not None:
            results[key] = bool(entry.get("successful", False))
    return results


def _normalize_fork_key(
    raw_path: str,
    ts_repo: Path | None,
    ets_repo: Path | None,
) -> str | None:
    """Convert an absolute or repo-relative fork verdict key to `ts:<rel>` / `ets:<rel>`."""
    if raw_path.startswith(("ts:", "ets:")):
        return raw_path
    p = Path(raw_path)
    if ts_repo is not None:
        ts_root = ts_repo.resolve()
        try:
            return f"ts:{p.resolve().relative_to(ts_root).as_posix()}"
        except ValueError:
            pass
        marker = f"/{ts_repo.name}/"
        if marker in raw_path:
            return f"ts:{raw_path.split(marker, 1)[1]}"
    if ets_repo is not None:
        ets_root = ets_repo.resolve()
        try:
            return f"ets:{p.resolve().relative_to(ets_root).as_posix()}"
        except ValueError:
            pass
        marker = f"/{ets_repo.name}/"
        if marker in raw_path:
            return f"ets:{raw_path.split(marker, 1)[1]}"
    return None


def load_or_run_fork_oracle(
    files: dict[str, Path],
    ts_repo: Path | None,
    ets_repo: Path | None,
    fork_results_path: Path | None,
    fork_oracle_script: Path,
    skip_fork: bool,
    refresh_fork: bool = False,
) -> tuple[dict[str, bool] | None, dict[str, list[str]], str]:
    """Return (fork_verdicts_by_key, fork_ts_only_by_key, source_description)."""
    if skip_fork:
        return None, {}, "skipped"

    if not refresh_fork and fork_results_path is not None and fork_results_path.exists():
        raw = json.loads(fork_results_path.read_text(encoding="utf-8"))
        raw_verdicts = raw.get("verdicts", raw)
        raw_ts_only = raw.get("ts_only", {})
        verdicts: dict[str, bool] = {}
        ts_only: dict[str, list[str]] = {}
        for k, v in raw_verdicts.items():
            norm = _normalize_fork_key(k, ts_repo, ets_repo)
            if norm is not None and norm in files:
                ok = v.get("ok", False) if isinstance(v, dict) else bool(v)
                verdicts[norm] = ok
                if isinstance(v, dict) and v.get("ts_only"):
                    ts_only[norm] = list(v["ts_only"])
        for k, kinds in raw_ts_only.items():
            norm = _normalize_fork_key(k, ts_repo, ets_repo)
            if norm is not None and norm in files and kinds:
                ts_only[norm] = list(kinds)
        return verdicts, ts_only, f"precomputed:{fork_results_path.name}"

    node_bin = shutil.which("node")
    lib_js = (ts_repo / "lib" / "typescript.js") if ts_repo is not None else None
    if node_bin and lib_js is not None and lib_js.exists() and fork_oracle_script.exists():
        abs_to_key = {str(p): k for k, p in files.items()}
        with tempfile.TemporaryDirectory(prefix="fork_e2e_") as tmpdir:
            paths_file = Path(tmpdir) / "fork_paths.txt"
            out_file = Path(tmpdir) / "fork_out.json"
            paths_file.write_text("\n".join(str(p) for p in files.values()), encoding="utf-8")
            proc = subprocess.run(
                [node_bin, str(fork_oracle_script), str(lib_js), str(paths_file), str(out_file)],
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
            if proc.returncode != 0 or not out_file.exists():
                raise RuntimeError(f"fork_oracle.js failed (rc={proc.returncode}): {proc.stderr[-500:]}")
            raw = json.loads(out_file.read_text(encoding="utf-8"))
        verdicts = {}
        ts_only = {}
        for p, r in raw.items():
            key = abs_to_key.get(p)
            if key is not None:
                verdicts[key] = bool(r.get("ok", False))
                if r.get("ts_only"):
                    ts_only[key] = list(r["ts_only"])
        if fork_results_path is not None:
            fork_results_path.parent.mkdir(parents=True, exist_ok=True)
            fork_results_path.write_text(
                json.dumps({"verdicts": verdicts, "ts_only": ts_only}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"[e2e] wrote fork results: {fork_results_path}")
        return verdicts, ts_only, "node:fork_oracle.js"

    return None, {}, "unavailable"


_TS_ONLY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("var_decl", re.compile(r"(?m)^[ \t]*(?:export\s+)?var\s+")),
    ("for_in", re.compile(r"\bfor\s*\(\s*(?:let|const|var)?\s*\w+\s+in\s+")),
    ("ctor_param_prop", re.compile(r"\bconstructor\s*\([^)]*\b(?:public|private|protected|readonly)\s+\w+")),
    ("import_assert", re.compile(r"\bimport\b[^\n;]*\bassert\s*\{")),
    ("export_eq", re.compile(r"(?m)^[ \t]*export\s*=\s*")),
    ("delete_op", re.compile(r"\bdelete\s+[A-Za-z_$]")),
    ("angle_cast", re.compile(r"(?:=|return|\()\s*<[A-Za-z_]\w*(?:\[\])?>\s*[A-Za-z_(\[]")),
    ("type_predicate", re.compile(r"\)\s*:\s*[A-Za-z_$]\w*\s+is\s+[A-Za-z_$]")),
    ("regex_literal", re.compile(r"(?:=|return|\(|,|\[)\s*/[^/*\s=][^/\n]*/[gimsuy]*")),
]


def detect_ts_only_syntax(path: Path) -> str | None:
    """Return TS-only syntax category if file uses constructs absent from ArkTS spec."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
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
        return "jsx"
    for name, pat in _TS_ONLY_PATTERNS:
        if pat.search(stripped):
            return name
    return None


def _suite_of(key: str) -> str:
    """Group key into a readable suite name, e.g. `ts:tests/arkTSTest` or `ets:ets2panda/linter`."""
    prefix, rest = key.split(":", 1)
    parts = rest.split("/")
    if prefix == "ts" and len(parts) >= 2 and parts[0] == "tests":
        return f"ts:{parts[0]}/{parts[1]}"
    if len(parts) >= 2:
        return f"{prefix}:{parts[0]}/{parts[1]}"
    return f"{prefix}:{parts[0]}"


def build_report(
    files: dict[str, Path],
    ours: dict[str, bool],
    fork: dict[str, bool] | None,
    fork_source: str,
    exclude_ts_syntax: bool = True,
    fork_ts_only: dict[str, list[str]] | None = None,
) -> dict:
    """Join our parse results with optional fork verdicts into a structured report."""
    suites: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    quadrants: dict[str, int] = {
        "must_pass": 0,
        "must_fail": 0,
        "divergence": 0,
        "over_acceptance": 0,
    }
    divergence_categories: dict[str, int] = defaultdict(int)
    ts_excluded: dict[str, int] = defaultdict(int)
    per_file: dict[str, dict] = {}
    divergent_files: dict[str, str] = {}
    over_accepted_files: list[str] = []

    for key, path in files.items():
        our_ok = ours.get(key)
        if our_ok is None:
            continue
        suite = _suite_of(key)
        suites[suite]["total"] += 1
        if our_ok:
            suites[suite]["ours_pass"] += 1
        else:
            suites[suite]["ours_fail"] += 1

        if fork is not None and key in fork:
            fork_ok = fork[key]
            ts_reason: str | list[str] | None = None
            if exclude_ts_syntax and fork_ok:
                ast_kinds = (fork_ts_only or {}).get(key)
                if ast_kinds:
                    ts_reason = ast_kinds
                    for kind_name in ast_kinds:
                        ts_excluded[kind_name] += 1
                else:
                    regex_reason = detect_ts_only_syntax(path)
                    if regex_reason is not None:
                        ts_reason = regex_reason
                        ts_excluded[regex_reason] += 1
            if ts_reason is not None:
                fork_ok = False
            if fork_ok and our_ok:
                q = "must_pass"
                per_file[key] = {"quadrant": q, "ours": True, "fork": True}
            elif not fork_ok and not our_ok:
                q = "must_fail"
                entry: dict = {"quadrant": q, "ours": False, "fork": False}
                if ts_reason is not None:
                    entry["ts_excluded"] = ts_reason
                per_file[key] = entry
            elif fork_ok and not our_ok:
                q = "divergence"
                cat = _categorize_divergence(path)
                divergence_categories[cat] += 1
                divergent_files[key] = cat
                per_file[key] = {
                    "quadrant": q,
                    "ours": False,
                    "fork": True,
                    "category": cat,
                }
            else:
                q = "over_acceptance"
                over_accepted_files.append(key)
                entry = {"quadrant": q, "ours": True, "fork": False}
                if ts_reason is not None:
                    entry["ts_excluded"] = ts_reason
                per_file[key] = entry
            quadrants[q] += 1
            suites[suite][q] += 1
        else:
            per_file[key] = {"ours": our_ok}

    summary = {
        "total_collected": len(files),
        "total_parsed": len(per_file),
        "ours_pass": sum(1 for v in ours.values() if v),
        "ours_fail": sum(1 for v in ours.values() if not v),
        "fork_source": fork_source,
    }
    if fork is not None:
        summary["quadrants"] = quadrants
        summary["oracle_ts_excluded"] = dict(sorted(ts_excluded.items(), key=lambda kv: -kv[1]))
        summary["divergence_categories"] = dict(sorted(divergence_categories.items(), key=lambda kv: -kv[1]))

    return {
        "summary": summary,
        "suites": {k: dict(v) for k, v in sorted(suites.items())},
        "divergent_files": divergent_files,
        "over_accepted_files": over_accepted_files,
        "per_file": per_file,
    }


def diff_baseline(report: dict, baseline: dict) -> dict:
    """Compare current report against a committed baseline ledger."""
    curr = report["per_file"]
    base = baseline.get("per_file", baseline)
    regressions: list[dict] = []
    improvements: list[dict] = []
    new_files: list[str] = []

    for key, info in curr.items():
        if key not in base:
            new_files.append(key)
            continue
        prev_ok = base[key].get("ours")
        curr_ok = info.get("ours")
        if prev_ok is True and curr_ok is False:
            regressions.append({"file": key, "prev": base[key], "curr": info})
        elif prev_ok is False and curr_ok is True:
            improvements.append({"file": key, "prev": base[key], "curr": info})

    return {
        "regressions": regressions,
        "improvements": improvements,
        "new_files": len(new_files),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E2E corpus test runner for tree-sitter-arkts.")
    ap.add_argument("--ts-repo", type=Path, default=None, help="Path to third_party_typescript repository")
    ap.add_argument("--ets-repo", type=Path, default=None, help="Path to arkcompiler_ets_frontend repository")
    ap.add_argument(
        "--ts-dirs",
        nargs="+",
        default=None,
        help="Subdirs under <ts-repo>/tests (default: ArkTS suites "
        "arkTSTest dets issues_cookbook_tests projects; pass ALL for everything)",
    )
    ap.add_argument("--grammar-dir", type=Path, default=DEFAULT_GRAMMAR_DIR, help="Path to arkts_grammar directory")
    ap.add_argument(
        "--fork-results",
        type=Path,
        default=DEFAULT_FORK_RESULTS,
        help="Path to fork_corpus_results.json (read/written under build/test/ by default)",
    )
    ap.add_argument(
        "--refresh-fork", action="store_true", help="Re-run node fork_oracle.js even if fork-results exists"
    )
    ap.add_argument(
        "--fork-oracle",
        type=Path,
        default=DEFAULT_FORK_ORACLE,
        help="Path to fork_oracle.js (used if node is available)",
    )
    ap.add_argument("--skip-fork", action="store_true", help="Skip fork oracle comparison (parse-only)")
    ap.add_argument(
        "--no-exclude-ts-syntax",
        action="store_true",
        help="Do not reclassify fork-accepted TS-only syntax as expected-fail",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=REPO / "build" / "test" / "e2e_corpus_report.json",
        help="Output JSON report path",
    )
    ap.add_argument(
        "--baseline", type=Path, default=None, help="Compare against baseline JSON and exit 2 on regressions"
    )
    ap.add_argument(
        "--update-baseline", type=Path, default=None, help="Write/update compact baseline JSON at this path"
    )
    args = ap.parse_args(argv)

    if args.ts_repo is None and args.ets_repo is None:
        ap.error("Provide at least one of --ts-repo or --ets-repo")

    files = collect_repo_files(args.ts_repo, args.ets_repo, args.ts_dirs)
    if not files:
        print("[e2e] ERROR: no corpus files found", file=sys.stderr)
        return 2

    print(f"[e2e] collected {len(files)} files")
    ours = run_tree_sitter_cli(files, args.grammar_dir)
    fork, fork_ts_only, fork_source = load_or_run_fork_oracle(
        files,
        args.ts_repo,
        args.ets_repo,
        args.fork_results,
        args.fork_oracle,
        args.skip_fork,
        args.refresh_fork,
    )
    report = build_report(
        files,
        ours,
        fork,
        fork_source,
        exclude_ts_syntax=not args.no_exclude_ts_syntax,
        fork_ts_only=fork_ts_only,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    s = report["summary"]
    print(
        f"[e2e] parsed {s['total_parsed']}/{s['total_collected']} | "
        f"ours_pass={s['ours_pass']} ours_fail={s['ours_fail']} | fork={s['fork_source']}"
    )
    if "quadrants" in s:
        q = s["quadrants"]
        print(
            f"[e2e] quadrants: must_pass={q['must_pass']} must_fail={q['must_fail']} "
            f"divergence={q['divergence']} over_acceptance={q['over_acceptance']}"
        )
        for cat, cnt in s["divergence_categories"].items():
            print(f"[e2e]   {cnt:6d}  {cat}")

    print("[e2e] per-suite:")
    for suite, counts in report["suites"].items():
        print(f"[e2e]   {suite:35s} pass={counts.get('ours_pass', 0):5d}/{counts['total']:5d}")
    print(f"[e2e] wrote report: {args.report}")

    if args.update_baseline is not None:
        baseline_payload = {
            "summary": s,
            "per_file": report["per_file"],
        }
        args.update_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.update_baseline.write_text(
            json.dumps(baseline_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[e2e] wrote baseline: {args.update_baseline}")

    if args.baseline is not None:
        base_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        diff = diff_baseline(report, base_data)
        print(
            f"[e2e] baseline diff: regressions={len(diff['regressions'])} "
            f"improvements={len(diff['improvements'])} new={diff['new_files']}"
        )
        if diff["regressions"]:
            for r in diff["regressions"][:20]:
                print(f"[e2e]   REGRESSION: {r['file']}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
