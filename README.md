# tree-sitter-arkts Generator

Automated pipeline that generates a [Tree-sitter](https://tree-sitter.github.io/) grammar, external ASI scanner, C shared library, and `SyntaxKind` mapping for **ArkTS** from the ArkTS specification PDF (`ArkTS-Sta_Specification_en.pdf`) and the `ohos-typescript` fork's `types.ts`.

## Project Structure

```text
arkts_gen/
├── binding.py              # Public Python runtime API (parse, parse_file, to_syntaxkind)
├── pipeline.py             # End-to-end in-process orchestrator (Stages 1 -> 4)
├── parse_arkts.py          # CLI parser tool
├── preprocess/             # Stage 1: Spec PDF -> EBNF -> typed AST
│   ├── ebnf.py
│   ├── ebnf_ast.py
│   └── run.py
├── grammar/                # Stage 2: AST -> grammar.js + tree-sitter generate -> libarkts.so
│   ├── emitter.py
│   └── run.py
├── mapping/                # Stage 3: Fork SyntaxKind extraction & node-type mapping
│   ├── kinds.py
│   └── build_kind_map.py
└── verify/                 # Stage 4: Four-oracle harness & E2E corpus runner
    ├── verify.py
    ├── third_party_corpus.py
    └── fork_oracle.js

arkts_grammar/src/scanner.c # Hand-written external scanner (ASI & trivia, spec §2.11)
fields.json                 # Hand-editable node field manifest
kind_map_overrides.json     # Hand-maintained SyntaxKind renames, conditionals & forbidden kinds
tests/                      # pytest unit & regression suite
build/                      # Gitignored generated & debug artifacts
```

## Build Artifacts (`build/`)

```text
build/
├── libarkts.so                     # Compiled parser shared library
├── preprocess/
│   ├── grammar_ast.json            # Typed EBNF AST (255 productions)
│   └── ...                         # (--debug / error): grammar.ebnf, extract_report.json, ast_report.json
├── grammar/
│   ├── emit_conflicts.json         # Auto-derived GLR conflicts (113)
│   └── ...                         # (--debug / error): emit_report.json, scanner_needs.json
├── mapping/
│   └── node_type_to_syntaxkind.json# Node type -> fork SyntaxKind table (0 residual kinds)
└── test/
    ├── verify_summary.json         # Conflict & SyntaxKind coverage summary
    ├── fork_corpus_results.json    # Cached fork oracle verdicts + AST ts_only flags
    └── e2e_corpus_report.json      # ArkTS-scoped E2E corpus report
```

## Usage

### 1. Build from scratch
```sh
rm -rf build && uv run python -m arkts_gen.pipeline --debug \
  --types-ts third_party_typescript/src/compiler/types.ts
```
*(Omit `--debug` to skip mid-stage debug reports; omit `--types-ts` if `third_party_typescript` is not present.)*

### 2. Run unit & regression tests
```sh
uv run pytest -q
uv run ruff check
```

### 3. Run ArkTS-scoped E2E corpus tests
```sh
uv run python -m arkts_gen.verify.third_party_corpus \
  --ts-repo third_party_typescript \
  --ets-repo arkcompiler_ets_frontend
```
Uses `build/test/fork_corpus_results.json` when present (pass `--refresh-fork` to re-run `node arkts_gen/verify/fork_oracle.js`, or `--skip-fork` for parse-only).

### 4. Parse files / Tree-sitter Playground
```sh
uv run python -m arkts_gen.parse_arkts path/to/file.ets --tree
cd arkts_grammar && tree-sitter build --wasm && tree-sitter playground
```

## Current Status

- **Pipeline**: `255` spec productions (`0` AST parse errors) → `230` Tree-sitter rules (`113` declared conflicts) → `365` fork `SyntaxKind`s (`129` mapped, `122` explicitly forbidden/internal, `0` residual).
- **Tests & Lint**: `100` pytest tests passing (`0` warnings); `ruff check` passing.
- **ArkTS-Scoped E2E Corpus (`3,310` files)** (`third_party_typescript` ArkTS suites + `arkcompiler_ets_frontend` `.ets`/`.d.ets`, with AST-level exclusion of TS-only `arkts_forbidden` / `not_in_spec` constructs):
  - **Ours Pass**: `2,635 / 3,310` (`Ours Fail`: `675`)
  - **Fork Pass** (after TS-only AST exclusion): `2,529 / 3,310`
  - **Quadrants**: `must_pass = 2,307` | `must_fail = 453` | `divergence = 222` (Fork ✓, Ours ✗) | `over_acceptance = 328` (Fork ✗, Ours ✓)
