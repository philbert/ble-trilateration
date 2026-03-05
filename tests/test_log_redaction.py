"""Tests for secret redaction in Bermuda logging."""

from __future__ import annotations

import logging

from custom_components.bermuda.const import BermudaSecretFilter, redact_secret_hex32


def test_redact_secret_hex32_masks_irk_like_values() -> None:
    """32-hex secret-like tokens should be masked."""
    msg = "Private BLE Callback registered for bermuda_be40185faff50a6b18019281988aaf0a"
    redacted = redact_secret_hex32(msg)
    assert "be40185faff50a6b18019281988aaf0a" not in redacted
    assert "[REDACTED_HEX32]" in redacted


def test_secret_filter_rewrites_logrecord_message() -> None:
    """The central logging filter should mask secrets without per-call redaction."""
    record = logging.LogRecord(
        name="custom_components.bermuda",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="Saved NEW Macirk pair: %s %s",
        args=("65:db:f5:7d:44:5f", "bd11a32a52a032e9f5230de003d8e263"),
        exc_info=None,
    )
    assert BermudaSecretFilter().filter(record)
    assert "bd11a32a52a032e9f5230de003d8e263" not in record.msg
    assert "[REDACTED_HEX32]" in record.msg
