#!/usr/bin/env python3
"""A strict legacy stdio MCP server that rejects any pre-``initialize`` method.

This reproduces the behaviour of Rust ``rmcp``-based servers (e.g. pplx-mcp
0.11.0). When the SDK's ``mode="auto"`` probe sends ``server/discover`` before
``initialize``, rmcp raises ``ExpectedInitializeRequest`` and **closes the
connection** rather than replying with a JSON-RPC error.

Servers that answer the probe with ``-32601 Method not found`` are handled fine
by the SDK. It is the hard close that strands the hub, so that is what this
fixture reproduces.

Speaks the 2025-06-18 handshake era once ``initialize`` arrives, and exposes a
single ``ping`` tool so a successful connection is observable.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-06-18"
TOOLS = [
    {
        "name": "ping",
        "description": "Return pong.",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    initialized = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return 1

        method = msg.get("method")
        msg_id = msg.get("id")

        # The defining behaviour: anything before initialize kills the process.
        if not initialized and method != "initialize":
            if method == "notifications/initialized":
                continue
            sys.stderr.write(f"ExpectedInitializeRequest: got {method!r}\n")
            return 1

        if method == "initialize":
            initialized = True
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "strict-legacy-server",
                            "version": "1.0.0",
                        },
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "pong"}]},
                }
            )
        elif msg_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
