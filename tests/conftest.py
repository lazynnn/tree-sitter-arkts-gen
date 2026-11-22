"""Shared fixtures for the audit-campaign regression suite.

Two session-scoped availability fixtures keep the build outputs fresh:

  arkts_parser  — build/libarkts.so exists and is newer than the grammar
                  sources (parser.c, scanner.c, grammar.js); rebuilt via
                  build_lib.py when stale. Skips the parse-dependent tests
                  when no library can be produced (e.g. no C compiler).
  kind_map      — node_type_to_syntaxkind.json exists and is newer than
                  kind_map_overrides.json + build_kind_map.py; regenerated
                  via build_kind_map.py when stale. Returns True when it
                  regenerated the file.
  binding       — the arkts_gen.binding module, imported lazily AFTER the
                  kind map is known fresh (binding.py caches the table at
                  import; a regeneration in this session must invalidate
                  that cache, hence the reload).

These fixtures may write the gitignored build outputs (that is their
purpose); every other test artifact goes to pytest's tmp_path.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import arkts_gen.binding as _binding

REPO = Path(__file__).resolve().parent.parent
LIB_PATH = REPO / "build" / "libarkts.so"
KIND_MAP_PATH = REPO / "build" / "mapping" / "node_type_to_syntaxkind.json"


def _is_stale(target: Path, sources: list[Path]) -> bool:
    if not target.exists():
        return True
    t = target.stat().st_mtime
    return any(s.exists() and t < s.stat().st_mtime for s in sources)


@pytest.fixture(scope="session")
def arkts_parser() -> Path:
    """Fresh build/libarkts.so, or a skip when it cannot be produced."""
    sources = [
        REPO / "arkts_grammar" / "src" / "parser.c",
        REPO / "arkts_grammar" / "src" / "scanner.c",
        REPO / "arkts_grammar" / "grammar.js",
    ]
    if _is_stale(LIB_PATH, sources):
        proc = subprocess.run(
            [sys.executable, "-m", "arkts_gen.grammar.run", "--build-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if proc.returncode != 0:
            if not LIB_PATH.exists():
                pytest.skip(
                    "parser library unavailable: build_lib.py failed and "
                    f"build/libarkts.so is missing ({proc.stderr.strip()[:200]})"
                )
            pytest.fail(
                f"build_lib.py failed but a stale {LIB_PATH} exists — the "
                f"regression suite would test a stale parser:\n{proc.stderr}"
            )
    return LIB_PATH


@pytest.fixture(scope="session")
def kind_map() -> bool:
    """Fresh node_type_to_syntaxkind.json; True when it was regenerated."""
    sources = [REPO / "kind_map_overrides.json", REPO / "arkts_gen" / "mapping" / "build_kind_map.py"]
    if not _is_stale(KIND_MAP_PATH, sources):
        return False
    types_ts = REPO / "third_party_typescript" / "src" / "compiler" / "types.ts"
    if not types_ts.exists():
        if KIND_MAP_PATH.exists():
            return False
        pytest.skip(f"kind map unavailable: missing {types_ts}")
    proc = subprocess.run(
        [sys.executable, "-m", "arkts_gen.mapping.build_kind_map", "--types-ts", str(types_ts)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, (
        "build_kind_map.py failed while refreshing the kind map "
        f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    )
    return True


@pytest.fixture(scope="session")
def binding(arkts_parser, kind_map):
    """The arkts_gen.binding module (reloaded if kind_map was regenerated)."""
    _ = arkts_parser  # fixture dependency: ensure build/libarkts.so is built/fresh
    if kind_map:
        return importlib.reload(_binding)
    return _binding
