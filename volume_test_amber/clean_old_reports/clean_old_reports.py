"""
清洗旧测试报告
==============

把 ``backup/测试报告/`` 下旧版报告（无"丢弃"概念）按当前 ``config.json``
中 ``drop_with_log_errnos`` / ``drop_silent_errnos`` 规则重算，并按当前
``run.go`` 的 HTML 模板重新渲染，输出到
``backup/测试报告_cleaned/<同样的相对路径>/``。

每个清洗后的运行目录会生成：

* ``config.snapshot.json``   旧字段 + 新增的 ``drop_with_log_errnos`` / ``drop_silent_errnos``
* ``errors.log``             仅保留非 silent drop 的错误条目（JSONL）
* ``report.html``            新模板渲染（含"成功率样本数"、"丢弃统计"、"丢弃明细"）

注意：
    旧 ``errors.log`` 只包含错误请求的 ``elapsed_ms``，没有成功请求的耗时，
    所以无法精确按新规则剔除 silent drop 后重算延迟分布。脚本保留旧报告
    里展示的延迟数值（min/avg/p50/p95/p99/max）原样输出。
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SOURCE_ROOT = ROOT / "backup" / "测试报告"
OUTPUT_ROOT = ROOT / "backup" / "测试报告_cleaned"
MAX_ERRORS_IN_HTML = 500


# --------------------------------------------------------------------------- #
# 配置 / 扫描
# --------------------------------------------------------------------------- #
def load_drop_rules() -> tuple[set[int], set[int]]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return (
        set(cfg.get("drop_with_log_errnos", [])),
        set(cfg.get("drop_silent_errnos", [])),
    )


def find_run_dirs(root: Path) -> list[Path]:
    """递归找出所有"运行目录"（含 ``config.snapshot.json`` 和 ``report.html``）。"""
    runs: list[Path] = []
    for snapshot in root.rglob("config.snapshot.json"):
        if (snapshot.parent / "report.html").exists():
            runs.append(snapshot.parent)
    return sorted(runs)


# --------------------------------------------------------------------------- #
# 旧报告解析
# --------------------------------------------------------------------------- #
_METRIC_RE = (
    r'<div class="label">{label}</div>\s*<div class="value[^"]*">([^<]+)</div>'
)


def _metric(html_text: str, label: str) -> str | None:
    m = re.search(_METRIC_RE.format(label=re.escape(label)), html_text)
    return m.group(1).strip() if m else None


def parse_old_report(html_text: str) -> dict:
    header = re.search(
        r"接口：<code>([^<]+)</code>.*?开始：([\d\-: ]+).*?结束：([\d\-: ]+)<",
        html_text,
        re.S,
    )
    if not header:
        raise ValueError("无法解析报告头部 接口/开始/结束")

    duration_text_raw = _metric(html_text, "测试时长") or ""
    duration_num = re.match(r"([\d.]+)", duration_text_raw)
    duration_sec = float(duration_num.group(1)) if duration_num else 0.0

    return {
        "full_url": header.group(1).strip(),
        "start_time": header.group(2).strip(),
        "end_time": header.group(3).strip(),
        "device_count": int(_metric(html_text, "设备数量 X")),
        "duration_sec": duration_sec,
        "duration_text": duration_text_raw,
        "interval": _metric(html_text, "发送间隔") or "",
        "planned_qps": _metric(html_text, "目标 QPS") or "",
        "actual_qps": _metric(html_text, "实际 QPS") or "",
        "total_requests": int(_metric(html_text, "总请求数")),
        "success_count": int(_metric(html_text, "成功数")),
        "fail_count_old": int(_metric(html_text, "失败数")),
        "success_rate_old": _metric(html_text, "成功率") or "",
        "latency_min": _metric(html_text, "min") or "",
        "latency_avg": _metric(html_text, "avg") or "",
        "latency_p50": _metric(html_text, "p50") or "",
        "latency_p95": _metric(html_text, "p95") or "",
        "latency_p99": _metric(html_text, "p99") or "",
        "latency_max": _metric(html_text, "max") or "",
    }


def parse_errors_log(path: Path) -> list[dict]:
    items: list[dict] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: 跳过无法解析的 errors.log 行: {e}", file=sys.stderr)
    return items


def split_url(full_url: str) -> tuple[str, str]:
    """``https://host/path`` -> ``('https://host', '/path')``。"""
    m = re.match(r"^(https?://[^/]+)(/.*)$", full_url)
    if not m:
        return full_url, ""
    return m.group(1), m.group(2)


