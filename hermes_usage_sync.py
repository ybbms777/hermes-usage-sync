#!/usr/bin/env python3
"""hermes-usage-sync —— 把 Hermes Agent 的 token 用量同步进 CC Switch 的「用量统计」看板。

背景
----
CC Switch（farion1231/cc-switch）的用量看板原生支持 Claude / Codex / Gemini /
OpenCode / Grok / MiniMax Code / Pi 的会话日志导入，**没有 Hermes Agent**：

* 上游曾加过 Hermes 用量跟踪（commit f061b777，2026-04-29），第二天被
  `chore(usage): drop Hermes Agent tracking integration`（518d945e）撤掉；
* PR #6120（feat/hermes-session-usage）至今 open，未进入任何发布版；
* v4.0.5 的 `src-tauri/src/services/` 里没有 `session_usage_hermes.rs`。

Hermes 侧的用量数据本身是完整且准确的：`state.db` 的 `session_model_usage`
表按 `(session_id, model, billing_provider, billing_base_url, billing_mode, task)`
分行累计 api 调用数、token 数与成本。本工具把这张表的**累计快照**转成增量，
写入 CC Switch 的 `proxy_request_logs`（`app_type='hermes'`,
`data_source='hermes_session'`），看板即可显示 Hermes 的用量。

写入口径（照抄 CC Switch v4.0.5 自己的会话导入器的做法，见
`src-tauri/src/services/session_usage_codex.rs`）
--------------------------------------------------------------------
* `provider_id  = '_hermes_session'`   `app_type = 'hermes'`
* `provider_type= 'hermes_session'`    `data_source = 'hermes_session'`
* `model = request_model = pricing_model = <Hermes 的模型名>`
* `input_tokens` 是**新增输入**（不含缓存命中）→ `input_token_semantics = 2`
  （FRESH）。CC Switch 的 `CACHE_INCLUSIVE_APP_TYPES` 只含 codex/gemini/
  grokbuild，hermes 不在其中，所以它不会再做任何减法。
* `cache_creation_tokens = Hermes cache_write_tokens`（语义一致）
* `status_code = 200`：Herome 的计数器只在**拿到了 usage 的 API 调用**上累加，
  失败调用（429/5xx）不进这张表；这也是 CC Switch 自家导入器的取值。
  可用 `--status-code` 改成 0（表示“未知”，但看板成功率会显示 0%）。
* `latency_ms = 0`、`first_token_ms = NULL`：聚合源没有计时。0 表示“无计时”而不
  是“0 毫秒”，和 CC Switch 自家 Codex 导入器一致；因为 `first_token_ms` 为
  NULL 且 `latency_ms < 1000`，这些行不会进入看板的「速度」统计（
  `speed_eligible_sql` / `speed_estimate_eligible_sql`），不会污染速度列。
* `input_cost_usd` 等组件成本：CC Switch 的 `model_pricing` 表能匹配到定价时按
  它算（与它自家导入器同源），否则为 0；`total_cost_usd` 优先用 Hermes 报的成本
  （`estimated_cost_usd` / `actual_cost_usd` 的增量），因为它们来自真实计费口径。
* 时间轴：聚合快照没有逐请求时间，行的 `created_at` 取该行 `last_seen`（Hermes
  最后一次更新该累计行的时刻），因此看板趋势反映的是**同步窗口**，不是精确的
  逐请求时间。越早的数据 CC Switch 会按自己的策略（默认 30 天）折叠进
  `usage_daily_rollups`，无需本工具干预。

安全契约
--------
1. Hermes `state.db` **只读**（`mode=ro` + `PRAGMA query_only=ON`）。
2. 只写 CC Switch 库与自己的状态库；绝不 UPDATE/DELETE 别人的行，唯一的删除是
   `--purge`（只删 `data_source='hermes_session'` 的行）。
3. 幂等：`request_id` 由「累计计数器的完整快照」派生，重复跑、崩溃重跑、状态库
   丢失后重跑都不会双计（`INSERT OR IGNORE`）。
4. 不联网、不读凭据、不写日志里的会话内容。

退出码
------
0 成功（含 0 增量）      10 Hermes state.db 不存在
11 Hermes 表结构不兼容   20 CC Switch 库不存在
21 CC Switch 表结构不兼容 30 写入失败
40 状态库丢失但 CC Switch 已有本工具写入的行（拒绝回填，防双计）
42 需要确认（`--check` 发现可疑状态）

用法
----
    python hermes_usage_sync.py --check          # 体检，不写入
    python hermes_usage_sync.py --dry-run -v     # 试运行，打印将写入的行
    python hermes_usage_sync.py -v               # 正式同步（幂等）
    python hermes_usage_sync.py --report         # 两侧总量对账
    python hermes_usage_sync.py --purge          # 删除本工具写入的所有行
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

__version__ = "1.0.0"

# ---------------------------------------------------------------- 常量（写入口径）

APP_TYPE = "hermes"
DATA_SOURCE = "hermes_session"
PROVIDER_ID = "_hermes_session"
PROVIDER_NAME = "Hermes Agent"
PROVIDER_TYPE = "hermes_session"

#: CC Switch 的 `input_token_semantics`：0=LEGACY, 1=TOTAL(含缓存), 2=FRESH(新增输入)
INPUT_TOKEN_SEMANTICS_FRESH = 2

#: 快速失败/重试参数（CC Switch 用非 WAL 的 delete journal，写时要等锁）
DB_BUSY_TIMEOUT_MS = 30_000

#: 行唯一键：session_model_usage 的一行 = 这六列的组合
KEY_COLS = (
    "session_id",
    "model",
    "billing_provider",
    "billing_base_url",
    "billing_mode",
    "task",
)

#: 累计计数器（做差分的列）
COUNTER_COLS = (
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
)

#: session_model_usage 里必须有、否则视为不兼容的列
REQUIRED_HERMES_COLS = set(KEY_COLS) | set(COUNTER_COLS) | {"cost_status", "last_seen"}

#: 本工具会写的 proxy_request_logs 列
LOG_COLUMNS = (
    "request_id", "provider_id", "app_type", "model", "request_model", "pricing_model",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "input_token_semantics",
    "input_cost_usd", "output_cost_usd", "cache_read_cost_usd", "cache_creation_cost_usd",
    "total_cost_usd", "latency_ms", "first_token_ms", "duration_ms", "status_code",
    "error_message", "session_id", "provider_type", "is_streaming", "cost_multiplier",
    "created_at", "data_source",
)

EXIT_OK = 0
EXIT_NO_HERMES_DB = 10
EXIT_HERMES_SCHEMA = 11
EXIT_NO_CC_DB = 20
EXIT_CC_SCHEMA = 21
EXIT_WRITE_FAILED = 30
EXIT_STATE_LOST = 40
EXIT_NEEDS_ATTENTION = 42


class SyncError(Exception):
    """带退出码的致命错误。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 路径解析


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def default_state_dir() -> Path:
    """状态库目录：Windows 走 %LOCALAPPDATA%，其它平台走 ~/.local/share。"""
    override = _env_path("HERMES_USAGE_SYNC_HOME")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes-usage-sync"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "hermes-usage-sync"
    return Path.home() / ".local" / "share" / "hermes-usage-sync"


