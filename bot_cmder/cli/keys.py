"""`bot-cmder gen-master-key` — print a fresh Fernet master key.

Replaces the inline python one-liner the Justfile and README used to
recommend. Pipe it into a `.env` like:

    bot-cmder gen-master-key >> ~/.config/bot-cmder/.env  # nope — this just appends raw bytes
    echo "BOT_CMDER_MASTER_KEY=$(bot-cmder gen-master-key)" >> .env

The output is a single line of url-safe base64 — exactly what the
`cryptography` package's `Fernet.generate_key()` produces. `bot-cmder
init` calls this internally; this subcommand exists for the case
where an SRE wants to rotate keys outside the init flow.
"""

from __future__ import annotations

import argparse


def cmd_gen_master_key(args: argparse.Namespace) -> int:
    # Imported lazily so `bot-cmder --help` doesn't pull in cryptography.
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "gen-master-key",
        help="Print a fresh Fernet master key (32 url-safe base64 bytes)",
        description=(
            "Generate a fresh BOT_CMDER_MASTER_KEY value. Pipe into your .env. "
            "Rotating this key invalidates every existing TOTP enrollment — "
            "users will need to re-run `bot-cmder enroll-totp`."
        ),
    )
    p.set_defaults(func=cmd_gen_master_key)
