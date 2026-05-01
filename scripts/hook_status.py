#!/usr/bin/env python3
"""Print a one-screen summary of the current Telegram webhook state.

Reads TELEGRAM_TOKEN from the environment (loaded by `just`).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_TOKEN not set", file=sys.stderr)
        return 1
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except Exception as exc:
        print(f"ERROR: getWebhookInfo failed: {exc}", file=sys.stderr)
        return 2
    if not data.get("ok"):
        print(f"ERROR: Telegram returned: {data}", file=sys.stderr)
        return 3
    r = data["result"]
    print(f"url:        {r.get('url') or '<unset>'}")
    print(f"pending:    {r.get('pending_update_count', 0)}")
    print(f"max_conn:   {r.get('max_connections', '-')}")
    print(f"last_error: {r.get('last_error_message', '<none>')}")
    if "last_error_date" in r:
        print(f"error_age:  {r['last_error_date']}")
    print(f"ip:         {r.get('ip_address', '<none>')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
