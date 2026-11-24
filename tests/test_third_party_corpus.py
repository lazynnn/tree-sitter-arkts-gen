"""Tests for the E2E corpus test runner (third_party_corpus.py)."""

from __future__ import annotations

import json

from arkts_gen.verify.third_party_corpus import (
    _extract_json_summary,
    _normalize_fork_key,
    build_report,
    collect_repo_files,
    detect_ts_only_syntax,
    diff_baseline,
)


def test_collect_repo_files_uses_repo_relative_keys(tmp_path):
    ts_repo = tmp_path / "third_party_typescript"
    (ts_repo / "tests" / "arkTSTest").mkdir(parents=True)
    (ts_repo / "tests" / "arkTSTest" / "ok.ets").write_text("let x = 1;", encoding="utf-8")
    (ts_repo / "tests" / "arkTSTest" / "types.d.ts").write_text("declare const x: number;", encoding="utf-8")
    # full-TS suite excluded by default ArkTS scope
    (ts_repo / "tests" / "cases").mkdir(parents=True)
    (ts_repo / "tests" / "cases" / "ts_only.ts").write_text("var x = 1;", encoding="utf-8")

    ets_repo = tmp_path / "arkcompiler_ets_frontend"
    (ets_repo / "ets2panda" / "test").mkdir(parents=True)
    (ets_repo / "ets2panda" / "test" / "a.ets").write_text("let y = 2;", encoding="utf-8")

    files = collect_repo_files(ts_repo, ets_repo)
    assert list(files.keys()) == [
        "ets:ets2panda/test/a.ets",
        "ts:tests/arkTSTest/ok.ets",
    ]

    all_files = collect_repo_files(ts_repo, ets_repo, ts_dirs=["ALL"])
    assert "ts:tests/cases/ts_only.ts" in all_files


def test_normalize_fork_key_handles_abs_paths(tmp_path):
    ts_repo = tmp_path / "third_party_typescript"
    ets_repo = tmp_path / "arkcompiler_ets_frontend"
    (ts_repo / "tests").mkdir(parents=True)
    (ets_repo / "ets2panda").mkdir(parents=True)

    assert (
        _normalize_fork_key(
            "/other/machine/third_party_typescript/tests/cases/foo.ts",
            ts_repo,
            ets_repo,
        )
        == "ts:tests/cases/foo.ts"
    )
    assert (
        _normalize_fork_key(
            str(ets_repo / "ets2panda" / "bar.ets"),
            ts_repo,
            ets_repo,
        )
        == "ets:ets2panda/bar.ets"
    )


def test_extract_json_summary_ignores_leading_error_lines():
    stdout = (
        "/tmp/foo.ts\tParse: 0.1 ms\t(ERROR [1, 0] - [1, 5])\n"
        '{\n  "parse_summaries": [{"file": "/tmp/foo.ts", "successful": false}]\n}\n'
    )
    data = _extract_json_summary(stdout)
    assert data["parse_summaries"][0]["successful"] is False


def test_build_report_quadrants_and_baseline_diff(tmp_path):
    f1 = tmp_path / "a.ets"
    f2 = tmp_path / "b.ets"
    f3 = tmp_path / "c.ets"
    f4 = tmp_path / "d.ets"
    for f in (f1, f2, f3, f4):
        f.write_text("let x = 1;\n", encoding="utf-8")
    f3.write_text("let x = <div>hi</div>;\n", encoding="utf-8")

    files = {
        "ts:tests/arkTSTest/a.ets": f1,
        "ts:tests/arkTSTest/b.ets": f2,
        "ts:tests/arkTSTest/c.ets": f3,
        "ts:tests/arkTSTest/d.ets": f4,
    }
    ours = {
        "ts:tests/arkTSTest/a.ets": True,
        "ts:tests/arkTSTest/b.ets": False,
        "ts:tests/arkTSTest/c.ets": False,
        "ts:tests/arkTSTest/d.ets": True,
    }
    fork = {
        "ts:tests/arkTSTest/a.ets": True,
        "ts:tests/arkTSTest/b.ets": False,
        "ts:tests/arkTSTest/c.ets": True,
        "ts:tests/arkTSTest/d.ets": False,
    }
    report = build_report(files, ours, fork, "test")
    assert report["summary"]["quadrants"] == {
        "must_pass": 1,
        "must_fail": 2,
        "divergence": 0,
        "over_acceptance": 1,
    }
    assert report["summary"]["oracle_ts_excluded"] == {"jsx": 1}

    baseline = json.loads(json.dumps(report))
    ours_regressed = dict(ours)
    ours_regressed["ts:tests/arkTSTest/a.ets"] = False
    report2 = build_report(files, ours_regressed, fork, "test")
    diff = diff_baseline(report2, baseline)
    assert len(diff["regressions"]) == 1
    assert diff["regressions"][0]["file"] == "ts:tests/arkTSTest/a.ets"


def test_detect_ts_only_syntax_rules(tmp_path):
    p_var = tmp_path / "a.ets"
    p_var.write_text("var x = 1;\n", encoding="utf-8")
    assert detect_ts_only_syntax(p_var) == "var_decl"

    p_ok = tmp_path / "b.ets"
    p_ok.write_text("let x: number = 1;\n", encoding="utf-8")
    assert detect_ts_only_syntax(p_ok) is None


def test_build_report_prefers_ast_ts_only_when_provided(tmp_path):
    f1 = tmp_path / "a.ets"
    f1.write_text("let x = 1;\n", encoding="utf-8")
    files = {"ts:tests/arkTSTest/a.ets": f1}
    ours = {"ts:tests/arkTSTest/a.ets": False}
    fork = {"ts:tests/arkTSTest/a.ets": True}
    report = build_report(
        files,
        ours,
        fork,
        "test",
        fork_ts_only={"ts:tests/arkTSTest/a.ets": ["TypeLiteral"]},
    )
    assert report["summary"]["quadrants"]["must_fail"] == 1
    assert report["summary"]["quadrants"]["divergence"] == 0
    assert report["summary"]["oracle_ts_excluded"] == {"TypeLiteral": 1}
