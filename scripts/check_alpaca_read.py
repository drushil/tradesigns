"""Read-only Alpaca connectivity check for Codex sandbox escalation."""

from __future__ import annotations

import json

from dotenv import load_dotenv

load_dotenv()

from backend.broker.alpaca import get_account


def main() -> None:
    account = get_account()
    payload = {
        "ok": "error" not in account,
        "account": account,
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
