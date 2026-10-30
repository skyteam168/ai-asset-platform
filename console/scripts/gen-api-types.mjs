/** Run tools/gen_console_api_types.py (G1.4). Prefer repo .venv Python. */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const script = path.join(root, "tools", "gen_console_api_types.py");
const candidates = [
  path.join(root, ".venv", "Scripts", "python.exe"),
  path.join(root, ".venv", "bin", "python"),
  process.env.PYTHON,
  "python3",
  "python",
].filter(Boolean);

let py = candidates.find((p) => p === "python" || p === "python3" || existsSync(p));
if (!py) {
  console.error("Python not found; create .venv or set PYTHON");
  process.exit(1);
}
const r = spawnSync(py, [script], { stdio: "inherit", cwd: root, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
process.exit(r.status ?? 1);
