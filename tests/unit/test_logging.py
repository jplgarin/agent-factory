import json
import logging

import pytest
import structlog

from apps.shared.logging import REDACTED, configure_logging, redact_secrets


def test_redacts_secret_keys_at_any_depth() -> None:
    event = {
        "event": "x",
        "api_key": "k",
        "nested": {"Authorization": "Bearer t", "ok": 1, "items": [{"refresh_token": "r"}]},
    }
    out = redact_secrets(None, "info", event)
    assert out["api_key"] == REDACTED
    assert out["nested"]["Authorization"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["nested"]["items"][0]["refresh_token"] == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abc.def-123",
        "key sk-ant-api03-FAKEFAKEFAKE",
        "google ya29.a0FAKEFAKE",
        "refresh 1//0gFAKEFAKEFAKEFAKEFAKEFAKE",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
    ],
)
def test_redacts_token_shaped_values_under_ordinary_keys(text: str) -> None:
    out = redact_secrets(None, "info", {"event": text, "detail": [text]})
    assert REDACTED in out["event"]
    assert REDACTED in out["detail"][0]
    assert "FAKE" not in out["event"]


def test_redacts_credentials_in_urls() -> None:
    out = redact_secrets(None, "info", {"event": "connect postgresql://af:s3cret@db:5432/x"})
    assert out["event"] == f"connect postgresql://{REDACTED}@db:5432/x"


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_structlog_and_stdlib_both_emit_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", service="test", space="personal")
    structlog.get_logger("a").info("hello", password="p", count=2)
    logging.getLogger("temporalio.test").warning("from stdlib %s", "Bearer leaked-token")
    logging.getLogger("x").debug("filtered out")
    first, second = _lines(capsys)
    assert first["event"] == "hello"
    assert first["password"] == REDACTED
    assert first["count"] == 2
    assert first["service"] == "test"
    assert first["space"] == "personal"
    assert first["level"] == "info"
    assert second["event"] == f"from stdlib {REDACTED}"
    assert second["level"] == "warning"
    assert second["service"] == "test"


def test_exceptions_render_as_structured_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", service="test")
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger().exception("failed")
    (line,) = _lines(capsys)
    assert line["event"] == "failed"
    assert isinstance(line["exception"], list)
