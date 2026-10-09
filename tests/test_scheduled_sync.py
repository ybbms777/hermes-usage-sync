#!/usr/bin/env python3
"""tools/scheduled_sync.py 的契约测试：**成功必须静默**（否则计划任务/cron 会发噪音）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools" / "scheduled_sync.py"

spec = importlib.util.spec_from_file_location("hermes_usage_sync", ROOT / "hermes_usage_sync.py")
sync = importlib.util.module_from_spec(spec)
sys.modules["hermes_usage_sync"] = sync
spec.loader.exec_module(sync)

sys.path.insert(0, str(ROOT / "tests"))
from test_sync import CC_SCHEMA, HERMES_SCHEMA  # noqa: E402  复用同一套最小 schema


@contextmanager
def rw(path):
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class TestScheduledWrapper(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.hermes = base / "hermes" / "state.db"
        self.hermes.parent.mkdir(parents=True, exist_ok=True)
        self.cc = base / "cc-switch" / "cc-switch.db"
        self.cc.parent.mkdir(parents=True, exist_ok=True)
        self.state = base / "state" / "sync-state.db"
        self.log = base / "logs" / "sync.log"
        with rw(self.hermes) as c:
            c.executescript(HERMES_SCHEMA)
            c.execute(
                "INSERT INTO session_model_usage (session_id, model, billing_provider,"
                " billing_base_url, billing_mode, task, api_call_count, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                " estimated_cost_usd, actual_cost_usd, cost_status, first_seen, last_seen)"
                " VALUES ('s1','deepseek-flash','deepseek','https://api.example.com/v1','','',"
                " 1, 100, 50, 1000, 0, 7, 0.001, 0, 'estimated', 1.0, 1800000000.0)")
        with rw(self.cc) as c:
            c.executescript(CC_SCHEMA)

    def tearDown(self):
        self._tmp.cleanup()

    def run_wrapper(self, *extra):
        return subprocess.run(
            [sys.executable, str(WRAPPER), "--log", str(self.log), "--"] + list(extra),
            capture_output=True, text=True, errors="replace",
        )

    def args(self):
        return ["--hermes-db", str(self.hermes), "--cc-switch-db", str(self.cc),
                "--state-db", str(self.state)]

    def test_success_is_silent_and_logged(self):
        proc = self.run_wrapper(*self.args())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")          # ← 关键：成功不发声
        line = self.log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("inserted=1", line)
        self.assertIn("mode=backfill", line)
        with rw(self.cc) as c:
            rows = c.execute("SELECT COUNT(*) FROM proxy_request_logs").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_noop_run_is_silent(self):
        self.run_wrapper(*self.args())
        proc = self.run_wrapper(*self.args())      # 第二次没有新用量
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("inserted=0", self.log.read_text(encoding="utf-8"))

    def test_failure_prints_one_line_and_keeps_exit_code(self):
        proc = self.run_wrapper(*self.args(), "--cc-switch-db", str(self.cc) + ".missing")
        detail = "rc=%r stdout=%r stderr=%r" % (proc.returncode, proc.stdout, proc.stderr)
        self.assertEqual(proc.returncode, sync.EXIT_NO_CC_DB, detail)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1, detail)
        self.assertIn("失败", proc.stdout, detail)
        self.assertIn("exit=%d" % sync.EXIT_NO_CC_DB, self.log.read_text(encoding="utf-8"))

    def test_log_rotation_caps_size(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("x" * (2 * 1024 * 1024) + "\n", encoding="utf-8")
        self.run_wrapper(*self.args())
        self.assertLess(self.log.stat().st_size, 1024 * 1024)

    def test_failure_line_survives_narrow_console_encoding(self):
        """回归（windows CI 抓到过）：包装器自己也要处理窄编码，否则失败路径变 traceback。

        用 PYTHONIOENCODING 复现 cp1252 环境，任何平台都能跑这条用例。
        """
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        proc = subprocess.run(
            [sys.executable, str(WRAPPER), "--log", str(self.log), "--",
             *self.args(), "--cc-switch-db", str(self.cc) + ".missing"],
            capture_output=True, text=True, errors="replace", env=env,
        )
        detail = "rc=%r stdout=%r stderr=%r" % (proc.returncode, proc.stdout, proc.stderr)
        self.assertEqual(proc.returncode, sync.EXIT_NO_CC_DB, detail)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1, detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
