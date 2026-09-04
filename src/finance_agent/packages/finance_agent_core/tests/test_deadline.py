from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.agent.providers import local_test as local_test_module
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.storage import connect_read_only


def test_cancelled_request_interrupts_long_running_sqlite_statement(tmp_path: Path) -> None:
    database_path = tmp_path / "deadline.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE marker (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (1)")
        connection.commit()

    deadline = RequestDeadline.after(5)
    deadline.cancel()
    with bind_request_deadline(deadline):
        with closing(connect_read_only(database_path)) as connection:
            with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                connection.execute(
                    """
                    WITH RECURSIVE counter(value) AS (
                        SELECT 1
                        UNION ALL
                        SELECT value + 1 FROM counter WHERE value < 1000000
                    )
                    SELECT SUM(value) FROM counter
                    """
                ).fetchone()


def test_local_development_provider_timeout_is_clamped_to_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"data": []}'

    def fake_urlopen(_request: object, *, timeout: float) -> Response:
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(local_test_module, "urlopen", fake_urlopen)
    provider = LocalTestProvider(
        LocalTestSettings(
            base_url="http://127.0.0.1:18000/v1",
            model="development-test-double",
            timeout_seconds=180,
        )
    )
    with bind_request_deadline(RequestDeadline.after(2)):
        provider._request_json("models", None)  # noqa: SLF001

    assert 0 < captured["timeout"] <= 2
