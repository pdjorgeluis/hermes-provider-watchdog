#!/usr/bin/env bash
# Thin wrapper: the watchdog implementation is provider_watchdog.py (the standing
# test suite binds that name). This file exists so the card's promised filename
# provider_watchdog.sh also works, e.g. for manual runs:
#   ~/household/watchdog/provider_watchdog.sh            (full cycle, DRY_RUN=1)
#   WATCHDOG_COMMAND=burn ~/household/watchdog/provider_watchdog.sh
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/provider_watchdog.py" "$@"
