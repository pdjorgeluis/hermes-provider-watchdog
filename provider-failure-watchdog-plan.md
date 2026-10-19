# Provider-Failure Watchdog — Research Findings & Proposed Plan (rev 2)

**Author:** steward — researched at operator's order; **revised** after operator's evidence
**Status:** DRAFT rev 2 — awaiting operator's review. Nothing below is built or enabled.
**What rev 2 changes:** the rev-1 "429 → do nothing" level is DEAD. Operator's manual tests prove the sustained-throttle state is not cured by waiting and not cured by session resets — only by a full process restart. The plan is rebuilt around one probe-centered decision tree whose remedy of record is **total silence + fresh process**.

---

## 1. Evidence of record

### 1.1 Operator empirical tests — the controlling evidence (Master, opencode, 2026-10-14)

- When the 40-RPM limit (or a hidden spend rule) trips in earnest: **increasing the retry delay does NOT recover.**
- **Starting a new session (`/new`) does NOT recover.**
- **ONLY closing the CLI entirely and reopening recovers.**
- Implication: the poison is **process-level, not session-level** — the HTTP connection pool, TLS session state, or an in-process limiter — possibly combined with a hidden server-side rule: a cooldown keyed to the API key, or to cumulative token/request spend. (A TPM bucket is plausible for some installations: large requests run significant input tokens each — one fat request could drain a per-minute token budget that a nominal RPM limit allows.)
- **Design consequence:** we stay cause-agnostic about NVIDIA's hidden rules — we cannot see them; we can only behave correctly against their observed symptoms. The remedy of record is what your tests proved works: **total silence, then a fresh process.** Waiting alone is insufficient; session resets alone are insufficient.

### 1.2 Translation to Hermes

- Hermes's "close the CLI and reopen" = **restarting the gateway services** — the processes that own the HTTP clients and connection pools to nvidia. Not session resets (your `/new` evidence rules them out as the lever), not longer waits.
- A brief 429 blip inside the normal ~60s window still self-heals (Hermes honors Retry-After). **The danger state is sustained throttle: failures continuing for minutes despite the ladder's own waits.** Working definition: Retry-After honored, but failure signatures persist ≥5 minutes.
- **Probes are requests too.** If a hidden cooldown renews on any request, even a 1-token probe could extend it (inference, marked — unverifiable from outside). Therefore: probes during the silence phase are **sparse** (first probe after the quiet period, then every 5 minutes), and always from a **fresh connection** — plain `curl`, a new process per probe, immune to connection-pinning.

---

## 2. The mechanism — one decision tree, probe at the center

**Form:** independent watchdog script + systemd user timer, 2-minute cadence, zero LLM cost, deliberately outside every Hermes process (a watcher must not depend on the watched; a Hermes cron job was rejected — it spends pool requests and dies with the very gateway it guards).

### Level 0 — Burn-rate observability (always on)
Parse each profile's `agent.log` API-call lines (`in=X out=Y` — the token accounting is already in our logs) into a rolling family-wide requests/min and tokens/min estimate. **Every watchdog notification carries the burn snapshot.** Purpose: your "hidden spend rule" hypothesis becomes *measurable* — over time we correlate incidents against our own burn rate and can identify the hidden threshold empirically, whatever it is. Also captures full 429 response bodies verbatim (if NVIDIA's body names quotas or windows, that is direct evidence).

### Trigger — sustained failure
Failure signatures (`failure_reason` stamps: `rate_limit` / `server_error` / `timeout` / `connection`; the DEGRADED-function 400; Clean-EOF storms) in any profile's logs within the last 5 minutes, with **either** (a) failures persisting ≥5 minutes despite Hermes's own Retry-After waits, **or** (b) ≥2 profiles failing simultaneously. A brief self-healed 429 never triggers.

### Step 1 — The probe
One 1-token chat completion: fresh `curl`, family key, 15s timeout, body captured.

### Step 2a — Probe OK, agents failing
**Verdict: our processes are poisoned; the provider is open to fresh connections.** Remedy: **restart the multiplexed gateway** (`systemctl --user restart hermes-gateway.service`); if ≥2 profiles affected, restart the one gateway unit once (multiplex mode — all profiles share it). Fresh processes = your proven cure. Interrupted sessions auto-reset to fresh children on next message (proven this morning). Cap: one restart round per profile per 30 minutes.

