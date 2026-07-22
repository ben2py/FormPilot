const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const manifestPath = path.join(root, "manifest.json");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const requiredFiles = [
  manifest.action.default_popup,
  ...manifest.content_scripts.flatMap((entry) => entry.js)
];

for (const file of requiredFiles) {
  if (!fs.existsSync(path.join(root, file))) throw new Error(`Manifest references missing file: ${file}`);
}

if (manifest.manifest_version !== 3) throw new Error("FormPilot must use Manifest V3");
if (manifest.permissions.includes("webRequestBlocking")) throw new Error("Unsafe blocking network permission is not allowed");

const scripts = [
  "src/core/formpilot-core.js",
  "src/content/content.js",
  "src/popup/popup.js"
];

for (const script of scripts) {
  const result = spawnSync(process.execPath, ["--check", path.join(root, script)], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || `Syntax check failed: ${script}`);
}

console.log(`Validated Manifest V${manifest.manifest_version} and ${scripts.length} JavaScript files.`);
