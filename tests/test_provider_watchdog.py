#!/usr/bin/env python3
"""Hermetic test suite for the provider-failure watchdog.

Zero network. Zero access to real household logs, real .env, or real systemctl.
Everything is staged in a per-test tmp dir: fake log files with synthetic lines
that mimic the REAL formats captured from the 2026-10-14 outage, a fake .env,
mock curl, mock systemctl, mock hermes.

Run:  python3 tests/run_tests.py        (from ~/household/watchdog)
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCHDOG = HERE.parent / "provider_watchdog.py"

NOW = time.time()
NOW_STR = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW))


def ts(offset_seconds: float) -> str:
    """Timestamp offset from 'now' in the real hermes log format."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW + offset_seconds)) + f",{int((NOW + offset_seconds) * 1000) % 1000:03d}"


# ---- synthetic lines in REAL log formats (verified against the 10-14 outage) ----

def L(sec, level, text):
    return f"{ts(sec)} {level} [sess_abc123] agent.conversation_loop: {text}"


LINE_429_STREAM = "Streaming failed before delivery: Error code: 429 - {'status': 429, 'title': 'Too Many Requests'}"
LINE_429_APIFAIL = "API call failed (attempt 1/3) error_type=RateLimitError thread=hermes-gateway_0:123 provider=nvidia base_url=https://integrate.api.nvidia.com/v1 model=z-ai/glm-5.3"
LINE_DEGRADED_400 = "Streaming failed before delivery: Error code: 400 - {'status': 400, 'title': 'Bad Request', 'detail': \"Function id '0cd5c583-435c-4649-a621-f4e421fa12a0': DEGRADED function cannot be invoked\"}"
LINE_CLEAN_EOF = "Clean EOF, no finish_reason: server ended the stream (no transport exception) after delivering text with no tool calls. The server or a proxy closed the stream cleanly."
LINE_TIMEOUT = "Streaming failed before delivery: Request timed out."
LINE_API_FAILED_AFTER = "API failed after 3 attempts error_type=ReadTimeout provider=nvidia"
LINE_FAILURE_REASON_RATE = "turn_failure: failure_reason=rate_limit detail=provider throttled"
LINE_FAILURE_REASON_CONN = "turn_failure: failure_reason=connection detail=APIConnectError"
LINE_HTTP_429 = "httpx: HTTP Request: POST https://integrate.api.nvidia.com/v1/chat/completions \"HTTP/1.1 429 Too Many Requests\""
LINE_HTTP_404 = "httpx: HTTP Request: POST https://integrate.api.nvidia.com/v1/chat/completions \"HTTP/1.1 404 Not Found\""
LINE_BENIGN_INFO = "INFO [sess_abc123] agent.tool_executor: tool skill_view completed (0.25s, 2862 chars)"
LINE_BENIGN_TELEGRAM = "WARNING hermes_plugins.platforms__telegram.adapter: [Telegram] Telegram network error, scheduling reconnect: httpx.ConnectError: [Errno 111] Connection refused"
LINE_BENIGN_MEMTRIM = "INFO hermes_cli.mem_trim: memory trim: reason=messaging gateway housekeeping malloc_trim=1"
# TERMINAL-shaped failure line: the three-tier split of defect C (spec §1
# Defect C) makes this the only signature class that may trigger detection.
# Fixtures that need "the provider really failed" must use this, not a soft
# (retryable) line: Hermes retries soft blips and most recover on try 1-2, so
# counting raw soft lines is exactly the bug defect C removed.
LINE_TERMINAL_FAIL = "API failed after 3 retries error_type=ReadTimeout provider=nvidia base_url=https://integrate.api.nvidia.com/v1"

PROFILES = ("default", "alpha", "beta")


def api_call_line(sec, n, i, o, latency="3.0"):
    return L(sec, "INFO", f"API call #{n}: model=z-ai/glm-5.3 provider=nvidia in={i} out={o} total={i + o} latency={latency}s")


