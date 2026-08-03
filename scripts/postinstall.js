#!/usr/bin/env node
/** Install the matching Python release into an npm-package-local virtualenv. */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const packageJson = require('../package.json');

const packageRoot = path.resolve(__dirname, '..');
const venvDir = path.join(packageRoot, '.slm-hub-venv');

function commandParts(command) {
    return command.trim().split(/\s+/);
}

function pythonCandidates() {
    const configured = process.env.SLM_HUB_PYTHON;
    const candidates = configured ? [configured] : ['python3', 'python'];
    if (!configured && os.platform() === 'win32') candidates.push('py -3');
    return candidates.map(commandParts);
}

function pythonVersion(parts, runner = spawnSync) {
    const result = runner(parts[0], [...parts.slice(1), '--version'], {
        encoding: 'utf8',
        timeout: 5000,
        env: process.env,
    });
    if (result.status !== 0) return null;
    const output = `${result.stdout || ''} ${result.stderr || ''}`;
    const match = output.match(/Python\s+(\d+)\.(\d+)/);
    if (!match) return null;
    return { major: Number(match[1]), minor: Number(match[2]) };
}

function findPython(runner = spawnSync) {
    for (const parts of pythonCandidates()) {
        const version = pythonVersion(parts, runner);
        if (version && (version.major > 3 || (version.major === 3 && version.minor >= 11))) {
            return parts;
        }
    }
    return null;
}

function venvPython(root = venvDir) {
    return os.platform() === 'win32'
        ? path.join(root, 'Scripts', 'python.exe')
        : path.join(root, 'bin', 'python');
}

function install(runner = spawnSync, filesystem = fs) {
    const python = findPython(runner);
    if (!python) {
        throw new Error('Python 3.11 or newer is required. Set SLM_HUB_PYTHON to its executable.');
    }

    const create = runner(python[0], [...python.slice(1), '-m', 'venv', venvDir], {
        stdio: 'inherit',
        timeout: 120000,
        env: process.env,
    });
    if (create.status !== 0) {
        throw new Error('Could not create the package-local Python virtual environment.');
    }

    const isolatedPython = venvPython();
    if (!filesystem.existsSync(isolatedPython)) {
        throw new Error('Virtual environment was created without a Python executable.');
    }

    const requirement = process.env.SLM_HUB_PYTHON_REQUIREMENT
        || `slm-mcp-hub==${packageJson.version}`;
    const result = runner(isolatedPython, [
        '-m', 'pip', 'install', '--disable-pip-version-check', requirement,
    ], {
        stdio: 'inherit',
        timeout: 180000,
        env: process.env,
    });
    if (result.status !== 0) {
        throw new Error(`Could not install the matching Python package ${requirement}.`);
    }

    const verify = runner(isolatedPython, [
        '-c',
        'import slm_mcp_hub; print(slm_mcp_hub.__version__)',
    ], {
        encoding: 'utf8',
        timeout: 30000,
        env: process.env,
    });
    if (verify.status !== 0 || (verify.stdout || '').trim() !== packageJson.version) {
        throw new Error(`Installed Python package is not version ${packageJson.version}.`);
    }
}

if (require.main === module) {
    try {
        console.log(`Installing SLM MCP Hub ${packageJson.version} in an isolated environment...`);
        install();
        console.log('SLM MCP Hub installed. Run: slm-hub --help');
    } catch (error) {
        console.error(`SLM MCP Hub installation failed: ${error.message}`);
        process.exit(1);
    }
}

module.exports = { findPython, install, pythonVersion, venvPython };