# --------------------------------------------------------------------------- #
# 重算
# --------------------------------------------------------------------------- #
def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "..."


def compute(
    parsed: dict,
    errors: list[dict],
    drop_with_log: set[int],
    drop_silent: set[int],
) -> dict:
    """根据旧 report 头与 errors.log，按新规则重算。"""
    total = parsed["total_requests"]
    success = parsed["success_count"]

    with_log_count: dict[int, int] = defaultdict(int)
    silent_count: dict[int, int] = defaultdict(int)
    fail_count: dict[int, int] = defaultdict(int)
    sample_msg: dict[int, str] = {}
    fail_entries: list[dict] = []
    with_log_entries: list[dict] = []

    for entry in errors:
        try:
            errno = int(entry.get("errno", 0))
        except (TypeError, ValueError):
            continue
        msg = entry.get("msg", "") or ""
        sample_msg.setdefault(errno, truncate(msg, 160))

        if errno in drop_silent:
            silent_count[errno] += 1
        elif errno in drop_with_log:
            with_log_count[errno] += 1
            with_log_entries.append(entry)
        else:
            fail_count[errno] += 1
            fail_entries.append(entry)

    discarded_with_log = sum(with_log_count.values())
    discarded_silent = sum(silent_count.values())
    effective_total = total - discarded_with_log - discarded_silent
    new_fail = max(effective_total - success, 0)
    success_rate = (
        success / effective_total * 100.0 if effective_total > 0 else 0.0
    )

    # ---------- errno 主表 ----------
    main_dist: list[dict] = []
    if success > 0 or 0 in sample_msg:
        main_dist.append(
            {
                "errno": 0,
                "tag": "0 (成功)",
                "tag_class": "",
                "count": success,
                "ratio": (
                    f"{success / effective_total * 100:.2f}%"
                    if effective_total > 0
                    else "-"
                ),
                "sample": sample_msg.get(0, "Operation successful"),
            }
        )

    for errno, cnt in sorted(with_log_count.items(), key=lambda x: (-x[1], x[0])):
        main_dist.append(
            {
                "errno": errno,
                "tag": f"{errno} (丢弃-记日志)",
                "tag_class": "bad",
                "count": cnt,
                "ratio": "-",
                "sample": sample_msg.get(errno, ""),
            }
        )

    for errno, cnt in sorted(fail_count.items(), key=lambda x: (-x[1], x[0])):
        main_dist.append(
            {
                "errno": errno,
                "tag": str(errno),
                "tag_class": "bad",
                "count": cnt,
                "ratio": (
                    f"{cnt / effective_total * 100:.2f}%"
                    if effective_total > 0
                    else "-"
                ),
                "sample": sample_msg.get(errno, ""),
            }
        )

    # ---------- 丢弃明细 ----------
    def _drop_dist(counts: dict[int, int]) -> list[dict]:
        return [
            {
                "errno": errno,
                "count": cnt,
                "sample": sample_msg.get(errno, ""),
            }
            for errno, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ]

    # ---------- 异常明细 ----------
    error_entries = with_log_entries + fail_entries
    error_entries.sort(key=lambda e: e.get("timestamp", ""))

    return {
        "discarded_with_log": discarded_with_log,
        "discarded_silent": discarded_silent,
        "effective_total": effective_total,
        "new_fail": new_fail,
        "success_rate_text": f"{success_rate:.2f}%",
        "main_dist": main_dist,
        "with_log_dist": _drop_dist(with_log_count),
        "silent_dist": _drop_dist(silent_count),
        "error_entries": error_entries,
    }


