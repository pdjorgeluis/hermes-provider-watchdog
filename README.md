# Provider-Failure Watchdog — Reference & Documentation

**What it is:** an independent, zero-LLM-cost watchdog that watches a Hermes
Agent household's LLM provider health by grepping the agents' own logs, probes
the provider with a 1-token request when failures accumulate, and acts on a
probe-gated decision tree: restart poisoned gateway processes, or enforce
total silence during a provider-side outage — paging the operator's Telegram
DM directly either way. Every alert carries a live burn-rate snapshot.

**Design law:** *a watcher must not depend on the watched.* It is a plain
Python 3 script + systemd user timer, fully outside every Hermes process. It
spends zero API requests on healthy runs (only the burn snapshot + log grep,
~1.6 s CPU), and its pager talks to the Telegram Bot API directly — if every
gateway is dead, the watchdog still rings.

**To install this on any Hermes:** follow **`INSTALL.md`** (same directory) —
the complete agent-executable procedure, phases A–G, with generic paths. The
operator's arming commands and the rollback runbook live there too (one copy,
no drift). This file is the reference: how it works, how to operate and tune
it, and why it is shaped this way.

**Path convention:** examples use `$WATCHDOG_DIR` = wherever this `watchdog/`
directory is staged on the machine (the code resolves everything else
relative to itself), and `$ENV_FILE` = the install's Hermes env file.

**Origin:** built for a multi-agent Hermes household (three agent profiles
behind one provider, on a fragile free tier); the design is universal. The
hermetic 29-test suite and the implementation were crafted under
test-first discipline. It runs in **DRY_RUN=1** until the operator arms it.

---

## 1. How it works

Every timer tick (2 minutes) the script runs one **full cycle**:

```
burn ──► detect ──► (triggered?) ──► probe ──► decide ──► notify
 │          │ no                        │        │
 │          └── "healthy: reason=none — no action" (log only, zero requests)
 └── every notification carries the burn line
```

### 1.1 Burn (Level 0 observability — always on)

Parses `API call #…in=X out=Y` lines from every profile's `agent.log` over the
**last 600 s, family-wide**, and computes (THE RULING — one formula feeds the
snapshot file AND every notification text):

- `requests_per_min` = (count of API-call lines in window) / 10, one decimal
- `tokens_per_min` = (sum of in+out across those lines) / 10, one decimal

Written to `state/burn_snapshot.json` every run. Purpose: makes any hidden
provider spend-rule *measurable* — incidents become correlatable against your
own burn rate over time.

### 1.2 Detect (failure signatures, per profile)

Scans each profile's `logs/agent.log` + `logs/errors.log` (identical lines in
both count once) for these signatures:

- `Streaming failed before delivery`
- `API call failed (attempt`
- `Clean EOF, no finish_reason`
- `API failed after`
- `turn_failure: failure_reason=`
- `HTTP/1.1 429 Too Many Requests`
- `HTTP/1.1 404 Not Found`

Semantics (bound to the test suite):

