#!/usr/bin/env python3
"""hermes-usage-sync 的单元测试（标准库 unittest，无需第三方依赖）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_usage_sync", ROOT / "hermes_usage_sync.py")
sync = importlib.util.module_from_spec(spec)
sys.modules["hermes_usage_sync"] = sync
spec.loader.exec_module(sync)

# Hermes state.db 的最小真实结构（列名/语义取自 Hermes 0.21.x）
HERMES_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, started_at REAL);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
"""

# CC Switch cc-switch.db 的最小结构（列名取自 v4.0.5 的 schema.rs）
CC_SCHEMA = """
CREATE TABLE proxy_request_logs (
    request_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, app_type TEXT NOT NULL,
    model TEXT NOT NULL, request_model TEXT, pricing_model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    input_token_semantics INTEGER NOT NULL DEFAULT 0,
    input_cost_usd TEXT NOT NULL DEFAULT '0', output_cost_usd TEXT NOT NULL DEFAULT '0',
    cache_read_cost_usd TEXT NOT NULL DEFAULT '0', cache_creation_cost_usd TEXT NOT NULL DEFAULT '0',
    total_cost_usd TEXT NOT NULL DEFAULT '0', latency_ms INTEGER NOT NULL, first_token_ms INTEGER,
    duration_ms INTEGER, status_code INTEGER NOT NULL, error_message TEXT, session_id TEXT,
    provider_type TEXT, is_streaming INTEGER NOT NULL DEFAULT 0,
    cost_multiplier TEXT NOT NULL DEFAULT '1.0', created_at INTEGER NOT NULL,
    data_source TEXT NOT NULL DEFAULT 'proxy'
);
CREATE TABLE providers (
    id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL, settings_config TEXT NOT NULL,
    website_url TEXT, category TEXT, created_at INTEGER, sort_index INTEGER, notes TEXT,
    icon TEXT, icon_color TEXT, meta TEXT NOT NULL DEFAULT '{}', is_current BOOLEAN NOT NULL DEFAULT 0,
    in_failover_queue BOOLEAN NOT NULL DEFAULT 0, cost_multiplier TEXT NOT NULL DEFAULT '1.0',
    limit_daily_usd TEXT, limit_monthly_usd TEXT, provider_type TEXT, PRIMARY KEY (id, app_type)
);
CREATE TABLE model_pricing (
    model_id TEXT PRIMARY KEY, input_cost_per_million REAL DEFAULT 0,
    output_cost_per_million REAL DEFAULT 0, cache_read_cost_per_million REAL DEFAULT 0,
    cache_creation_cost_per_million REAL DEFAULT 0
);
"""


