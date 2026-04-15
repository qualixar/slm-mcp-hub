#!/usr/bin/env node
/**
 * SLM MCP Hub — NPM Postinstall Script
 *
 * Installs the Python package via pip.
 *
 * Copyright (c) 2026 Varun Pratap Bhardwaj / Qualixar
 * Licensed under AGPL-3.0-or-later
 */

const { spawnSync } = require('child_process');
const os = require('os');

console.log('\n════════════════════════════════════════════════════════════');
console.log('  SLM MCP Hub — Intelligent MCP Gateway');
console.log('  by Varun Pratap Bhardwaj / Qualixar');
console.log('  https://qualixar.com/slm-mcp-hub');
console.log('════════════════════════════════════════════════════════════\n');

// Find Python 3
function findPython() {
    const candidates = ['python3', 'python'];
    if (os.platform() === 'win32') candidates.push('py -3');
    for (const cmd of candidates) {
        try {
            const parts = cmd.split(' ');
            const r = spawnSync(parts[0], [...parts.slice(1), '--version'], {
                stdio: 'pipe', timeout: 5000,
                env: { ...process.env, PATH: '/opt/homebrew/bin:/usr/local/bin:/usr/bin:' + (process.env.PATH || '') },
            });
            if (r.status === 0 && (r.stdout || '').toString().includes('3.')) return parts;
        } catch (e) { /* next */ }
    }
    return null;
}

const pythonParts = findPython();
if (!pythonParts) {
    console.log('  Python 3.11+ required. Install from: https://python.org/downloads/');
    console.log('  After installing Python, run: pip install slm-mcp-hub');
    process.exit(0);
}
console.log('  Found Python: ' + pythonParts.join(' '));

// Install slm-mcp-hub via pip
console.log('  Installing slm-mcp-hub...\n');

const pipArgs = [
    ...pythonParts.slice(1), '-m', 'pip', 'install', '--quiet',
    '--disable-pip-version-check', 'slm-mcp-hub',
];
const envWithPath = {
    ...process.env,
    PATH: '/opt/homebrew/bin:/usr/local/bin:/usr/bin:' + (process.env.PATH || ''),
};

let result = spawnSync(pythonParts[0], pipArgs, {
    stdio: 'pipe', timeout: 120000, env: envWithPath,
});

if (result.status !== 0) {
    const stderr = (result.stderr || '').toString();
    if (stderr.includes('externally-managed') || stderr.includes('PEP 668')) {
        result = spawnSync(pythonParts[0], [...pipArgs, '--user'], {
            stdio: 'pipe', timeout: 120000, env: envWithPath,
        });
        if (result.status !== 0) {
            result = spawnSync(pythonParts[0], [...pipArgs, '--break-system-packages'], {
                stdio: 'pipe', timeout: 120000, env: envWithPath,
            });
        }
    }
}

if (result.status === 0) {
    console.log('  slm-mcp-hub installed successfully!\n');
} else {
    console.log('  pip install failed. Run manually: pip install slm-mcp-hub');
    process.exit(0);
}

console.log('════════════════════════════════════════════════════════════');
console.log('  SLM MCP Hub installed!');
console.log('');
console.log('  Quick start:');
console.log('    slm-hub config init          # Initialize config');
console.log('    slm-hub setup import ~/.claude.json  # Import MCPs');
console.log('    slm-hub start                # Start the hub');
console.log('');
console.log('  Docs: https://qualixar.com/slm-mcp-hub');
console.log('════════════════════════════════════════════════════════════\n');