- **blip** — 1 signature line inside the 5-min window in one profile → **no
  trigger** (Hermes's own retry ladder handles blips).
- **sustained** — ≥2 lines inside the 5-min window in one profile, OR ≥2 lines
  with at least one recent and the rest inside a 15-min lookback → trigger.
- **multi-profile** — ≥2 profiles each with ≥1 recent line → trigger, even if
  young (known false-positive risk: one blip in each of two profiles; watch it
  during the dry-run week).
- Priority: multi-profile > sustained > blip > none.

### 1.3 Probe (only on trigger)

One 1-token chat completion (`max_tokens: 1`) via a **fresh `curl` process**
(`--max-time 15`). The API key NEVER touches argv, logs, state, or stdout: it
rides only inside a transient `curl --config` file (mode 0600) that is deleted
immediately after use. Interesting failure bodies (429/5xx/404) are captured
verbatim to `state/last_429_body.txt` for evidence; the scrubbed body goes to
`state/probe_last_body.txt`.

### 1.4 Decide (the probe-gated tree)

**Probe OK, agents failing** → verdict: *our processes are poisoned; the
provider is open to fresh connections.* Remedy: `systemctl --user restart` of
the affected gateway service(s); if ≥2 profiles are affected, restart all. The
process restart is the proven cure (a session reset is not the lever).

**Probe FAILS** → verdict: *key-level cooldown or provider outage.* Remedy:
**total silence, then fresh start.**
1. Page the operator's DM immediately: silence enforced.
2. Guaranteed quiet window (default 5 min) — no probes at all; the watchdog
   itself goes sparse (one probe per 5 min) until recovery.
3. Recovery is **probe-gated, never clock-gated**: 2 consecutive OK probes →
   one restart round over ALL gateways, silence lifts, full timeline paged.

### 1.5 Safety rails (live mode)

- One restart round per profile per 30 minutes (cooldown).
- More than 2 restart rounds per hour → stop acting entirely, page
  "MANUAL ATTENTION NEEDED".
- One silence cycle per hour.
- Notification dedup: one page per key per 5 minutes.
- Cooldowns/rounds are recorded **only in live mode** — a dry-run period never
  burns the real budgets.
- `WATCHDOG_PAUSE_CRON=1` (OFF by default): attempts `hermes cron pause`
  during silence. Caveat: that CLI pauses per-job, not globally — do not
  enable unless the operator has named the jobs or authorized another
  mechanism.

### 1.6 DRY_RUN (delivered state)

`DRY_RUN=1` (default; ANY value other than exactly `0` means dry-run): detect
+ probe + notify work fully, but **no `systemctl restart` ever executes** —
the watchdog pages "DRY-RUN — would restart …" instead. This is how it ships;
the operator arms live mode personally (see `INSTALL.md`).

### 1.7 State files (`$WATCHDOG_DIR/state/` — gitignored, never archive)

`burn_snapshot.json`, `detection.json`, `probe_result.json`,
`probe_last_body.txt`, `last_429_body.txt`, `silence.json`,
`restart_stamps.json`, `rounds.json`, `notify_history.json`, `watchdog.log`.

### 1.8 Manual subcommands (via `WATCHDOG_COMMAND` env)

```bash
WATCHDOG_COMMAND=burn   "$WATCHDOG_DIR/provider_watchdog.sh"   # print burn only
WATCHDOG_COMMAND=detect "$WATCHDOG_DIR/provider_watchdog.sh"   # print detection only
WATCHDOG_COMMAND=probe  "$WATCHDOG_DIR/provider_watchdog.sh"   # one probe
"$WATCHDOG_DIR/provider_watchdog.sh"                           # full cycle
```

Environment overrides: `DRY_RUN`, `WATCHDOG_STATE_DIR`, `WATCHDOG_ENV_FILE`,
`WATCHDOG_CHAT_ID`, `WATCHDOG_PAUSE_CRON`, `WATCHDOG_COMMAND`.

### 1.9 Requirements (any Hermes install)

- Linux with **systemd user session** (`systemctl --user` works; `loginctl
  enable-linger <user>` if it must survive logout).
- `python3` (≥3.8; stdlib only) and `curl` on PATH.
- A Hermes layout of this machine's age: default profile logs at
  `~/.hermes/logs/`, other profiles at `~/.hermes/profiles/<name>/logs/`
  (`agent.log` + `errors.log` each).
- Gateway systemd units named `hermes-gateway.service` (default profile) and
  `hermes-gateway-<profile>.service` (others).
- An env file containing the provider API key and the Telegram bot token as
  `KEY=VALUE` lines (both read at runtime; values never persisted anywhere).
- Telegram: the bot must be able to DM the operator's chat id.

---

## 2. Operations cheat-sheet

- **Install / arm / rollback:** see `INSTALL.md` (the single procedure copy).
- **Health at a glance:** `tail "$WATCHDOG_DIR/state/watchdog.log"`
- **Why did it fire?** `cat "$WATCHDOG_DIR/state/detection.json"` +
  `state/probe_result.json`
- **429 evidence:** `state/last_429_body.txt` (captured verbatim, key scrubbed)
- **Burn history:** `state/burn_snapshot.json` (overwritten each run)
- **One manual cycle:** `"$WATCHDOG_DIR/provider_watchdog.sh"`
- **Test suite:** `cd "$WATCHDOG_DIR" && python3 tests/run_tests.py`

### Adding / removing agent profiles (household changes)

The profile list is per-install configuration, never code. When a new agent
profile is born (or one retires):

1. Prereqs hold automatically for standard Hermes profiles: logs at
   `~/.hermes/profiles/<name>/logs/agent.log` + `errors.log`, gateway unit
   named `hermes-gateway-<profile>.service`.
2. Edit the service unit's `Environment=WATCHDOG_PROFILES=…` line
   (comma-separated; the `default` profile covers `~/.hermes/logs/`).
3. `systemctl --user daemon-reload` — the next timer tick (≤2 min) watches
   the new profile; no restarts, no code or test changes (the suite uses its
   own fake profiles).
4. Verify after the next tick: `systemctl --user show
   hermes-watchdog.service -p Environment`, and `state/detection.json` must
   list the new profile under `per_profile` (`recent: 0` when healthy).

Rollback: remove the name from the line and `daemon-reload` again.

## 3. Tuning constants (top of `provider_watchdog.py`)

| Constant | Default | Meaning |
|---|---|---|
| `BURN_WINDOW_S` | 600 | burn window (THE RULING) |
| `DETECT_WINDOW_S` | 300 | recent-failure window |
| `PERSIST_LOOKBACK_S` | 900 | "persisting" evidence lookback |
| `SILENCE_QUIET_S` | 300 | guaranteed quiet before first probe |
| `SPARSE_PROBE_S` | 300 | min spacing of silence-phase probes |
| `RESTART_COOLDOWN_S` | 1800 | per-profile restart cooldown |
| `ROUND_CAP_PER_HOUR` | 2 | restart rounds/hour before manual-attention |
| `PROBE_TIMEOUT_S` | 15 | curl timeout |
| `NOTIFY_DEDUP_S` | 300 | per-key page dedup |

Household-specific **configuration** is per-install environment, never code
edits (see `INSTALL.md` Phase B): `WATCHDOG_PROFILES` (profile names),
`WATCHDOG_CHAT_ID` (operator chat id — unset means detect/probe/log but
never notify). Only-if-different code constants: `API_URL` + probe model,
`service_name()` mapping, env key names.

Any change to burn semantics must keep ONE formula feeding snapshot and
notification (THE RULING), and the test suite is rewritten to match a binding
ruling — never the reverse.

## 4. Design invariants (why it is shaped this way)

1. **Independent of the watched** — plain script + timer; no Hermes process,
   no LLM calls on healthy runs; pager rides the Bot API directly.
2. **DRY_RUN is the shipped state** — the operator personally flips the one
   line that grants restart authority; a dry-run week is the rollback test.
3. **Probe-gated, never clock-gated** — action follows evidence the provider
   answers, on both the trigger and the recovery sides.
4. **Process restart is the cure; session resets are not** — operator-proven
   (2026-10-14): only a fresh process recovers the sustained-throttle state.
5. **Secrets ride only in transient 0600 curl configs** — never argv, never
   logs, never state files; key NAMES may be logged, values never.
6. **Every page carries the burn line** — observability ships with the alarm.

*Documentation and procedure authored 2026-10-16. The suite is the smith's
craft; the house keeps it green.*
