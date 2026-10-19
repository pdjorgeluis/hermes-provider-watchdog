# Watchdog INSTALL.md — agent-followable install procedure

**Audience:** an installing agent on ANY Hermes Agent machine. This file is
the complete procedure: follow it top to bottom, verify each phase, stop and
report on any failure. Background on how the watchdog works lives in
`README.md` (same directory) — you do not need it to install.

**Conventions used below:**
- `$WATCHDOG_DIR` — where this `watchdog/` directory is staged on the target
  machine. Any location works; the code resolves paths relative to itself.
  Export it once before starting:
  ```bash
  export WATCHDOG_DIR="/absolute/path/to/watchdog"   # <- set by the installer
  ```
- `$ENV_FILE` — the target's Hermes env file (default `~/.hermes/.env`).
- `$WATCHDOG_PROFILES` — comma-separated profile names for the target
  (gathered in Phase B; exported for Phase D's substitution).
- `$WATCHDOG_CHAT_ID` — the operator's Telegram chat id (gathered in Phase B;
  obtained from the operator, never guessed).
- Export all three before Phase D:
  ```bash
  export WATCHDOG_DIR="/absolute/path/to/watchdog"      # <- set by the installer
  export WATCHDOG_PROFILES="default,alpha,beta"          # <- Phase B item 1
  export WATCHDOG_CHAT_ID="424242"                       # <- Phase B item 2, from operator
  ```

**STOP rules (hard):** never print, cat, echo, or log secret values (env-file
checks are by COUNT only); never install with a red test suite; never enable
live mode (DRY_RUN=0) — that flip is the operator's personal decision; if the
watchdog is already installed (timer `hermes-watchdog.timer` exists), do not
reinstall — report state and point the operator at README §Operations.

---

## Phase A — Stage and prove the craft

1. Copy the ENTIRE `watchdog/` directory (code, `tests/` with `fixtures/`,
   `README.md`, this file) to the target machine; it must contain
   `provider_watchdog.py`, `provider_watchdog.sh`, `tests/run_tests.py`,
   `tests/test_provider_watchdog.py`, `tests/fixtures/`. If the package
   carries `SHA256SUMS`, verify it first, from inside the staged directory:
   `sha256sum -c SHA256SUMS` — every line must print `OK`.
2. Then:
   ```bash
   cd "$WATCHDOG_DIR"
   chmod +x provider_watchdog.py provider_watchdog.sh
   python3 tests/run_tests.py
   ```
3. Expected: `Ran 26 tests` … `OK`, exit code 0. Anything else → STOP; report
   the suite output verbatim to the operator. Do not fix tests to make them
   pass — report.

## Phase B — State per-install configuration

The code ships neutral defaults; each install states its specifics via
environment variables, set in the systemd unit file in Phase D. Gather these
values now (you will paste them in Phase D):

1. `WATCHDOG_PROFILES` — the target's Hermes profile names, comma-separated
   (e.g. `default` alone, or `default,alpha,beta`). Log layout the script
   expects: default profile logs under `~/.hermes/logs/`, every other
   profile under `~/.hermes/profiles/<name>/logs/` (files `agent.log`,
   `errors.log`). The list is plain config — it can change any time later
   without touching code (see README §2, "Adding / removing agent profiles").
2. `WATCHDOG_CHAT_ID` — the operator's Telegram chat id (obtain from the
   operator, never guess). With this unset the watchdog still detects, probes,
   and logs — but never sends notifications.
3. Only-if-different code constants (edit `provider_watchdog.py`, top of
   file, ONLY if the target needs them): `API_URL` for another provider
   endpoint; `service_name()` for differently
   named gateway units; env key names in `read_env_secrets()` if the target's
   env file uses different variable names.
4. `WATCHDOG_PROBE_MODEL` — optional explicit probe-model override (set in
   the unit file). When unset, the probe resolves the model automatically:
   first `model.default` of the watched profiles from their `config.yaml`
   (default profile scanned first), then `WATCHDOG_FALLBACK_MODEL`, then the
   module constant. The source used is logged on every probe.

## Phase C — Verify secrets WITHOUT ever printing them

Grep for the key names your Phase B adaptation ended with (defaults shown
below; if you renamed the env keys in Phase B item 5, grep for THOSE):

```bash
grep -c '^NVIDIA_API_KEY=' "$ENV_FILE"
grep -c '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE"
```

Both must print `1` (or more). If either prints `0` → STOP and report the
missing KEY NAME (never hunt for the value; never cat the file). If the env
file is not `~/.hermes/.env`, you must also set `WATCHDOG_ENV_FILE` for the
service unit in Phase D (add `Environment=WATCHDOG_ENV_FILE=/path` beside
`DRY_RUN`).

## Phase D — Write the systemd user units

Write `$HOME/.config/systemd/user/hermes-watchdog.service` exactly as follows
(`<WATCHDOG_DIR>` is a literal placeholder; you substitute it in step 3):

