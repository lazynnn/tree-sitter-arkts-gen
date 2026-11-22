/**
 * Fork-as-oracle: parse each file with the ohos-typescript fork's own
 * parser and report syntax diagnostics + any TS-only AST constructs from
 * kind_map_overrides.json (arkts_forbidden / not_in_spec).
 *
 * Usage: node fork_oracle.js <lib-typescript.js> <paths-file> <out-json> [overrides-json]
 */
const fs = require("fs");
const path = require("path");

const libPath = process.argv[2];
const pathsFile = process.argv[3];
const outFile = process.argv[4];
const overridesPath =
  process.argv[5] || path.resolve(__dirname, "..", "..", "kind_map_overrides.json");

const ts = require(path.resolve(libPath));
const paths = fs.readFileSync(pathsFile, "utf8").split("\n").filter(Boolean);

// Build numeric SyntaxKind ID -> name map from arkts_forbidden + not_in_spec
const forbiddenById = new Map();
if (fs.existsSync(overridesPath)) {
  const overrides = JSON.parse(fs.readFileSync(overridesPath, "utf8"));
  const cats = overrides.forbidden_categories || {};
  for (const cat of ["arkts_forbidden", "not_in_spec"]) {
    const kinds = (cats[cat] && cats[cat].kinds) || {};
    for (const kindName of Object.keys(kinds)) {
      const id = ts.SyntaxKind[kindName];
      if (typeof id === "number") {
        forbiddenById.set(id, kindName);
      }
    }
  }
}

const PARAM_PROP_MODIFIERS = new Set([
  ts.SyntaxKind.PublicKeyword,
  ts.SyntaxKind.PrivateKeyword,
  ts.SyntaxKind.ProtectedKeyword,
  ts.SyntaxKind.ReadonlyKeyword,
]);

function collectTsOnlyFeatures(sf) {
  const found = new Set();
  function visit(node) {
    if (!node) return;
    const kindName = forbiddenById.get(node.kind);
    if (kindName) {
      found.add(kindName);
    }
    if (
      node.kind === ts.SyntaxKind.VariableDeclarationList &&
      (node.flags & ts.NodeFlags.Let) === 0 &&
      (node.flags & ts.NodeFlags.Const) === 0
    ) {
      found.add("VarDeclaration");
    }
    if (
      node.kind === ts.SyntaxKind.Parameter &&
      node.parent &&
      node.parent.kind === ts.SyntaxKind.Constructor &&
      node.modifiers &&
      node.modifiers.some((m) => PARAM_PROP_MODIFIERS.has(m.kind))
    ) {
      found.add("ConstructorParameterProperty");
    }
    ts.forEachChild(node, visit);
  }
  visit(sf);
  return Array.from(found).sort();
}

const results = {};
for (const p of paths) {
  try {
    const text = fs.readFileSync(p, "utf8");
    const sf = ts.createSourceFile(p, text, ts.ScriptTarget.ES2021, true);
    const n = sf.parseDiagnostics ? sf.parseDiagnostics.length : 0;
    const entry = { ok: n === 0, diagnostics: n };
    if (forbiddenById.size > 0) {
      const tsOnly = collectTsOnlyFeatures(sf);
      if (tsOnly.length > 0) {
        entry.ts_only = tsOnly;
      }
    }
    results[p] = entry;
  } catch (e) {
    results[p] = { ok: false, diagnostics: -1, error: String(e.message) };
  }
}

fs.writeFileSync(outFile, JSON.stringify(results));
const okCount = Object.values(results).filter((r) => r.ok).length;
console.error(
  `[fork] ${okCount}/${Object.keys(results).length} files accepted by fork parser`
);
