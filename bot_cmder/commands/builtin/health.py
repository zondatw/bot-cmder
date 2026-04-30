from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from bot_cmder.config.schema import HealthTarget
from bot_cmder.core.context import CommandContext
from bot_cmder.core.events import OutgoingResponse
from bot_cmder.core.registry import CommandRegistry, Risk, register

SLOW_THRESHOLD_MS = 1000


@dataclass(frozen=True)
class CheckResult:
    name: str
    label: str  # "OK" / "SLOW" / "FAIL"
    code: int  # -1 on connection failure
    latency_ms: int


async def check_one(target: HealthTarget, client: httpx.AsyncClient) -> CheckResult:
    started = time.monotonic()
    try:
        resp = await client.get(str(target.url), timeout=target.timeout_s)
    except (httpx.HTTPError, OSError):
        return CheckResult(target.name, "FAIL", -1, int((time.monotonic() - started) * 1000))
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code != target.expect_status:
        label = "FAIL"
    elif latency_ms > SLOW_THRESHOLD_MS:
        label = "SLOW"
    else:
        label = "OK"
    return CheckResult(target.name, label, resp.status_code, latency_ms)


def format_table(results: list[CheckResult]) -> str:
    name_w = max((len(r.name) for r in results), default=4)
    lines = [f"{'name'.ljust(name_w)}  status  code  latency"]
    for r in results:
        code = "-" if r.code < 0 else str(r.code)
        lines.append(f"{r.name.ljust(name_w)}  {r.label.ljust(6)}  {code.rjust(4)}  {r.latency_ms} ms")
    return "\n".join(lines)


def install(registry: CommandRegistry) -> None:
    @register(
        "health",
        risk=Risk.SAFE,
        description="HTTP healthcheck against configured targets",
        registry=registry,
    )
    async def _health(ctx: CommandContext, args: list[str]) -> OutgoingResponse:
        targets = ctx.config.healthcheck.targets
        if args:
            wanted = set(args)
            targets = [t for t in targets if t.name in wanted]
            if not targets:
                return OutgoingResponse.text_reply(f"unknown target(s): {', '.join(args)}")
        if not targets:
            return OutgoingResponse.text_reply("no healthcheck targets configured")

        async with httpx.AsyncClient() as client:
            results = list(await asyncio.gather(*(check_one(t, client) for t in targets)))

        return OutgoingResponse.text_reply(format_table(results))
