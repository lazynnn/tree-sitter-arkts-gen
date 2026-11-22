#!/usr/bin/env python3
"""Parse ArkTS files with the Python binding.

Usage:
    python parse_arkts.py FILE [FILE...]
    python parse_arkts.py --tree FILE

Prints per-file: OK or the error nodes (type, position, text). With --tree,
prints the full S-expression tree. Exit code 2 if any file has errors.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from arkts_gen.binding import error_nodes, parse_file


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse ArkTS files (Python binding).")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--tree", action="store_true", help="print the full tree")
    args = ap.parse_args()

    failed = False
    for f in args.files:
        tree = parse_file(f)
        errs = error_nodes(tree)
        if errs:
            failed = True
            print(f"{f}: FAIL ({len(errs)} error node(s))")
            for e in errs[:10]:
                print(
                    f"  {e.type} [{e.start_point.row}, {e.start_point.column}] "
                    f"- [{e.end_point.row}, {e.end_point.column}] "
                    f"{e.text[:60]!r}"
                )
        else:
            print(f"{f}: OK")
        if args.tree:
            print(tree.root_node.__str__())
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
