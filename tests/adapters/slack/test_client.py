"""Tests for SlackClient (response_url POST).

Pins the contract that adapter.send relies on:
  - in_channel=True/False maps to the right `response_type` string
  - long output is truncated with a marker (no silent 4xx from Slack)
  - the URL itself is taken at face value (we never rewrite it — that
    URL is a one-shot capability granted by Slack)
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bot_cmder.adapters.slack.client import SlackClient

RESPONSE_URL = "https://hooks.slack.com/commands/T0/123/abc"


@respx.mock
@pytest.mark.asyncio
async def test_send_response_in_channel_marks_response_type():
    route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))
    async with SlackClient() as client:
        await client.send_response(RESPONSE_URL, "hello team", in_channel=True)
    body = json.loads(route.calls.last.request.read())
    assert body["response_type"] == "in_channel"
    assert body["text"] == "hello team"


@respx.mock
@pytest.mark.asyncio
async def test_send_response_ephemeral_default():
    route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))
    async with SlackClient() as client:
        await client.send_response(RESPONSE_URL, "private", in_channel=False)
    body = json.loads(route.calls.last.request.read())
    assert body["response_type"] == "ephemeral"


@respx.mock
@pytest.mark.asyncio
async def test_long_output_is_truncated():
    """Slack would 400 a 50_000 char message; we cap at 3000 with marker."""
    route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))
    async with SlackClient() as client:
        await client.send_response(RESPONSE_URL, "x" * 50000, in_channel=False)
    body = json.loads(route.calls.last.request.read())
    assert len(body["text"]) <= 3000
    assert body["text"].endswith("[...truncated]")


@respx.mock
@pytest.mark.asyncio
async def test_empty_text_replaced_with_placeholder():
    """A handler that returns '' would produce a Slack 400; we substitute."""
    route = respx.post(RESPONSE_URL).mock(return_value=httpx.Response(200, text="ok"))
    async with SlackClient() as client:
        await client.send_response(RESPONSE_URL, "", in_channel=False)
    body = json.loads(route.calls.last.request.read())
    assert body["text"] == "(no output)"