class FakeWorld:
    """A hermetic fake environment: home, .hermes logs, .env, mock bins, state dir."""

    def __init__(self, tmp: Path, profiles=PROFILES):
        self.root = tmp
        self.profiles = tuple(profiles)
        self.home = tmp / "home"
        self.hermes = self.home / ".hermes"
        self.state = tmp / "watchdog" / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.env_file = self.hermes / ".env"
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text("NVIDIA_API_KEY=fake-nvidia-key-123\nTELEGRAM_BOT_TOKEN=fake-telegram-token-456\n")
        for p in self.profiles:
            logs = self.hermes if p == "default" else self.hermes / "profiles" / p
            (logs / "logs").mkdir(parents=True, exist_ok=True)
            (logs / "logs" / "agent.log").write_text("")
            (logs / "logs" / "errors.log").write_text("")
        self.mockbin = tmp / "mockbin"
        self.mockbin.mkdir()
        self.calls = tmp / "calls"
        self.calls.mkdir()
        self.curl_log = self.calls / "curl.log"
        self.curl_config_log = self.calls / "curl_config.log"
        self.systemctl_log = self.calls / "systemctl.log"
        self.hermes_log = self.calls / "hermes.log"
        self.curl_queue = tmp / "curl_queue.json"
        self.curl_queue.write_text("[]")
        self._install_mocks()

    def _mock(self, name: str, body: str) -> None:
        p = self.mockbin / name
        p.write_text(textwrap.dedent(body))
        p.chmod(stat.S_IRWXU)

    def _install_mocks(self):
        self._mock("curl", f"""\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            log = Path({str(self.curl_log)!r})
            qpath = Path({str(self.curl_queue)!r})
            with log.open("a") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            # defect 7 witness: --config files are the sanctioned secret carrier
            # (0600, transient). Recording WHAT rode in them lets tests prove
            # the probe authenticates without the key ever touching argv.
            args1 = sys.argv[1:]
            for i, a in enumerate(args1):
                if a == "--config" and i + 1 < len(args1):
                    try:
                        content = Path(args1[i + 1]).read_text()
                    except OSError:
                        content = "<unreadable>"
                    with Path({str(self.curl_config_log)!r}).open("a") as cfh:
                        cfh.write(json.dumps({{"argv": args1, "config": content}}) + "\\n")
            q = json.loads(qpath.read_text() or "[]")
            if q:
                item = q.pop(0)
                qpath.write_text(json.dumps(q))
                if isinstance(item, dict):
                    sys.stdout.write(item.get("stdout", ""))
                    sys.stderr.write(item.get("stderr", ""))
                    sys.exit(item.get("exit", 0))
                sys.stdout.write(item)
                sys.exit(0)
            sys.stdout.write('{{"id":"chatcmpl-ok","choices":[{{"message":{{"role":"assistant","content":"ok"}}}}],"usage":{{"total_tokens":1}}}}')
            sys.exit(0)
            """)
        self._mock("systemctl", f"""\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            log = Path({str(self.systemctl_log)!r})
            with log.open("a") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            sys.exit(0)
            """)
        self._mock("hermes", f"""\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            log = Path({str(self.hermes_log)!r})
            with log.open("a") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            sys.exit(0)
            """)

    def queue_curl(self, *responses):
        """Queue curl outcomes: 'str' prints+exit0; {'stdout','exit'} custom."""
        q = json.loads(self.curl_queue.read_text() or "[]")
        q.extend(responses)
        self.curl_queue.write_text(json.dumps(q))

    def read_log_lines(self, path: Path):
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def write_log(self, profile: str, which: str, lines) -> None:
        base = self.hermes if profile == "default" else self.hermes / "profiles" / profile
        (base / "logs" / f"{which}.log").write_text("\n".join(lines) + ("\n" if lines else ""))

    def env(self, extra=None):
        e = dict(os.environ)
        e.update({
            "HOME": str(self.home),
            "PATH": str(self.mockbin) + ":" + e.get("PATH", ""),
            "WATCHDOG_STATE_DIR": str(self.state),
            "WATCHDOG_ENV_FILE": str(self.env_file),
            "WATCHDOG_PROFILES": "default,alpha,beta",
            "WATCHDOG_CHAT_ID": "100200300",
            "NVIDIA_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
        })
        e.pop("NVIDIA_API_KEY", None)
        e.pop("TELEGRAM_BOT_TOKEN", None)
        if extra:
            e.update(extra)
        return e

    def run(self, extra_env=None, timeout=60):
        """Run the watchdog as a subprocess against the fake world."""
        env = self.env(extra_env)
        proc = subprocess.run(
            [sys.executable, str(WATCHDOG)],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
        return proc

    def main_log(self) -> str:
        p = self.state / "watchdog.log"
        return p.read_text() if p.exists() else ""

    def notifications(self):
        """Telegram sends the mock curl captured: extract message texts."""
        out = []
        for args in self.read_log_lines(self.curl_log):
            if "api.telegram.org" in " ".join(args):
                try:
                    i = args.index("-d")
                    d = json.loads(args[i + 1])
                    out.append(d.get("text", ""))
                except Exception:
                    pass
        return out


class TestBurnRate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _snapshot(self, world_logs):
        for prof, logs in world_logs.items():
            self.world.write_log(prof, "agent", logs)
        proc = self.world.run({"WATCHDOG_COMMAND": "burn"})
        assert proc.returncode == 0, f"burn rc={proc.returncode} stderr={proc.stderr[-2000:]}"
        snap = json.loads((self.world.state / "burn_snapshot.json").read_text())
        return snap, proc

    def test_burn_snapshot_counts_all_profiles(self):
        logs = {
            "default": [api_call_line(-30, 1, 1000, 100), api_call_line(-90, 2, 2000, 200)],
            "alpha": [api_call_line(-120, 1, 3000, 300)],
            "beta": [api_call_line(-700, 1, 9999, 999)],
        }
        snap, proc = self._snapshot(logs)
        # window = 10 min => default 2 + alpha 1 = 3 requests, beta line is 11+ min old
        self.assertEqual(snap["requests"], 3)
        self.assertEqual(snap["in_tokens"], 6000)
        self.assertEqual(snap["out_tokens"], 600)
        self.assertAlmostEqual(snap["requests_per_min"], 0.3)
        self.assertAlmostEqual(snap["tokens_per_min"], 660.0)
        self.assertIn("burn", proc.stdout.lower() or self.world.main_log().lower())

    def test_burn_ignores_non_api_lines(self):
        logs = {
            "default": [L(-30, "INFO", LINE_BENIGN_INFO), L(-20, "INFO", LINE_BENIGN_MEMTRIM)],
            "alpha": [], "beta": [],
        }
        snap, _ = self._snapshot(logs)
        self.assertEqual(snap["requests"], 0)

    def test_burn_snapshot_in_notifications(self):
        # sustained failure in one profile -> dry-run -> notification must carry burn snapshot
        # AMENDED (defect C): terminal-shaped sustained evidence (see LINE_TERMINAL_FAIL).
        logs = {
            "default": [
                L(-290, "WARNING", LINE_TERMINAL_FAIL), L(-250, "WARNING", LINE_TERMINAL_FAIL),
                L(-240, "WARNING", LINE_TERMINAL_FAIL), L(-200, "WARNING", LINE_TERMINAL_FAIL),
                L(-60, "WARNING", LINE_TERMINAL_FAIL), L(-30, "WARNING", LINE_TERMINAL_FAIL),
                api_call_line(-50, 1, 5000, 400), api_call_line(-100, 2, 6000, 500),
            ],
            "alpha": [], "beta": [],
        }
        for prof, lines in logs.items():
            self.world.write_log(prof, "errors", lines)
            self.world.write_log(prof, "agent", [])
        self.world.queue_curl({"stdout": '{"id":"chatcmpl-ok","choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        notes = self.world.notifications()
        self.assertTrue(notes, "expected at least one Telegram notification")
        blob = "\n".join(notes)
        self.assertIn("DRY-RUN", blob)
        self.assertRegex(blob, r"burn[:\s].*(req|min|requests/min)", blob)
        # Trued to THE RULING (card t_f35dec9e): rpm = count/10, tpm = tokens/10,
        # one decimal place, one computation feeding snapshot and notification.
        # 2 API calls in the 10-min window -> 0.2 req/min; 11900 tokens -> 1190.0 tok/min.
        self.assertIn("0.2 req/min", blob)  # 2 calls in 10-min window, /10 (ruling)
        self.assertIn("1190.0 tok/min", blob)  # (5000+400+6000+500)/10 (ruling)


class TestDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _detect(self, logs, extra_env=None):
        for prof, spec in logs.items():
            self.world.write_log(prof, "agent", spec.get("agent", []))
            self.world.write_log(prof, "errors", spec.get("errors", []))
        env = {"WATCHDOG_COMMAND": "detect"} if extra_env is None else extra_env
        proc = self.world.run(env if "WATCHDOG_COMMAND" in env else {"WATCHDOG_COMMAND": "detect", **env})
        det = json.loads((self.world.state / "detection.json").read_text())
        return det, proc

    def test_sustained_single_profile_triggers(self):
        # failures at T-5:00 and T-4:00 and T-0:30 — spanning >= 5 minutes => sustained.
        # AMENDED (defect C, spec §1 C): LINE_429_APIFAIL / LINE_429_STREAM are SOFT
        # (retryable) signatures and by design may no longer trigger; this fixture now
        # uses the terminal-shaped LINE_TERMINAL_FAIL, which still triggers sustained.
        logs = {"default": {"errors": [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL),
        ]}}
        det, _ = self._detect(logs)
        self.assertTrue(det["triggered"], det)
        self.assertEqual(det["affected"], ["default"])
        self.assertEqual(det["reason"], "sustained")

    def test_single_blip_does_not_trigger(self):
        # one SOFT failure line, 30s ago, no repetition => no trigger, and it is
        # now a 'none' (soft lines are evidence, never a decision input) rather
        # than the old 'blip'. AMENDED (defect C): the old soft-line fixture gave
        # 'blip'; under the tier split a soft-only profile reads reason=none.
        logs = {"default": {"errors": [L(-30, "WARNING", LINE_429_STREAM)]}}
        det, _ = self._detect(logs)
        self.assertFalse(det["triggered"], det)
        self.assertEqual(det["reason"], "none")

    def test_multi_profile_triggers_immediately(self):
        # 2 profiles failing within the same 5-min window => trigger even if young.
        # AMENDED (defect C): LINE_TIMEOUT is soft; the terminal-shaped line keeps
        # this test's original intent (two profiles, young evidence, immediate).
        logs = {
            "alpha": {"errors": [L(-30, "WARNING", LINE_TERMINAL_FAIL)]},
            "beta": {"errors": [L(-20, "WARNING", LINE_DEGRADED_400)]},
        }
        det, _ = self._detect(logs)
        self.assertTrue(det["triggered"], det)
        self.assertEqual(sorted(det["affected"]), ["alpha", "beta"])
        self.assertEqual(det["reason"], "multi-profile")

    def test_all_signature_types_detected(self):
        # every signature class is still recorded as interesting by the union
        # FAILURE_SIGNATURES; SOFT classes are recorded but must not trigger
        # (defect C). Terminal classes trigger as the sustained snapshot demands.
        # Tier assignment is the spec's, verified line by line:
        #   terminal: "failed after 3 retries" / "API failed after 3 retries" /
        #             "DEGRADED function cannot be invoked"
        #   soft:     the retryable blips, including LINE_API_FAILED_AFTER
        #             ("API failed after 3 attempts error_type=..." — the
        #             non-"3 retries" prefix, so SOFT, not terminal)
        # LINE_DEGRADED_400 carries DEGRADED text, so it is TERMINAL despite
        # opening with the soft-shaped "Streaming failed before delivery".
        terminal_lines = (LINE_TERMINAL_FAIL, LINE_DEGRADED_400)
        soft_lines = (LINE_429_STREAM, LINE_429_APIFAIL, LINE_API_FAILED_AFTER,
                      LINE_CLEAN_EOF, LINE_TIMEOUT, LINE_FAILURE_REASON_RATE,
                      LINE_FAILURE_REASON_CONN, LINE_HTTP_429, LINE_HTTP_404)
        with self.subTest(tier="terminal"):
            for line in terminal_lines:
                logs = {"default": {"errors": [L(-200, "WARNING", line), L(-100, "WARNING", line)]}}
                det, _ = self._detect(logs)
                self.assertTrue(det["triggered"], f"signature not detected: {line[:60]}")
        with self.subTest(tier="soft"):
            for line in soft_lines:
                logs = {"default": {"errors": [L(-200, "WARNING", line), L(-100, "WARNING", line)]}}
                det, _ = self._detect(logs)
                self.assertFalse(det["triggered"], f"soft signature must not trigger: {line[:60]}")
                self.assertGreaterEqual(det["per_profile"]["default"]["soft"], 2, line[:60])

    def test_benign_lines_do_not_trigger(self):
        # telegram/memtrim/tool lines must never count as provider failure
        logs = {"default": {"errors": [
            L(-30, "WARNING", LINE_BENIGN_TELEGRAM), L(-20, "INFO", LINE_BENIGN_INFO),
            L(-10, "INFO", LINE_BENIGN_MEMTRIM),
        ]}}
        det, _ = self._detect(logs)
        self.assertFalse(det["triggered"], det)

    def test_old_failures_outside_window_ignored(self):
        # failure lines 30+ minutes old: outside window entirely
        logs = {"default": {"errors": [L(-2400, "WARNING", LINE_429_STREAM), L(-1800, "WARNING", LINE_429_APIFAIL)]}}
        det, _ = self._detect(logs)
        self.assertFalse(det["triggered"], det)


class TestProbe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_probe_ok_and_body_scrubbed(self):
        self.world.queue_curl({"stdout": '{"id":"chatcmpl-ok","choices":[{"message":{"content":"hi"}}], "usage": {"total_tokens": 1}}', "exit": 0})
        proc = self.world.run({"WATCHDOG_COMMAND": "probe"})
        res = json.loads((self.world.state / "probe_result.json").read_text())
        self.assertTrue(res["ok"])
        # scrubbed body must not contain the auth header or key
        body_f = self.world.state / "probe_last_body.txt"
        raw = body_f.read_text()
        self.assertNotIn("fake-nvidia-key-123", raw)
        # the curl invocation must use --max-time 15 and the family key env var
        args = self.world.read_log_lines(self.world.curl_log)[0]
        joined = " ".join(args)
        self.assertIn("--max-time", joined)
        self.assertIn("integrate.api.nvidia.com/v1/chat/completions", joined)
        # key must never appear on the curl argv (it rides via -H "Authorization: Bearer $..." env expansion? no: it must be via config file or env var reference)
        self.assertNotIn("fake-nvidia-key-123", joined)

    def test_probe_fail_exit_nonzero(self):
        self.world.queue_curl({"stdout": '{"error": {"status": 429}}', "exit": 22})
        self.world.run({"WATCHDOG_COMMAND": "probe"})
        res = json.loads((self.world.state / "probe_result.json").read_text())
        self.assertFalse(res["ok"])
        self.assertEqual(res["http_status"], "429")

    def test_probe_fail_empty(self):
        self.world.queue_curl({"stdout": "", "exit": 28})  # curl timeout
        self.world.run({"WATCHDOG_COMMAND": "probe"})
        res = json.loads((self.world.state / "probe_result.json").read_text())
        self.assertFalse(res["ok"])

    def test_429_body_captured_verbatim(self):
        body = '{"status": 429, "title": "Too Many Requests", "detail": "Quota: RPM_LIMIT_EXCEEDED; window: 60s"}'
        self.world.queue_curl({"stdout": body, "exit": 22})
        self.world.run({"WATCHDOG_COMMAND": "probe"})
        cap = self.world.state / "last_429_body.txt"
        self.assertTrue(cap.exists(), "429 body must be captured verbatim to state dir")
        self.assertIn("RPM_LIMIT_EXCEEDED", cap.read_text())

    def test_probe_config_carries_real_auth_header(self):
        """Defect 7 (unnumbered, filed on card t_887a4065): the probe's curl
        --config wrote a LITERAL 'Authorization: Bearer ***' (f-string with no
        placeholder), so every production probe went out unauthenticated.
        The header must interpolate the real key — exactly like notify() does
        for the bot token — while still never touching argv."""
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"WATCHDOG_COMMAND": "probe"})
        entries = self.world.read_log_lines(self.world.curl_config_log)
        self.assertTrue(entries, "probe must pass a --config file to curl")
        probe_cfgs = [e["config"] for e in entries
                      if "integrate.api.nvidia.com" in " ".join(e["argv"])]
        self.assertTrue(probe_cfgs, f"no probe config captured: {entries}")
        header = probe_cfgs[0]
        self.assertIn("Authorization: Bearer fake-nvidia-key-123", header,
                      f"probe config must carry the real key: {header!r}")
        self.assertNotIn("***", header, f"literal stars must be gone: {header!r}")
        # hygiene unchanged: the key still never rides argv
        argv = " ".join(" ".join(a) for a in self.world.read_log_lines(self.world.curl_log))
        self.assertNotIn("fake-nvidia-key-123", argv)


