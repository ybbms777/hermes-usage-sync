# hermes-usage-sync

把 **Hermes Agent** 的 token 用量同步进 **CC Switch** 的「用量统计」看板。

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

[CC Switch](https://github.com/farion1231/cc-switch) 的用量看板能汇总 Claude Code、Codex、
Gemini CLI、OpenCode、Grok Build、MiniMax Code、Pi 的 token 消耗，**但没有 Hermes Agent**。
本项目读 Hermes 本地数据库里的用量计数器，算出增量后写进 CC Switch 的统计库，让 Hermes 的
用量第一次出现在同一张看板上。

它是一个**单文件 Python 脚本**（只用标准库，Python 3.8+），不需要 CC Switch 或 Hermes
改一行代码，也不联网。

### 为什么官方没有这个功能

不是你的数据没生成，是 CC Switch 侧没有 Hermes 的解析器：

| 事实 | 证据 |
|---|---|
| 上游加过又撤掉 | commit `f061b777`（2026-04-29）`feat(usage): add Hermes Agent tracking`，次日 `518d945e` `chore(usage): drop Hermes Agent tracking integration` |
| 重新实现的 PR 未合并 | [PR #6120](https://github.com/farion1231/cc-switch/pull/6120)（`feat/hermes-session-usage`）仍 open |
| 发布版里没有解析器 | v4.0.5 的 `src-tauri/src/services/` 只有 `session_usage{,_codex,_gemini,_grokbuild,_mcode,_opencode,_pi}.rs`，没有 `session_usage_hermes.rs` |
| 看板里 Hermes 归零 | `proxy_request_logs` 的 `app_type` 只有 claude / codex / gemini / opencode / grokbuild / mcode / pi |

Hermes 侧的用量数据本身是完整的：`state.db` 的 `session_model_usage` 表按
`(session_id, model, billing_provider, billing_base_url, billing_mode, task)` 分行累计
api 调用数、token 数与成本，辅助任务（`title_generation` / `approval` / `background_review`
/ `compression` …）也各自成行。

### 数据流

```
Hermes Agent
  %LOCALAPPDATA%\hermes\state.db   （macOS/Linux: ~/.hermes/state.db，含 profiles/*/state.db）
  表 session_model_usage —— 每行是 (会话, 模型, 供应商, 任务) 的累计计数器
        │
        │  本脚本：读累计值 → 与上次快照做差 → 得到增量
        │  状态存在自己的小库（默认 %LOCALAPPDATA%\hermes-usage-sync\sync-state.db）
        ▼
CC Switch
  ~/.cc-switch/cc-switch.db
  表 proxy_request_logs —— app_type='hermes', data_source='hermes_session'
        │
        ▼
CC Switch 界面 → 用量统计 → 选「当天/本周/…」+ App 选「全部」
```

### 安装

```bash
git clone https://github.com/<you>/hermes-usage-sync.git
cd hermes-usage-sync
python hermes_usage_sync.py --check        # 体检：找到哪些库、有多少行、有什么问题
python hermes_usage_sync.py --dry-run -v   # 试运行：打印将要写入的行，不写库
python hermes_usage_sync.py                # 正式同步（幂等，可反复跑）
```

第一次写入前，脚本会自动把 CC Switch 的库备份成 `cc-switch.db.hermes-usage-sync.bak`。

建议每 10–30 分钟跑一次，用带静默包装的入口，成功时**不产生任何输出**（不会发通知/邮件），
只在真的失败时打印一行：

| 调度器 | 用法 |
|---|---|
| Windows 计划任务 | `schtasks /create /tn "hermes-usage-sync" /sc minute /mo 15 /tr "\"C:\Path\to\python.exe\" \"C:\Path\to\hermes-usage-sync\tools\scheduled_sync.py\""` |
| cron / launchd | `*/15 * * * * /usr/bin/python3 /path/to/hermes-usage-sync/tools/scheduled_sync.py` |
| Hermes 自身的 cron | `cronjob_manage(action="create", schedule="every 15m", no_agent=True, script="<launcher>.py")` —— 把定时器放进已经在后台跑的 Hermes 里，不必再挂系统计划任务 |

`tools/scheduled_sync.py` 每次运行会往 `<状态目录>/sync.log` 追加一行
（`inserted=… duplicates=… mode=…`），超过 1MB 自动截断；退出码 0 时 stdout 为空，
因此任务计划/cron 的"成功也发通知"问题不存在。

### 命令行

| 参数 | 说明 |
|---|---|
| `--check` | 只体检，不写入。发现「状态库丢失但 CC Switch 已有本工具的行」会以退出码 42 提示 |
| `--dry-run` | 打印将要写入的行（最多 20 行 + 总数），不写库、不推进状态 |
| `--report` | 两侧总量对账：Hermes 累计 vs 本工具已写入的增量合计 |
| `--purge` | 删除本工具写入的所有行（只删 `data_source='hermes_session'`） |
| `--first-run {backfill,baseline}` | 首次发现某个键时：`backfill`（默认）把累计量写成一行；`baseline` 只记基线不回填 |
| `--cost-source {auto,hermes,pricing}` | 总成本口径，默认 `auto`＝Hermes 报的值优先 |
| `--status-code N` | 写入行的状态码，默认 200（聚合源没有逐请求状态，见下） |
| `--register-provider` | 在 `providers` 表登记 `Hermes Agent`，让看板「数据来源」下拉显示它 |
| `--unregister-provider` | 删掉上面那条合成记录 |
| `--hermes-db/--cc-switch-db/--state-db` | 手动指定库路径（默认自动发现） |
| `--json` / `-v` | JSON 输出 / 详细日志 |

退出码：`0` 成功，`10` 找不到 Hermes 库，`11` Hermes 表结构不兼容，`20` 找不到 CC Switch 库，
`21` CC Switch 表结构不兼容，`30` 写入失败，`40` 拒绝回填（防双计），`42` 需要人工确认。

中文输出在窄编码控制台（Windows cp1252 等）上会自动降级成 `?` 而不是报
`UnicodeEncodeError`；想看到完整中文，设 `PYTHONIOENCODING=utf-8`，或在 Windows 上先
`chcp 65001`。

### 写入口径（为什么看板能正确显示）

每一条写入的行都照 CC Switch **自己的**会话导入器（`session_usage_codex.rs`）的写法：

* `app_type='hermes'`、`provider_id='_hermes_session'`、`provider_type='hermes_session'`、
  `data_source='hermes_session'`；
* `input_tokens` = **新增输入**（不含缓存命中），因此标 `input_token_semantics=2`（FRESH）；
  CC Switch 的 `CACHE_INCLUSIVE_APP_TYPES` 只含 codex/gemini/grokbuild，hermes 不在其中，
  它不会再做任何减法；
* `cache_creation_tokens` = Hermes 的 `cache_write_tokens`（语义一致）；
* `status_code=200`：Hermes 的计数器只在**拿到了 usage 的调用**上累加，失败调用不进这张表；
  这也是 CC Switch 自家导入器的取值。想要「未知」可传 `--status-code 0`；
* `latency_ms=0`、`first_token_ms=NULL`、`duration_ms=NULL`：聚合源没有计时。0 表示「没有计时」
  而不是「0 毫秒」；因为 `first_token_ms` 为 NULL 且 `latency_ms < 1000`，这些行不会进入
  看板的「速度」统计（`speed_eligible_sql` / `speed_estimate_eligible_sql`），速度列自然留空；
* `created_at` = 该累计行的 `last_seen`（Hermes 最后更新它的时刻）。聚合快照没有逐请求时间，
  所以趋势图反映的是**同步窗口**，不是精确的逐请求时间；
* 成本：`total_cost_usd` 默认用 Hermes 报的成本（`estimated_cost_usd` / `actual_cost_usd`
  的增量，等同真实计费口径）；各组件成本（`input_cost_usd` 等）用 CC Switch 的
  `model_pricing` 表按它的算法算，匹配不到定价时为 0。两者可能因定价快照不同而有小差异，
  需要完全一致时用 `--cost-source pricing`。

### 幂等与防双计

* **`request_id` 内容寻址**：由「键 + 该行累计计数器的完整快照」派生（`sha1` 取前 24 位）。
  同一份快照永远得到同一个 id，配合 `INSERT OR IGNORE`，重复跑、崩溃重跑、状态库丢失后重跑
  都不会双计。
* **计数器回退**（会话被重置/回滚）只重建基线，不写负数行。
* **状态库丢了、但 CC Switch 里已经有本工具写的行** → 直接拒绝（退出码 40），要求你先
  `--purge` 再重建，避免把历史再算一遍。
* **只读源库**：Hermes 的 `state.db` 以 `mode=ro` + `PRAGMA query_only=ON` 打开，任何写操作
  会被 SQLite 自己拒绝。
* **只写自己的行**：本工具唯一的删除动作是 `--purge`，且条件写死
  `data_source='hermes_session'`。

### 已知局限（先看这里，别把它当缺陷）

1. **看板上没有「Hermes」这个 App 按钮。** CC Switch v4.0.5 前端 `AppType` 联合类型是
   `claude|codex|gemini|grokbuild|opencode|pi|mcode`，注释里明确写着 openclaw/hermes
   「只作为被管理的应用出现」。所以 Hermes 的数据只在 **App = 全部** 的口径里出现，
   不能单独点一个 Hermes 芯片把它筛出来；单个 Hermes 会话的用量也无法在会话页头部显示。
2. **成功率/速度列对 Hermes 没有意义**：聚合源没有逐请求状态码与首字时间。
3. **成本可能有小差异**：Hermes 的成本来自它自己的定价快照，CC Switch 的组件成本来自
   `model_pricing` 表，两者不保证同价。
4. **reasoning tokens 存不下**：CC Switch 的 `proxy_request_logs` 没有 reasoning 列
   （Hermes 侧照常统计，只是看板无处安放）。
5. **一次同步 = 一行**，不是一次请求一行：粒度受源表（累计计数器）限制。
   上游 PR #6120 用插件直采逐请求元数据来补这一块，但要求 Hermes ≥0.21.5 且要装插件。
6. **靠外部调度**：本工具不自带常驻进程，需要计划任务 / cron 定期跑。

### 隐私

* 只读本地数据库、只写本地数据库，**不联网、不上传、不读凭据**。
* 日志与输出里不含会话内容（消息正文、提示词、回复）。
* 仓库里没有任何个人路径、密钥或数据库文件；默认路径全部由环境变量推导
  （`LOCALAPPDATA` / `HOME`），可被 `--hermes-db` 等参数覆盖。

### 实测（写这份 README 时本机的结果）

```
$ python hermes_usage_sync.py --check
Hermes 库: C:\...\hermes\state.db
Hermes 累计行: 35
CC Switch 库: C:\...\.cc-switch\cc-switch.db
本工具已写入: 0 行
状态库: C:\...\hermes-usage-sync\sync-state.db (0 个键)

$ python hermes_usage_sync.py -v
已备份 CC Switch 库到 ...\cc-switch.db.hermes-usage-sync.bak
同步完成: 计划 35 行, 写入 35 行, 重复跳过 0, 无变化 0, 计数器重置 0（模式 backfill）

$ python hermes_usage_sync.py          # 再跑一次
同步完成: 计划 0 行, 写入 0 行, 重复跳过 0, 无变化 35, 计数器重置 0（模式 delta）

$ python hermes_usage_sync.py --report
Hermes 侧（累计快照）: 35 行 / 966 次调用 / 10 个会话
  新增输入 1936231 / 输出 1130932 / 缓存命中 132418942 / 缓存写入 0 / 推理 670280
  成本 $1.3663
CC Switch 侧（data_source='hermes_session'，即本工具已写入的增量合计）: 35 行 / $1.3663
  新增输入 1936231 / 输出 1130932 / 缓存命中 132418942 / 缓存写入 0
```

看板侧（用 CC Switch 自己的 SQL 口径核对，`provider_name_coalesce` 解析出 `Hermes Agent`）：

```
hermes 行按 provider 名聚合: (36, 1.37249242, 1940645, 1134337, 133597822, 0, 'Hermes Agent')
全部范围 各 app_type:  ('claude', …) ('codex', …) ('hermes', 36, 1940645, 1134337, 133597822, 1.3725) ('opencode', …)
2026-10-09 当天:      ('hermes', 8, 588898, 377361, 0.3998)
```

### 测试

```bash
python -m unittest discover -s tests -v      # 19 个用例，标准库，无依赖
```

覆盖：首次回填、增量、重复跑不变、状态库丢失拒绝、计数器回退、`--dry-run` / `--purge` /
`--report` / `--check`、缺库与表结构不兼容、幂等 id 稳定、定价兜底。

### 与已有方案的关系

* [`Chunai-Bboy/cc-switch-hermes-usage`](https://github.com/Chunai-Bboy/cc-switch-hermes-usage)（MIT）
  —— 同一问题的另一个独立实现（opencode skill 形态，作者是上游 PR #6120 的作者）。
  本项目的「累计快照 → 增量」「聚合源不伪造 status/latency」等口径与该文一致；
  实现、代码与文档均为独立编写。
* 上游 `f061b777` / `518d945e` / [PR #6120](https://github.com/farion1231/cc-switch/pull/6120)
  —— CC Switch 官方的两条尝试，供对照。

### 许可

MIT，见 `LICENSE`。

---

## English

**hermes-usage-sync** pushes your local Hermes Agent token usage into the CC Switch
usage dashboard, which today tracks Claude Code / Codex / Gemini / OpenCode / Grok Build /
MiniMax Code / Pi but not Hermes (upstream added it in `f061b777`, reverted it in `518d945e`;
[PR #6120](https://github.com/farion1231/cc-switch/pull/6120) is still open, and v4.0.5 ships no
`session_usage_hermes.rs`).

It is a single-file, stdlib-only Python script. It reads the cumulative counters in Hermes'
`state.db` (`session_model_usage`), diffs them against its own snapshot store, and inserts the
deltas into `proxy_request_logs` as `app_type='hermes'`, `data_source='hermes_session'` — using
the exact row conventions CC Switch's own session importers use.

```bash
python hermes_usage_sync.py --check      # doctor: find the DBs, report problems
python hermes_usage_sync.py --dry-run -v # show what would be written
python hermes_usage_sync.py              # sync (idempotent)
python hermes_usage_sync.py --report     # reconcile both sides
```

Key properties: Hermes' DB is opened read-only; `request_id`s are content-addressed from the
counter snapshot, so re-runs (even after losing the state DB) never double count; counter
resets rebuild the baseline instead of writing negative rows; a lost state DB with existing
rows makes the tool refuse (exit 40) rather than backfill twice; `--purge` deletes only rows
whose `data_source='hermes_session'`.

Limitations (by design of the source): CC Switch v4.0.5's frontend `AppType` union has no
`hermes`, so Hermes data appears under **App = all** and cannot be filtered by its own chip;
the aggregate source has no per-request status/latency, so success-rate and speed columns stay
meaningless for Hermes; Reasoning tokens have no column in `proxy_request_logs`; cost components
come from CC Switch's `model_pricing` while the total prefers Hermes' own reported cost.

Everything is local: no network, no credentials, no session content in any output.

MIT licensed.

---

## 相关上游代码位置（v4.0.5）

| 用途 | 文件 |
|---|---|
| 行口径参照（会话导入器） | `src-tauri/src/services/session_usage_codex.rs` |
| provider 名解析 CASE | `src-tauri/src/services/usage_stats.rs` `provider_name_coalesce` |
| 去重白名单（hermes 不在其中） | `src-tauri/src/services/usage_stats.rs` `effective_usage_log_filter` |
| 缓存语义常量 | `src-tauri/src/services/sql_helpers.rs` |
| 30 天折叠进 rollup | `src-tauri/src/database/dao/usage_rollup.rs` `rollup_and_prune` |
| 合成 provider 不会被写进 live 配置 | `src-tauri/src/services/provider/live.rs` `sync_all_providers_to_live` + `src-tauri/src/provider.rs` `liveConfigManaged` |

更细的核对笔记见 [`docs/cc-switch-usage-internals.md`](docs/cc-switch-usage-internals.md)。