# --------------------------------------------------------------------------- #
# 新 HTML 模板（结构与 run.go 中 reportHTMLTemplate 保持一致）
# --------------------------------------------------------------------------- #
_REPORT_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Amber 下发压测报告 - $start_time</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f4f6f9; color: #2c3e50; }
  h1 { margin: 0 0 16px; }
  h2 { margin: 24px 0 12px; color: #34495e; border-left: 4px solid #3498db; padding-left: 10px; }
  .card { background: #fff; padding: 18px 22px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 18px; }
  .metrics { display: flex; flex-wrap: wrap; gap: 18px; }
  .metric { flex: 1 1 180px; background: #fafbfc; border: 1px solid #eceff1; border-radius: 6px; padding: 12px 14px; }
  .metric .label { color: #7f8c8d; font-size: 12px; margin-bottom: 4px; }
  .metric .value { font-size: 22px; font-weight: 600; color: #2c3e50; }
  .ok   { color: #27ae60 !important; }
  .bad  { color: #c0392b !important; }
  .warn { color: #e67e22 !important; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { border: 1px solid #e1e4e8; padding: 8px 10px; text-align: left; }
  th { background: #ecf0f1; }
  tr:nth-child(even) td { background: #fbfcfd; }
  code { background: #ecf0f1; padding: 1px 6px; border-radius: 3px; font-size: 13px; }
  .muted { color: #95a5a6; font-size: 12px; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #ecf0f1; color: #34495e; }
  .tag.bad { background: #fdecea; color: #c0392b; }
  .tag.warn { background: #fff4e5; color: #b26a00; }
  details summary { cursor: pointer; color: #7f8c8d; font-size: 13px; user-select: none; }
  details[open] summary { margin-bottom: 12px; }
</style>
</head>
<body>
  <h1>Amber 下发压测报告</h1>
  <div class="muted">接口：<code>$domain$api_path</code> &nbsp; 开始：$start_time &nbsp; 结束：$end_time</div>

  <h2>总览</h2>
  <div class="card">
    <div class="metrics">
      <div class="metric"><div class="label">设备数量 X</div><div class="value">$device_count</div></div>
      <div class="metric"><div class="label">测试时长</div><div class="value">$duration_text</div></div>
      <div class="metric"><div class="label">发送间隔</div><div class="value">$interval</div></div>
      <div class="metric"><div class="label">目标 QPS</div><div class="value">$planned_qps</div></div>
      <div class="metric"><div class="label">实际 QPS</div><div class="value">$actual_qps</div></div>
      <div class="metric"><div class="label">总请求数</div><div class="value">$total_requests</div></div>
      <div class="metric"><div class="label">成功率样本数</div><div class="value">$effective_total</div></div>
      <div class="metric"><div class="label">成功数</div><div class="value ok">$success_count</div></div>
      <div class="metric"><div class="label">失败数</div><div class="value bad">$fail_count</div></div>
      <div class="metric"><div class="label">成功率</div><div class="value">$success_rate</div></div>
    </div>
  </div>

  <h2>延迟分布 (ms)</h2>
  <div class="card">
    <div class="metrics">
      <div class="metric"><div class="label">min</div><div class="value">$latency_min</div></div>
      <div class="metric"><div class="label">avg</div><div class="value">$latency_avg</div></div>
      <div class="metric"><div class="label">p50</div><div class="value">$latency_p50</div></div>
      <div class="metric"><div class="label">p95</div><div class="value">$latency_p95</div></div>
      <div class="metric"><div class="label">p99</div><div class="value">$latency_p99</div></div>
      <div class="metric"><div class="label">max</div><div class="value">$latency_max</div></div>
    </div>
  </div>

  <h2>Errno 分布</h2>
  <div class="card">
    $errno_table
    <div class="muted" style="margin-top: 10px;">丢弃统计：记录错误并丢弃 $discarded_with_log 条，不记录错误并丢弃 $discarded_silent 条</div>
    <details style="margin-top: 8px;">
      <summary>展开查看按错误码统计的丢弃明细</summary>
$with_log_block
$silent_block
    </details>
  </div>

  <h2>异常明细 (errno != 0)</h2>
  <div class="card">
    <div class="muted">完整异常日志（JSONL）：<code>$error_log_abs</code>&nbsp;&nbsp;共 $errors_total 条$extra_shown_text</div>
    $errors_table
  </div>

</body>
</html>
"""
)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _render_main_errno_table(items: list[dict]) -> str:
    if not items:
        return '<div class="muted">无数据</div>'
    rows = []
    for d in items:
        cls = d["tag_class"]
        rows.append(
            "        <tr>\n"
            f'          <td><span class="tag {cls}">{html.escape(d["tag"])}</span></td>\n'
            f'          <td>{d["count"]}</td>\n'
            f'          <td>{d["ratio"]}</td>\n'
            f'          <td><code>{html.escape(d["sample"])}</code></td>\n'
            "        </tr>"
        )
    return (
        "<table>\n"
        '      <thead><tr><th style="width:120px">errno</th>'
        '<th style="width:120px">次数</th>'
        '<th style="width:120px">占比</th>'
        "<th>示例 msg</th></tr></thead>\n"
        "      <tbody>\n"
        + "\n".join(rows)
        + "\n      </tbody>\n"
        "    </table>"
    )


def _render_drop_table(items: list[dict]) -> str | None:
    if not items:
        return None
    rows = []
    for d in items:
        rows.append(
            "          <tr>\n"
            f'            <td><span class="tag warn">{d["errno"]}</span></td>\n'
            f'            <td>{d["count"]}</td>\n'
            f'            <td><code>{html.escape(d["sample"])}</code></td>\n'
            "          </tr>"
        )
    return (
        "      <table>\n"
        '        <thead><tr><th style="width:120px">errno</th>'
        '<th style="width:120px">次数</th>'
        "<th>示例 msg</th></tr></thead>\n"
        "        <tbody>\n"
        + "\n".join(rows)
        + "\n        </tbody>\n"
        "      </table>"
    )


def _render_drop_block(items: list[dict], heading: str, empty_text: str) -> str:
    table = _render_drop_table(items)
    if table is None:
        return f'      <div class="muted" style="margin: 8px 0;">{empty_text}</div>'
    return f'      <div class="muted" style="margin: 8px 0;">{heading}</div>\n{table}'


def _render_errors_table(entries: list[dict], shown: int) -> str:
    if not entries:
        return '<div class="muted ok">没有发现任何异常请求</div>'
    rows = []
    for e in entries[:shown]:
        ts_text = str(e.get("timestamp", ""))
        sn = str(e.get("sn", ""))
        try:
            errno = int(e.get("errno", 0))
        except (TypeError, ValueError):
            errno = 0
        msg = e.get("msg", "") or ""
        try:
            elapsed_ms = float(e.get("elapsed_ms", 0))
        except (TypeError, ValueError):
            elapsed_ms = 0.0
        rows.append(
            "        <tr>\n"
            f"          <td>{html.escape(ts_text)}</td>\n"
            f"          <td><code>{html.escape(sn)}</code></td>\n"
            f'          <td><span class="tag bad">{errno}</span></td>\n'
            f"          <td>{elapsed_ms:.2f}</td>\n"
            f"          <td>{html.escape(truncate(msg, 240))}</td>\n"
            "        </tr>"
        )
    return (
        "<table>\n"
        '      <thead><tr><th style="width:190px">时间</th>'
        "<th>SN</th>"
        '<th style="width:90px">errno</th>'
        '<th style="width:120px">耗时(ms)</th>'
        "<th>msg</th></tr></thead>\n"
        "      <tbody>\n"
        + "\n".join(rows)
        + "\n      </tbody>\n"
        "    </table>"
    )


def render_html(parsed: dict, computed: dict, error_log_abs: str) -> str:
    domain, api_path = split_url(parsed["full_url"])

    duration_text = parsed["duration_text"].strip()
    if not duration_text:
        duration_text = f"{parsed['duration_sec']:.2f} s"
    elif not duration_text.endswith("s"):
        duration_text = f"{duration_text} s"

    errors = computed["error_entries"]
    shown = min(len(errors), MAX_ERRORS_IN_HTML)
    extra_shown_text = f"，此处仅展示前 {shown} 条" if shown < len(errors) else ""

    return _REPORT_TEMPLATE.substitute(
        start_time=html.escape(parsed["start_time"]),
        end_time=html.escape(parsed["end_time"]),
        domain=html.escape(domain),
        api_path=html.escape(api_path),
        device_count=parsed["device_count"],
        duration_text=html.escape(duration_text),
        interval=html.escape(parsed["interval"]),
        planned_qps=html.escape(parsed["planned_qps"]),
        actual_qps=html.escape(parsed["actual_qps"]),
        total_requests=parsed["total_requests"],
        effective_total=computed["effective_total"],
        success_count=parsed["success_count"],
        fail_count=computed["new_fail"],
        success_rate=html.escape(computed["success_rate_text"]),
        latency_min=html.escape(parsed["latency_min"]),
        latency_avg=html.escape(parsed["latency_avg"]),
        latency_p50=html.escape(parsed["latency_p50"]),
        latency_p95=html.escape(parsed["latency_p95"]),
        latency_p99=html.escape(parsed["latency_p99"]),
        latency_max=html.escape(parsed["latency_max"]),
        errno_table=_render_main_errno_table(computed["main_dist"]),
        discarded_with_log=computed["discarded_with_log"],
        discarded_silent=computed["discarded_silent"],
        with_log_block=_render_drop_block(
            computed["with_log_dist"],
            heading="记录错误并丢弃",
            empty_text="记录错误并丢弃：0 条",
        ),
        silent_block=_render_drop_block(
            computed["silent_dist"],
            heading="不记录错误并丢弃",
            empty_text="不记录错误并丢弃：0 条",
        ),
        error_log_abs=html.escape(error_log_abs),
        errors_total=len(errors),
        extra_shown_text=extra_shown_text,
        errors_table=_render_errors_table(errors, shown),
    )


# --------------------------------------------------------------------------- #
# 写出
# --------------------------------------------------------------------------- #
def _dump_error_line(e: dict) -> str:
    """与 Go 的 ``json.Marshal(map)`` 输出保持字段字典序一致，便于 diff 对比。"""
    ordered = {
        "elapsed_ms": e.get("elapsed_ms"),
        "errno": e.get("errno"),
        "msg": e.get("msg", ""),
        "sn": e.get("sn", ""),
        "timestamp": e.get("timestamp", ""),
    }
    return json.dumps(ordered, ensure_ascii=False)


def write_config_snapshot(
    old_path: Path,
    new_path: Path,
    drop_with_log: set[int],
    drop_silent: set[int],
) -> None:
    cfg = json.loads(old_path.read_text(encoding="utf-8"))
    # 移除可能残留的旧字段后追加，保持新报告字段顺序在末尾
    for key in ("drop_with_log_errnos", "drop_silent_errnos"):
        cfg.pop(key, None)
    cfg["drop_with_log_errnos"] = sorted(drop_with_log)
    cfg["drop_silent_errnos"] = sorted(drop_silent)
    new_path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def clean_one(
    run_dir: Path,
    out_dir: Path,
    drop_with_log: set[int],
    drop_silent: set[int],
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    html_text = (run_dir / "report.html").read_text(encoding="utf-8")
    parsed = parse_old_report(html_text)
    errors = parse_errors_log(run_dir / "errors.log")
    computed = compute(parsed, errors, drop_with_log, drop_silent)

    new_errors_log = out_dir / "errors.log"
    with new_errors_log.open("w", encoding="utf-8") as f:
        for e in computed["error_entries"]:
            f.write(_dump_error_line(e) + "\n")

    write_config_snapshot(
        run_dir / "config.snapshot.json",
        out_dir / "config.snapshot.json",
        drop_with_log,
        drop_silent,
    )

    html_out = render_html(parsed, computed, str(new_errors_log.resolve()))
    (out_dir / "report.html").write_text(html_out, encoding="utf-8")

    return {
        "total": parsed["total_requests"],
        "success": parsed["success_count"],
        "drop_with_log": computed["discarded_with_log"],
        "drop_silent": computed["discarded_silent"],
        "fail": computed["new_fail"],
        "success_rate": computed["success_rate_text"],
    }


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> int:
    if not SOURCE_ROOT.exists():
        print(f"[ERROR] 未找到源目录: {SOURCE_ROOT}", file=sys.stderr)
        return 1

    drop_with_log, drop_silent = load_drop_rules()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    runs = find_run_dirs(SOURCE_ROOT)
    print(f"源目录       : {SOURCE_ROOT}")
    print(f"输出目录     : {OUTPUT_ROOT}")
    print(f"丢弃规则     : drop_with_log={sorted(drop_with_log)}  "
          f"drop_silent={sorted(drop_silent)}")
    print(f"待清洗运行目录数 : {len(runs)}")
    print("-" * 72)

    ok, fail = 0, 0
    for run_dir in runs:
        rel = run_dir.relative_to(SOURCE_ROOT)
        out_dir = OUTPUT_ROOT / rel
        try:
            stat = clean_one(run_dir, out_dir, drop_with_log, drop_silent)
            print(
                f"[OK] {rel}\n"
                f"     total={stat['total']}  success={stat['success']}  "
                f"fail={stat['fail']}  drop(log)={stat['drop_with_log']}  "
                f"drop(silent)={stat['drop_silent']}  rate={stat['success_rate']}"
            )
            ok += 1
        except Exception as exc:
            print(f"[FAIL] {rel}: {exc}", file=sys.stderr)
            fail += 1

    print("-" * 72)
    print(f"完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
