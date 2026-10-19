# SPEC — watchdog correctness fix (Master-approved, 2026-10-18)

**Authoritative spec for kanban card `t_watchdog-fix`. Signature: Crimvael (steward). Status: ratified by Master 2026-10-18 ("have Sindri execute the watchdog fix using this model").**

Target file: `~/household/watchdog/provider_watchdog.py` (572 lines) and its suite `~/household/watchdog/tests/test_provider_watchdog.py` (35 KB).

---

## 1. The defects being fixed (all diagnosed 2026-10-18, evidence in `INCIDENT-2026-10-18-glm-5.3-rate-wall.md` §4 and live diagnosis)

### Defect A — the probe is hardcoded to a single model

`provider_watchdog.py` L166–227, `probe()`: the payload is
`{"model": "z-ai/glm-5.3", ...}` (L183). During the 10-16/10-18 wall,
glm-5.3 was the walled model while the household had already rotated to
`stepfun-ai/step-5-preview`. Result: every probe failed with 429 while
the house was serving traffic fine.

**Required behavior:** the probe must target **the model the household
actually runs**. Resolve it in this order at each probe:

1. `WATCHDOG_PROBE_MODEL` env var (explicit per-install override, set in the systemd unit) — if non-empty, use it.
2. `model.default` of each watched profile, read from `~/.hermes/config.yaml` / `~/.hermes/profiles/<p>/config.yaml`. Use the **first** value found, scanning profiles in `WATCHDOG_PROFILES` order, then the default profile (even if `WATCHDOG_PROFILES` names others — the default profile is the steward).
3. `WATCHDOG_FALLBACK_MODEL` env var — if set, probe this as well (see Defect B).
4. Only if nothing resolves from any source above, fall back to a module
   constant `DEFAULT_PROBE_MODEL = "stepfun-ai/step-5-preview"` (current
   household default as of this spec) and log which source was used.

Config parsing must be minimal and dependency-free — no yaml library
(not guaranteed installed). Parse the `model:` block with a line scan:
find the line `default:` that follows a line whose stripped form is
`model:`, within the first 30 lines of the file. Stop at the first
`default:` found under `model:`. Do NOT use a regex over the whole file
(the word `default:` appears in unrelated blocks — verified on this host's
real config).

### Defect B — recovery/recovery detection only knows one model

Recovery should be declared when a **usable** model answers. Implement
multi-model probing:

- Probe the primary model (Defect A resolution) and, if
  `WATCHDOG_FALLBACK_MODEL` is set, probe the fallback.
- `probe()` returns a dict with an added key `per_model`: a list of
  `{model, ok, http_status, exit_code}` entries, one per model probed.
  Keep the existing top-level keys (`ok`, `http_status`, `exit_code`,
  `body_chars`, `ts`) — **`ok` must be True if ANY probed model
  succeeded**, so all existing callers and tests keep working.
- Log one line naming every model and its result, e.g.
  `probe: stepfun-ai/step-5-preview=ok(200) glm-5.3=fail(429)`.
- On success the log line names the model that answered:
  `probe: ok=True model=stepfun-ai/step-5-preview http_status=200`.

A **model-scoped wall** (primary 429, fallback 200) must NOT enter silence
mode or fire any restart. The watchdog's job is "is the provider usable",
not "is one specific model usable".

### Defect C — detection counts raw failure-signature lines instead of terminal failures

`FAILURE_SIGNATURES` (L50–58) includes `"API call failed (attempt"` and
`"HTTP/1.1 429 Too Many Requests"`. Hermes retries a 429 up to 3 times and
the overwhelming majority recover on attempt 1–2 (in a 20-minute window on
2026-10-18, 5 of 6 attempt-1 failures recovered; only one reached attempt
3). Counting raw lines therefore puts the watchdog in a permanent trigger
state and it never leaves.

**Required behavior — two signature tiers:**

- `TERMINAL_SIGNATURES` — these alone may trigger:
  - `"failed after 3 retries"`
  - `"API failed after 3 retries"`
  - `"DEGRADED function cannot be invoked"` (provider outage, kills the run)
