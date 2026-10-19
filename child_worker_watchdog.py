#!/usr/bin/env python3
"""Child-worker watchdog for Hermes kanban tasks.

A boring log-and-process checker, run on a timer:
  1. scan   — list running kanban tasks with worker_pid
  2. check  — ps -p <pid>, last_heartbeat_at, worker log API-call pace
  3. detect — time-to-first-token (spawn → first API call) exceeds threshold
  4. notify — Telegram DM to the operator on silent spawn
  5. log    — everything to ~/household/watchdog/

Per-install configuration (environment, set in the systemd unit file):
  WATCHDOG_CHAT_ID    — operator Telegram chat id
  WATCHDOG_STATE_DIR  — state directory (default ~/household/watchdog/state)
  DRY_RUN=1           — detect + notify only; no systemctl (default)

Secrets (Telegram bot token) are read from ~/.hermes/.env and NEVER appear
in argv, logs, state files, or stdout: they ride only inside transient
curl --config files (0600, deleted immediately after use).

Hermetic suite: tests/test_child_worker_watchdog.py (zero network).
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
FIRST_TOKEN_THRESHOLD_S = 90      # OmO: time-to-first-token death condition
HEARTBEAT_STALE_S = 120           # heartbeats older than this = wedged
PROCESS_GONE_GRACE_S = 30         # pid vanished but run may still be settling
NOTIFY_DEDUP_S = 300              # suppress repeated notification within 5 min
NOTIFY_MIN_INTERVAL_S = 600       # hard floor per notification key
API_CALL_RE = re.compile(r"API call #\d+")
DEFAULT_CHAT_ID = ""
DEFAULT_PROFILES = ("default",)

# ---------------- paths & environment ----------------
def home_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))

def state_dir() -> Path:
    d = os.environ.get("WATCHDOG_STATE_DIR")
    return Path(d) if d else home_dir() / "household" / "watchdog" / "state"

def env_file() -> Path:
    f = os.environ.get("WATCHDOG_ENV_FILE")
    return Path(f) if f else home_dir() / ".hermes" / ".env"

def chat_id() -> str:
    return os.environ.get("WATCHDOG_CHAT_ID", "").strip()

def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "1") != "0"

def kanban_db() -> Path:
    return home_dir() / ".hermes" / "kanban.db"

def worker_log_dir() -> Path:
    return home_dir() / ".hermes" / "kanban" / "logs"

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
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " [child-watchdog] " + msg
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

# ---------------- kanban db access ----------------
def running_tasks() -> list:
    """Return list of task dicts with status='running' and worker_pid set."""
    import sqlite3
    db = kanban_db()
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, title, assignee, worker_pid, started_at,
                      max_runtime_seconds, reasoning_effort, model_override
               FROM tasks
               WHERE status='running' AND worker_pid IS NOT NULL
               ORDER BY started_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def latest_run(task_id: str) -> dict:
    """Return the most recent task_run row for a task, or {}."""
    import sqlite3
    db = kanban_db()
    if not db.exists():
        return {}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT id, status, worker_pid, last_heartbeat_at,
                      started_at, ended_at, outcome
               FROM task_runs
               WHERE task_id=?
               ORDER BY id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()

# ---------------- worker log scanning ----------------
def worker_log_path(task_id: str) -> Path:
    return worker_log_dir() / f"{task_id}.log"

def first_api_call_ts(log_path: Path, after_epoch: float):
    """Return (epoch_seconds, line) of the first API call # line at/after after_epoch,
    or (None, None) if none found."""
    try:
        text = log_path.read_text()
    except OSError:
        return None, None
    for line in text.splitlines():
        if not API_CALL_RE.search(line):
            continue
        t = parse_line_ts(line)
        if t is None:
            continue
        if t >= after_epoch - 5:  # 5s slack for clock drift
            return t, line
    return None, None

def parse_line_ts(line: str):
    """'2026-10-18 22:18:24 [watchdog] ...' -> epoch seconds, else None."""
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None

# ---------------- process checking ----------------
def process_alive(pid: int) -> bool:
    """True if pid exists and is a Hermes worker process."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # Verify it's actually our worker, not a reused pid
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid,etime,cmd"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "kanban" in out or "work kanban task" in out or "hermes_cli" in out

