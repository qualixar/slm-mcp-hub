"""Protocol package — transport-neutral product models and operations.

P02: Extract and type the existing meta-tool, resource, prompt, and routing
business operations without changing wire behaviour.

Packages:
  models            — frozen dataclasses; no SDK objects, no credentials.
  product_operations — HubProductOperations business logic.
  conversion        — pure functions: neutral ↔ wire dicts ↔ SDK mcp.types.

P03 will add inbound.py (SDK server adapter) and outbound.py (SDK client
adapter) to this package.
"""
