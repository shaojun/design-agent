"""SDK callback bridge and shutdown, without opening a real connection or running a model."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from design_agent.cli import main
from design_agent.config import Settings
from design_agent.surfaces.feishu import app


@pytest.mark.parametrize(
    "message,level,expected",
    [
        (
            "connected to wss://example.invalid/?token=private-value",
            logging.INFO,
            "long connection established",
        ),
        ("connect failed, err: private-value", logging.ERROR, "transport error"),
    ],
)
def test_sdk_connection_logs_hide_urls_and_credentials(
    message: str, level: int, expected: str
) -> None:
    record = logging.LogRecord("Lark", level, "", 0, message, (), None)
    assert app.FeishuSdkLogFilter().filter(record)
    assert expected in record.getMessage()
    assert "private-value" not in record.getMessage()
    assert "wss://" not in record.getMessage()


def test_feishu_config_and_cli_need_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_TENANT_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(SystemExit, match="FEISHU_APP_ID"):
        app.serve(Path("."), settings)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
    monkeypatch.setenv("FEISHU_TENANT_KEY", "tenant")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.feishu_app_id == "cli_test"
    assert "test-secret" not in repr(settings)
    assert CliRunner().invoke(main, ["feishu", "--help"]).exit_code == 0
    assert CliRunner().invoke(main, ["test", "feishu", "--help"]).exit_code == 0


def test_sdk_callback_returns_before_agent_finishes_and_worker_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lark = pytest.importorskip("lark_oapi")
    from lark_oapi.core.model import RawRequest

    received: Future[dict[str, Any]] = Future()
    stopped = threading.Event()
    closed = threading.Event()

    class Client:
        def __init__(self, *args: Any) -> None:
            pass

        def close(self) -> None:
            closed.set()

    class Adapter:
        def dispatch(self, payload: dict[str, Any]) -> None:
            received.set_result(payload)

    async def worker(root: Path, settings: Settings, client: Any, ready: Any) -> None:
        stop = asyncio.Event()
        ready.set_result((Adapter(), asyncio.get_running_loop(), stop))
        await asyncio.wait_for(stop.wait(), timeout=5)
        stopped.set()

    class Socket:
        def __init__(self, *args: Any, event_handler: Any, **kwargs: Any) -> None:
            self.handler = event_handler

        def start(self) -> None:
            req = RawRequest()
            req.uri = "/offline-event"
            req.body = json.dumps(
                {
                    "schema": "2.0",
                    "header": {
                        "event_type": "im.message.receive_v1",
                        "app_id": "cli_test",
                        "tenant_key": "tenant",
                    },
                    "event": {
                        "sender": {"sender_type": "user"},
                        "message": {
                            "message_id": "om_test",
                            "chat_id": "oc_test",
                            "message_type": "text",
                            "content": '{"text":"hello"}',
                        },
                    },
                }
            ).encode()
            started = time.monotonic()
            result = self.handler.do(req)
            assert result.status_code == 200
            assert time.monotonic() - started < 3
            assert received.result(timeout=3)["event"]["message"]["message_id"] == "om_test"
            raise KeyboardInterrupt

    monkeypatch.setattr(app, "FeishuClient", Client)
    monkeypatch.setattr(app, "_worker", worker)
    monkeypatch.setattr(lark.ws, "Client", Socket)
    app.serve(
        Path("."),
        Settings(
            _env_file=None,
            feishu_app_id="cli_test",
            feishu_app_secret="test-secret",
            feishu_tenant_key="tenant",
        ),
    )  # type: ignore[call-arg]
    assert received.done() and stopped.is_set() and closed.is_set()