def default_hermes_dirs() -> list[Path]:
    """候选的 Hermes 数据目录（默认 profile + 命名 profile）。"""
    roots: list[Path] = []
    env_home = _env_path("HERMES_HOME")
    if env_home:
        roots.append(env_home)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        roots.append(Path(base) / "hermes")
    roots.append(Path.home() / ".hermes")
    return roots


def hermes_db_paths(explicit: str | None) -> list[Path]:
    """要读取的 Hermes state.db 列表（含 profiles/<name>/state.db）。"""
    if explicit:
        p = Path(explicit).expanduser()
        return [p] if p.is_file() else []
    found: list[Path] = []
    for root in default_hermes_dirs():
        main = root / "state.db"
        if main.is_file():
            found.append(main)
        profiles = root / "profiles"
        if profiles.is_dir():
            for child in sorted(profiles.iterdir()):
                candidate = child / "state.db"
                if candidate.is_file():
                    found.append(candidate)
    # 去重（HERMES_HOME 与 %LOCALAPPDATA% 可能指向同一处）
    unique: list[Path] = []
    seen: set[str] = set()
    for p in found:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def default_cc_switch_db() -> Path | None:
    override = _env_path("CC_SWITCH_DB") or _env_path("CC_SWITCH_HOME")
    if override:
        p = Path(override).expanduser()
        return p / "cc-switch.db" if p.is_dir() else p
    home = Path.home() / ".cc-switch" / "cc-switch.db"
    return home if home.is_file() else None


