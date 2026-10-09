# CC Switch 用量看板内部口径（v4.0.5 核对笔记）

这份笔记记录本工具写入口径的**依据**：每一条都能在 CC Switch v4.0.5 的源码里找到出处。
上游换版本后，先用这里的方法重核一遍再改脚本。

核对方式是 `gh api repos/farion1231/cc-switch/contents/<path>?ref=v4.0.5`（或直接看
本地安装包解包后的 `src-tauri/`），引用中的行号为 v4.0.5 tag。

## 1. 看板读哪张表

* `usage_stats.rs::get_usage_summary`（约 840 行起）把两块**相加**：
  * 明细：`proxy_request_logs l`（近 30 天）
  * 历史：`usage_daily_rollups r`（更早的按天折叠）
  * 也就是 `SELECT … UNION 两侧求和`，见 `COALESCE(d.…,0) + COALESCE(r.…,0)`。
* `usage_stats.rs::get_session_usage_summary`（约 970 行）只数
  `data_source <> 'proxy'` 的行，用于会话阅读页头部。
* 折叠逻辑在 `database/dao/usage_rollup.rs::rollup_and_prune(retain_days)`：把早于
  保留期的明细聚合成按天行并删除明细。**后端自己维护**，所以本工具只写明细、
  不碰 `usage_daily_rollups`。

## 2. 行到底怎么被“有效”过滤（关键：hermes 不会被去重掉）

`usage_stats.rs::effective_usage_log_filter`（约 398 行）：

```sql
NOT (
  COALESCE(l.data_source,'proxy') IN ('session_log','codex_session','gemini_session','opencode_session')
  AND EXISTS ( … 同窗口同 token 的 proxy 行 … )
)
```

去重白名单只有 claude/codex/gemini/opencode 四类会话来源。`hermes_session` 不在其中，
所以本工具写的行**永远参与统计**；即使将来同窗口出现代理行也不会被消掉。
（若上游把 hermes 加进白名单，就要重新评估这条。）

## 3. provider 显示名

`usage_stats.rs::provider_name_coalesce`（约 302 行）：

```sql
COALESCE(p.name, CASE l.provider_id
    WHEN '_session'          THEN 'Claude (Session)'
    WHEN '_codex_session'    THEN 'Codex (Session)'
    WHEN '_gemini_session'   THEN 'Gemini (Session)'
    WHEN '_opencode_session' THEN 'OpenCode (Session)'
    WHEN '_grok_session'     THEN 'Grok Build (Session)'
    WHEN '_mcode_session'    THEN 'MiniMax Code (Session)'
    WHEN '_pi_session'       THEN 'Pi (Session)'
    ELSE l.provider_id END)
```

v4.0.5 没有 `_hermes_session` 分支 → 不登记 providers 行时，「数据来源」下拉会显示
原始 id `_hermes_session`。所以本工具提供 `--register-provider`：
在 `providers` 表写入 `(id='_hermes_session', app_type='hermes', name='Hermes Agent')`，
`COALESCE` 就会取 `name`，看板显示 **Hermes Agent**。

## 4. 合成 provider 会不会污染 Hermes 的 config.yaml？

会——如果不管它。`services/provider/live.rs::sync_all_providers_to_live`（约 706 行）
会遍历该 app 的**全部** provider 写进 live 配置；对 `AppType::Hermes` 走
`hermes_config::set_provider(&provider.id, …)`（约 687 行），即在
`~/.hermes/config.yaml` 的 `custom_providers:` 里 upsert 一条同名条目。

但同一个函数开头有一条跳过规则：

```rust
if provider.meta.as_ref().and_then(|meta| meta.live_config_managed) == Some(false) {
    continue;   // 仅存在于数据库的 provider，不写 live
}
```

字段定义在 `src-tauri/src/provider.rs`：

```rust
/// 累加模式应用中，该 provider 是否已写入 live config。
#[serde(rename = "liveConfigManaged", skip_serializing_if = "Option::is_none")]
pub live_config_managed: Option<bool>,
```

因此 `--register-provider` 写的是 `meta = {"liveConfigManaged": false}`：
既能显示成 "Hermes Agent"，又**不会**被 CC Switch 同步进 Hermes 的配置文件。

## 5. token 语义

`services/sql_helpers.rs`：

