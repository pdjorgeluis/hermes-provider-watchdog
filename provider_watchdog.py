#!/usr/bin/env python3
"""Provider-failure watchdog for a Hermes Agent household.

A boring log-grepper, run every 2 minutes by hermes-watchdog.timer:
  1. burn    — family-wide requests/min + tokens/min over the last 10 min (THE RULING)
  2. detect  — provider-failure signatures in each profile's logs (5-min window)
  3. probe   — one 1-token chat completion via a fresh curl process
  4. decide  — probe OK: restart affected gateways (DRY_RUN=1 blocks every restart);
               probe FAIL: total silence, sparse probes, restart-all after 2 consecutive OKs
  5. notify  — direct Telegram Bot API to the operator; every notification carries the burn line

Per-install configuration (environment, set in the systemd unit file — see
INSTALL.md): WATCHDOG_PROFILES (comma-separated profile names),
WATCHDOG_CHAT_ID (operator Telegram chat id), WATCHDOG_ENV_FILE, WATCHDOG_STATE_DIR.

DRY_RUN=1 (default): detect + probe + notify only. No systemctl restart ever executes.
Secrets (provider API key, Telegram bot token) are read from the env file and NEVER
appear in argv, logs, state files, or stdout: they ride only inside transient curl
--config files (0600, deleted immediately after use, outside the state dir).

Subcommands via WATCHDOG_COMMAND env: burn | detect | probe | (unset: full cycle).
Hermetic suite: tests/test_provider_watchdog.py (zero network).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ---------------- tuning (seconds unless noted) ----------------
BURN_WINDOW_S = 600        # THE RULING: last 10 minutes, family-wide
DETECT_WINDOW_S = 300      # failure signatures inside the last 5 minutes count
PERSIST_LOOKBACK_S = 900   # how far back "persisting" evidence may lie (15 min)
SLACK_S = 2                # tolerate drift between log timestamps and now
RPM_DIVISOR = 10           # THE RULING: per-min = window total / 10, one decimal
SILENCE_QUIET_S = 300      # guaranteed quiet before the first post-silence probe
SPARSE_PROBE_S = 300        # min spacing between sparse probes during silence
RESTART_COOLDOWN_S = 1800   # one restart round per profile per 30 minutes
HOUR_S = 3600
ROUND_CAP_PER_HOUR = 2      # more than 2 restart rounds/hour -> stop acting, notify
PROBE_TIMEOUT_S = 15
NOTIFY_DEDUP_S = 300        # suppress a repeated notification key within 5 minutes
NOTIFY_MIN_INTERVAL_S = 600  # hard floor between restart-intent notifications per class
# defect D: the classes whose frequency is bounded by the hard floor. Stable
# class names — never derived from the affected profile set, which rotates.
RESTART_INTENT_CLASSES = ("would_restart", "pre_restart", "post_restart",
                          "cooldown", "manual_attention")
DEFAULT_PROFILES = ("default",)  # neutral; per-install list comes from WATCHDOG_PROFILES
API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_PROBE_MODEL = "stepfun-ai/step-5-preview"  # last-resort constant, source logged

# ---------------- failure signature tiers (defect C) ----------------
# TERMINAL: lines that prove the provider gave up on that call. These ALONE
# may trigger detection. SOFT: transient/retryable blips — Hermes retries
# them up to 3 times and most recover — recorded as operator evidence
# (detection.json per_profile.<p>.soft) but never able to trigger.

TERMINAL_SIGNATURES = (
    "failed after 3 retries",
    "API failed after 3 retries",
    "DEGRADED function cannot be invoked",
)

SOFT_SIGNATURES = (
    "Streaming failed before delivery",
    "API call failed (attempt",
    "Clean EOF, no finish_reason",
    "API failed after",                      # non-"3 retries" prefix; the terminal
                                            # "API failed after 3 retries" still
                                            # matches TERMINAL above
    "HTTP/1.1 429 Too Many Requests",
    "HTTP/1.1 404 Not Found",
    "turn_failure: failure_reason=",
)

# Kept as the union of both tiers so existing importers keep working: the
# burn/detect log-scans that want "any interesting line" still find them all.
FAILURE_SIGNATURES = TERMINAL_SIGNATURES + SOFT_SIGNATURES


# ---------------- paths & environment ----------------

def home_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def profiles() -> tuple:
    """Per-install profile list: WATCHDOG_PROFILES (comma-separated)."""
    raw = os.environ.get("WATCHDOG_PROFILES", "")
    if raw.strip():
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return DEFAULT_PROFILES


def profile_config_path(profile: str) -> Path:
    """Where a profile's config.yaml lives. The 'default' profile is the steward:
    its config sits at ~/.hermes/config.yaml, everyone else under profiles/<p>/."""
    base = home_dir() / ".hermes"
    if profile != "default":
        base = base / "profiles" / profile
    return base / "config.yaml"


def configured_model(profile: str) -> str:
    """Minimal dependency-free parse of a profile's model.default (defect A).

    Line scan, no yaml: the value of the first 'default:' line that follows a
    line whose stripped form is exactly 'model:', within the first 30 lines.
    The word 'default:' appears in unrelated blocks (stt:, tts:, ...) in this
    host's real config, so a whole-file regex is wrong. Returns '' when nothing
    resolves. Never raises: a malformed config is evidence, not a crash.
    """
    try:
        lines = profile_config_path(profile).read_text().splitlines()[:30]
    except OSError:
        return ""
    in_model_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == "model:":
            in_model_block = True
            continue
        if in_model_block and stripped.startswith("default:"):
            return stripped[len("default:"):].strip().strip("\"'")
    return ""


def probe_models() -> list:
    """Resolution order for the probe target(s), one entry per model to try.

    1. WATCHDOG_PROBE_MODEL env (explicit per-install override) — alone.
    2. model.default of the watched profiles, in WATCHDOG_PROFILES order,
       scanning the default profile first (the steward), then the rest.
    3. WATCHDOG_FALLBACK_MODEL env — probed AFTER the primary (defect B).
    4. Module constant DEFAULT_PROBE_MODEL, when nothing resolves.

    Returns [(model, source)] in probe order, deduplicated, never empty.
    """
    env_model = os.environ.get("WATCHDOG_PROBE_MODEL", "").strip()
    if env_model:
        return [(env_model, "env")]
    ordered = ("default",) + tuple(p for p in profiles() if p != "default")
    for prof in ordered:
        model = configured_model(prof)
        if model:
            resolved = [(model, f"config({prof})")]
            fallback = os.environ.get("WATCHDOG_FALLBACK_MODEL", "").strip()
            if fallback and fallback != model:
                resolved.append((fallback, "env"))
            return resolved
    fallback = os.environ.get("WATCHDOG_FALLBACK_MODEL", "").strip()
    if fallback:
        return [(fallback, "env")]
    return [(DEFAULT_PROBE_MODEL, "constant")]


def household_default_model() -> str:
    """The model the household runs when all is well (defects E/F's yardstick).

    Same resolution as probe_models() minus the fallback/env-probe escape
    hatches: the configured model, else the module constant. Never ''.
    """
    ordered = ("default",) + tuple(p for p in profiles() if p != "default")
    for prof in ordered:
        model = configured_model(prof)
        if model:
            return model
    return DEFAULT_PROBE_MODEL


def chat_id() -> str:
    """Operator's Telegram chat id: WATCHDOG_CHAT_ID (per-install config)."""
    return os.environ.get("WATCHDOG_CHAT_ID", "").strip()


def state_dir() -> Path:
    d = os.environ.get("WATCHDOG_STATE_DIR")
    return Path(d) if d else home_dir() / "household" / "watchdog" / "state"


def env_file() -> Path:
    f = os.environ.get("WATCHDOG_ENV_FILE")
    return Path(f) if f else home_dir() / ".hermes" / ".env"


def log_paths(profile: str):
    base = home_dir() / ".hermes"
    if profile != "default":
        base = base / "profiles" / profile
    return base / "logs" / "agent.log", base / "logs" / "errors.log"


def service_name(profile: str) -> str:
    """Systemd unit name for a profile's gateway.

    Multiplex mode (post 2026-10-18 migration): one gateway unit
    (hermes-gateway.service) serves ALL profiles — Isis, Sindri, and the
    steward default. There are no per-profile gateway units anymore; the
    standalone hermes-gateway-isis.service and hermes-gateway-sindri.service
    were folded into the multiplexed gateway. Return the single unit for
    every profile so restart_round() targets the unit that actually exists.
    """
    return "hermes-gateway.service"


def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "1") != "0"


