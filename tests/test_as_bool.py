"""Unit tests for the as_bool() config helper (W8-P6).

Verifies:
- JSON booleans pass through unchanged (no behavior change for correct configs).
- String "false"/"0"/"no"/"off" parses as False (the bool("false") is True fix).
- None uses the default.
- Numeric 1/0 delegates to bool().
"""

from slm_mcp_hub.core.config_io import as_bool


class TestAsBool:
    # --- JSON boolean passthrough (MUST be behavior-preserving) ---

    def test_true_bool_passthrough(self) -> None:
        assert as_bool(True) is True

    def test_false_bool_passthrough(self) -> None:
        assert as_bool(False) is False

    # --- String "false" variants that used to be truthy ---

    def test_string_false_is_false(self) -> None:
        assert as_bool("false") is False

    def test_string_0_is_false(self) -> None:
        assert as_bool("0") is False

    def test_string_no_is_false(self) -> None:
        assert as_bool("no") is False

    def test_string_off_is_false(self) -> None:
        assert as_bool("off") is False

    def test_empty_string_is_false(self) -> None:
        assert as_bool("") is False

    # --- String "true" variants ---

    def test_string_true_is_true(self) -> None:
        assert as_bool("true") is True

    def test_string_1_is_true(self) -> None:
        assert as_bool("1") is True

    def test_string_yes_is_true(self) -> None:
        assert as_bool("yes") is True

    def test_string_on_is_true(self) -> None:
        assert as_bool("on") is True

    # --- Case-insensitive ---

    def test_string_false_uppercase(self) -> None:
        assert as_bool("FALSE") is False

    def test_string_false_mixed_case(self) -> None:
        assert as_bool("False") is False

    def test_string_true_uppercase(self) -> None:
        assert as_bool("TRUE") is True

    def test_string_true_mixed_case(self) -> None:
        assert as_bool("True") is True

    # --- Leading/trailing whitespace ---

    def test_string_false_with_whitespace(self) -> None:
        assert as_bool("  false  ") is False

    def test_string_true_with_whitespace(self) -> None:
        assert as_bool("  true  ") is True

    # --- None uses default ---

    def test_none_returns_default_false(self) -> None:
        assert as_bool(None) is False

    def test_none_returns_default_true(self) -> None:
        assert as_bool(None, default=True) is True

    # --- Numeric values delegate to bool() ---

    def test_int_1_is_true(self) -> None:
        assert as_bool(1) is True

    def test_int_0_is_false(self) -> None:
        assert as_bool(0) is False

    def test_int_negative_is_true(self) -> None:
        assert as_bool(-1) is True
