"""W4-P2 tests — TimeoutClass, TimeoutPolicy, TimeoutRegistry (timeouts.py).

TDD: written BEFORE implementation. Run first to confirm RED.
"""

from __future__ import annotations

import logging

import pytest


class TestTimeoutRegistryBuiltins:
    """TimeoutRegistry resolves the four built-in classes correctly."""

    def test_timeout_registry_resolves_builtin_classes(self) -> None:
        """TimeoutRegistry().resolve('fast') returns TimeoutPolicy(timeout_s=30.0);
        resolve('unbounded') returns TimeoutPolicy(timeout_s=None)."""
        from slm_mcp_hub.federation.timeouts import (
            TIMEOUT_CLASS_DEFAULT,
            TIMEOUT_CLASS_EXTENDED,
            TIMEOUT_CLASS_FAST,
            TIMEOUT_CLASS_UNBOUNDED,
            TimeoutRegistry,
        )

        registry = TimeoutRegistry()

        fast = registry.resolve(TIMEOUT_CLASS_FAST)
        assert fast.timeout_s == 30.0
        assert fast.keepalive_interval_s is None

        default = registry.resolve(TIMEOUT_CLASS_DEFAULT)
        assert default.timeout_s == 120.0
        assert default.keepalive_interval_s is None

        extended = registry.resolve(TIMEOUT_CLASS_EXTENDED)
        assert extended.timeout_s == 600.0
        assert extended.keepalive_interval_s == 55.0

        unbounded = registry.resolve(TIMEOUT_CLASS_UNBOUNDED)
        assert unbounded.timeout_s is None
        assert unbounded.keepalive_interval_s == 55.0

    def test_timeout_registry_fallback_on_unknown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """resolve('nonexistent') returns DEFAULT policy (120s); emits a warning log."""
        from slm_mcp_hub.federation.timeouts import TimeoutRegistry

        registry = TimeoutRegistry()

        with caplog.at_level(logging.WARNING, logger="slm_mcp_hub.federation.timeouts"):
            policy = registry.resolve("nonexistent_class")

        assert policy.timeout_s == 120.0  # DEFAULT
        assert "Unknown timeout class" in caplog.text
        assert "nonexistent_class" in caplog.text

    def test_timeout_registry_override_takes_precedence(self) -> None:
        """Operator override for 'extended' overrides the builtin timeout_s."""
        from slm_mcp_hub.federation.timeouts import (
            TIMEOUT_CLASS_EXTENDED,
            TIMEOUT_CLASS_FAST,
            TimeoutPolicy,
            TimeoutRegistry,
        )

        overrides = {
            TIMEOUT_CLASS_EXTENDED: TimeoutPolicy(
                timeout_s=900.0, keepalive_interval_s=30.0
            )
        }
        registry = TimeoutRegistry(overrides=overrides)

        extended = registry.resolve(TIMEOUT_CLASS_EXTENDED)
        assert extended.timeout_s == 900.0
        assert extended.keepalive_interval_s == 30.0

        # Other classes are unaffected by the override
        fast = registry.resolve(TIMEOUT_CLASS_FAST)
        assert fast.timeout_s == 30.0

    def test_resolve_for_server_call_override_wins(self) -> None:
        """resolve_for_server('unbounded', call_timeout_override_s=60.0) returns
        TimeoutPolicy(timeout_s=60.0, keepalive_interval_s=55.0) —
        override replaces class timeout_s but keepalive from class is preserved."""
        from slm_mcp_hub.federation.timeouts import (
            TIMEOUT_CLASS_UNBOUNDED,
            TimeoutRegistry,
        )

        registry = TimeoutRegistry()

        policy = registry.resolve_for_server(
            TIMEOUT_CLASS_UNBOUNDED, call_timeout_override_s=60.0
        )

        # Override wins for timeout_s
        assert policy.timeout_s == 60.0
        # keepalive_interval_s from UNBOUNDED class is preserved
        assert policy.keepalive_interval_s == 55.0


class TestMCPServerConfigTimeoutClass:
    """MCPServerConfig timeout_class field: default + validation."""

    def test_mcpserverconfig_timeout_class_default(self) -> None:
        """MCPServerConfig with no timeout_class field defaults to 'default'."""
        from slm_mcp_hub.core.config import MCPServerConfig
        from slm_mcp_hub.federation.timeouts import TIMEOUT_CLASS_DEFAULT

        config = MCPServerConfig(name="test", transport="http", url="http://127.0.0.1:1")
        assert config.timeout_class == TIMEOUT_CLASS_DEFAULT

    def test_mcpserverconfig_timeout_class_explicit_values(self) -> None:
        """MCPServerConfig accepts all four valid timeout_class values."""
        from slm_mcp_hub.core.config import MCPServerConfig, validate_server_config

        for cls in ("fast", "default", "extended", "unbounded"):
            config = MCPServerConfig(
                name="test",
                transport="http",
                url="http://127.0.0.1:1",
                timeout_class=cls,
            )
            # validate_server_config should not raise
            validate_server_config(config)

    def test_mcpserverconfig_validates_timeout_class(self) -> None:
        """MCPServerConfig with timeout_class='nonsense' raises ConfigValidationError."""
        from slm_mcp_hub.core.config import (
            ConfigValidationError,
            MCPServerConfig,
            validate_server_config,
        )

        config = MCPServerConfig(
            name="test",
            transport="http",
            url="http://127.0.0.1:1",
            timeout_class="nonsense",
        )
        with pytest.raises(ConfigValidationError, match="timeout_class"):
            validate_server_config(config)

    def test_mcpserverconfig_max_concurrency_default(self) -> None:
        """MCPServerConfig.max_concurrency defaults to 10."""
        from slm_mcp_hub.core.config import MCPServerConfig

        config = MCPServerConfig(name="test", transport="http", url="http://127.0.0.1:1")
        assert config.max_concurrency == 10

    def test_mcpserverconfig_validates_max_concurrency_zero(self) -> None:
        """MCPServerConfig with max_concurrency=0 raises ConfigValidationError."""
        from slm_mcp_hub.core.config import (
            ConfigValidationError,
            MCPServerConfig,
            validate_server_config,
        )

        config = MCPServerConfig(
            name="test",
            transport="http",
            url="http://127.0.0.1:1",
            max_concurrency=0,
        )
        with pytest.raises(ConfigValidationError, match="max_concurrency"):
            validate_server_config(config)