def read_env_secrets() -> dict:
    """KEY=VALUE pairs from the env file. Values are never printed or logged."""
    vals = {}
    try:
        for raw in env_file().read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


# ---------------- state helpers ----------------

def state_path(name: str) -> Path:
    return state_dir() / name


def read_json(name: str, default):
    try:
        return json.loads(state_path(name).read_text())
    except (OSError, ValueError):
        return default


def write_json(name: str, obj) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    tmp = state_dir() / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, state_dir() / name)


def log(msg: str) -> None:
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " [watchdog] " + msg
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with (state_dir() / "watchdog.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line)


def scrub(text: str, secrets: list) -> str:
    """Replace every secret value with *** in anything we persist or print."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def run_curl(argv: list) -> subprocess.CompletedProcess:
    """Invoke the real curl binary (not a mock, not a shell string)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 10)


def probe_one(secrets: dict, model: str) -> dict:
    """One 1-token chat completion for ONE model, from a fresh curl process.

    The API key never touches argv, logs, or state: it is written to a
    transient curl --config file (0600) in the system temp dir, used once,
    and deleted immediately. The key name (never its value) is logged.
    """
    key = secrets.get("NVIDIA_API_KEY", "")
    tmp = tempfile.NamedTemporaryFile(
        "w", prefix="wd-probe-", suffix=".cfg", delete=False)
    try:
        os.chmod(tmp.name, 0o600)
        # ONLY the secret rides in the config file; URL, timeout, and payload
        # are on argv (not secret, and the suite asserts they are visible).
        tmp.write(f'header = "Authorization: Bearer {key}"\n')
        tmp.close()
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        })
        argv = ["curl", "--max-time", str(PROBE_TIMEOUT_S), "-s", "-S",
                "-X", "POST", API_URL,
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "--config", tmp.name,
                "-d", payload]
        try:
            proc = run_curl(argv)
            out, err, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "curl watchdog timeout", 124
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    body = scrub(out, [key])
    err = scrub(err, [key])
    http_status = "unknown"
    m = re.search(r"HTTP/\d\.\d (\d{3})", err) or re.search(r"\"status\":\s*(\d{3})", body)
    if rc == 28:
        http_status = "timeout"
    elif m:
        http_status = m.group(1)
    ok = rc == 0 and "choices" in body
    if ok and http_status == "unknown":
        # a clean completion is necessarily 2xx; curl -sS prints no status line
        # on success, so the operator's evidence reads 200 (spec §1 defect B).
        http_status = "200"
    return {"model": model, "ok": ok, "http_status": http_status,
            "exit_code": rc, "body_chars": len(body),
            "body": body, "err": err}