class TestDecisionTree(unittest.TestCase):
    """Full-cycle tests: detection + probe + decision + notify, in dry-run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _sustained_logs(self, prof="default"):
        # AMENDED (defect C): terminal-shaped evidence. LINE_429_APIFAIL /
        # LINE_429_STREAM are retryable blips that by design no longer trigger.
        return {prof: {"errors": [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL),
        ]}}

    def _seed_sustained(self, prof="default"):
        for p, spec in self._sustained_logs(prof).items():
            self.world.write_log(p, "agent", spec.get("agent", []))
            self.world.write_log(p, "errors", spec.get("errors", []))

    def test_dry_run_probe_ok_restarts_blocked(self):
        self._seed_sustained()
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        # systemctl mock must NEVER have been called for restart
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        notes = self.world.notifications()
        self.assertTrue(notes)
        blob = "\n".join(notes)
        self.assertIn("DRY-RUN", blob)
        self.assertIn("would restart", blob.lower())

    def test_live_mode_probe_ok_restarts_affected(self):
        """NOT dry-run: affected gateway restart executes (against MOCK systemctl)."""
        self._seed_sustained("alpha")
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        calls = self.world.read_log_lines(self.world.systemctl_log)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0], ["--user", "restart", "hermes-gateway.service"])
        # notification before AND after the action
        notes = self.world.notifications()
        self.assertGreaterEqual(len(notes), 2)

    def test_live_mode_two_profiles_restarts_all_three(self):
        # AMENDED (defect C): LINE_TIMEOUT is soft; LINE_TERMINAL_FAIL keeps
        # the original intent (two profiles failing simultaneously).
        logs = {
            "alpha": {"errors": [L(-30, "WARNING", LINE_TERMINAL_FAIL)]},
            "beta": {"errors": [L(-20, "WARNING", LINE_DEGRADED_400)]},
        }
        for p, spec in logs.items():
            self.world.write_log(p, "agent", [])
            self.world.write_log(p, "errors", spec["errors"])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        calls = self.world.read_log_lines(self.world.systemctl_log)
        self.assertEqual(len(calls), 1, calls)
        restarted = sorted(c[2] for c in calls)
        self.assertEqual(restarted, ["hermes-gateway.service"])

    def test_probe_fail_enforces_silence_and_notifies(self):
        self._seed_sustained()
        self.world.queue_curl({"stdout": '{"status": 429, "title": "Too Many Requests"}', "exit": 22})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        st = json.loads((self.world.state / "silence.json").read_text())
        self.assertTrue(st["enforced"])
        notes = self.world.notifications()
        self.assertTrue(any("silence" in n.lower() for n in notes))
        # no restarts even in dry-run
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])

    def test_recovery_two_consecutive_probes_restart_all(self):
        """Silence phase: probe fails, then 2 consecutive OK probes -> restart all 3 once."""
        self._seed_sustained()
        # first run: probe fails -> silence enforced
        self.world.queue_curl({"stdout": '{"status":429}', "exit": 22})
        self.world.run({"DRY_RUN": "0"})
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        # Trued to the spec's quiet window (card t_f35dec9e): the first sparse
        # probe is gated by 5 minutes of quiet after enforcement, exactly as
        # test_silence_phase_sparse_probe_first_probe_gated_by_quiet_window
        # demands. Forge the silence clock so this run is past the quiet window
        # (same idiom as the restart_stamps forging in TestSafetyRails).
        st = json.loads((self.world.state / "silence.json").read_text())
        st["since"] = time.time() - 400
        st["last_probe"] = time.time() - 400
        (self.world.state / "silence.json").write_text(json.dumps(st))
        # silence phase: probe OK (1st consecutive)
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "0"})
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        # 2nd consecutive OK
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "0"})
        calls = self.world.read_log_lines(self.world.systemctl_log)
        self.assertEqual(len(calls), 1, calls)
        # timeline notification must fire
        notes = self.world.notifications()
        self.assertTrue(any("recovered" in n.lower() or "timeline" in n.lower() for n in notes))

    def test_silence_phase_sparse_probe_first_probe_gated_by_quiet_window(self):
        # during silence, run immediately again -> NO probe must be sent (quiet window)
        self._seed_sustained()
        self.world.queue_curl({"stdout": '{"status":429}', "exit": 22})
        self.world.run({"DRY_RUN": "0"})
        curl_calls_after_run1 = len(self.world.read_log_lines(self.world.curl_log))
        # immediate second run: within quiet window => no probe
        self.world.run({"DRY_RUN": "0"})
        curl_calls_after_run2 = len(self.world.read_log_lines(self.world.curl_log))
        self.assertEqual(curl_calls_after_run2, curl_calls_after_run1,
                         "no probe may fire within the silence quiet window")


class TestSafetyRails(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, prof="default"):
        # AMENDED (defect C): terminal-shaped evidence (see LINE_TERMINAL_FAIL).
        self.world.write_log(prof, "agent", [])
        self.world.write_log(prof, "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL),
        ])

    def _probe_ok_queue(self):
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})

    def test_restart_cooldown_30min(self):
        # live mode, sustained failure -> restart round 1
        self._seed()
        self._probe_ok_queue()
        self.world.run({"DRY_RUN": "0"})
        self.assertEqual(len(self.world.read_log_lines(self.world.systemctl_log)), 1)
        # fresh sustained failure right after: within 30-min cooldown -> NO second restart
        # AMENDED (defect C): terminal-shaped second burst.
        self.world.write_log("default", "errors", [
            L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-10, "WARNING", LINE_TERMINAL_FAIL)])
        self._probe_ok_queue()
        self.world.run({"DRY_RUN": "0"})
        self.assertEqual(len(self.world.read_log_lines(self.world.systemctl_log)), 1,
                        "cooldown must block the second restart round")
        # but a notification about the suppressed round should exist
        self.assertTrue(any("cooldown" in n.lower() for n in self.world.notifications()),
                        self.world.notifications())

    def test_three_restarts_in_hour_stops_acting(self):
        # round 1 (default), round 2 (alpha+beta => all three), round 3 -> must refuse
        # AMENDED (defect C): terminal-shaped evidence per profile.
        for prof in ("default", "alpha"):
            self.world.write_log(prof, "errors", [
                L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
                L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.write_log("beta", "errors", [
            L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-20, "WARNING", LINE_TERMINAL_FAIL)])
        self._probe_ok_queue()
        self.world.run({"DRY_RUN": "0"})  # >=2 profiles -> all three (counts 1 round each)
        calls1 = len(self.world.read_log_lines(self.world.systemctl_log))
        self.assertEqual(calls1, 1)
        # new failures, cooldowns manually expired: forge state as if 31 min passed
        st = json.loads((self.world.state / "restart_stamps.json").read_text())
        past = time.time() - 2100
        for k in st:
            st[k] = past
        (self.world.state / "restart_stamps.json").write_text(json.dumps(st))
        # AMENDED (defect C): terminal-shaped sustained evidence.
        for prof in ("default", "alpha", "beta"):
            self.world.write_log(prof, "errors", [
                L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-10, "WARNING", LINE_TERMINAL_FAIL)])
        self._probe_ok_queue()
        self.world.run({"DRY_RUN": "0"})  # round 2
        calls2 = len(self.world.read_log_lines(self.world.systemctl_log))
        # round 3 within the hour: cap tripped -> no restarts
        st = json.loads((self.world.state / "restart_stamps.json").read_text())
        past = time.time() - 2100
        for k in st:
            st[k] = past
        (self.world.state / "restart_stamps.json").write_text(json.dumps(st))
        # AMENDED (defect C): terminal-shaped sustained evidence.
        for prof in ("default", "alpha", "beta"):
            self.world.write_log(prof, "errors", [
                L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-10, "WARNING", LINE_TERMINAL_FAIL)])
        self._probe_ok_queue()
        self.world.run({"DRY_RUN": "0"})
        calls3 = len(self.world.read_log_lines(self.world.systemctl_log))
        self.assertEqual(calls3, calls2, "hourly restart cap must stop further restarts")
        self.assertTrue(any("manual attention" in n.lower() for n in self.world.notifications()),
                        "cap trip must notify the operator")


class TestSecretsNeverLeak(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_no_secret_in_logs_state_or_stdout(self):
        # sustained failure + dry run: full cycle; then grep everything for both fake keys
        # AMENDED (defect C): terminal-shaped sustained evidence.
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        secrets = ["fake-nvidia-key-123", "fake-telegram-token-456", "Bearer fake"]
        haystacks = {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "watchdog.log": self.world.main_log(),
        }
        for f in self.world.state.rglob("*"):
            if f.is_file():
                haystacks[f.name] = f.read_text()
        curl_args = " ".join(json.dumps(a) for a in self.world.read_log_lines(self.world.curl_log))
        haystacks["curl_args"] = curl_args
        for name, text in haystacks.items():
            for s in secrets:
                self.assertNotIn(s, text, f"SECRET LEAK in {name}: {s}")


class TestPerInstallConfig(unittest.TestCase):
    """Per-install configuration: WATCHDOG_PROFILES and WATCHDOG_CHAT_ID.

    The code ships neutral defaults; each install states its profile list and
    operator chat id via environment (systemd unit file). No code edits and
    no household identifiers in the source."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_profiles_configurable(self):
        # a 3-profile install named default,alpha,beta; sustained failure in
        # alpha must be detected through the env-stated profile list alone
        world = FakeWorld(Path(self._tmp.name), profiles=("default", "alpha", "beta"))
        # AMENDED (defect C): terminal-shaped sustained evidence.
        world.write_log("alpha", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = world.run({"DRY_RUN": "1", "WATCHDOG_PROFILES": "default,alpha,beta",
                          "WATCHDOG_CHAT_ID": "100200300"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        det = json.loads((world.state / "detection.json").read_text())
        self.assertTrue(det["triggered"], det)
        self.assertEqual(det["affected"], ["alpha"], det)
        self.assertEqual(det["reason"], "sustained", det)

    def test_missing_chat_id_never_notifies(self):
        # no chat id configured -> the watchdog must NOT send anything;
        # it logs the missing config and still exits clean
        world = FakeWorld(Path(self._tmp.name))
        # AMENDED (defect C): terminal-shaped sustained evidence.
        world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        proc = world.run({"DRY_RUN": "1", "WATCHDOG_CHAT_ID": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(world.notifications(), [],
                         "notification sent with no chat id configured")
        self.assertIn("WATCHDOG_CHAT_ID", world.main_log())

    def test_chat_id_rides_in_payload(self):
        # the configured chat id must be the one in the Telegram payload
        world = FakeWorld(Path(self._tmp.name))
        # AMENDED (defect C): terminal-shaped sustained evidence.
        world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = world.run({"DRY_RUN": "1", "WATCHDOG_CHAT_ID": "424242"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        sent = [json.loads(a[a.index("-d") + 1])
                for a in world.read_log_lines(world.curl_log)
                if "api.telegram.org" in " ".join(a) and "-d" in a]
        self.assertTrue(sent, "no telegram payload captured")
        self.assertEqual(sent[0]["chat_id"], "424242")


class TestPauseCron(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_pause_cron_default_off(self):
        # probe-fail path with PAUSE_CRON unset -> hermes cron pause NOT called
        # AMENDED (defect C): terminal-shaped sustained evidence.
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"status":429}', "exit": 22})
        self.world.run({"DRY_RUN": "1"})
        self.assertEqual(self.world.read_log_lines(self.world.hermes_log), [])

    def test_pause_cron_flag_on_pauses_via_cli(self):
        # AMENDED (defect C): terminal-shaped sustained evidence.
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"status":429}', "exit": 22})
        self.world.run({"DRY_RUN": "1", "WATCHDOG_PAUSE_CRON": "1"})
        calls = self.world.read_log_lines(self.world.hermes_log)
        self.assertTrue(any("pause" in " ".join(c) for c in calls), calls)


class TestE2EDryRunUnderTimer(unittest.TestCase):
    """Mimic what the systemd timer does every 2 minutes: plain run, DRY_RUN=1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_clean_logs_dry_run_quiet(self):
        # healthy world: no failures -> no probe, no notify, exit 0
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(self.world.read_log_lines(self.world.curl_log), [], "no probe on healthy logs")
        self.assertEqual(self.world.notifications(), [], "no notification on healthy logs")

    def test_dry_run_sustained_full_cycle(self):
        # AMENDED (defect C): terminal-shaped sustained evidence.
        self.world.write_log("default", "agent", [api_call_line(-60, 1, 7000, 300)])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        # watchdog.log must exist with timestamped lines
        log = self.world.main_log()
        self.assertRegex(log, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        # no systemctl calls in dry-run, ever
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        notes = self.world.notifications()
        self.assertTrue(notes)
        self.assertIn("DRY-RUN", "\n".join(notes))


# ---- defect A: config-driven probe model (red tests, 2026-10-18) ----
#
# Six defects fixed per ~/household/watchdog/watchdog-fix-spec-2026-10-18.md.
# These tests were written RED before any implementation change, per the
# card's iron-law method: a defect is fixed when its test goes red -> green.

TERMINAL_AFTER_3 = "API failed after 3 retries error_type=ReadTimeout provider=nvidia base_url=https://integrate.api.nvidia.com/v1"
TERMINAL_DEGRADED = LINE_DEGRADED_400  # already terminal-shaped: DEGRADED function cannot be invoked


TERMINAL_LINE_1 = "API failed after 3 retries error_type=ReadTimeout provider=nvidia base_url=https://integrate.api.nvidia.com/v1"
TERMINAL_LINE_DEGRADED = LINE_DEGRADED_400
SOFT_429 = LINE_HTTP_429
SOFT_APIFAIL = LINE_429_APIFAIL


class TestProbeMultiModel(unittest.TestCase):
    """Defect B / acceptance A2: provider usable when ANY model answers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = FakeWorld(Path(self._tmp.name))
        self._write_profile_config("default", "primary/model-x")
        self.world.queue_curl(
            {"stdout": '{"status": 429, "title": "Too Many Requests"}', "exit": 22})
        self.world.queue_curl(
            {"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})

    def _write_profile_config(self, profile, default_model):
        path = (self.world.hermes if profile == "default"
                else self.world.hermes / "profiles" / profile) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(f"""\
            model:
              provider: nvidia
              default: {default_model}
            """))

    def test_primary_429_fallback_200_is_ok(self):
        # detection must trigger, then the multi-model probe runs: without
        # terminal lines in the logs the cycle never reaches the probe at all.
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-290, "WARNING", TERMINAL_LINE_1), L(-30, "WARNING", TERMINAL_LINE_1)])
        env = {"DRY_RUN": "1", "WATCHDOG_FALLBACK_MODEL": "fallback/model-y"}
        proc = self.world.run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        res = json.loads((self.world.state / "probe_result.json").read_text())
        self.assertTrue(res["ok"], res)
        models = [m["model"] for m in res["per_model"]]
        self.assertEqual(models, ["primary/model-x", "fallback/model-y"])
        self.assertEqual(res["model"], "fallback/model-y")
        statuses = {m["model"]: m["http_status"] for m in res["per_model"]}
        self.assertEqual(statuses["primary/model-x"], "429")
        self.assertEqual(statuses["fallback/model-y"], "200")
        # silence must NOT be entered, no restart round
        self.assertFalse((self.world.state / "silence.json").exists(),
                         "model-scoped wall must not enforce silence")
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        self.assertIn("fallback/model-y", self.world.main_log())
        self.assertIn("429", self.world.main_log())

    def test_probe_logs_every_model_tried(self):
        # same reason as the sibling test: the probe only runs once detection
        # triggers, so seed a terminal-shaped failure first.
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-290, "WARNING", TERMINAL_LINE_1), L(-30, "WARNING", TERMINAL_LINE_1)])
        env = {"DRY_RUN": "1", "WATCHDOG_FALLBACK_MODEL": "fallback/model-y"}
        proc = self.world.run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        log = self.world.main_log()
        # one line names every model and its result
        self.assertRegex(log, r"primary/model-x=fail\(429\).*fallback/model-y=ok\(200\)")

    def test_env_probe_model_is_probed_alone(self):
        # explicit override wins: ONLY that model is tried (no config, no fallback)
        self.world.queue_curl({"stdout": '{"choices":[]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1", "WATCHDOG_COMMAND": "probe",
                               "WATCHDOG_PROBE_MODEL": "explicit/model-z"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        res = json.loads((self.world.state / "probe_result.json").read_text())
        self.assertEqual(res["models"], ["explicit/model-z"], res)


class TestDetectionTiers(unittest.TestCase):
    """Defect C / acceptance A4-A5: only TERMINAL signatures may trigger."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = FakeWorld(Path(self._tmp.name))

    def _detect(self, lines, extra_env=None):
        self.world.write_log("default", "errors", lines)
        self.world.write_log("default", "agent", [])
        env = {"WATCHDOG_COMMAND": "detect"}
        if extra_env:
            env.update(extra_env)
        proc = self.world.run(env)
        det = json.loads((self.world.state / "detection.json").read_text())
        return det, proc

    def test_soft_only_never_triggers(self):
        # 20 soft lines, zero terminal: trigger must stay False, soft counted
        lines = [L(-290 + i * 10, "WARNING", SOFT_429 if i % 2 else SOFT_APIFAIL)
                 for i in range(20)]
        det, proc = self._detect(lines)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertFalse(det["triggered"], det)
        self.assertEqual(det["reason"], "none", det)
        self.assertGreaterEqual(det["per_profile"]["default"]["soft"], 20, det)

    def test_terminal_after_3_retries_triggers(self):
        # A5 sustained branch: two terminal lines, one recent + one persisting
        lines = [L(-290, "WARNING", TERMINAL_LINE_1), L(-30, "WARNING", TERMINAL_LINE_1)]
        det, proc = self._detect(lines)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertTrue(det["triggered"], det)
        self.assertEqual(det["reason"], "sustained", det)

    def test_terminal_persisting_branch_triggers(self):
        # A5 persisting branch: one recent terminal + one older terminal inside
        # the 15-min lookback but OUTSIDE the 5-min detect window. -350s is
        # past the detect cutoff (302s) and well inside the lookback (902s).
        lines = [L(-350, "WARNING", TERMINAL_LINE_1), L(-30, "WARNING", TERMINAL_LINE_1)]
        det, _ = self._detect(lines)
        self.assertTrue(det["triggered"], det)
        self.assertGreaterEqual(det["per_profile"]["default"]["persisting"], 1)

    def test_degraded_400_is_terminal(self):
        lines = [L(-290, "WARNING", TERMINAL_LINE_DEGRADED), L(-30, "WARNING", TERMINAL_LINE_DEGRADED)]
        det, _ = self._detect(lines)
        self.assertTrue(det["triggered"], det)
        self.assertEqual(det["reason"], "sustained", det)

    def test_soft_and_terminal_mixed_terminal_decides(self):
        # soft lines alone don't count; adding 2 terminal lines flips it
        soft = [L(-300 + i * 20, "WARNING", SOFT_429) for i in range(4)]
        lines = soft + [L(-290, "WARNING", TERMINAL_LINE_1), L(-30, "WARNING", TERMINAL_LINE_1)]
        det, _ = self._detect(lines)
        self.assertTrue(det["triggered"], det)
        self.assertEqual(det["reason"], "sustained")


class TestProbeModelResolution(unittest.TestCase):
    """Defect A: the probe model must resolve from configuration, not a literal.

    Card acceptance A1: resolution from env / profile config / module constant,
    with the source named in the log. The probe payload's model is asserted on
    the curl argv (the model name is not a secret) and the resolved value is
    echoed into probe_result.json for the decision path to reason about.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.world = FakeWorld(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _probe_result(self, extra_env=None):
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        env = {"WATCHDOG_COMMAND": "probe"}
        if extra_env:
            env.update(extra_env)
        proc = self.world.run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads((self.world.state / "probe_result.json").read_text())

    def _write_profile_config(self, profile: str, default_model: str):
        path = (self.world.hermes if profile == "default"
                else self.world.hermes / "profiles" / profile) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        # decoy 'default:' in an unrelated block (stt:) guards the line scan:
        # a whole-file regex would grab the decoy, the spec's scan must not.
        path.write_text(textwrap.dedent(f"""\
            model:
              provider: nvidia
              default: {default_model}
            stt:
              enabled: true
              default: whisper-decoy
            """))

    def test_primary_model_resolves_from_profile_config(self):
        # WATCHDOG_PROFILES=default,alpha,beta: only beta carries a model.default
        self._write_profile_config("beta", "beta-configured/model-x")
        res = self._probe_result()
        self.assertEqual(res["model"], "beta-configured/model-x", res)
        # argv carries the resolved model (not a secret; asserted visible)
        argv = self.world.read_log_lines(self.world.curl_log)[0]
        self.assertIn("beta-configured/model-x", " ".join(argv))
        # the log line names the source
        self.assertIn("beta-configured/model-x", self.world.main_log())
        self.assertIn("config", self.world.main_log())

    def test_env_probe_model_overrides_config(self):
        self._write_profile_config("default", "config-should-lose/model")
        res = self._probe_result({"WATCHDOG_PROBE_MODEL": "env-override/model"})
        self.assertEqual(res["model"], "env-override/model", res)
        argv = self.world.read_log_lines(self.world.curl_log)[0]
        self.assertIn("env-override/model", " ".join(argv))
        self.assertNotIn("config-should-lose/model", " ".join(argv))

    def test_module_constant_when_nothing_configured(self):
        # no config files, no env override -> module constant (empty), source logged
        res = self._probe_result()
        self.assertEqual(res["model"], "", res)
        argv = self.world.read_log_lines(self.world.curl_log)[0]
        self.assertIn("constant", self.world.main_log())


# ---- defect D: stable class keys + hard 10-min floor (acceptance A6) ----

# The spec binds NOTIFY_MIN_INTERVAL_S to at least 600 and forbids sleeping in
# tests; the floor is asserted here through the value the watchdog keeps in
# its own module. Kept local so the hermetic subprocess suite never imports it.
NOTIFY_FLOOR_S = 600


class TestNotifyClassFloor(unittest.TestCase):
    """Six would_restart events over 8 minutes with a ROTATING affected set.

    The old code built the dedup key from the affected profile set, so every
    new set looked fresh and all six were delivered (75+ in 30 min live).
    Now the class key is stable: exactly ONE delivered, five suppressed by
    the class floor, five suppression lines in the log.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = FakeWorld(Path(self._tmp.name))

    def _run_restart_cycle(self, profiles, probe_ok=True):
        """One full cycle whose detection fires on `profiles`.
        Returns the notifications delivered so far."""
        for p in profiles:
            self.world.write_log(p, "agent", [])
            self.world.write_log(p, "errors", [
                L(-300, "WARNING", LINE_TERMINAL_FAIL),
                L(-240, "WARNING", LINE_TERMINAL_FAIL),
                L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        other = [p for p in PROFILES if p not in profiles]
        for p in other:
            self.world.write_log(p, "agent", [])
            self.world.write_log(p, "errors", [])
        stdout = ('{"choices":[{"message":{"content":"ok"}}]}' if probe_ok
                  else '{"status": 429, "title": "Too Many Requests"}')
        self.world.queue_curl({"stdout": stdout, "exit": 0 if probe_ok else 22})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])

    def test_rotating_sets_deliver_exactly_one(self):
        # 6 would_restart events across an 8-MINUTE span (not one instant),
        # affected set ROTATING each cycle: default -> alpha -> beta -> alpha
        # -> beta -> default. Old behaviour: the rotating set made every dedup
        # key look fresh, so all 6 were delivered (75+ in 30 min live).
        # Now: exactly ONE delivered, five suppressed by the class floor.
        #
        # Timing is forged, not slept on: each cycle's class timestamp is back-
        # dated by the elapsed span MINUS the floor, so the tests stay hermetic
        # and fast while still exercising the 8-minute timeline the incident
        # produced. The last cycle pushes past the 10-min floor to prove a
        # floor-bound notice can eventually fire again.
        starts = [0, 96, 192, 288, 384, 480]  # seconds between the 6 events
        plan = [("default",), ("alpha",), ("beta",), ("alpha",), ("beta",), ("default",)]
        for step, (target, elapsed) in enumerate(zip(plan, starts)):
            self._run_restart_cycle(target)
            if step == len(plan) - 1:
                # push the last cycle past the floor: it should be allowed
                hist = json.loads((self.world.state / "notify_history.json").read_text())
                for k in hist:
                    if k.startswith("class:"):
                        hist[k] = time.time() - NOTIFY_FLOOR_S - 60
                (self.world.state / "notify_history.json").write_text(json.dumps(hist))
                self._run_restart_cycle(target)
                break
            # back-date only the class entry: the 5-min per-key dedup must stay
            # relevant too, so both are aged to match the elapsed span
            hist_path = self.world.state / "notify_history.json"
            hist = json.loads(hist_path.read_text())
            for k in hist:
                hist[k] = time.time() - elapsed
            hist_path.write_text(json.dumps(hist))
        texts = [t for t in self.world.notifications() if "would restart" in t.lower()]
        # exactly two: the first delivery and the post-floor re-delivery
        self.assertEqual(len(texts), 2, f"expected one delivery + one post-floor: {texts}")
        sup = [l for l in self.world.main_log().splitlines()
               if "suppressed by 10-min class floor" in l]
        self.assertEqual(len(sup), len(plan) - 1,
                         f"five suppressions expected across the 6 events: {sup}")
        for l in sup:
            self.assertIn("would_restart", l)

    def test_suppression_is_logged_not_silent(self):
        self._run_restart_cycle(("default",))
        self._run_restart_cycle(("alpha",))  # rotating set: same class
        log = self.world.main_log()
        self.assertIn("notify[would_restart]: suppressed by 10-min class floor", log)
        # the suppression mentions how long ago the last one went out
        self.assertRegex(log, r"last \d+m ago")


# ---- defect E: probe OK on a non-default model must not restart (A3) ----


class TestRotationGuard(unittest.TestCase):
    """A3: recovery on a NON-default model -> no restart round, skip logged,
    and a model_rotation_needed notification attempted (dry-run: no systemctl)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = FakeWorld(Path(self._tmp.name))
        # the household runs primary/model-x (from config); the walled model
        # is the DEFAULT, and only fallback/model-y answers.
        self._write_profile_config("default", "primary/model-x")

    def _write_profile_config(self, profile, default_model):
        path = (self.world.hermes if profile == "default"
                else self.world.hermes / "profiles" / profile) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(f"""\
            model:
              provider: nvidia
              default: {default_model}
            """))

    def test_non_default_recovery_skips_restart(self):
        # terminal evidence -> trigger; probe: primary 429, fallback 200
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL),
            L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl(
            {"stdout": '{"status": 429, "title": "Too Many Requests"}', "exit": 22})
        self.world.queue_curl(
            {"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1",
                               "WATCHDOG_FALLBACK_MODEL": "fallback/model-y"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        # no restart round was even attempted (dry-run logs 'would restart')
        calls = self.world.read_log_lines(self.world.systemctl_log)
        self.assertEqual(calls, [], "rotation guard must not restart")
        self.assertNotIn("would restart", self.world.main_log().lower())
        # the skip is logged, naming both models
        self.assertIn("restart skipped: probe OK on fallback/model-y", self.world.main_log())
        self.assertIn("rotation needed, not restart", self.world.main_log())
        # a model_rotation_needed notification was attempted
        texts = self.world.notifications()
        self.assertTrue(any("model rotation needed" in t.lower() for t in texts), texts)

    def test_default_model_recovery_still_restarts(self):
        # the mirror case: the DEFAULT model answered -> the old behaviour
        # holds (would-restart notification, still no systemctl in dry-run)
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL),
            L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("would restart", self.world.main_log().lower())
        self.assertNotIn("restart skipped", self.world.main_log())

    def test_silence_phase_non_default_recovery_skips_round(self):
        """Defect F, same guard on the recovery path.

        Silence is enforced, then probes answer only via a non-default model:
        the operator must be told rotation is needed and NO restart round may
        fire. Silence stays enforced (never lifted) because the household's
        model is still walled.
        """
        self.world.write_log("default", "agent", [])
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL),
            L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        # run 1: BOTH models fail -> silence enforced. The multi-model probe
        # sends one request per target, so each target needs its own queued
        # response (a single 429 would leave the fallback to the mock's
        # default success and no silence would ever be enforced).
        self.world.queue_curl({"stdout": '{"status": 429}', "exit": 22})
        self.world.queue_curl({"stdout": '{"status": 429}', "exit": 22})
        self.world.run({"DRY_RUN": "1",
                        "WATCHDOG_FALLBACK_MODEL": "fallback/model-y"})
        st = json.loads((self.world.state / "silence.json").read_text())
        self.assertTrue(st["enforced"])
        # forge past the quiet window and the sparse spacing
        st["since"] = time.time() - 400
        st["last_probe"] = time.time() - 400
        (self.world.state / "silence.json").write_text(json.dumps(st))
        # run 2: primary still walled, fallback answers -> recovery on a
        # NON-default model: guard must skip the restart round
        self.world.queue_curl(
            {"stdout": '{"status": 429, "title": "Too Many Requests"}', "exit": 22})
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        proc = self.world.run({"DRY_RUN": "1",
                               "WATCHDOG_FALLBACK_MODEL": "fallback/model-y"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertNotIn("would restart", self.world.main_log().lower())
        self.assertNotIn("restart round done", self.world.main_log())
        self.assertEqual(self.world.read_log_lines(self.world.systemctl_log), [])
        self.assertIn("restart round skipped, rotation needed", self.world.main_log())
        self.assertTrue(any("recovered on fallback/model-y" in t
                            for t in self.world.notifications()),
                        self.world.notifications())
        # silence must STAY enforced: the household model never recovered
        st = json.loads((self.world.state / "silence.json").read_text())
        self.assertTrue(st["enforced"], "silence must not lift on a foreign model")


# ---- defect D / A7: DRY_RUN blocks every systemctl on every decision path ----


class TestDryRunBlocksAllPaths(unittest.TestCase):
    """A7: no systemctl in argv on ANY decision path while DRY_RUN=1.

    Each scenario is a distinct branch that could reach a restart; every one
    is checked against the mock's argv log.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = FakeWorld(Path(self._tmp.name))

    def _assert_no_systemctl(self):
        calls = self.world.read_log_lines(self.world.systemctl_log)
        self.assertEqual(calls, [], f"systemctl ran under DRY_RUN: {calls}")

    def test_no_systemctl_on_probe_ok_restart_path(self):
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})
        self._assert_no_systemctl()

    def test_no_systemctl_on_probe_fail_silence_path(self):
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"status": 429}', "exit": 22})
        self.world.run({"DRY_RUN": "1"})
        self._assert_no_systemctl()

    def test_no_systemctl_on_recovery_restart_path(self):
        """The recovery round (2 consecutive OK probes) is the most tempting
        path to leak a live restart. Silence then recovery, all under DRY_RUN."""
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"status": 429}', "exit": 22})
        self.world.run({"DRY_RUN": "1"})
        st = json.loads((self.world.state / "silence.json").read_text())
        st["since"] = time.time() - 400
        st["last_probe"] = time.time() - 400
        (self.world.state / "silence.json").write_text(json.dumps(st))
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})  # OK #1
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})  # OK #2 -> would restart
        self._assert_no_systemctl()
        self.assertIn("would restart", self.world.main_log().lower())

    def test_no_systemctl_on_cooldown_and_cap_paths(self):
        """Both blocked branches still try to notify; neither may restart."""
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})
        # second cycle inside the 30-min cooldown: cooldown branch
        self.world.write_log("default", "errors", [
            L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-10, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})
        self._assert_no_systemctl()

    def test_no_systemctl_on_class_floor_suppression_path(self):
        """A delivery suppressed by the class floor must not systemctl either."""
        self.world.write_log("default", "errors", [
            L(-300, "WARNING", LINE_TERMINAL_FAIL), L(-240, "WARNING", LINE_TERMINAL_FAIL),
            L(-30, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "0"})  # live: perform the restart round
        live_calls = len(self.world.read_log_lines(self.world.systemctl_log))
        self.assertEqual(live_calls, 1)
        # now dry-run again within the floor: nothing may execute
        self.world.write_log("default", "errors", [
            L(-30, "WARNING", LINE_TERMINAL_FAIL), L(-10, "WARNING", LINE_TERMINAL_FAIL)])
        self.world.queue_curl({"stdout": '{"choices":[{"message":{"content":"ok"}}]}', "exit": 0})
        self.world.run({"DRY_RUN": "1"})
        self.assertEqual(len(self.world.read_log_lines(self.world.systemctl_log)),
                         live_calls, "DRY_RUN must add no systemctl calls")


if __name__ == "__main__":
    unittest.main(verbosity=2)