- `SOFT_SIGNATURES` — recorded in `detection.json` under
  `per_profile.<p>.soft` for the operator's evidence, but **never
  contribute to the trigger decision**:
  - `"Streaming failed before delivery"`
  - `"API call failed (attempt"`
  - `"Clean EOF, no finish_reason"`
  - `"API failed after"` (the non-"3 retries" prefix; the terminal line
    `"API failed after 3 retries"` still matches terminal)
  - `"HTTP/1.1 429 Too Many Requests"`
  - `"HTTP/1.1 404 Not Found"`
  - `"turn_failure: failure_reason="`

Keep the exported name `FAILURE_SIGNATURES` as the union of both tiers so
any existing test referencing it still imports. Existing semantics that
must survive: blip → no trigger; sustained (≥2 terminal lines in 5 min in
one profile, or ≥1 recent + ≥1 persisting in the 15-min lookback) →
trigger; ≥2 profiles with terminal evidence → multi-profile trigger.
`detection.json` keeps `per_profile` but each entry gains `soft` alongside
`recent`/`persisting`.

### Defect D — notification dedup is defeated by rotating keys

`notify()` dedups on a `key` string with a 300-second window, but
`restart_round()` builds the key from the affected profile set
(`would_restart_<tkey>`, `f"cooldown_{tkey}"`, `f"manual_attention_{tkey}"`).
During the incident the affected set rotated, each new key looked fresh,
and 75+ notices slipped past the 5-minute dedup in 30 minutes.

**Required behavior:**
- Notification **class** keys must be stable, not set-derived:
  `would_restart`, `cooldown`, `manual_attention`, `silence_enforced`,
  `probe_ok_<n>`, `pre_restart`, `post_restart`, `recovered`.
  The affected profiles move into the **message body** instead of the key.
- Add a **hard floor** between any two restart-intent notifications per
  restart class, independent of key rotation: minimum 10 minutes
  (`NOTIFY_MIN_INTERVAL_S = 600`, new module constant) between any two
  `would_restart` / `pre_restart` / `post_restart` / `cooldown` /
  `manual_attention` notifications. Record the last send timestamp per
  class in `notify_history.json` alongside the existing per-key entries
  (key format: `class:<class>` vs plain `<key>` — keep both; the class
  entry is the floor check, the per-key entry the legacy dedup).
- When a notification is suppressed by the class floor, log it: e.g.
  `notify[would_restart]: suppressed by 10-min class floor (last 3m ago)`.
  Silent suppression is forbidden — the operator must be able to see from
  the log that something wanted to fire.

### Defect E — "probe OK, agents poisoned" fires while the probe is only OK because it targets a healthy model

With Defect A/B fixed this class of false positive mostly disappears, but
the decision path needs one more guard: `decide_restart()` currently
restarts when a probe is OK and detection is triggered. With
multi-model probing, "OK" can mean "some other model is fine". Add to
`decide_restart()`: if the probe succeeded **only** on a model other than
the household's configured default, log
`restart skipped: probe OK on <model> but household default <model2> still walled — rotation needed, not restart`
and notify the operator with that text (dedup'd by the stable class key
`model_rotation_needed`). Do not call `restart_round()`.

### Defect F — recovery restart restarts gateways for no benefit

`handle_silence_phase()` (L489–523) declares recovery after 2 consecutive
OK probes and fires one restart round over all profiles. Keep this
behavior (Master ratified the design), but add the same guard as E: if
recovery was achieved on a non-default model, notify
`recovered on <model>; household default still walled — needs rotation`
and skip the restart round, logging the skip.

---

## 2. Constraints (all binding)

1. **DRY_RUN stays the default.** `DRY_RUN=1` must keep blocking every
   `systemctl` call. No code path may execute a restart when DRY_RUN=1.
   Master arms it himself. Verify this with a test that asserts no
   subprocess call with `systemctl` in argv occurs for any decision path.
2. **Secrets never in argv, logs, state files, or stdout.** The probe key
   and the Telegram bot token continue to ride only inside transient
   0600 curl `--config` files. Keep the existing scrub behaviour. No new
   secret exposure may be introduced.