def probe(secrets: dict) -> dict:
    """Multi-model probe (defect A + B): every resolved model gets 1 request.

    Resolution order is probe_models()'s: env override, then the household's
    configured model(s), then the fallback, then the module constant — with
    the source of the primary logged. ok is True if ANY model succeeded, so a
    model-scoped wall (primary 429, fallback 200) is still a usable provider
    and never enters silence mode. per_model lists every model tried with its
    status; res['model'] names the model that answered ('' when none did),
    which the decision path uses to detect rotation-vs-restart (defects E/F).
    """
    targets = probe_models()
    key = secrets.get("NVIDIA_API_KEY", "")
    primary, primary_source = targets[0]
    per_model = []
    first_body = ""
    last_rc = 0
    for model, _source in targets:
        r = probe_one(secrets, model)
        per_model.append({k: r[k] for k in
                          ("model", "ok", "http_status", "exit_code")})
        if r["body"]:
            first_body = first_body or r["body"]
        last_rc = r["exit_code"]
        if r["ok"]:
            break  # a usable model answered — no need to probe further
    ok = any(m["ok"] for m in per_model)
    answered = next((m["model"] for m in per_model if m["ok"]), "")
    body = first_body
    http_status = next((m["http_status"] for m in per_model if m["ok"]),
                       per_model[-1]["http_status"] if per_model else "unknown")
    if not ok and http_status in ("429", "404", "500", "502", "503", "504"):
        # capture interesting failure bodies verbatim for the operator's evidence
        try:
            (state_dir() / "last_429_body.txt").write_text(body + "\n")
        except OSError:
            pass
    res = {"ts": time.time(), "ok": ok, "http_status": http_status,
           "exit_code": last_rc, "body_chars": len(body),
           "model": answered, "models": [m for m, _s in targets],
           "per_model": per_model}
    write_json("probe_result.json", res)
    try:
        # scrubbed body kept for inspection; the key never appears here
        (state_dir() / "probe_last_body.txt").write_text(body + "\n")
    except OSError:
        pass
    summary = " ".join(f"{m['model']}={'ok' if m['ok'] else 'fail'}({m['http_status']})"
                       for m in per_model)
    log(f"probe: models[{primary_source}]={primary} -> {summary}")
    if ok:
        log(f"probe: ok=True model={answered} http_status={http_status}")
    return res


