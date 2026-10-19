"""Hermetic test suite for the child-worker watchdog.

Run:  python3 tests/run_tests.py   (from ~/household/watchdog)

Zero network. Uses monkeypatched run_curl seam, fake kanban.db, fake worker logs,
fake process table, and fake state dir — nothing touches the real endpoint,
the real board, or the real process list.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

WATCHDOG = Path(__file__).resolve().parent.parent / "child_worker_watchdog.py"

sys.path.insert(0, str(WATCHDOG.parent))
import child_worker_watchdog as wd


class FakeWorld:
    """Holds everything the watchdog touches: kanban.db, logs, state, env."""

    def __init__(self, root: Path):
        self.root = root
        self.tmp = root
        self.state = root / "state"
        self.logs = root / "logs"
        self.state.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.db = root / "kanban.db"
        self.procs = {}  # pid -> {"alive": bool, "cmd": str}
        self.notify_calls = []  # list of (key, text, cls)
        self.log_lines = []  # list of log messages
        self.curl_calls = []  # list of argv lists
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT,
            worker_pid INTEGER, started_at INTEGER,
            max_runtime_seconds INTEGER, reasoning_effort TEXT,
            model_override TEXT, status TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            profile TEXT, step_key TEXT, status TEXT,
            claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
            max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
            started_at INTEGER NOT NULL, ended_at INTEGER,
            outcome TEXT, summary TEXT, metadata TEXT, error TEXT,
            worker_started_at INTEGER
        )""")
        conn.commit()
        conn.close()

    def add_task(self, task_id, title, assignee, pid, started_at, status="running",
                 heartbeat=None, run_status="running"):
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "INSERT INTO tasks (id,title,assignee,worker_pid,started_at,status) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, title, assignee, pid, started_at, status),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id,status,worker_pid,last_heartbeat_at,started_at,outcome) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, run_status, pid, heartbeat, started_at, None),
        )
        conn.commit()
        conn.close()

    def write_log(self, task_id, lines):
        (self.logs / f"{task_id}.log").write_text("\n".join(lines) + "\n")


class TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wdc-test-"))
        self.world = FakeWorld(self.tmp)
        self.state_dir = self.world.state
        self.logs_dir = self.world.logs
        self.db_path = self.world.db

        # Save originals for restoration
        self._orig_state_dir = wd.state_dir
        self._orig_env_file = wd.env_file
        self._orig_kanban_db = wd.kanban_db
        self._orig_worker_log_dir = wd.worker_log_dir
        self._orig_chat_id = wd.chat_id
        self._orig_dry_run = wd.dry_run
        self._orig_subprocess_run = wd.subprocess.run
        self._orig_os_kill = wd.os.kill
        self._orig_notify = wd.notify
        self._orig_log = wd.log

        # Monkeypatch module-level functions that touch the real world
        wd.state_dir = lambda: self.state_dir
        wd.env_file = lambda: self.world.tmp / ".env"
        wd.kanban_db = lambda: self.db_path
        wd.worker_log_dir = lambda: self.logs_dir
        wd.chat_id = lambda: "12345"
        wd.dry_run = lambda: True

        # Fake subprocess.run: intercepts curl (notifications) and ps (process checks)
        def fake_run(argv, **kwargs):
            if argv[0] == "curl":
                self.world.curl_calls.append(list(argv))
                class R:
                    returncode = 0
                    stdout = '{"ok":true}'
                    stderr = ""
                return R()
            # ps call: ["ps", "-p", "<pid>", "-o", "pid,etime,cmd"]
            if argv[0] == "ps" and "-p" in argv:
                p_idx = argv.index("-p")
                pid = int(argv[p_idx + 1])
                info = self.world.procs.get(pid, {"alive": False, "cmd": "unknown"})
                class R:
                    pass
                r = R()
                if info["alive"]:
                    r.stdout = f"PID ETIME CMD\n{pid}   00:05   {info['cmd']}\n"
                else:
                    r.stdout = "PID ETIME CMD\n"
                r.stderr = ""
                r.returncode = 0
                return r
            # Anything else: pass through to real subprocess.run
            return self._orig_subprocess_run(argv, **kwargs)
        wd.subprocess.run = fake_run

        # Fake os.kill
        def fake_os_kill(pid, sig):
            if pid not in self.world.procs or not self.world.procs[pid]["alive"]:
                raise OSError("no such process")
        wd.os.kill = fake_os_kill

        # Fake notify with dedup
        def fake_notify(secrets, text, key, cls=""):
            now = time.time()
            hist = wd.read_json("notify_history.json", {})
            if hist.get(key) and now - hist[key] < wd.NOTIFY_DEDUP_S:
                return False
            self.world.notify_calls.append((key, text, cls))
            hist[key] = now
            wd.write_json("notify_history.json", hist)
            return True
        wd.notify = fake_notify

        # Fake log
        def fake_log(msg):
            self.world.log_lines.append(msg)
        wd.log = fake_log

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # Restore module functions
        wd.state_dir = self._orig_state_dir
        wd.env_file = self._orig_env_file
        wd.kanban_db = self._orig_kanban_db
        wd.worker_log_dir = self._orig_worker_log_dir
        wd.chat_id = self._orig_chat_id
        wd.dry_run = self._orig_dry_run
        wd.subprocess.run = self._orig_subprocess_run
        wd.os.kill = self._orig_os_kill
        wd.notify = self._orig_notify
        wd.log = self._orig_log


