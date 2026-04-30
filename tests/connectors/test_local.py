from __future__ import annotations

from bot_cmder.connectors.local import LocalConnector


async def test_runs_command_and_captures_stdout():
    r = await LocalConnector().execute(["echo", "hello"])
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello"
    assert r.stderr == ""
    assert r.target == "local"
    assert r.truncated is False


async def test_returns_nonzero_exit_and_stderr():
    r = await LocalConnector().execute(["sh", "-c", "echo bad 1>&2; exit 7"])
    assert r.exit_code == 7
    assert "bad" in r.stderr


async def test_timeout_kills_process():
    r = await LocalConnector().execute(["sleep", "5"], timeout_s=1)
    assert r.exit_code == -1
    assert "timeout" in r.stderr


async def test_truncation_marker_records_dropped_bytes():
    r = await LocalConnector().execute(
        ["sh", "-c", "head -c 5000 /dev/urandom | base64"],
        max_output_bytes=200,
    )
    assert r.truncated is True
    assert "truncated" in r.stdout
