#!/bin/sh
# Thin wrapper: the watchdog implementation is child_worker_watchdog.py
exec python3 "$(dirname "$(readlink -f "$0")")/child_worker_watchdog.py" "$@"