### Step 2b — Probe FAILS
**Verdict: key-level hidden cooldown, or a provider outage.** Remedy — total silence, then fresh start:
1. **Notify the operator's DM immediately**: sustained throttle/outage detected; silence enforced.
2. **Enforce quiet**: the watchdog itself goes sparse (first probe after the silence window, then every 5 min) — and every other request is suspect during a cooldown: the notification asks for (or performs, if authorized — decision 6) a pause of cron jobs. Zero requests is the state achieved by closing the CLI; nothing less certain.
3. **On 2 consecutive probe successes**: restart ALL gateway services once — fresh processes are the only proven cure — then notify with the full timeline and the burn snapshot.
4. Log everything to `~/.hermes/watchdog/`.

**What replaces the fixed 5-minute wait:** the silence default is 5 minutes of *guaranteed quiet* (configurable), but recovery is **probe-gated, not clock-gated** — we act on evidence the provider answers, never sooner, never blindly later.

**ADDENDUM (operator's order — applies to ALL plans, exemplified here):** long tasks on a free-tier provider are to be **planned as small parts, each landable in one short session**. Proven failure mode: single long streaming turns at high context die. The counter-method: plan small parts before starting, one write per turn, artifacts on disk early, sessions chained by files rather than one growing stream, cheap-resume notes for every fresh session. Spec-writers: every future plan bakes this in from the start — the commissioning brief itself should name the parts.

---

## 3. Implementation sketch

```
~/.hermes/watchdog/provider_watchdog.sh     (the script)
~/.hermes/watchdog/state/                    (probe history, restart stamps, burn window)
~/.config/systemd/user/hermes-watchdog.timer   (every 2 min, Persistent=true)
~/.config/systemd/user/hermes-watchdog.service (short-lived, runs the script)
```

- Detection: tail each profile's `logs/agent.log` + `logs/errors.log` (signatures verified against this morning's real outage lines).
- Probe: `curl --max-time 15`, fresh process each time, response body to the state dir.
- Notifications: direct Telegram Bot API to your DM from the script — no gateway dependency — before and after every action.
- **Dry-run week first**: detect + notify only, no restarts (recommended default; your call).
- Safety rails: 1 restart round per profile per 30 min; 1 silence cycle per hour; more than 2 service restarts in an hour → stop acting, DM you "manual attention needed"; the script never touches sessions directly — the process restart IS the cure, and session freshness is the gateway's own proven machinery.

---

## 4. Decisions for Master

1. **Restart authority** — authorize the watchdog to restart gateway services (dry-run week first, my recommendation)?
2. **Silence default** — 5 minutes of total quiet before the first probe. Given your opencode tests, longer (10)? Configurable either way.
3. **Mass session termination** — still recommended AGAINST (rev-1 reasoning stands, and your `/new` evidence now shows session-level surgery isn't even the right lever — the *process* is).
4. **Notifications** — direct DM from the script (recommended), or via a Hermes sentinel too?
5. **One evidence question, Master:** in your opencode tests, after reopening the CLI — did you resume the *same* conversation (full context restored) and it worked? If yes, context size wasn't the blocker, which strengthens the connection/process theory over the token-budget theory. Level 0's data will settle it either way.
6. **Cron pause during silence** — may the watchdog pause household cron jobs itself (I'd verify the CLI path first), or does that stay a steward's manual act on notification?

---

## 5. Sources

- **Operator empirical tests (opencode, 2026-10-14) — controlling evidence for rev 2:** retry-delay insufficiency, `/new` insufficiency, full-restart as the only cure.
- Live source, v0.21.6 checkout: `agent/turn_failure_copy.py` (failure taxonomy, 429→`rate_limit` mapping), `agent/retry_utils.py` + `chat_completion_helpers.py` (Retry-After honoring, 600s cap), `gateway/run_turn.py` (session auto-reset, `is_fresh_reset`), `gateway/run_shutdown.py` (stuck-loop auto-suspend), `gateway/hooks.py`, systemd service units.
- Logs: the 10-14 12:29–12:44 outage timeline (all three profiles); the single historical 429 (10-08 23:34); `in=/out=` token accounting lines (Level 0's data source).
- Docs: sessions, event hooks, CLI (`hermes sessions`, `hermes cron`).
- Marked inferences: probe-renews-cooldown (why probes are sparse); the TPM-bucket hypothesis (Level 0 exists to *test* it, not assume it).
