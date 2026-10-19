# Commission: Provider-Failure Watchdog — build, install (dry-run), verify

**Dispatched by Crimvael, steward, by Master's order, 2026-10-14.**
**SPEC (authoritative, read it first): `~/household/watchdog/provider-failure-watchdog-plan.md` (rev 2).** It is attached to this card as well. Every design decision in it is Master-ratified — implement it as written; where the spec marks something inference, the script must behave conservatively exactly as the spec says.

## What you are building

An independent watchdog (bash or python, your call) + systemd user timer + service:

1. **Detection** — scan the last 5 minutes of the household's agent/error logs for the failure signatures listed in the spec §Trigger:
   - default profile: `~/.hermes/logs/agent.log`, `~/.hermes/logs/errors.log`
   - isis: `~/.hermes/profiles/isis/logs/{agent,errors}.log`
   - sindri: `~/.hermes/profiles/sindri/logs/{agent,errors}.log`
   Signatures (verified against the real 2026-10-14 outage): `failure_reason` stamps `rate_limit|server_error|timeout|connection`, `API call failed`, `API failed after`, `DEGRADED function cannot be invoked`, `Clean EOF, no finish_reason`, `Streaming failed before delivery`, HTTP 429/404 lines. Trigger = sustained (signatures persisting ≥5 min) OR ≥2 profiles failing within the window. Brief self-healed blips must NOT trigger.
2. **Probe** — one 1-token chat completion via fresh `curl --max-time 15` against `https://integrate.api.nvidia.com/v1/chat/completions` using `NVIDIA_API_KEY` from `~/.hermes/.env` (NEVER print the key or full auth header; log response body to the state dir only after scrubbing). Sparse probing per spec: during silence, first probe after the quiet window, then every 5 min.
3. **Decision tree** — spec §2 exactly:
   - Probe OK + agents failing → restart the multiplexed gateway (`systemctl --user restart hermes-gateway.service` — serves all profiles); ≥2 profiles → restart once.
   - Probe FAILS → notify Master, enforce silence, sparse-probe; on 2 consecutive successes restart the gateway once, notify with timeline.
4. **Level 0 burn-rate** — parse `API call #N: ... in=X out=Y ...` lines into a rolling 10-min family requests/min + tokens/min estimate; include the snapshot in every notification. Also capture any 429 response body verbatim to the state dir.
5. **Notifications** — direct Telegram Bot API message to Master's DM (chat 8735869705) using `TELEGRAM_BOT_TOKEN` from `~/.hermes/.env` — no Hermes dependency, key never printed. Notify BEFORE and AFTER every action.
6. **DRY-RUN IS THE DEFAULT**: `DRY_RUN=1` (state file or env) → detect + probe + notify only; NO systemctl restarts ever execute; notifications say "DRY-RUN" prominently. Master arms it himself after the dry-run week.
7. **Safety rails**: one restart round per profile per 30 min; one silence cycle per hour; >2 restarts in an hour → stop acting, notify "manual attention needed"; state under `~/household/watchdog/state/`; log to `~/household/watchdog/watchdog.log` with timestamps.
8. As part of the build, verify the CLI path for pausing household cron jobs (`hermes cron --help`) and implement `--pause-cron` BEHIND a config flag, default OFF (Master decides when to enable).

## Method (your iron laws apply)

- Fail first: write the failing tests before the script (your law #3).
- Test detection WITHOUT a real outage: craft synthetic log samples (you may also use the real 2026-10-14 outage lines — see `~/.hermes/profiles/isis/logs/errors.log` around 12:29–12:44 that day) and prove: trigger fires on sustained/multi-profile signatures; does NOT fire on a single blip; DRY_RUN blocks every restart path.
- Test the probe against the live endpoint ONCE with 1 token (request-budget: one request, that's the whole cost).
- Test the Telegram notification by sending ONE message to Master's DM announcing the watchdog test (he knows it's coming — this dispatch is his order).

## Acceptance (the card's definition of done)

1. Script + timer + service installed: `systemctl --user list-timers | grep watchdog` shows the 2-min cadence, `Persistent=true`.
2. Script runs clean under the timer in DRY_RUN=1.
3. Test suite green: synthetic sustained-failure sample triggers detect+notify (dry-run, no restart); single-blip sample does not trigger; probe OK/fail branches both proven; restart paths provably blocked in dry-run.
4. Burn-rate snapshot appears in the test notification.
5. Report on this card: what was built, test results, the exact commands Master uses to arm it after the dry-run week, and anything the spec left ambiguous (list it — do not silently decide).

## Constraints

- `~/.hermes/.env` values are never printed, logged, or echoed — key names only.
- No gateway restarts execute while DRY_RUN=1 — that includes your own testing.
- Request-budget discipline: the whole card should cost the family pool a handful of requests; one live probe, one DM, no more.
- The watched processes are your family's gateways: treat `systemctl --user restart` code paths with the respect of a smith — tested, gated, capped.

— Crimvael, steward, by Master's order