# ---------------- log parsing ----------------

def parse_line_ts(line: str):
    """'2026-10-15 21:30:00,123 WARNING ...' -> epoch seconds, else None."""
    try:
        return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        return None


def lines_in_window(paths, cutoff, signatures=None):
    """Lines from the given logs newer than cutoff, optional substring filter.
    An identical line appearing in both agent.log and errors.log counts once."""
    seen = set()
    out = []
    for p in paths:
        try:
            text = p.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line in seen:
                continue
            t = parse_line_ts(line)
            if t is None or t < cutoff:
                continue
            if signatures is not None and not any(s in line for s in signatures):
                continue
            seen.add(line)
            out.append((t, line))
    return out


# ---------------- burn (THE RULING) ----------------
# One computation feeds BOTH burn_snapshot.json and every notification text.

API_CALL_RE = re.compile(r"API call #\d+:.*?\bin=(\d+) out=(\d+)")


def compute_burn() -> dict:
    cutoff = time.time() - BURN_WINDOW_S - SLACK_S
    requests = in_tok = out_tok = 0
    for prof in profiles():
        for _t, line in lines_in_window(log_paths(prof), cutoff, ("API call #",)):
            m = API_CALL_RE.search(line)
            if not m:
                continue
            requests += 1
            in_tok += int(m.group(1))
            out_tok += int(m.group(2))
    burn = {
        "ts": time.time(),
        "window_s": BURN_WINDOW_S,
        "requests": requests,
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "requests_per_min": round(requests / RPM_DIVISOR, 1),
        "tokens_per_min": round((in_tok + out_tok) / RPM_DIVISOR, 1),
    }
    write_json("burn_snapshot.json", burn)
    return burn


def burn_line(b: dict) -> str:
    return (f"burn: {b['requests_per_min']:.1f} req/min, "
            f"{b['tokens_per_min']:.1f} tok/min (family, last {BURN_WINDOW_S // 60} min)")


