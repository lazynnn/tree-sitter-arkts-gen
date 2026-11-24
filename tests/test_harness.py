"""Tests for divergence categorization and kind-map CLI output creation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arkts_gen.verify.verify import _categorize_divergence

REPO = Path(__file__).resolve().parent.parent
JSX = "jsx (absent from ArkTS spec)"


def _categorized(tmp_path, text):
    src = tmp_path / "probe.ts"
    src.write_text(text, encoding="utf-8")
    return _categorize_divergence(src)


@pytest.mark.parametrize(
    "text",
    [
        "<T>(x: T) => x",
        "let x = a<b>c",
        '/// <reference path="x.d.ts" />',
        "let y = x as Foo<Bar>",
        "let z: Foo<Bar> = w",
    ],
)
def test_non_jsx_shapes_are_not_categorized_as_jsx(tmp_path, text):
    cat = _categorized(tmp_path, text)
    assert cat != JSX, f"mis-categorized as JSX: {text!r} -> {cat}"


def test_real_jsx_line_is_categorized(tmp_path):
    assert _categorized(tmp_path, "<div>hi</div>") == JSX


def test_jsx_categorization_ignores_reference_comment_line(tmp_path):
    cat = _categorized(tmp_path, '/// <reference path="x.d.ts" />\n<div>hi</div>')
    assert cat == JSX


def test_build_kind_map_creates_nested_out_dir(tmp_path):
    types_ts = REPO / "third_party_typescript" / "src" / "compiler" / "types.ts"
    if not types_ts.exists():
        pytest.skip(f"missing {types_ts}")
    out = tmp_path / "deeply" / "nested" / "new" / "map.json"
    assert not out.parent.exists()
    proc = subprocess.run(
        [sys.executable, "-m", "arkts_gen.mapping.build_kind_map", "--types-ts", str(types_ts), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists(), "nested output file was not created"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "table" in data and data["mapped"] > 0
