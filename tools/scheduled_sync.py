#!/usr/bin/env python3
"""给计划任务 / cron / Hermes cron 用的**静默包装**。

为什么要它：`hermes_usage_sync.py` 每次都会打印一行「同步完成: …」，直接挂到
任务计划或 cron 上，成功也会发出通知/邮件。这个包装把输出收进日志文件，
只在**真的失败**时打印一行，因此：

* 退出码 0（包含「0 增量」）→ stdout 为空 → 任务计划 / Hermes cron 什么都不发
* 其它退出码 → 只打印一行简短错误（让报警有意义，而不是静默失败）
* 每次运行都往日志追加一行（默认 `<状态目录>/sync.log`，超过 1MB 自动截断）

用法::

    python tools/scheduled_sync.py                 # 正常同步
    python tools/scheduled_sync.py -- --dry-run    # 传参给同步脚本（-- 之后原样转发）
    python tools/scheduled_sync.py --log D:\\logs\\sync.log

Windows 计划任务示例::

    schtasks /create /tn "hermes-usage-sync" /sc minute /mo 15 ^
      /tr "\"C:\\Path\\to\\python.exe\" \"C:\\Path\\to\\hermes-usage-sync\\tools\\scheduled_sync.py\""

Hermes cron（无 LLM，纯脚本）::

    cronjob_manage(action="create", schedule="every 15m", no_agent=True,
                   script="C:\\Path\\to\\hermes-usage-sync\\tools\\scheduled_sync.py", ...)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYNC = ROOT / "hermes_usage_sync.py"
MAX_LOG_BYTES = 1024 * 1024


def default_log_path() -> pathlib.Path:
    """复用同步脚本的状态目录（不再重复一套路径规则）。"""
    spec = importlib.util.spec_from_file_location("hermes_usage_sync", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.default_state_dir() / "sync.log"


def rotate(path: pathlib.Path, keep_bytes: int = 256 * 1024) -> None:
    """按字节截断：只保留末尾 keep_bytes，并丢掉第一条不完整的行。

    按行截断在这个场景会失效——日志里可能只有一条超长行（例如异常堆栈），
    那样"保留最后 N 行"等于没截。
    """
    try:
        if not path.exists() or path.stat().st_size <= MAX_LOG_BYTES:
            return
        tail = path.read_bytes()[-keep_bytes:]
        newline = tail.find(b"\n")
        path.write_bytes(tail[newline + 1:] if newline >= 0 else b"")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hermes-usage-sync 的静默包装")
    parser.add_argument("--log", help="日志文件路径（默认 <状态目录>/sync.log）")
    parser.add_argument("extra", nargs=argparse.REMAINDER,
                        help="传给 hermes_usage_sync.py 的参数（前面加 --）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    extra = [a for a in args.extra if a != "--"]

    log_file = pathlib.Path(args.log) if args.log else default_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotate(log_file)

    proc = subprocess.run(
        [sys.executable, str(SYNC), "--json"] + extra,
        capture_output=True, text=True, errors="replace",
    )
    payload: dict = {}
    text = (proc.stdout or "").strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    summary = "inserted=%s duplicates=%s planned=%s mode=%s" % (
        payload.get("inserted"), payload.get("duplicates"),
        payload.get("planned"), payload.get("mode"),
    )
    if proc.returncode != 0:
        summary = "exit=%d error=%s" % (proc.returncode, payload.get("error") or (proc.stderr or "").strip()[:200])
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("%s %s\n" % (stamp, summary))

    if proc.returncode != 0:
        # 失败才发声：任务计划 / Hermes cron 会把这行当结果发出去
        print("hermes-usage-sync 失败：%s（详见 %s）" % (summary, log_file))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
