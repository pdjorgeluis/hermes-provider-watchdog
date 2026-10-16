#!/usr/bin/env python3
"""One-shot live self-test of the watchdog's real Telegram delivery path.

Fires a single self-test DM to the operator's chat through the script's own
notify() (real token, real Bot API, no gateway involved) so the delivered
pager is known to ring before the dry-run week starts. Run once; nothing else.

Requires WATCHDOG_CHAT_ID in the environment (the operator's Telegram chat
id — the same value the systemd unit file sets).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_watchdog as w


def main() -> int:
    if not w.chat_id():
        print("SELFTEST=NO-CHAT-ID (set WATCHDOG_CHAT_ID first)")
        return 1
    secrets = w.read_env_secrets()
    if not secrets.get("TELEGRAM_BOT_TOKEN"):
        print("SELFTEST=NO-TOKEN")
        return 1
    ok = w.notify(
        secrets,
        "watchdog SELF-TEST: notification delivery path verified live. "
        "Watchdog is armed in DRY-RUN: detect + notify only, no gateway "
        "restarts will execute. Burn snapshot rides every alert.",
        "selftest_notify",
    )
    print("DELIVERED=", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