def state_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = _env_path("HERMES_USAGE_SYNC_STATE_DB")
    return env if env else default_state_dir() / "sync-state.db"


# ---------------------------------------------------------------- SQLite 打开/工具


def sqlite_uri(path: Path, read_only: bool) -> str:
    """SQLite URI。Windows 路径要转成 file:C:/... 形式并转义特殊字符。"""
    posix = path.resolve().as_posix()
    if posix.startswith("//"):  # UNC
        posix = "/" + posix.lstrip("/")
    return "file:%s%s" % (quote(posix, safe="/:"), "?mode=ro" if read_only else "")


_OPEN_CONNS: list[sqlite3.Connection] = []


def _track(conn: sqlite3.Connection) -> sqlite3.Connection:
    _OPEN_CONNS.append(conn)
    return conn


def close_all() -> None:
    """关闭本次进程打开的所有连接（Windows 上不关会锁住文件）。"""
    while _OPEN_CONNS:
        try:
            _OPEN_CONNS.pop().close()
        except Exception:
            pass


def connect_readonly(path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_uri(path, True), uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return _track(conn)


def connect_rw(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    # isolation_level=None → 自己管事务（BEGIN IMMEDIATE/COMMIT），避免隐式事务里再 BEGIN
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    return _track(conn)


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.DatabaseError:
        return []


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- 状态库


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    key             TEXT PRIMARY KEY,
    profile         TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode    TEXT NOT NULL DEFAULT '',
    task            TEXT NOT NULL DEFAULT '',
    api_call_count  INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd    REAL NOT NULL DEFAULT 0,
    cost_status     TEXT NOT NULL DEFAULT '',
    source_last_seen REAL NOT NULL DEFAULT 0,
    established     INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    mode TEXT NOT NULL,
    inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);
"""


def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_rw(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_SCHEMA)
    conn.commit()
    return conn


def load_snapshots(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM snapshots"):
        rec = dict(row)
        out[rec["key"]] = rec
    return out


def key_of(profile: str, row: dict) -> str:
    parts = [profile] + [str(row.get(c, "") or "") for c in KEY_COLS]
    return "\x1f".join(parts)


def upsert_snapshot(conn: sqlite3.Connection, key: str, profile: str, row: dict,
                    established: bool) -> None:
    params = {
        "key": key,
        "profile": profile,
        "cost_status": row.get("cost_status") or "",
        "source_last_seen": float(row.get("last_seen") or 0.0),
        "established": 1 if established else 0,
        "updated_at": time.time(),
    }
    for c in KEY_COLS:
        params[c] = row.get(c) or ""
    for c in COUNTER_COLS:
        params[c] = float(row.get(c) or 0)
    cols = list(params.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join("%s=excluded.%s" % (c, c) for c in cols if c != "key")
    conn.execute(
        "INSERT INTO snapshots (%s) VALUES (%s) ON CONFLICT(key) DO UPDATE SET %s"
        % (", ".join(cols), placeholders, updates),
        [params[c] for c in cols],
    )


# ---------------------------------------------------------------- Hermes 侧读取


def read_hermes_rows(conn: sqlite3.Connection, profile: str) -> list[dict]:
    cols = table_columns(conn, "session_model_usage")
    if not cols:
        raise SyncError(EXIT_HERMES_SCHEMA, "Hermes 库里没有 session_model_usage 表")
    missing = REQUIRED_HERMES_COLS - set(cols)
    if missing:
        raise SyncError(
            EXIT_HERMES_SCHEMA,
            "Hermes session_model_usage 缺列: %s（版本过旧或结构变化）"
            % ", ".join(sorted(missing)),
        )
    sql = "SELECT %s FROM session_model_usage" % ", ".join(
        ["session_id"] + [c for c in KEY_COLS[1:]] + list(COUNTER_COLS)
        + ["cost_status", "last_seen"]
    )
    rows = []
    for r in conn.execute(sql):
        rec = {
            "profile": profile,
            "session_id": r[0],
            "model": r[1],
            "billing_provider": r[2],
            "billing_base_url": r[3],
            "billing_mode": r[4],
            "task": r[5],
        }
        for i, c in enumerate(COUNTER_COLS):
            rec[c] = r[6 + i]
        rec["cost_status"] = r[6 + len(COUNTER_COLS)]
        rec["last_seen"] = r[7 + len(COUNTER_COLS)]
        rows.append(rec)
    return rows


# ---------------------------------------------------------------- 定价（可选兜底）


def load_pricing(conn: sqlite3.Connection) -> dict[str, dict]:
    """CC Switch 的 model_pricing：{model_id: {input/output/cache_read/cache_creation}}（美元/百万）。"""
    cols = table_columns(conn, "model_pricing")
    if not cols:
        return {}
    wanted = {
        "input": ("input_cost_per_million", "input_price_per_million", "input_cost"),
        "output": ("output_cost_per_million", "output_price_per_million", "output_cost"),
        "cache_read": ("cache_read_cost_per_million", "cache_read_price_per_million"),
        "cache_creation": ("cache_creation_cost_per_million", "cache_creation_price_per_million",
                           "cache_write_cost_per_million"),
    }
    picked: dict[str, str | None] = {}
    for field, candidates in wanted.items():
        picked[field] = next((c for c in candidates if c in cols), None)
    id_col = next((c for c in ("model_id", "model", "name") if c in cols), None)
    if not id_col:
        return {}
    out: dict[str, dict] = {}
    select = ", ".join([id_col] + [picked[f] for f in ("input", "output", "cache_read", "cache_creation")
                                   if picked[f]])
    for row in conn.execute("SELECT %s FROM model_pricing" % select):
        model = str(row[0] or "")
        if not model:
            continue
        rec = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}
        idx = 1
        for f in ("input", "output", "cache_read", "cache_creation"):
            if picked[f]:
                try:
                    rec[f] = float(row[idx] or 0)
                except (TypeError, ValueError):
                    rec[f] = 0.0
                idx += 1
        out[model.lower()] = rec
    return out


def find_pricing(pricing: dict[str, dict], model: str) -> dict | None:
    if not model:
        return None
    key = model.lower().strip()
    if key in pricing:
        return pricing[key]
    # 去掉常见命名空间前缀再试一次
    for sep in ("/", ":"):
        if sep in key:
            tail = key.rsplit(sep, 1)[-1]
            if tail in pricing:
                return pricing[tail]
    return None


# ---------------------------------------------------------------- 增量与写行


def delta_row(prev: dict | None, cur: dict) -> dict | None:
    """返回 cur 相对 prev 的增量；无增量或计数器回退时返回 None。"""
    if prev is None:
        return {c: float(cur.get(c) or 0) for c in COUNTER_COLS}
    out = {}
    for c in COUNTER_COLS:
        d = float(cur.get(c) or 0) - float(prev.get(c) or 0)
        if d < 0:
            return None  # 计数器回退 → 视为重置，不做负数行
        out[c] = d
    if all(v == 0 for v in out.values()):
        return None
    return out


def request_id_for(profile: str, row: dict) -> str:
    """内容寻址：id 只由「键 + 累计计数器快照」决定 → 重复跑不会双计。"""
    payload = "|".join(
        [profile] + [str(row.get(c, "") or "") for c in KEY_COLS]
        + ["%r" % float(row.get(c) or 0) for c in COUNTER_COLS]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]
    return "%s:%s" % (DATA_SOURCE, digest)


def effective_cost(cur: dict, delta: dict, cost_source: str) -> float:
    """该增量的成本：auto = Hermes 报的值优先，缺失时用定价算。"""
    hermes_cost = float(delta.get("actual_cost_usd") or 0)
    if hermes_cost <= 0:
        hermes_cost = float(delta.get("estimated_cost_usd") or 0)
    if cost_source == "hermes":
        return hermes_cost
    if cost_source == "pricing":
        return -1.0  # 由定价决定
    return hermes_cost


def build_log_row(profile: str, cur: dict, delta: dict, pricing: dict | None,
                  cost_source: str, status_code: int, created_at: int) -> dict:
    multiplier = 1_000_000.0
    if pricing:
        in_c = delta["input_tokens"] / multiplier * pricing["input"]
        out_c = delta["output_tokens"] / multiplier * pricing["output"]
        cr_c = delta["cache_read_tokens"] / multiplier * pricing["cache_read"]
        cc_c = delta["cache_write_tokens"] / multiplier * pricing["cache_creation"]
    else:
        in_c = out_c = cr_c = cc_c = 0.0
    pricing_total = in_c + out_c + cr_c + cc_c
    hermes_total = effective_cost(cur, delta, cost_source)
    if hermes_total < 0:                       # --cost-source pricing
        total = pricing_total
    elif hermes_total > 0:                     # Hermes 报的值优先
        total = hermes_total
    else:                                      # Hermes 没报成本 → 用定价兜底
        total = pricing_total
    return {
        "request_id": request_id_for(profile, cur),
        "provider_id": PROVIDER_ID,
        "app_type": APP_TYPE,
        "model": cur.get("model") or "unknown",
        "request_model": cur.get("model") or "unknown",
        "pricing_model": cur.get("model") or "unknown",
        "input_tokens": int(delta["input_tokens"]),
        "output_tokens": int(delta["output_tokens"]),
        "cache_read_tokens": int(delta["cache_read_tokens"]),
        "cache_creation_tokens": int(delta["cache_write_tokens"]),
        "input_token_semantics": INPUT_TOKEN_SEMANTICS_FRESH,
        "input_cost_usd": "%.8f" % in_c,
        "output_cost_usd": "%.8f" % out_c,
        "cache_read_cost_usd": "%.8f" % cr_c,
        "cache_creation_cost_usd": "%.8f" % cc_c,
        "total_cost_usd": "%.8f" % total,
        "latency_ms": 0,
        "first_token_ms": None,
        "duration_ms": None,
        "status_code": int(status_code),
        "error_message": None,
        "session_id": cur.get("session_id"),
        "provider_type": PROVIDER_TYPE,
        "is_streaming": 1,
        "cost_multiplier": "1.0",
        "created_at": int(created_at),
        "data_source": DATA_SOURCE,
    }


def insert_log_rows(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """INSERT OR IGNORE；返回 (写入数, 重复跳过数)。"""
    if not rows:
        return 0, 0
    cols = [c for c in LOG_COLUMNS if c in table_columns(conn, "proxy_request_logs")]
    sql = "INSERT OR IGNORE INTO proxy_request_logs (%s) VALUES (%s)" % (
        ", ".join(cols), ", ".join("?" for _ in cols))
    inserted = duplicates = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.cursor()
        for row in rows:
            cur.execute(sql, [row.get(c) for c in cols])
            if cur.rowcount > 0:
                inserted += 1
            else:
                duplicates += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return inserted, duplicates


def register_provider(conn: sqlite3.Connection) -> bool:
    """可选：在 providers 表登记合成 provider，让看板「数据来源」显示 Hermes Agent。

    `meta.liveConfigManaged=false` 是关键：CC Switch 的
    `sync_all_providers_to_live()` 会跳过这种「仅存在于数据库」的 provider，
    因此它不会被写进 ~/.hermes/config.yaml 的 custom_providers。
    （证据：v4.0.5 src-tauri/src/services/provider/live.rs 的
    `sync_all_providers_to_live` 与 src-tauri/src/provider.rs 的
    `#[serde(rename = "liveConfigManaged")]`。）
    """
    if not table_exists(conn, "providers"):
        return False
    cols = table_columns(conn, "providers")
    values = {
        "id": PROVIDER_ID,
        "app_type": APP_TYPE,
        "name": PROVIDER_NAME,
        "settings_config": "{}",
        "category": "session",
        "created_at": int(time.time()),
        "sort_index": 999,
        "notes": "synthetic provider written by hermes-usage-sync (db-only, not live-managed)",
        "meta": json.dumps({"liveConfigManaged": False}),
        "is_current": 0,
        "in_failover_queue": 0,
        "cost_multiplier": "1.0",
        "provider_type": "session",
    }
    use = [c for c in values if c in cols]
    conn.execute(
        "INSERT OR IGNORE INTO providers (%s) VALUES (%s)"
        % (", ".join(use), ", ".join("?" for _ in use)),
        [values[c] for c in use],
    )
    conn.commit()
    return True


# ---------------------------------------------------------------- 统计/对账


def hermes_totals(conns: list[tuple[str, sqlite3.Connection]]) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0, "api_call_count": 0,
             "cost_usd": 0.0, "rows": 0, "sessions": 0}
    for _profile, conn in conns:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(api_call_count),0), COALESCE(SUM(input_tokens),0),"
            " COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_tokens),0),"
            " COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(reasoning_tokens),0),"
            " COALESCE(SUM(CASE WHEN actual_cost_usd > 0 THEN actual_cost_usd"
            "  ELSE estimated_cost_usd END),0) FROM session_model_usage"
        ).fetchone()
        total["rows"] += row[0]
        total["api_call_count"] += row[1]
        total["input_tokens"] += row[2]
        total["output_tokens"] += row[3]
        total["cache_read_tokens"] += row[4]
        total["cache_write_tokens"] += row[5]
        total["reasoning_tokens"] += row[6]
        total["cost_usd"] += float(row[7] or 0)
        total["sessions"] += conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM session_model_usage").fetchone()[0]
    return total


def cc_totals(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
        " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens),0),"
        " COALESCE(SUM(CAST(total_cost_usd AS REAL)),0)"
        " FROM proxy_request_logs WHERE data_source = ?", (DATA_SOURCE,)
    ).fetchone()
    return {"rows": row[0], "input_tokens": row[1], "output_tokens": row[2],
            "cache_read_tokens": row[3], "cache_write_tokens": row[4],
            "cost_usd": float(row[5] or 0)}


# ---------------------------------------------------------------- 主流程


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hermes_usage_sync.py",
        description="把 Hermes Agent 的 token 用量同步进 CC Switch 的用量看板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--hermes-db", help="Hermes state.db（默认自动发现，含 profiles/*）")
    p.add_argument("--cc-switch-db", help="CC Switch 的 cc-switch.db（默认 ~/.cc-switch）")
    p.add_argument("--state-db", help="本工具的状态库（默认用户数据目录）")
    p.add_argument("--check", action="store_true", help="只体检，不写入")
    p.add_argument("--dry-run", action="store_true", help="试运行，只打印将要写入的行")
    p.add_argument("--report", action="store_true", help="两侧总量对账后退出")
    p.add_argument("--purge", action="store_true",
                   help="删除本工具写入的所有行（只删 data_source='hermes_session'）")
    p.add_argument("--first-run", choices=("backfill", "baseline"), default="backfill",
                   help="首次发现的键：backfill=把累计量写成一行(默认)，baseline=只记基线不回填")
    p.add_argument("--cost-source", choices=("auto", "hermes", "pricing"), default="auto",
                   help="总成本来源：auto=Hermes 优先(默认)，hermes=只用 Hermes，pricing=只按 CC Switch 定价算")
    p.add_argument("--status-code", type=int, default=200,
                   help="写入行的 HTTP 状态码（默认 200；聚合源没有逐请求状态）")
    p.add_argument("--register-provider", action="store_true",
                   help="在 providers 表登记 'Hermes Agent'（让「数据来源」下拉显示它；注意见 README）")
    p.add_argument("--unregister-provider", action="store_true", help="删除上面那条合成 provider")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("-v", "--verbose", action="store_true", help="打印细节")
    p.add_argument("--version", action="version", version="hermes-usage-sync %s" % __version__)
    return p.parse_args(argv)


def log(args: argparse.Namespace, message: str) -> None:
    if args.verbose and not args.json:
        print(message)


def emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


def open_hermes_conns(paths: list[Path]) -> list[tuple[str, sqlite3.Connection]]:
    out: list[tuple[str, sqlite3.Connection]] = []
    for path in paths:
        profile = "" if path.parent.name != "profiles" else path.parent.parent.name
        if path.parent.name == "profiles":       # .../profiles/<name>/state.db
            profile = path.parent.name
        elif path.parent.name == "hermes":       # 默认 profile
            profile = "default"
        else:
            profile = path.parent.name or "default"
        out.append((profile, connect_readonly(path)))
    return out


def annotate_profile(conns: list[tuple[str, sqlite3.Connection]]) -> list[tuple[str, sqlite3.Connection]]:
    """同一 profile 名重复时加序号，保证键稳定可区分。"""
    seen: dict[str, int] = {}
    out = []
    for profile, conn in conns:
        n = seen.get(profile, 0)
        seen[profile] = n + 1
        out.append((profile if n == 0 else "%s#%d" % (profile, n), conn))
    return out


def run(args: argparse.Namespace) -> int:
    started = time.time()
    cc_path = Path(args.cc_switch_db).expanduser() if args.cc_switch_db else default_cc_switch_db()
    if cc_path is None or not cc_path.is_file():
        raise SyncError(EXIT_NO_CC_DB, "找不到 CC Switch 的 cc-switch.db（用 --cc-switch-db 指定）")
    hermes_paths = hermes_db_paths(args.hermes_db)
    if not hermes_paths:
        raise SyncError(EXIT_NO_HERMES_DB, "找不到 Hermes state.db（用 --hermes-db 指定）")
    state_path = state_db_path(args.state_db)

    cc = connect_rw(cc_path)
    if not table_exists(cc, "proxy_request_logs"):
        raise SyncError(EXIT_CC_SCHEMA, "CC Switch 库里没有 proxy_request_logs 表")
    hermes_conns = annotate_profile(open_hermes_conns(hermes_paths))

    if args.purge:
        cc.execute("BEGIN IMMEDIATE")
        deleted = cc.execute("DELETE FROM proxy_request_logs WHERE data_source = ?",
                             (DATA_SOURCE,)).rowcount
        cc.execute("COMMIT")
        emit(args, {"purged": deleted},
             "已删除 %d 条 data_source='%s' 的行" % (deleted, DATA_SOURCE))
        return EXIT_OK

    if args.unregister_provider:
        cc.execute("BEGIN IMMEDIATE")
        cc.execute("DELETE FROM providers WHERE id = ? AND app_type = ?", (PROVIDER_ID, APP_TYPE))
        cc.execute("COMMIT")
        emit(args, {"unregistered": True}, "已删除合成 provider '%s'" % PROVIDER_ID)
        return EXIT_OK

    if args.report:
        h = hermes_totals(hermes_conns)
        c = cc_totals(cc)
        emit(args, {"hermes": h, "ccSwitch": c}, "\n".join([
            "Hermes 侧（累计快照）: %d 行 / %d 次调用 / %d 个会话" %
            (h["rows"], h["api_call_count"], h["sessions"]),
            "  新增输入 %d / 输出 %d / 缓存命中 %d / 缓存写入 %d / 推理 %d" %
            (h["input_tokens"], h["output_tokens"], h["cache_read_tokens"],
             h["cache_write_tokens"], h["reasoning_tokens"]),
            "  成本 $%.4f" % h["cost_usd"],
            "CC Switch 侧（data_source='%s'，即本工具已写入的增量合计）: %d 行 / $%.4f" %
            (DATA_SOURCE, c["rows"], c["cost_usd"]),
            "  新增输入 %d / 输出 %d / 缓存命中 %d / 缓存写入 %d" %
            (c["input_tokens"], c["output_tokens"], c["cache_read_tokens"],
             c["cache_write_tokens"]),
        ]))
        return EXIT_OK

    # ---- 读 Hermes 行
    current: dict[str, dict] = {}
    rows_total = 0
    for profile, conn in hermes_conns:
        rows = read_hermes_rows(conn, profile)
        rows_total += len(rows)
        for row in rows:
            current[key_of(profile, row)] = row
    log(args, "Hermes: %d 个 profile、%d 行累计快照" % (len(hermes_conns), rows_total))

    state = open_state(state_path)
    snapshots = load_snapshots(state)
    cc_has_ours = cc_totals(cc)["rows"] > 0
    first_ever = not snapshots

    if args.check:
        problems = []
        if not snapshots:
            problems.append("状态库为空：本次将是首次同步（默认回填累计量）")
        if cc_has_ours and not snapshots:
            problems.append("CC Switch 已有本工具写入的行，但状态库为空 → 再回填会双计，"
                            "请先备份并从 --report 对账")
        if len(current) < len(snapshots):
            problems.append("Hermes 行数少于上次快照（有会话/模型行被删除）")
        payload = {
            "hermesDb": [str(p) for p in hermes_paths],
            "hermesRows": rows_total,
            "ccSwitchDb": str(cc_path),
            "ccSwitchRows": cc_totals(cc)["rows"],
            "stateDb": str(state_path),
            "snapshots": len(snapshots),
            "problems": problems,
        }
        emit(args, payload, "\n".join([
            "Hermes 库: %s" % ", ".join(str(p) for p in hermes_paths),
            "Hermes 累计行: %d" % rows_total,
            "CC Switch 库: %s" % cc_path,
            "本工具已写入: %d 行" % payload["ccSwitchRows"],
            "状态库: %s (%d 个键)" % (state_path, len(snapshots)),
            "问题: %s" % ("; ".join(problems) if problems else "无"),
        ]))
        return EXIT_NEEDS_ATTENTION if (cc_has_ours and not snapshots) else EXIT_OK

    if first_ever and cc_has_ours:
        raise SyncError(
            EXIT_STATE_LOST,
            "状态库丢失但 CC Switch 里已有本工具写入的 %d 行 → 拒绝回填（防双计）。"
            "确认要用当前累计量重建基线时，先 --purge 再跑一次。" % cc_totals(cc)["rows"],
        )

    pricing = load_pricing(cc)
    now = int(time.time())
    planned: list[dict] = []
    modes: dict[str, int] = {"backfill": 0, "delta": 0, "skip": 0, "reset": 0}
    for key, row in current.items():
        prev = snapshots.get(key)
        is_new = prev is None or not prev.get("established")
        if is_new:
            if args.first_run == "baseline":
                modes["skip"] += 1
                if not args.dry_run:
                    upsert_snapshot(state, key, row["profile"], row, True)
                continue
            delta = {c: float(row.get(c) or 0) for c in COUNTER_COLS}
            modes["backfill"] += 1
        else:
            delta = delta_row(prev, row)
            if delta is None:
                if any(float(row.get(c) or 0) < float(prev.get(c) or 0) for c in COUNTER_COLS):
                    modes["reset"] += 1
                    log(args, "计数器回退（会话被重置/回滚）：%s %s/%s" %
                        (row["session_id"], row["model"], row["task"] or "<main>"))
                    if not args.dry_run:
                        upsert_snapshot(state, key, row["profile"], row, True)
                else:
                    modes["skip"] += 1
                    if not args.dry_run:
                        upsert_snapshot(state, key, row["profile"], row, True)
                continue
            modes["delta"] += 1
        if all(float(delta[c] or 0) == 0 for c in COUNTER_COLS):
            modes["skip"] += 1
            if not args.dry_run:
                upsert_snapshot(state, key, row["profile"], row, True)
            continue
        last_seen = float(row.get("last_seen") or 0) or now
        created_at = min(int(last_seen), now + 60)
        planned.append(build_log_row(row["profile"], row, delta, find_pricing(pricing, row.get("model")),
                                     args.cost_source, args.status_code, created_at))
        if not args.dry_run:
            upsert_snapshot(state, key, row["profile"], row, True)

    mode = "backfill+delta" if modes["backfill"] and modes["delta"] else (
        "backfill" if modes["backfill"] else "delta")
    inserted = duplicates = 0
    if not args.dry_run and planned:
        if first_ever:
            # 第一次写入前备份一次 CC Switch 库（不覆盖已有备份）
            backup = cc_path.with_suffix(cc_path.suffix + ".hermes-usage-sync.bak")
            if not backup.exists():
                shutil.copy2(cc_path, backup)
                log(args, "已备份 CC Switch 库到 %s" % backup)
        inserted, duplicates = insert_log_rows(cc, planned)
        state.execute(
            "INSERT INTO runs (started_at, finished_at, mode, inserted, duplicates, skipped, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (started, time.time(), mode, inserted, duplicates, modes["skip"], ""),
        )
        state.commit()
        if args.register_provider:
            register_provider(cc)

    if args.dry_run:
        for row in planned[:20]:
            print(json.dumps(row, ensure_ascii=False))
        if len(planned) > 20:
            print("... 共 %d 行" % len(planned))

    payload = {
        "mode": mode,
        "hermesRows": rows_total,
        "planned": len(planned),
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": modes["skip"],
        "resets": modes["reset"],
        "dryRun": bool(args.dry_run),
        "stateDb": str(state_path),
        "ccSwitchDb": str(cc_path),
    }
    emit(args, payload, (
        "同步完成: 计划 %d 行, 写入 %d 行, 重复跳过 %d, 无变化 %d, 计数器重置 %d"
        "（模式 %s）%s"
    ) % (len(planned), inserted, duplicates, modes["skip"], modes["reset"], mode,
         "  [dry-run，未写库]" if args.dry_run else ""))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args)
    except SyncError as exc:
        payload = {"error": str(exc), "code": exc.code}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("ERROR[%d] %s" % (exc.code, exc), file=sys.stderr)
        return exc.code
    except sqlite3.DatabaseError as exc:
        payload = {"error": str(exc), "code": EXIT_WRITE_FAILED}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("ERROR[%d] 数据库错误: %s" % (EXIT_WRITE_FAILED, exc), file=sys.stderr)
        return EXIT_WRITE_FAILED
    finally:
        close_all()


if __name__ == "__main__":
    sys.exit(main())