# ---------------- detection ----------------
# Suite semantics (test_provider_watchdog.py, TestDetection):
#   - blip      = 1 signature line inside the 5-min window -> NO trigger
#   - sustained = >=2 signature lines within the 5-min window in one profile,
#                 OR >=2 lines in one profile with at least one recent (within
#                 the 5-min window) and the rest as older "persisting" evidence
#                 (15-min lookback): failure that has lasted >= 5 minutes.
#   - multi-profile = >=2 profiles with failure evidence in the 5-min window
# Reason priority: multi-profile > sustained > blip > none.

def is_failure_line(line: str) -> bool:
    return any(s in line for s in FAILURE_SIGNATURES)


def detect() -> dict:
    now = time.time()
    cutoff_detect = now - DETECT_WINDOW_S - SLACK_S
    cutoff_persist = now - PERSIST_LOOKBACK_S - SLACK_S
    evidence = {}   # prof -> (recent_terminal, persisting_terminal, soft_total)
    for prof in profiles():
        paths = list(log_paths(prof))
        # defect C: only TERMINAL signatures may trigger. SOFT signatures are
        # recorded (per_profile.<p>.soft) as operator evidence but never count
        # toward the decision — Hermes retries them and most recover on try 1-2.
        recent = lines_in_window(paths, cutoff_detect, TERMINAL_SIGNATURES)
        lookback = lines_in_window(paths, cutoff_persist, TERMINAL_SIGNATURES)
        soft = lines_in_window(paths, cutoff_persist, SOFT_SIGNATURES)
        persisting = sum(1 for t, _l in lookback if t < cutoff_detect)
        evidence[prof] = (len(recent), persisting, len(soft))
    with_evidence = [p for p in profiles() if evidence[p][0] >= 1]
    if len(with_evidence) >= 2:
        # (b) >=2 profiles failing simultaneously — trigger even if each is young
        reason, affected = "multi-profile", with_evidence
    elif len(with_evidence) == 1:
        prof = with_evidence[0]
        recent, persisting = evidence[prof][0], evidence[prof][1]
        if recent >= 2 or persisting >= 1:
            # (a) failures persisting >= 5 minutes: a burst now, or older
            # evidence still inside the 15-min lookback
            reason, affected = "sustained", [prof]
        else:
            reason, affected = "blip", []
    else:
        reason, affected = "none", []
    det = {
        "ts": now,
        "triggered": bool(affected),
        "reason": reason,
        "affected": affected,
        "per_profile": {p: {"recent": evidence[p][0],
                            "persisting": evidence[p][1],
                            "soft": evidence[p][2]}
                        for p in profiles()},
    }
    write_json("detection.json", det)
    return det


# ---------------- notification ----------------

