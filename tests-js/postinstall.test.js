'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const { findPython, install, pythonVersion, venvPython } = require('../scripts/postinstall');
const packageJson = require('../package.json');

test('pythonVersion accepts version output from stdout', () => {
    const runner = () => ({ status: 0, stdout: 'Python 3.12.4', stderr: '' });
    assert.deepEqual(pythonVersion(['python3'], runner), { major: 3, minor: 12 });
});

test('pythonVersion accepts Windows-style stderr output', () => {
    const runner = () => ({ status: 0, stdout: '', stderr: 'Python 3.11.9' });
    assert.deepEqual(pythonVersion(['py', '-3'], runner), { major: 3, minor: 11 });
});

test('pythonVersion rejects command failures and malformed output', () => {
    assert.equal(pythonVersion(['python'], () => ({ status: 1 })), null);
    assert.equal(
        pythonVersion(['python'], () => ({ status: 0, stdout: 'not python', stderr: '' })),
        null,
    );
});

test('findPython rejects versions older than 3.11', () => {
    const oldValue = process.env.SLM_HUB_PYTHON;
    process.env.SLM_HUB_PYTHON = 'custom-python';
    try {
        const runner = () => ({ status: 0, stdout: 'Python 3.10.14', stderr: '' });
        assert.equal(findPython(runner), null);
    } finally {
        if (oldValue === undefined) delete process.env.SLM_HUB_PYTHON;
        else process.env.SLM_HUB_PYTHON = oldValue;
    }
});

test('findPython honors an explicitly configured interpreter', () => {
    const oldValue = process.env.SLM_HUB_PYTHON;
    process.env.SLM_HUB_PYTHON = 'custom-python --isolated';
    try {
        const calls = [];
        const runner = (command, args) => {
            calls.push([command, args]);
            return { status: 0, stdout: 'Python 3.13.1', stderr: '' };
        };
        assert.deepEqual(findPython(runner), ['custom-python', '--isolated']);
        assert.deepEqual(calls, [['custom-python', ['--isolated', '--version']]]);
    } finally {
        if (oldValue === undefined) delete process.env.SLM_HUB_PYTHON;
        else process.env.SLM_HUB_PYTHON = oldValue;
    }
});

test('venvPython resolves inside the package-owned environment', () => {
    const resolved = venvPython('/tmp/slm-test-venv');
    assert.equal(resolved.startsWith(path.resolve('/tmp/slm-test-venv')), true);
    assert.equal(resolved.includes('python'), true);
});

test('install creates a venv and pins the exact npm version', () => {
    const calls = [];
    const runner = (command, args) => {
        calls.push([command, args]);
        if (args.includes('--version')) {
            return { status: 0, stdout: 'Python 3.12.4', stderr: '' };
        }
        if (args.includes('import slm_mcp_hub; print(slm_mcp_hub.__version__)')) {
            return { status: 0, stdout: `${packageJson.version}\n`, stderr: '' };
        }
        return { status: 0 };
    };
    install(runner, { existsSync: () => true });

    assert.equal(calls.some(([, args]) => args.includes('venv')), true);
    assert.equal(
        calls.some(([, args]) => args.includes(`slm-mcp-hub==${packageJson.version}`)),
        true,
    );
    assert.equal(calls.flat(2).includes('--break-system-packages'), false);
});

test('install fails hard when no supported Python exists', () => {
    const oldValue = process.env.SLM_HUB_PYTHON;
    process.env.SLM_HUB_PYTHON = 'missing-python';
    try {
        assert.throws(
            () => install(() => ({ status: 1 }), { existsSync: () => false }),
            /Python 3\.11 or newer is required/,
        );
    } finally {
        if (oldValue === undefined) delete process.env.SLM_HUB_PYTHON;
        else process.env.SLM_HUB_PYTHON = oldValue;
    }
});

test('install fails hard when venv creation fails', () => {
    const runner = (_command, args) => args.includes('--version')
        ? { status: 0, stdout: 'Python 3.12.4', stderr: '' }
        : { status: 1 };
    assert.throws(
        () => install(runner, { existsSync: () => false }),
        /Could not create/,
    );
});

test('install fails hard when exact package installation fails', () => {
    const runner = (_command, args) => {
        if (args.includes('--version') || args.includes('venv')) {
            return args.includes('--version')
                ? { status: 0, stdout: 'Python 3.12.4', stderr: '' }
                : { status: 0 };
        }
        return { status: 1 };
    };
    assert.throws(
        () => install(runner, { existsSync: () => true }),
        new RegExp(`slm-mcp-hub==${packageJson.version}`),
    );
});

test('install fails when the installed Python version differs from npm', () => {
    const runner = (_command, args) => {
        if (args.includes('--version')) {
            return { status: 0, stdout: 'Python 3.12.4', stderr: '' };
        }
        if (args.includes('import slm_mcp_hub; print(slm_mcp_hub.__version__)')) {
            return { status: 0, stdout: '0.0.0\n', stderr: '' };
        }
        return { status: 0 };
    };
    assert.throws(
        () => install(runner, { existsSync: () => true }),
        /Installed Python package is not version/,
    );
});