```rust
pub(crate) const CACHE_INCLUSIVE_APP_TYPES: &[&str] = &["codex", "gemini", "grokbuild"];
pub(crate) const INPUT_TOKEN_SEMANTICS_LEGACY: i64 = 0;
pub(crate) const INPUT_TOKEN_SEMANTICS_TOTAL:  i64 = 1;   // input 含 cache read
pub(crate) const INPUT_TOKEN_SEMANTICS_FRESH:  i64 = 2;   // input = 新增输入
```

`fresh_input_sql()` 只对 `CACHE_INCLUSIVE_APP_TYPES` 里的 app 做减法；hermes 不在其中，
所以本工具写 `input_token_semantics=2` 且 `input_tokens` 直接是 Hermes 的新增输入，
看板的「新增输入」不会再被扣一次。`real_total_tokens_sql()` 的总量口径是
`新增输入 + 输出 + 缓存写入 + 缓存命中`。

## 6. 速度列为什么留空

`usage_stats.rs::speed_eligible_sql` 要求
`first_token_ms IS NOT NULL AND output_tokens >= SPEED_MIN_OUTPUT_TOKENS AND latency_ms - first_token_ms >= SPEED_MIN_GENERATION_MS`；
`speed_estimate_eligible_sql` 则要求
`first_token_ms IS NULL AND data_source <> 'proxy' AND output_tokens >= 200 AND latency_ms >= 1000`。

本工具写 `latency_ms=0`、`first_token_ms=NULL`，两条都不满足 → 不估速度、不显示 0ms。
这与 CC Switch 自家 Codex 导入器的取值一致（`latency_ms.unwrap_or(0)` + `first_token_ms=None`）。

## 7. 行模板（照抄 `session_usage_codex.rs` 的 INSERT）

```rust
request_id, provider_id, app_type, model, request_model,
input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd, total_cost_usd,
latency_ms, first_token_ms, status_code, error_message, session_id,
provider_type, is_streaming, cost_multiplier, created_at, data_source
```

Codex 导入器的取值对照：`provider_id='_codex_session'`、`provider_type=Some("codex_session")`、
`status_code=200`、`latency_ms=0`（无计时）、`is_streaming=1`、`cost_multiplier="1.0"`、
成本来自 `model_pricing`（匹配不到就全 0）。本工具同构，只把 hermes 的字段换掉。

## 8. 前端（为什么没有 Hermes 芯片）

`src/types/usage.ts`（v4.0.5）：

```ts
export type AppType = "claude" | "codex" | "gemini" | "grokbuild" | "opencode" | "pi" | "mcode";
export const KNOWN_APP_TYPES: ReadonlyArray<AppType> = [ … ];
```

注释原文：「`openclaw` / `hermes` appear only as managed apps elsewhere.」
`UsageDashboard.tsx` 的 App 芯片由 `KNOWN_APP_TYPES.map(…)` 渲染，所以 Hermes 没有独立
筛选按钮——数据只在 **App = 全部** 里出现；「数据来源」下拉的数据来自
`get_provider_stats`，因此 `--register-provider` 之后会多出 **Hermes Agent** 一项。

## 9. 复现核对（每次上游升级后照做）

```bash
# 1) 上游源码
gh api repos/farion1231/cc-switch/contents/src-tauri/src/services/usage_stats.rs?ref=<tag> \
  --jq .content | base64 -d | grep -n "provider_name_coalesce\|effective_usage_log_filter" -A 20
gh api repos/farion1231/cc-switch/contents/src-tauri/src/services?ref=<tag> --jq '.[].name'

# 2) 本地安装包（字符串级核对，最贴近你机器上真正跑的那份）
python - <<'PY'
import re
d = open(r"D:\\cc-switch.exe", "rb").read()          # 换成实际安装路径
pats = ["hermes_session", "session_usage_hermes", "'_pi_session'", "usage_daily_rollups"]
for p in pats:
    hits = len(re.findall(p.encode(), d)) + len(re.findall(p.encode("utf-16le"), d))
    print(p, hits)
PY

# 3) 本地库
python -c "import sqlite3;c=sqlite3.connect(r'C:/Users/<you>/.cc-switch/cc-switch.db');\
print(c.execute('select app_type,data_source,count(*) from proxy_request_logs group by 1,2').fetchall())"
```

第 2 步能直接回答“我这个版本到底有没有 Hermes 的解析器”。