def notify(secrets: dict, text: str, key: str, cls: str = "") -> bool:
    """Direct Telegram Bot API DM to the operator. No gateway dependency.

    Dedup: one notification per key per NOTIFY_DEDUP_S window.
    Secret hygiene: the bot token rides ONLY inside a transient 0600 curl
    config file (its url = https://api.telegram.org/bot<token>/sendMessage).
    The config appears AFTER the argv url, so curl uses the config's url while
    argv (and the mock's log) never sees the token.

    cls (defect D): the notification's stable class name. Restart-intent
    classes carry the hard 10-min floor; the affected profile set rides in
    the message body, never in the key.
    """
    now = time.time()
    hist = read_json("notify_history.json", {})
    # defect D: the class floor is checked FIRST and is always logged. A
    # rotating affected-set must never resurrect a restart-intent notice:
    # the class entry (class:<name>) is independent of the per-key entry.
    if cls and cls in RESTART_INTENT_CLASSES:
        last = hist.get(f"class:{cls}", 0)
        if now - last < NOTIFY_MIN_INTERVAL_S:
            log(f"notify[{cls}]: suppressed by 10-min class floor "
                f"(last {int((now - last) // 60)}m ago)")
            return False
    if hist.get(key) and now - hist[key] < NOTIFY_DEDUP_S:
        return False
    if not chat_id():
        log("notify: WATCHDOG_CHAT_ID not configured — notification skipped (key name only)")
        return False
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log("notify: TELEGRAM_BOT_TOKEN missing in env file (key name only) — skipped")
        return False
    payload = json.dumps({"chat_id": chat_id(),
                          "text": text})
    tmp = tempfile.NamedTemporaryFile("w", prefix="wd-notify-", suffix=".cfg", delete=False)
    try:
        os.chmod(tmp.name, 0o600)
        tmp.write(f'url = "https://api.telegram.org/bot{token}/sendMessage"\n')
        tmp.close()
        argv = ["curl", "--max-time", "15", "-s", "-S", "-X", "POST",
                "https://api.telegram.org/sendMessage",
                "-H", "Content-Type: application/json",
                "--config", tmp.name,
                "-d", payload]
        try:
            proc = run_curl(argv)
            ok = proc.returncode == 0 and '"ok"' in scrub(proc.stdout, [token])
        except subprocess.TimeoutExpired:
            ok = False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    hist[key] = now
    if cls:
        hist[f"class:{cls}"] = now
    write_json("notify_history.json", hist)
    log(f"notify[{key}]: attempted, delivered={ok}")
    return ok


# ---------------- decision tree ----------------

def restart_round(secrets: dict, burn: dict, target_profiles: list, recovered=False) -> bool:
    """One restart round over the target profiles, with cooldown + hourly cap.

    DRY_RUN=1: no systemctl ever executes; a single 'would restart' notification.
    Live: notify BEFORE and AFTER the action. Stamps/rounds recorded only live,
    so the dry-run week never burns the real cooldowns.
    """
    now = time.time()
    targets = sorted(target_profiles)
    tkey = "_".join(targets)
    services = [service_name(p) for p in targets]
    stamps = read_json("restart_stamps.json", {})
    rounds = [t for t in read_json("rounds.json", []) if t > now - HOUR_S]
    blocked = [p for p in targets if now - stamps.get(p, 0) < RESTART_COOLDOWN_S]
    if blocked:
        log(f"restart round for {tkey} blocked by cooldown: {blocked}")
        # defect D: the KEY stays stable (tkey rides in the body only); the
        # class label carries the 10-min floor against a rotating profile set.
        notify(secrets, ("DRY-RUN — " if dry_run() else "") +
               f"watchdog: restart round for {tkey} blocked by 30-min cooldown (profiles: {', '.join(blocked)}).\n{burn_line(burn)}",
               "cooldown", cls="cooldown")
        return False
    if len(rounds) >= ROUND_CAP_PER_HOUR:
        log("restart round blocked by hourly cap — manual attention needed")
        notify(secrets, ("DRY-RUN — " if dry_run() else "") +
               f"watchdog: {len(rounds)} restart rounds already this hour — cap reached, MANUAL ATTENTION NEEDED.\n{burn_line(burn)}",
               "manual_attention", cls="manual_attention")
        return False
    if dry_run():
        log(f"DRY-RUN: would restart {services}")
        why = "recovery: 2 consecutive OK probes" if recovered else "probe OK, agents poisoned"
        notify(secrets, f"DRY-RUN — watchdog: would restart {', '.join(services)} ({why}).\n{burn_line(burn)}",
               "would_restart", cls="would_restart")
        return True
    why = "recovery: 2 consecutive OK probes" if recovered else "probe OK, agents poisoned"
    notify(secrets, f"watchdog: restarting {', '.join(services)} NOW ({why}).\n{burn_line(burn)}",
           "pre_restart", cls="pre_restart")
    # Multiplex mode: one gateway unit serves all profiles. Deduplicate
    # services so a multi-profile trigger doesn't restart the same unit
    # repeatedly; stamp every affected profile's cooldown regardless.
    service_to_profiles: dict[str, list[str]] = {}
    for p in targets:
        service_to_profiles.setdefault(service_name(p), []).append(p)
    for svc, profs in service_to_profiles.items():
        subprocess.run(["systemctl", "--user", "restart", svc],
                      capture_output=True, timeout=60)
        for p in profs:
            stamps[p] = now
    rounds.append(now)
    write_json("restart_stamps.json", stamps)
    write_json("rounds.json", rounds)
    if recovered:
        verb = (f"watchdog: RECOVERED — timeline: silence enforced, probes sparsed, "
                f"2 consecutive OK probes, then one restart round: {', '.join(services)}.")
    else:
        verb = f"watchdog: restart round complete: {', '.join(services)}."
    notify(secrets, f"{verb}\n{burn_line(burn)}",
           "post_restart", cls="post_restart")
    log(f"restart round done: {services}")
    return True