3. **No new dependencies.** stdlib only (`json`, `os`, `re`,
   `subprocess`, `time`, `tempfile`, `datetime`, `pathlib`). The header
   docstring stays stdlib-only. Do not import yaml.
4. **Deterministic, hermetic test suite.** All tests run with zero
   network. `curl` is invoked through `run_curl`, which the existing suite
   monkeypatches — keep that seam. Tests must prove every new behavior
   below.
5. **Request budget: the card may spend at most TWO live requests** — one
   live probe (1 token) to prove multi-model probing works against the
   real endpoint, and one DM to Master announcing the dry-run test. No more.
6. **Rollback runbook required in the completion comment** per household
   law — the exact commands to restore the current file.
7. **Commit to `~/household` git** per the household-change-protocol, and
   **leave the timer/service stopped unless told otherwise.** Master
   stopped the watchdog at 13:41 on 2026-10-18 and has not re-armed it.
   Do NOT restart `hermes-watchdog.timer` in this card.

## 3. Deliverable

One updated `provider_watchdog.py` + updated
`tests/test_provider_watchdog.py`, committed to `~/household` (watchdog
directory), plus a completion comment on the card covering: what changed,
test results (real output), the arm commands, and anything ambiguous.

## 4. Acceptance criteria (each must be provable by real output)

1. `A1` Probe resolves the household's configured model from config, not
   a hardcoded string. Proof: a test with a synthetic config tree
   asserting the resolved model equals the config's `model.default`, and
   the log line naming the source (env / config / fallback constant).
2. `A2` Multi-model probe: primary walled (429) + fallback healthy (200)
   → `probe()["ok"] is True`, `per_model` lists both. Full-cycle
   simulation: this state does NOT enter silence and does NOT call
   restart.
3. `A3` Recovery on a non-default model does not fire a restart round;
   the log names the skip and a `model_rotation_needed` notification is
   attempted (dry-run: no systemctl).
4. `A4` Raw 429 / `API call failed (attempt` lines alone never trigger
   detection. Proof: synthetic log with 20 soft-signature lines and zero
   terminal lines → `triggered=False`, `per_profile.<p>.soft >= 20`.
5. `A5` `failed after 3 retries` lines DO trigger (sustained), exactly as
   before, including the persisting-evidence branch.
6. `A6` Notification class floor: 6 simulated `would_restart` events over
   8 minutes with rotating affected sets → exactly ONE notification
   delivered, five suppressed, five suppression lines in the log.
7. `A7` DRY_RUN: no systemctl across every decision path. Proof: a test
   asserting `run_curl`/subprocess never received `systemctl` in argv for
   a full cycle with sustained detection and an OK probe.
8. `A8` Existing suite still green: every pre-existing test in
   `test_provider_watchdog.py` passes unmodified (or is amended only where
   it asserted the old hardcoded-model / raw-signature behavior — each
   such amendment must be justified in the completion comment by citing
   the defect it encoded).
8. `A9` Suite runs clean: `cd ~/household/watchdog/tests && python3 run_tests.py`
   exits 0 with the full count printed.
9. `A10` Rollback runbook + git commit sha in the completion comment.

## 5. Grounds header

- **Sources used:** `~/household/watchdog/provider_watchdog.py` (read in
  full), `tests/test_provider_watchdog.py` (line numbers and fixture
  names verified), `~/household/watchdog/INCIDENT-2026-10-18-glm-5.3-rate-wall.md`
  §4, live diagnosis of the 11:03–11:06 watchdog spam loop on 2026-10-18,
  and the per-model-limit ruling recorded in the `request-budget` skill.
- **Evidence commissioned:** none — this is internal mechanics testable on
  our own machine (the tests are the evidence), per the dispatch-rite
  evidence rule.
- **Inferences marked:** the `model.default` line-scan parse (§1 Defect A)
  is the steward's judgment on a minimal stdlib-only parser; the exact
  constants (`NOTIFY_MIN_INTERVAL_S = 600`) are the steward's judgment and
  may be tuned upward if tests show the floor too tight, but never below
  600.