@contextlib.contextmanager
def rw_conn(path):
    """写连接：语句提交后关闭（`with sqlite3.connect(..)` 并不会关闭连接）。"""
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class Fixture:
    """临时目录里造一套 Hermes + CC Switch + 状态库，跑真实 CLI 入口。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.hermes = tmp / "hermes" / "state.db"
        self.hermes.parent.mkdir(parents=True, exist_ok=True)
        self.cc = tmp / "cc-switch" / "cc-switch.db"
        self.cc.parent.mkdir(parents=True, exist_ok=True)
        self.state = tmp / "state" / "sync-state.db"
        with rw_conn(self.hermes) as c:
            c.executescript(HERMES_SCHEMA)
        with rw_conn(self.cc) as c:
            c.executescript(CC_SCHEMA)

    # ---- 造数据
    def add_usage(self, session_id="s1", model="deepseek-flash", task="", *,
                  api_calls=1, input_tokens=100, output_tokens=50,
                  cache_read=1000, cache_write=0, reasoning=7,
                  est_cost=0.001, act_cost=0.0, provider="deepseek",
                  base_url="https://api.example.com/v1", mode="", last_seen=None):
        with rw_conn(self.hermes) as c:
            c.execute(
                "INSERT INTO session_model_usage (session_id, model, billing_provider,"
                " billing_base_url, billing_mode, task, api_call_count, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                " estimated_cost_usd, actual_cost_usd, cost_status, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id, model, billing_provider, billing_base_url,"
                " billing_mode, task) DO UPDATE SET"
                " api_call_count=excluded.api_call_count, input_tokens=excluded.input_tokens,"
                " output_tokens=excluded.output_tokens, cache_read_tokens=excluded.cache_read_tokens,"
                " cache_write_tokens=excluded.cache_write_tokens, reasoning_tokens=excluded.reasoning_tokens,"
                " estimated_cost_usd=excluded.estimated_cost_usd, actual_cost_usd=excluded.actual_cost_usd,"
                " last_seen=excluded.last_seen",
                (session_id, model, provider, base_url, mode, task, api_calls, input_tokens,
                 output_tokens, cache_read, cache_write, reasoning, est_cost, act_cost,
                 "actual" if act_cost else "estimated", 1.0, last_seen or 1_800_000_000.0),
            )
            c.execute("INSERT OR IGNORE INTO sessions (id, model, started_at) VALUES (?,?,?)",
                      (session_id, model, 1.0))

    # ---- 跑 CLI
    def run(self, *extra):
        argv = ["--hermes-db", str(self.hermes), "--cc-switch-db", str(self.cc),
                "--state-db", str(self.state)] + list(extra)
        return sync.main(argv)

    def cc_rows(self):
        with rw_conn(self.cc) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM proxy_request_logs ORDER BY rowid")]


class TestFirstRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_run_backfills_cumulative(self):
        self.fx.add_usage(api_calls=7, input_tokens=18441, output_tokens=2913,
                          cache_read=110976, reasoning=1003, est_cost=0.004846878)
        self.fx.add_usage(task="title_generation", api_calls=1, input_tokens=237,
                          output_tokens=9, cache_read=0, reasoning=0, est_cost=0.00004)
        self.assertEqual(self.fx.run("-v"), 0)
        rows = self.fx.cc_rows()
        self.assertEqual(len(rows), 2)
        total = sum(r["input_tokens"] for r in rows)
        self.assertEqual(total, 18441 + 237)

    def test_row_conventions_match_cc_switch_importers(self):
        self.fx.add_usage()
        self.fx.run()
        row = self.fx.cc_rows()[0]
        self.assertEqual(row["app_type"], "hermes")
        self.assertEqual(row["provider_id"], "_hermes_session")
        self.assertEqual(row["provider_type"], "hermes_session")
        self.assertEqual(row["data_source"], "hermes_session")
        self.assertEqual(row["status_code"], 200)
        self.assertEqual(row["latency_ms"], 0)
        self.assertIsNone(row["first_token_ms"])
        self.assertEqual(row["input_token_semantics"], 2)   # FRESH
        self.assertEqual(row["cache_creation_tokens"], 0)   # Hermes cache_write=0
        self.assertEqual(row["request_id"][:len("hermes_session:")], "hermes_session:")

    def test_cache_write_maps_to_cache_creation(self):
        self.fx.add_usage(cache_write=4321)
        self.fx.run()
        self.assertEqual(self.fx.cc_rows()[0]["cache_creation_tokens"], 4321)

    def test_baseline_mode_writes_nothing(self):
        self.fx.add_usage()
        self.assertEqual(self.fx.run("--first-run", "baseline"), 0)
        self.assertEqual(self.fx.cc_rows(), [])

    def test_dry_run_writes_nothing(self):
        self.fx.add_usage()
        self.assertEqual(self.fx.run("--dry-run"), 0)
        self.assertEqual(self.fx.cc_rows(), [])
        # 状态库也不应前进
        self.assertEqual(self.fx.run("--dry-run"), 0)

    def test_cost_prefers_hermes_value(self):
        self.fx.add_usage(est_cost=1.2345)
        self.fx.run()
        self.assertAlmostEqual(float(self.fx.cc_rows()[0]["total_cost_usd"]), 1.2345, places=6)

    def test_pricing_fallback_computes_components(self):
        with rw_conn(self.fx.cc) as c:
            c.execute("INSERT INTO model_pricing VALUES ('deepseek-flash', 0.28, 0.42, 0.028, 0)")
        self.fx.add_usage(input_tokens=1_000_000, output_tokens=1_000_000,
                          cache_read=1_000_000, est_cost=0.0)
        self.fx.run()
        row = self.fx.cc_rows()[0]
        self.assertAlmostEqual(float(row["input_cost_usd"]), 0.28, places=6)
        self.assertAlmostEqual(float(row["total_cost_usd"]), 0.28 + 0.42 + 0.028, places=6)


class TestDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_writes_only_delta(self):
        self.fx.add_usage(api_calls=1, input_tokens=100, output_tokens=10, cache_read=5)
        self.fx.run()
        self.fx.add_usage(api_calls=3, input_tokens=400, output_tokens=90, cache_read=205)
        self.assertEqual(self.fx.run(), 0)
        rows = self.fx.cc_rows()
        self.assertEqual(len(rows), 2)
        latest = rows[-1]      # cc_rows() 按 rowid（写入顺序）排序
        # 第二次只应写增量 300/80/200，加上首次的回填 100/10/5
        self.assertEqual(latest["input_tokens"], 300)
        self.assertEqual(latest["output_tokens"], 80)
        self.assertEqual(latest["cache_read_tokens"], 200)
        totals = (sum(r["input_tokens"] for r in rows),
                  sum(r["output_tokens"] for r in rows))
        self.assertEqual(totals, (400, 90))

    def test_rerun_without_new_usage_is_noop(self):
        self.fx.add_usage()
        self.fx.run()
        self.assertEqual(self.fx.run(), 0)
        self.assertEqual(len(self.fx.cc_rows()), 1)

    def test_replay_same_counters_same_request_id(self):
        self.fx.add_usage(input_tokens=123)
        self.fx.run()
        first_id = self.fx.cc_rows()[0]["request_id"]
        with rw_conn(self.fx.cc) as c:          # 模拟「重来一遍」
            c.execute("DELETE FROM proxy_request_logs")
        self.fx.state.unlink()                          # 状态库也丢了
        # 没有我们的行 → 允许重新建立基线；内容寻址的 request_id 保证与上次一致
        self.assertEqual(self.fx.run(), 0)
        self.assertEqual(self.fx.cc_rows()[0]["request_id"], first_id)

    def test_state_lost_while_cc_has_rows_refuses(self):
        self.fx.add_usage()
        self.fx.run()
        self.fx.state.unlink()
        self.assertEqual(self.fx.run(), sync.EXIT_STATE_LOST)

    def test_counter_reset_does_not_write_negative(self):
        self.fx.add_usage(input_tokens=1000, output_tokens=100)
        self.fx.run()
        self.fx.add_usage(input_tokens=10, output_tokens=2)   # 会话被重置
        self.assertEqual(self.fx.run(), 0)
        rows = self.fx.cc_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(r["input_tokens"] >= 0 for r in rows))

    def test_deleted_row_is_dropped_from_state(self):
        self.fx.add_usage(session_id="s1")
        self.fx.add_usage(session_id="s2", input_tokens=10)
        self.fx.run()
        with rw_conn(self.fx.hermes) as c:
            c.execute("DELETE FROM session_model_usage WHERE session_id='s2'")
        self.assertEqual(self.fx.run(), 0)
        self.assertEqual(len(self.fx.cc_rows()), 2)   # 只是不再跟踪，不删已写入的行


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_purge_removes_only_our_rows(self):
        self.fx.add_usage()
        self.fx.run()
        with rw_conn(self.fx.cc) as c:
            c.execute("INSERT INTO proxy_request_logs (request_id, provider_id, app_type, model,"
                      " input_tokens, output_tokens, latency_ms, status_code, created_at, data_source)"
                      " VALUES ('x', 'p', 'codex', 'm', 1, 1, 0, 200, 1, 'codex_session')")
        self.assertEqual(self.fx.run("--purge"), 0)
        remaining = self.fx.cc_rows()
        self.assertEqual([r["request_id"] for r in remaining], ["x"])

    def test_register_and_unregister_provider(self):
        self.fx.add_usage()
        self.fx.run("--register-provider")
        with rw_conn(self.fx.cc) as c:
            row = c.execute("SELECT name, app_type FROM providers WHERE id='_hermes_session'").fetchone()
        self.assertEqual(row, ("Hermes Agent", "hermes"))
        self.assertEqual(self.fx.run("--unregister-provider"), 0)
        with rw_conn(self.fx.cc) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM providers WHERE id='_hermes_session'").fetchone())

    def test_report_json(self):
        self.fx.add_usage(input_tokens=100, output_tokens=10)
        self.fx.run()
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.fx.run("--report", "--json"), 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["hermes"]["input_tokens"], 100)
        self.assertEqual(payload["ccSwitch"]["rows"], 1)

    def test_chinese_output_survives_narrow_console_encoding(self):
        """回归：cp1252 之类的窄编码控制台下，中文输出曾经直接抛 UnicodeEncodeError。"""
        self.fx.add_usage()
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
        argv = ["--hermes-db", str(self.fx.hermes), "--cc-switch-db", str(self.fx.cc),
                "--state-db", str(self.fx.state)]
        with contextlib.redirect_stdout(narrow):
            code = sync.main(argv)
            sync.main(argv + ["--check"])
            sync.main(argv + ["--json"])
            narrow.flush()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fx.cc_rows()), 1)

    def test_missing_hermes_db(self):
        Path(self.fx.hermes).unlink()
        self.assertEqual(self.fx.run(), sync.EXIT_NO_HERMES_DB)

    def test_incompatible_hermes_schema(self):
        with rw_conn(self.fx.hermes) as c:
            c.execute("DROP TABLE session_model_usage")
            c.execute("CREATE TABLE session_model_usage (session_id TEXT)")
        self.assertEqual(self.fx.run(), sync.EXIT_HERMES_SCHEMA)

    def test_missing_cc_switch_db(self):
        Path(self.fx.cc).unlink()
        self.assertEqual(self.fx.run(), sync.EXIT_NO_CC_DB)


if __name__ == "__main__":
    unittest.main(verbosity=2)