```ini
[Unit]
Description=Hermes provider-failure watchdog (one run; triggered by hermes-watchdog.timer)
Documentation=file://<WATCHDOG_DIR>/README.md

[Service]
Type=oneshot
# Delivered state: DRY_RUN=1 — detect + probe + notify only; gateway restarts are
# blocked. The operator arms live mode by setting DRY_RUN=0 here after a dry-run week.
Environment=DRY_RUN=1
Environment=WATCHDOG_PROFILES=<PROFILES_CSV>
Environment=WATCHDOG_CHAT_ID=<OPERATOR_CHAT_ID>
ExecStart=<WATCHDOG_DIR>/provider_watchdog.py
# Worst live-mode path (probe + gateway restarts) stays well under 5 min;
TimeoutStartSec=300
# A watcher must not compete with the watched:
Nice=10
IOSchedulingClass=idle
# Transient curl secret-config files get an isolated /tmp:
PrivateTmp=yes
NoNewPrivileges=yes
```

Write `$HOME/.config/systemd/user/hermes-watchdog.timer`:

```ini
[Unit]
Description=Run the Hermes provider-failure watchdog every 2 minutes

[Timer]
OnCalendar=*:0/2
Persistent=true
Unit=hermes-watchdog.service

[Install]
WantedBy=timers.target
```

Then substitute and verify — no placeholders may survive:

```bash
sed -i "s|<WATCHDOG_DIR>|$WATCHDOG_DIR|g; \
        s|<PROFILES_CSV>|$WATCHDOG_PROFILES|g; \
        s|<OPERATOR_CHAT_ID>|$WATCHDOG_CHAT_ID|g" \
  "$HOME/.config/systemd/user/hermes-watchdog.service"
grep -cE '<WATCHDOG_DIR>|<PROFILES_CSV>|<OPERATOR_CHAT_ID>' \
  "$HOME/.config/systemd/user/hermes-watchdog.service"   # must print 0
systemd-analyze verify "$HOME/.config/systemd/user/hermes-watchdog.timer" \
  "$HOME/.config/systemd/user/hermes-watchdog.service" && echo UNITS-OK
```

## Phase E — Enable and prove it live (still DRY_RUN)

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-watchdog.timer
systemctl --user list-timers --all | grep watchdog    # NEXT and LAST must show
systemctl --user start hermes-watchdog.service       # force one run now
journalctl --user -u hermes-watchdog.service -n 5 --no-pager
systemctl --user show hermes-watchdog.service -p Environment   # must show DRY_RUN=1, WATCHDOG_PROFILES, WATCHDOG_CHAT_ID
```

Expected in the journal: a line ending `… [watchdog] healthy: reason=none —
no action` (or, if real failures are in flight, a burn/detect/probe line —
also a pass; DRY_RUN forbids restarts either way).

## Phase F — Prove the pager

```bash
export WATCHDOG_CHAT_ID="<operator chat id from Phase B>"   # if not already exported
python3 "$WATCHDOG_DIR/selftest_notify.py"
```

Expected: `DELIVERED=True`, and the operator's Telegram chat receives the
self-test DM. `DELIVERED=False`, `SELFTEST=NO-TOKEN`, or
`SELFTEST=NO-CHAT-ID` → STOP and report.

## Phase G — Report to the operator

Deliver a report containing, in plain terms:
- where the directory was staged (`$WATCHDOG_DIR`), suite result line,
- timer active + next-fire time, first-run journal verdict,
- `Environment=DRY_RUN=1` confirmation, pager `DELIVERED=True`,
- the arm commands (below) and the rollback runbook (below),
- EVERY adaptation made in Phase B (constant → old → new), and anything that
  deviated from this procedure. List deviations; never silently decide.

## Arming live mode (OPERATOR ONLY — after a dry-run week)

```bash
sed -i 's/^Environment=DRY_RUN=1$/Environment=DRY_RUN=0/' "$HOME/.config/systemd/user/hermes-watchdog.service"
systemctl --user daemon-reload
systemctl --user show hermes-watchdog.service -p Environment   # verify DRY_RUN=0
```

## Rollback runbook (complete removal; gateways are never touched by this)

```bash
systemctl --user disable --now hermes-watchdog.timer
rm "$HOME/.config/systemd/user/hermes-watchdog.timer"
rm "$HOME/.config/systemd/user/hermes-watchdog.service"
systemctl --user daemon-reload
rm -rf "$WATCHDOG_DIR/state"
```

Staged files remain inert afterward; delete `$WATCHDOG_DIR` itself only if
full erasure is wanted.

## Verification checklist (all must hold before you report success)

- [ ] Suite: `Ran 29 tests` + `OK`, rc=0 (run from `$WATCHDOG_DIR`)
- [ ] Phase B configuration values listed in the report
- [ ] Secret counts printed `1` and `1` (values never seen)
- [ ] `grep -cE` placeholders check on the service unit printed `0`
- [ ] `systemd-analyze verify` printed `UNITS-OK`
- [ ] Timer listed with NEXT/LAST; first run journaled; `DRY_RUN=1` + both `WATCHDOG_*` vars shown
- [ ] Pager `DELIVERED=True`
- [ ] Report delivered; live arming left to the operator

*Install procedure extracted from README.md at Master Jorge's order,
2026-10-16 — paths genericized so any Hermes can raise a watchdog. — Crimvael*