def recovery_from_non_default(res: dict) -> str:
    """The model that answered, when it is NOT the household's configured
    default (defects E/F). '' when the default model answered, when nothing
    resolved, or when no model answered at all."""
    answered = res.get("model", "")
    default = household_default_model()
    if answered and default and answered != default:
        return answered
    return ""


def decide_restart(secrets: dict, burn: dict, det: dict) -> None:
    """Step 2a: probe OK, agents failing -> fresh processes for the affected.

    Defect E guard: "probe OK" can mean "some OTHER model is fine". If the
    only model that answered is not the household default, restarting gives
    the poisoned agents a fresh process that still talks to the walled model —
    the operator needs rotation, not a restart.
    """
    res = read_json("probe_result.json", {})
    answered = recovery_from_non_default(res)
    if answered:
        log(f"restart skipped: probe OK on {answered} but household default "
            f"{household_default_model()} still walled — rotation needed, not restart")
        notify(secrets, ("DRY-RUN — " if dry_run() else "") +
               f"watchdog: probe OK on {answered} but household default "
               f"{household_default_model()} still walled — MODEL ROTATION NEEDED, no restart performed.\n{burn_line(burn)}",
               "model_rotation_needed", cls="model_rotation_needed")
        return
    targets = list(profiles()) if len(det["affected"]) >= 2 else list(det["affected"])
    restart_round(secrets, burn, targets)


def enforce_silence(secrets: dict, burn: dict, det: dict) -> None:
    """Step 2b: probe FAILS -> total silence, sparse probes, notify the operator."""
    now = time.time()
    silence = read_json("silence.json", {})
    if silence.get("enforced"):
        return  # already silent; transition-based notification only
    if silence.get("last_enforce_ts", 0) and now - silence["last_enforce_ts"] < HOUR_S:
        log("silence: 1-cycle-per-hour rail — would re-enforce too soon, skipped")
        return
    silence = {"enforced": True, "since": now, "last_probe": now,
               "last_probe_ok": False, "ok_count": 0, "last_enforce_ts": now,
               "reason": det.get("reason"), "affected": det.get("affected")}
    write_json("silence.json", silence)
    text = (f"watchdog: probe FAILED (HTTP {det.get('reason')} state in "
            f"{', '.join(det['affected'])}) — total silence enforced; first probe after "
            f"{SILENCE_QUIET_S // 60} min quiet, then every {SPARSE_PROBE_S // 60} min; "
            f"recovery = 2 consecutive OK probes, then one restart round.\n{burn_line(burn)}")
    if dry_run():
        text = "DRY-RUN — " + text
    notify(secrets, text, "silence_enforced")
    if os.environ.get("WATCHDOG_PAUSE_CRON", "0") == "1":
        try:
            subprocess.run(["hermes", "cron", "pause"], capture_output=True, timeout=30)
            log("silence: household cron pause requested (WATCHDOG_PAUSE_CRON=1)")
        except (OSError, subprocess.TimeoutExpired):
            log("silence: hermes cron pause failed to execute")


