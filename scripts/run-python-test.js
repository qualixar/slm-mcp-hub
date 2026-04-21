#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const candidates = [
  { command: "python3", args: ["-m", "pytest", "-q"] },
  { command: "python", args: ["-m", "pytest", "-q"] },
  { command: "py", args: ["-3", "-m", "pytest", "-q"] },
];

for (const candidate of candidates) {
  const result = spawnSync(candidate.command, candidate.args, {
    stdio: "inherit",
    shell: false,
  });

  if (result.error && result.error.code === "ENOENT") {
    continue;
  }

  process.exit(result.status ?? 1);
}

console.error("No usable Python runtime found. Install Python 3.11+ and pytest, then retry.");
process.exit(1);