# ---------------- notification ----------------
def notify(secrets: dict, text: str, key: str, cls: str = "") -> bool:
    """Direct Telegram Bot API DM to the operator. No gateway dependency.

    Dedup: one notification per key per NOTIFY_DEDUP_S window.
    Secret hygiene: the bot token rides ONLY inside a transient 0600 curl
    config file. The config's url carries the token; argv never does.
    """
    now = time.time()
    hist = read_json("notify_history.json", {})
    if cls and cls in ("would_restart", "pre_restart", "post_restart", "cooldown", "manual_attention", "silence_enforced"):
        last = hist.get(f"class:{cls}", 0)
        if now - last < NOTIFY_MIN_INTERVAL_S:
            log(f"notify[{cls}]: suppressed by class floor (last {int((now - last)//60)}m ago)")
            return False
    if hist.get(key) and now - hist[key] < NOTIFY_DEDUP_S:
        return False
    if not chat_id():
        log("notify: WATCHDOG_CHAT_ID not configured — notification skipped (key name only)")
        return False
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log("notify: TELEGRAM_BOT_TOKEN missing in env file — notification skipped")
        return False
    payload = json.dumps({"chat_id": chat_id(), "text": text})
    tmp = tempfile.NamedTemporaryFile("w", prefix="wdc-notify-", suffix=".cfg", delete=False)
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
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=25)
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

# ---------------- main check ----------------
def check_workers(secrets: dict) -> dict:
    """Scan running kanban tasks for silent spawns. Returns a results dict."""
    tasks = running_tasks()
    results = {"checked": 0, "silent": [], "healthy": [], "no_pid": []}
    now = time.time()

    for task in tasks:
        results["checked"] += 1
        task_id = task["id"]
        pid = task.get("worker_pid")
        started = task.get("started_at") or 0
        run = latest_run(task_id)
        last_heartbeat = run.get("last_heartbeat_at") or 0
        run_status = run.get("status", "unknown")

        # No worker pid assigned yet — not our concern yet
        if not pid:
            results["no_pid"].append(task_id)
            log(f"child-watchdog: {task_id} running but no worker_pid yet — skipping")
            continue

        # Process check
        alive = process_alive(pid)
        pid_gone_s = now - last_heartbeat if last_heartbeat else None

        # Worker log: first API call after spawn
        log_path = worker_log_path(task_id)
        first_call_ts, first_call_line = first_api_call_ts(log_path, started)
        ttf_s = (first_call_ts - started) if (first_call_ts and started) else None

        # Determine silence status
        is_silent = False
        reasons = []

        if not alive:
            is_silent = True
            reasons.append(f"pid {pid} not alive")
        elif ttf_s is not None and ttf_s > FIRST_TOKEN_THRESHOLD_S:
            is_silent = True
            reasons.append(f"time-to-first-token {ttf_s:.0f}s > {FIRST_TOKEN_THRESHOLD_S}s")
        elif ttf_s is None and last_heartbeat and (now - last_heartbeat) > HEARTBEAT_STALE_S:
            is_silent = True
            reasons.append(f"no API call + heartbeat stale {(now - last_heartbeat):.0f}s")
        elif ttf_s is None and not last_heartbeat and started and (now - started) > FIRST_TOKEN_THRESHOLD_S:
            is_silent = True
            reasons.append(f"no API call + no heartbeat + spawned {(now - started):.0f}s ago")

        if is_silent:
            results["silent"].append({
                "task_id": task_id,
                "title": task.get("title", ""),
                "assignee": task.get("assignee", ""),
                "pid": pid,
                "alive": alive,
                "ttf_s": ttf_s,
                "last_heartbeat": last_heartbeat,
                "reasons": reasons,
            })
            log(f"child-watchdog: SILENT {task_id} pid={pid} reasons={'; '.join(reasons)}")
        else:
            results["healthy"].append({
                "task_id": task_id,
                "pid": pid,
                "ttf_s": ttf_s,
                "last_heartbeat": last_heartbeat,
            })
            log(f"child-watchdog: healthy {task_id} pid={pid} ttf={ttf_s}")

    return results

def main() -> int:
    secrets = read_env_secrets()
    results = check_workers(secrets)

    if results["silent"]:
        for entry in results["silent"]:
            text = (
                f"child-watchdog: silent worker detected\n"
                f"  card: {entry['task_id']} ({entry['title']})\n"
                f"  assignee: {entry['assignee']}\n"
                f"  pid: {entry['pid']} alive={entry['alive']}\n"
                f"  reasons: {'; '.join(entry['reasons'])}\n"
                f"  action: check sentinel-watch before reclaim — do not hand-respawn"
            )
            notify(secrets, text, f"silent-worker-{entry['task_id']}", cls="silence_enforced")

    summary = (f"checked={results['checked']} silent={len(results['silent'])} "
               f"healthy={len(results['healthy'])} no_pid={len(results['no_pid'])}")
    log(f"child-watchdog: {summary}")

    write_json("child_watchdog_last_run.json", {
        "ts": time.time(),
        "checked": results["checked"],
        "silent": results["silent"],
        "healthy": results["healthy"],
    })
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