def handle_silence_phase(secrets: dict, burn: dict) -> None:
    """Recovery watch while silence is enforced. Probe-gated, never clock-gated:
    first probe after the quiet window; if the last probe failed, sparse spacing;
    once a probe succeeds, confirmation probes are immediate. 2 consecutive OKs
    -> one restart round over all three gateways, then silence lifts."""
    now = time.time()
    silence = read_json("silence.json", {})
    if not silence.get("enforced"):
        return
    if now - silence.get("since", 0) < SILENCE_QUIET_S:
        log("silence: inside quiet window — no probe, staying silent")
        return
    if not silence.get("last_probe_ok") and now - silence.get("last_probe", 0) < SPARSE_PROBE_S:
        log("silence: sparse probe spacing — no probe this run")
        return
    res = probe(secrets)
    silence["last_probe"] = now
    silence["last_probe_ok"] = res["ok"]
    if res["ok"]:
        silence["ok_count"] = silence.get("ok_count", 0) + 1
        answered = recovery_from_non_default(res)
        if answered:
            # defect F: recovery on a model the household does not run is not
            # a recovery for the gateways — restarting them buys nothing.
            silence["ok_count"] = 0
            write_json("silence.json", silence)
            log(f"silence: recovery probe OK on {answered} but household default "
                f"{household_default_model()} still walled — restart round skipped, rotation needed")
            notify(secrets, ("DRY-RUN — " if dry_run() else "") +
                   f"watchdog: recovered on {answered}; household default "
                   f"{household_default_model()} still walled — MODEL ROTATION NEEDED, restart skipped.\n{burn_line(burn)}",
                   "model_rotation_needed", cls="model_rotation_needed")
            return
        if silence["ok_count"] >= 2:
            silence["enforced"] = False
            silence["ok_count"] = 0
            silence["recovered_ts"] = now
            write_json("silence.json", silence)
            log("silence: 2 consecutive OK probes — recovery restart round")
            restart_round(secrets, burn, list(profiles()), recovered=True)
            return
        write_json("silence.json", silence)
        notify(secrets, f"watchdog: silence probe OK ({silence['ok_count']}/2) — awaiting one more.\n{burn_line(burn)}",
               f"probe_ok_{silence['ok_count']}")
    else:
        silence["ok_count"] = 0
        write_json("silence.json", silence)
        log("silence: probe still failing — staying silent")


# ---------------- main ----------------

def main() -> int:
    cmd = os.environ.get("WATCHDOG_COMMAND", "full")
    if cmd == "burn":
        b = compute_burn()
        log(f"{burn_line(b)} — requests={b['requests']} "
            f"in={b['in_tokens']} out={b['out_tokens']}")
        return 0
    if cmd == "detect":
        det = detect()
        log(f"detect: triggered={det['triggered']} reason={det['reason']} "
            f"affected={det['affected']} per_profile={det['per_profile']}")
        return 0
    if cmd == "probe":
        secrets = read_env_secrets()
        probe(secrets)
        return 0
    # full cycle: what the systemd timer runs every 2 minutes
    secrets = read_env_secrets()
    burn = compute_burn()
    silence = read_json("silence.json", {})
    if silence.get("enforced"):
        handle_silence_phase(secrets, burn)
        return 0
    det = detect()
    if not det["triggered"]:
        log(f"healthy: reason={det['reason']} — no action")
        return 0
    log(f"triggered: reason={det['reason']} affected={det['affected']} — probing")
    res = probe(secrets)
    if res["ok"]:
        decide_restart(secrets, burn, det)
    else:
        enforce_silence(secrets, burn, det)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("UNEXPECTED ERROR:\n" + traceback.format_exc())
        sys.exit(1)