class TestProcessAlive(TestBase):
    def test_alive_worker(self):
        self.world.procs[999] = {"alive": True, "cmd": "hermes_cli work kanban task 999"}
        self.assertTrue(wd.process_alive(999))

    def test_dead_process(self):
        self.world.procs[998] = {"alive": False, "cmd": ""}
        self.assertFalse(wd.process_alive(998))

    def test_unknown_pid(self):
        self.assertFalse(wd.process_alive(55555))

    def test_non_worker_process(self):
        self.world.procs[997] = {"alive": True, "cmd": "some-other-process"}
        self.assertFalse(wd.process_alive(997))


class TestFirstApiCall(TestBase):
    def test_finds_api_call(self):
        self.world.write_log("t_abc", [
            "2026-10-18 22:00:00 [worker] starting",
            "2026-10-18 22:00:45 [worker] API call #1: model=... in=100 out=50",
        ])
        t, line = wd.first_api_call_ts(
            self.logs_dir / "t_abc.log",
            datetime(2026, 10, 18, 22, 0, 0).timestamp(),
        )
        self.assertIsNotNone(t)
        self.assertIsNotNone(line)

    def test_no_api_call(self):
        self.world.write_log("t_abc", ["2026-10-18 22:00:00 [worker] starting"])
        t, line = wd.first_api_call_ts(
            self.logs_dir / "t_abc.log",
            datetime(2026, 10, 18, 22, 0, 0).timestamp(),
        )
        self.assertIsNone(t)
        self.assertIsNone(line)


class TestRunningTasks(TestBase):
    def test_returns_running_tasks(self):
        now = time.time()
        self.world.add_task("t_1", "Task one", "sindri", 100, now - 100)
        self.world.add_task("t_2", "Task two", "isis", 200, now - 200, status="done")
        tasks = wd.running_tasks()
        ids = {t["id"] for t in tasks}
        self.assertIn("t_1", ids)
        self.assertNotIn("t_2", ids)

    def test_empty_db_returns_empty(self):
        tasks = wd.running_tasks()
        self.assertEqual(tasks, [])


class TestSilentWorkerDetection(TestBase):
    def test_silent_no_api_call_stale_heartbeat(self):
        now = time.time()
        self.world.add_task("t_silent", "Silent task", "sindri", 777, now - 120,
                            heartbeat=now - 180)
        self.world.procs[777] = {"alive": True, "cmd": "hermes_cli work kanban task 777"}
        self.world.write_log("t_silent", ["2026-10-18 22:00:00 [worker] starting"])

        results = wd.check_workers({"TELEGRAM_BOT_TOKEN": "fake-token"})
        silent_ids = {e["task_id"] for e in results["silent"]}
        self.assertIn("t_silent", silent_ids)
        self.assertGreater(len(results["silent"]), 0)

    def test_healthy_with_api_call(self):
        now = time.time()
        spawn = now - 100
        self.world.add_task("t_ok", "OK task", "isis", 888, spawn)
        self.world.procs[888] = {"alive": True, "cmd": "hermes_cli work kanban task 888"}
        call_ts = spawn + 30
        call_line = datetime.fromtimestamp(call_ts).strftime(
            "%Y-%m-%d %H:%M:%S") + " [worker] API call #1: ..."
        self.world.write_log("t_ok", [call_line])

        results = wd.check_workers({})
        healthy_ids = {e["task_id"] for e in results["healthy"]}
        self.assertIn("t_ok", healthy_ids)
        self.assertEqual(len(results["silent"]), 0)
        self.assertEqual(len(self.world.notify_calls), 0)

    def test_time_to_first_token_exceeds_threshold(self):
        now = time.time()
        spawn = now - 200  # 200s ago
        self.world.add_task("t_slow", "Slow task", "sindri", 999, spawn)
        self.world.procs[999] = {"alive": True, "cmd": "hermes_cli work kanban task 999"}
        call_ts = spawn + 150  # 150s after spawn > 90s threshold
        call_line = datetime.fromtimestamp(call_ts).strftime(
            "%Y-%m-%d %H:%M:%S") + " [worker] API call #1: ..."
        self.world.write_log("t_slow", [call_line])

        results = wd.check_workers({})
        silent_ids = {e["task_id"] for e in results["silent"]}
        self.assertIn("t_slow", silent_ids)
        entry = next(e for e in results["silent"] if e["task_id"] == "t_slow")
        self.assertIsNotNone(entry["ttf_s"])
        self.assertGreater(entry["ttf_s"], 90)

    def test_process_not_alive(self):
        now = time.time()
        self.world.add_task("t_dead", "Dead task", "sindri", 555, now - 60)
        self.world.write_log("t_dead", [
            datetime.fromtimestamp(now - 50).strftime(
                "%Y-%m-%d %H:%M:%S") + " [worker] API call #1: ..."
        ])

        results = wd.check_workers({})
        silent_ids = {e["task_id"] for e in results["silent"]}
        self.assertIn("t_dead", silent_ids)


class TestNotifyDedup(TestBase):
    def test_notify_dedup(self):
        secrets = {"TELEGRAM_BOT_TOKEN": "fake-token"}
        wd.notify(secrets, "msg1", "key-1")
        wd.notify(secrets, "msg2", "key-1")  # same key, within dedup window
        keys = [c[0] for c in self.world.notify_calls]
        self.assertEqual(keys.count("key-1"), 1)


class TestMainEndToEnd(TestBase):
    def test_full_cycle_silent_worker(self):
        now = time.time()
        spawn = now - 200
        self.world.add_task("t_silent", "Silent", "sindri", 444, spawn)
        self.world.procs[444] = {"alive": False, "cmd": ""}
        self.world.write_log("t_silent", ["2026-10-18 22:00:00 [worker] starting"])

        wd.main()

        self.assertGreaterEqual(len(self.world.notify_calls), 1)
        self.assertTrue((self.state_dir / "child_watchdog_last_run.json").exists())


if __name__ == "__main__":
    unittest.main()