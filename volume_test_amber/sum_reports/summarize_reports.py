"""
压测报告汇总
============

扫描脚本同级 ``测试报告_cleaned/``，挑出所有正式压测
（``duration_seconds == 300``）的运行报告，按时间排序生成两张汇总表：

* 【明细表】每次运行一行，列出关键指标
* 【组聚合表】把同一个测试组（如 "分布式72000，QPS=228.7"）下的多台机器
  汇总到一行，反映整组的总吞吐与最差表现

输出（位于脚本同级 ``summary_<时间戳>/`` 下）：

* ``summary.html``   汇报级页面：KPI 摘要卡片 + 4 张趋势图（重点突出 41203 与响应时间）
                     + 测试组聚合表 + 明细表 + 折叠收起的丢弃数据区。
                     图表通过 jsdelivr 加载 Chart.js，需联网访问。
* ``detail.csv``     明细表 CSV（utf-8-sig，Excel 可直接打开；保留所有列含丢弃）
* ``group.csv``      组聚合表 CSV（utf-8-sig；保留所有列含丢弃）

说明：
    "丢弃" 数据（``drop_with_log`` + ``drop_silent``）仅作为参考列展示，
    **不参与成功率计算**。成功率分母统一使用报告里的 "成功率样本数"
    （即 ``总请求 − 两类丢弃数``）。
"""

from __future__ import annotations

import csv
import html
import json
import re
import string
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "clean_old_reports"))

from clean_old_reports import parse_errors_log, parse_old_report  # noqa: E402

CLEANED_ROOT = ROOT / "测试报告_cleaned"

# 正式压测固定时长（秒）
TARGET_DURATION_SEC = 300

# 数据源根目录（同名 run_xxx 去重）
SOURCE_ROOTS = [CLEANED_ROOT]


# --------------------------------------------------------------------------- #
# 解析补充字段
# --------------------------------------------------------------------------- #
_EFFECTIVE_RE = re.compile(
    r'<div class="label">成功率样本数</div>\s*<div class="value[^"]*">(\d+)</div>'
)
_DROP_STATS_RE = re.compile(
    r"丢弃统计：记录错误并丢弃\s*(\d+)\s*条，不记录错误并丢弃\s*(\d+)\s*条"
)


def parse_new_metrics(html_text: str) -> dict:
    """从新模板渲染的 ``report.html`` 中读取『成功率样本数』与丢弃统计。

    若未匹配到（例如旧版未清洗的报告），分别返回 ``None`` / ``0``。
    """
    eff = _EFFECTIVE_RE.search(html_text)
    drop = _DROP_STATS_RE.search(html_text)
    return {
        "effective_total": int(eff.group(1)) if eff else None,
        "discarded_with_log": int(drop.group(1)) if drop else 0,
        "discarded_silent": int(drop.group(2)) if drop else 0,
    }


# --------------------------------------------------------------------------- #
# 扫描 / 收集
# --------------------------------------------------------------------------- #
def find_runs(roots: list[Path]) -> list[Path]:
    """跨多个根目录递归查找运行目录，按 ``run_xxx`` 名去重。

    同名时保留 ``roots`` 中靠前的那一个（即 ``cleaned/`` 优先于 ``reports/``）。
    """
    seen: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for snap in root.rglob("config.snapshot.json"):
            run_dir = snap.parent
            if not (run_dir / "report.html").exists():
                continue
            seen.setdefault(run_dir.name, run_dir)
    return sorted(seen.values())


def determine_group(run_dir: Path) -> tuple[str, str]:
    """根据相对路径推断 ``(测试组, 子项)``。

    例：
      ``cleaned/单台12000，QPS=37.4/run_xxx``                 -> ``("单台12000，QPS=37.4", "")``
      ``cleaned/分布式72000，QPS=228.7/第1台40000/run_xxx``   -> ``("分布式72000，QPS=228.7", "第1台40000")``
      ``reports/run_xxx``                                    -> ``("(reports/)", "")``
    """
    parts = run_dir.parts
    if "测试报告_cleaned" in parts:
        idx = parts.index("测试报告_cleaned")
        rel = parts[idx + 1 :]
        if len(rel) == 3:
            return rel[0], rel[1]
        if len(rel) == 2:
            return rel[0], ""
    if "reports" in parts:
        return "(reports/)", ""
    return "(unknown)", ""


def _safe_float(text: str) -> float:
    if text is None:
        return 0.0
    try:
        return float(re.sub(r"[^\d.\-]", "", str(text)))
    except ValueError:
        return 0.0


def collect_one(run_dir: Path) -> dict | None:
    cfg = json.loads((run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
    if int(cfg.get("duration_seconds", 0)) != TARGET_DURATION_SEC:
        return None

    html_text = (run_dir / "report.html").read_text(encoding="utf-8")
    parsed = parse_old_report(html_text)
    extra = parse_new_metrics(html_text)

    errors = parse_errors_log(run_dir / "errors.log")
    errno_counter: Counter[int] = Counter()
    for entry in errors:
        try:
            errno_counter[int(entry.get("errno", 0))] += 1
        except (TypeError, ValueError):
            continue
    top_errno_text = (
        "; ".join(f"{e}×{c}" for e, c in errno_counter.most_common(2))
        if errno_counter
        else "-"
    )

    group, subitem = determine_group(run_dir)

    effective = extra["effective_total"] or parsed["total_requests"]
    success = parsed["success_count"]
    fail = max(effective - success, 0)
    success_rate = (success / effective * 100.0) if effective > 0 else 0.0

    # 关注的两个错误码：41203（业务校验失败）与 -2（JSON 解析失败）。
    # 其余一律归到 err_other。
    err_41203 = int(errno_counter.get(41203, 0))
    err_neg2 = int(errno_counter.get(-2, 0))
    err_other = max(fail - err_41203 - err_neg2, 0)
    err_41203_pm = (err_41203 / effective * 1000.0) if effective > 0 else 0.0

    return {
        "run_dir": str(run_dir),
        "group": group,
        "subitem": subitem,
        "start_time": parsed["start_time"],
        "end_time": parsed["end_time"],
        "device_count": parsed["device_count"],
        "duration_sec": parsed["duration_sec"],
        "planned_qps": parsed["planned_qps"],
        "actual_qps": parsed["actual_qps"],
        "total": parsed["total_requests"],
        "effective": effective,
        "success": success,
        "fail": fail,
        "success_rate": success_rate,
        "lat_min": parsed["latency_min"],
        "lat_avg": parsed["latency_avg"],
        "lat_p50": parsed["latency_p50"],
        "lat_p95": parsed["latency_p95"],
        "lat_p99": parsed["latency_p99"],
        "lat_max": parsed["latency_max"],
        "drop_with_log": extra["discarded_with_log"],
        "drop_silent": extra["discarded_silent"],
        "drop_total": extra["discarded_with_log"] + extra["discarded_silent"],
        "top_errno": top_errno_text,
        "err_41203": err_41203,
        "err_neg2": err_neg2,
        "err_other": err_other,
        "err_41203_pm": err_41203_pm,
    }


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def aggregate_groups(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["group"]].append(r)

    out: list[dict] = []
    for name, items in groups.items():
        items.sort(key=lambda r: r["start_time"])
        total_devices = sum(r["device_count"] for r in items)
        actual_qps = sum(_safe_float(r["actual_qps"]) for r in items)
        planned_qps = sum(_safe_float(r["planned_qps"]) for r in items)
        total_total = sum(r["total"] for r in items)
        total_effective = sum(r["effective"] for r in items)
        total_success = sum(r["success"] for r in items)
        total_fail = sum(r["fail"] for r in items)
        total_drop_log = sum(r["drop_with_log"] for r in items)
        total_drop_silent = sum(r["drop_silent"] for r in items)
        total_err_41203 = sum(r["err_41203"] for r in items)
        total_err_neg2 = sum(r["err_neg2"] for r in items)
        total_err_other = sum(r["err_other"] for r in items)
        weighted_rate = (
            total_success / total_effective * 100.0 if total_effective > 0 else 0.0
        )
        weighted_41203_pm = (
            total_err_41203 / total_effective * 1000.0 if total_effective > 0 else 0.0
        )
        weighted_avg = (
            sum(_safe_float(r["lat_avg"]) * r["effective"] for r in items)
            / total_effective
            if total_effective > 0
            else 0.0
        )

        # 关注最差表现：组内 max p95/p99/max
        max_p95 = max(_safe_float(r["lat_p95"]) for r in items)
        max_p99 = max(_safe_float(r["lat_p99"]) for r in items)
        max_max = max(_safe_float(r["lat_max"]) for r in items)
        out.append(
            {
                "group": name,
                "first_start": items[0]["start_time"],
                "last_end": items[-1]["end_time"],
                "n_runs": len(items),
                "total_devices": total_devices,
                "planned_qps_sum": planned_qps,
                "actual_qps_sum": actual_qps,
                "total": total_total,
                "effective": total_effective,
                "success": total_success,
                "fail": total_fail,
                "drop_with_log": total_drop_log,
                "drop_silent": total_drop_silent,
                "err_41203": total_err_41203,
                "err_neg2": total_err_neg2,
                "err_other": total_err_other,
                "err_41203_pm": weighted_41203_pm,
                "rate": weighted_rate,
                "lat_avg_w": weighted_avg,
                "lat_p95_max": max_p95,
                "lat_p99_max": max_p99,
                "lat_max_max": max_max,
            }
        )
    out.sort(key=lambda g: g["first_start"])
    # 全局按时间升序编号"第N次"，方便汇报时按顺序点名
    for idx, g in enumerate(out, start=1):
        g["seq"] = idx
        g["seq_label"] = f"第{idx}次"
    return out


# --------------------------------------------------------------------------- #
# CSV 输出
# --------------------------------------------------------------------------- #
DETAIL_HEADERS = [
    ("group_seq_label", "序号"),
    ("start_time", "开始时间"),
    ("group", "测试组"),
    ("subitem", "子项"),
    ("device_count", "设备数"),
    ("duration_sec", "时长(s)"),
    ("planned_qps", "目标QPS"),
    ("actual_qps", "实际QPS"),
    ("total", "总请求"),
    ("effective", "样本数"),
    ("success", "成功"),
    ("fail", "失败"),
    ("err_41203", "41203 超时数"),
    ("err_other", "其他错误数"),
    ("success_rate", "成功率%"),
    ("lat_min", "min(ms)"),
    ("lat_avg", "avg(ms)"),
    ("lat_p50", "p50(ms)"),
    ("lat_p95", "p95(ms)"),
    ("lat_p99", "p99(ms)"),
    ("lat_max", "max(ms)"),
    ("drop_with_log", "丢弃-记日志"),
    ("drop_silent", "丢弃-静默"),
    ("top_errno", "Top错误码"),
]

GROUP_HEADERS = [
    ("seq_label", "序号"),
    ("first_start", "首次开始时间"),
    ("group", "测试组"),
    ("n_runs", "机器数"),
    ("total_devices", "总设备数"),
    ("planned_qps_sum", "目标总QPS"),
    ("actual_qps_sum", "实际总QPS"),
    ("total", "总请求"),
    ("effective", "样本数"),
    ("success", "成功"),
    ("fail", "失败"),
    ("err_41203", "41203 超时合计"),
    ("err_41203_pm", "41203 超时‰"),
    ("err_other", "其他错误合计"),
    ("rate", "加权成功率%"),
    ("lat_avg_w", "加权avg(ms)"),
    ("lat_p95_max", "最差p95(ms)"),
    ("lat_p99_max", "最差p99(ms)"),
    ("lat_max_max", "最差max(ms)"),
    ("drop_with_log", "丢弃-记日志合计"),
    ("drop_silent", "丢弃-静默合计"),
]

# HTML 主表里不展示 drop_*（迁到底部折叠区），其余列与 CSV 保持一致
_HIDDEN_IN_HTML = {"drop_with_log", "drop_silent"}
DETAIL_HTML_HEADERS = [h for h in DETAIL_HEADERS if h[0] not in _HIDDEN_IN_HTML]
GROUP_HTML_HEADERS = [h for h in GROUP_HEADERS if h[0] not in _HIDDEN_IN_HTML]


def write_csv(path: Path, rows: list[dict], headers: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([h for _, h in headers])
        for row in rows:
            line = []
            for key, _ in headers:
                value = row.get(key, "")
                if isinstance(value, float):
                    value = f"{value:.2f}"
                line.append(value)
            writer.writerow(line)


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #
# 使用 string.Template（$占位符），避免与 CSS / Chart.js 配置中的 {} 冲突。
HTML_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Amber 压测汇总 - $generated_at</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f4f6f9; color: #2c3e50; }
  h1 { margin: 0 0 16px; }
  h2 { margin: 24px 0 12px; color: #34495e; border-left: 4px solid #3498db; padding-left: 10px; }
  .card { background: #fff; padding: 18px 22px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 18px; overflow-x: auto; }
  .meta { color: #7f8c8d; font-size: 13px; line-height: 1.6; margin-bottom: 16px; }
  .meta code { background: #ecf0f1; padding: 1px 6px; border-radius: 3px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border: 1px solid #e1e4e8; padding: 7px 9px; text-align: right; white-space: nowrap; }
  th { background: #ecf0f1; text-align: center; font-weight: 600; }
  td.text { text-align: left; }
  tr:nth-child(even) td { background: #fbfcfd; }
  tr:hover td { background: #fff7e0; }
  .ok   { color: #27ae60; font-weight: 600; }
  .warn { color: #e67e22; font-weight: 600; }
  .bad  { color: #c0392b; font-weight: 600; }
  .group-row td { background: #f0f7ff; font-weight: 600; }
  .group-row:hover td { background: #e1efff; }
  .err41203 { color: #c0392b; font-weight: 600; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .kpi  { background: #fff; padding: 14px 18px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); border-left: 4px solid #3498db; }
  .kpi.ok   { border-left-color: #27ae60; }
  .kpi.warn { border-left-color: #e67e22; }
  .kpi.bad  { border-left-color: #c0392b; }
  .kpi .label { color: #7f8c8d; font-size: 12px; }
  .kpi .value { font-size: 24px; font-weight: 700; margin-top: 4px; color: #2c3e50; }
  .kpi .sub   { font-size: 12px; color: #95a5a6; margin-top: 2px; }
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 18px; }
  .chart-box { background: #fff; padding: 12px 16px 14px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .chart-box h3 { margin: 0 0 8px; font-size: 14px; color: #34495e; }
  .chart-box .canvas-wrap { position: relative; height: 280px; }
  .dropzone { background: #fafafa; border: 1px dashed #d5d8dc; border-radius: 8px; padding: 8px 14px; color: #95a5a6; margin-top: 24px; }
  .dropzone summary { cursor: pointer; font-size: 13px; color: #7f8c8d; padding: 4px 0; }
  .dropzone[open] { background: #fff; padding: 12px 18px; }
  .dropzone[open] summary { color: #34495e; margin-bottom: 8px; }
  .dropzone .drop-meta { color: #95a5a6; font-size: 12px; margin: 6px 0 10px; }
  .dropzone table { font-size: 12px; }
  .dropzone th { background: #f5f6f7; }
  @media (max-width: 900px) {
    .charts { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
  <h1>Amber 压测汇总</h1>
  <div class="meta">
    生成时间：<code>$generated_at</code><br>
    数据源：<code>测试报告_cleaned/</code>（仅 <code>duration_seconds = $target_duration</code> 的正式压测）<br>
    成功率分母 = 总请求 − 丢弃-记日志 − 丢弃-静默（丢弃数仅作参考，不参与成功率计算）<br>
    重点错误码说明：<code>errno 41203</code> = <b>请求超时</b>（本报告将其单列、并以柱+折线图突出展示）<br>
    共 <b>$n_total</b> 次有效压测，分为 <b>$n_groups</b> 个测试组（按时间升序统一编号"第1次 … 第N次"）
  </div>

  <h2>关键指标</h2>
  $kpi_section

  <h2>趋势图</h2>
  <div class="charts">
    <div class="chart-box"><h3>单台响应时间趋势（ms，横轴：单次压测按时间升序）</h3><div class="canvas-wrap"><canvas id="chartLatencySingle"></canvas></div></div>
    <div class="chart-box"><h3>分布式客户机响应时间趋势（ms，横轴：客户机/单次压测按时间升序）</h3><div class="canvas-wrap"><canvas id="chartLatencyDistributed"></canvas></div></div>
    <div class="chart-box"><h3>41203 请求超时（柱=绝对数；线=占样本数 ‰）</h3><div class="canvas-wrap"><canvas id="chart41203"></canvas></div></div>
    <div class="chart-box"><h3>加权成功率（%）</h3><div class="canvas-wrap"><canvas id="chartRate"></canvas></div></div>
    <div class="chart-box"><h3>实际 QPS vs 目标 QPS</h3><div class="canvas-wrap"><canvas id="chartQps"></canvas></div></div>
  </div>

  <h2>测试组聚合（按各组首次开始时间排序）</h2>
  <div class="card">
    $group_table
  </div>

  <h2>明细（按开始时间排序）</h2>
  <div class="card">
    $detail_table
  </div>

  <details class="dropzone">
    <summary>丢弃数据（参考，不计入成功率）&nbsp;&nbsp;合计：记日志 $drop_total_log 条 + 静默 $drop_total_silent 条 = $drop_total_all 条</summary>
    <div class="drop-meta">下列两类丢弃在每次运行后被排除在成功率分母之外：drop-with-log（已记入 errors.log）、drop-silent（静默丢弃）。仅作回溯参考。</div>
    $drop_table
  </details>

  <script>
    const GROUPS = $groups_json;
    const DETAILS_SINGLE = $details_single_json;
    const DETAILS_DISTRIBUTED = $details_distributed_json;

    function rateColor(v) {
      if (v >= 99.5) return '#27ae60';
      if (v >= 98.0) return '#e67e22';
      return '#c0392b';
    }
    const labels = GROUPS.map(g => g.label);
    const singleLabels = DETAILS_SINGLE.map(d => d.label);
    const distributedLabels = DETAILS_DISTRIBUTED.map(d => d.label);

    function latencyDatasets(details) {
      return [
        { label: 'min', data: details.map(d => d.lat_min), borderColor: '#16a085', backgroundColor: '#16a08533', tension: 0.2 },
        { label: 'avg', data: details.map(d => d.lat_avg), borderColor: '#3498db', backgroundColor: '#3498db33', tension: 0.2 },
        { label: 'p50', data: details.map(d => d.lat_p50), borderColor: '#8e44ad', backgroundColor: '#8e44ad33', tension: 0.2 },
        { label: 'p95', data: details.map(d => d.lat_p95), borderColor: '#f39c12', backgroundColor: '#f39c1233', tension: 0.2 },
        { label: 'p99', data: details.map(d => d.lat_p99), borderColor: '#c0392b', backgroundColor: '#c0392b33', tension: 0.2 },
        { label: 'max', data: details.map(d => d.lat_max), borderColor: '#2d3436', backgroundColor: '#2d343633', tension: 0.2 }
      ];
    }

    function buildLatencyChart(canvasId, labels, details) {
      new Chart(document.getElementById(canvasId), {
        type: 'line',
        data: {
          labels: labels,
          datasets: latencyDatasets(details)
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } },
          plugins: { legend: { position: 'bottom' } }
        }
      });
    }

    buildLatencyChart('chartLatencySingle', singleLabels, DETAILS_SINGLE);
    buildLatencyChart('chartLatencyDistributed', distributedLabels, DETAILS_DISTRIBUTED);

    new Chart(document.getElementById('chart41203'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          { type: 'bar',  label: '41203 超时数', data: GROUPS.map(g => g.err_41203),    yAxisID: 'y',  backgroundColor: '#c0392b' },
          { type: 'line', label: '41203 超时‰',  data: GROUPS.map(g => g.err_41203_pm), yAxisID: 'y1', borderColor: '#e67e22', backgroundColor: '#e67e2233', tension: 0.2 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          y:  { type: 'linear', position: 'left',  beginAtZero: true, title: { display: true, text: '绝对数' } },
          y1: { type: 'linear', position: 'right', beginAtZero: true, title: { display: true, text: '‰ (千分比)' }, grid: { drawOnChartArea: false } }
        },
        plugins: { legend: { position: 'bottom' } }
      }
    });

    new Chart(document.getElementById('chartRate'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{ label: '加权成功率 %', data: GROUPS.map(g => g.rate), backgroundColor: GROUPS.map(g => rateColor(g.rate)) }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { min: Math.max(0, Math.floor(Math.min.apply(null, GROUPS.map(g => g.rate)) - 2)), max: 100, title: { display: true, text: '%' } } },
        plugins: { legend: { display: false } }
      }
    });

    new Chart(document.getElementById('chartQps'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          { label: '目标 QPS', data: GROUPS.map(g => g.planned_qps_sum), backgroundColor: '#95a5a6' },
          { label: '实际 QPS', data: GROUPS.map(g => g.actual_qps_sum),  backgroundColor: '#3498db' }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { beginAtZero: true } },
        plugins: { legend: { position: 'bottom' } }
      }
    });
  </script>
</body>
</html>
""")


def _rate_class(rate: float) -> str:
    if rate >= 99.5:
        return "ok"
    if rate >= 98.0:
        return "warn"
    return "bad"


def _fmt(value, decimals: int | None = None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.{decimals if decimals is not None else 2}f}"
    return str(value)


def _render_detail_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="meta">没有匹配的报告</div>'
    head_html = "<tr>" + "".join(
        f"<th>{html.escape(h)}</th>" for _, h in DETAIL_HTML_HEADERS
    ) + "</tr>"

    body_rows = []
    for r in rows:
        cells = []
        for key, _ in DETAIL_HTML_HEADERS:
            v = r[key]
            if key == "success_rate":
                cls = _rate_class(v)
                cells.append(f'<td class="{cls}">{v:.2f}%</td>')
            elif key == "err_41203":
                cls = "err41203" if int(v) > 0 else ""
                cells.append(f'<td class="{cls}">{int(v)}</td>')
            elif key in ("group", "subitem", "start_time", "top_errno", "group_seq_label"):
                cells.append(f'<td class="text">{html.escape(str(v))}</td>')
            elif key == "duration_sec":
                cells.append(f"<td>{int(v)}</td>")
            else:
                cells.append(f"<td>{html.escape(_fmt(v))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        f"<table><thead>{head_html}</thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )


def _render_group_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="meta">没有匹配的报告</div>'
    head_html = "<tr>" + "".join(
        f"<th>{html.escape(h)}</th>" for _, h in GROUP_HTML_HEADERS
    ) + "</tr>"
    body_rows = []
    for r in rows:
        cells = []
        for key, _ in GROUP_HTML_HEADERS:
            v = r[key]
            if key == "rate":
                cls = _rate_class(v)
                cells.append(f'<td class="{cls}">{v:.2f}%</td>')
            elif key == "err_41203":
                cls = "err41203" if int(v) > 0 else ""
                cells.append(f'<td class="{cls}">{int(v)}</td>')
            elif key == "err_41203_pm":
                cls = "err41203" if v >= 1.0 else ""
                cells.append(f'<td class="{cls}">{v:.2f}</td>')
            elif key in ("group", "first_start", "seq_label"):
                cells.append(f'<td class="text">{html.escape(str(v))}</td>')
            elif key in ("planned_qps_sum", "actual_qps_sum"):
                cells.append(f"<td>{v:.2f}</td>")
            elif key in ("lat_avg_w", "lat_p95_max", "lat_p99_max", "lat_max_max"):
                cells.append(f"<td>{v:.2f}</td>")
            else:
                cells.append(f"<td>{v}</td>")
        body_rows.append('<tr class="group-row">' + "".join(cells) + "</tr>")

    return (
        f"<table><thead>{head_html}</thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )


def _render_kpi_section(detail_rows: list[dict], group_rows: list[dict]) -> str:
    """顶部 KPI 卡片：重点突出 41203 与响应时间。"""
    if not detail_rows:
        return '<div class="meta">没有匹配的报告</div>'

    total_sample = sum(r["effective"] for r in detail_rows)
    total_success = sum(r["success"] for r in detail_rows)
    total_fail = sum(r["fail"] for r in detail_rows)
    total_41203 = sum(r["err_41203"] for r in detail_rows)
    pm_41203 = (total_41203 / total_sample * 1000.0) if total_sample > 0 else 0.0
    weighted_rate = (total_success / total_sample * 100.0) if total_sample > 0 else 0.0
    worst_p95 = max((_safe_float(r["lat_p95"]) for r in detail_rows), default=0.0)
    worst_p99 = max((_safe_float(r["lat_p99"]) for r in detail_rows), default=0.0)

    rate_cls = _rate_class(weighted_rate)
    err_cls = "bad" if pm_41203 >= 5 else ("warn" if pm_41203 >= 1 else "ok")

    cards = [
        ("", "总样本数", f"{total_sample:,}", "成功率分母合计"),
        ("ok",  "总成功",  f"{total_success:,}", ""),
        ("bad", "总失败",  f"{total_fail:,}", ""),
        (err_cls, "41203 请求超时", f"{total_41203:,}", f"占样本数 {pm_41203:.2f} ‰"),
        (rate_cls, "加权成功率", f"{weighted_rate:.2f}%", f"{len(detail_rows)} 次压测 / {len(group_rows)} 组"),
        ("warn", "最差 p95 / p99", f"{worst_p95:.0f} / {worst_p99:.0f} ms", "全部明细中的最大值"),
    ]
    return (
        '<div class="kpis">'
        + "".join(
            f'<div class="kpi {cls}"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}</div>'
            f'<div class="sub">{html.escape(sub)}</div></div>'
            for cls, label, value, sub in cards
        )
        + "</div>"
    )


def _render_drop_table(rows: list[dict]) -> str:
    """折叠区里的丢弃数据小表，灰色低调样式。"""
    if not rows:
        return '<div class="meta">没有数据</div>'
    head = (
        "<tr>"
        "<th>开始时间</th><th>测试组</th><th>子项</th>"
        "<th>丢弃-记日志</th><th>丢弃-静默</th><th>合计</th>"
        "</tr>"
    )
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f'<td class="text">{html.escape(str(r["start_time"]))}</td>'
            f'<td class="text">{html.escape(str(r["group"]))}</td>'
            f'<td class="text">{html.escape(str(r["subitem"]))}</td>'
            f'<td>{int(r["drop_with_log"])}</td>'
            f'<td>{int(r["drop_silent"])}</td>'
            f'<td>{int(r["drop_total"])}</td>'
            "</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def _build_groups_json(group_rows: list[dict]) -> str:
    """提取给 Chart.js 的精简数组（label + 关键指标），序列化成 JSON。"""
    payload = [
        {
            "label": f'{g["seq_label"]} {g["group"]}',
            "lat_avg_w": float(g["lat_avg_w"]),
            "lat_p95_max": float(g["lat_p95_max"]),
            "lat_p99_max": float(g["lat_p99_max"]),
            "err_41203": int(g["err_41203"]),
            "err_41203_pm": float(g["err_41203_pm"]),
            "rate": float(g["rate"]),
            "planned_qps_sum": float(g["planned_qps_sum"]),
            "actual_qps_sum": float(g["actual_qps_sum"]),
        }
        for g in group_rows
    ]
    return json.dumps(payload, ensure_ascii=False)


def _is_distributed_row(row: dict) -> bool:
    """按目录结构判断是否属于分布式压测子机。"""
    return bool(str(row.get("subitem", "")).strip())


_SUBITEM_INDEX_RE = re.compile(r"第\s*(\d+)\s*台")


def _subitem_sort_key(text: str) -> tuple[int, str]:
    """对子项做自然排序：第1台 < 第2台 < 第10台。"""
    text = str(text or "").strip()
    match = _SUBITEM_INDEX_RE.search(text)
    if match:
        return int(match.group(1)), text
    return 10**9, text


def _build_details_json(detail_rows: list[dict]) -> str:
    """提取给 Chart.js 的明细级延迟序列。"""
    sorted_rows = sorted(
        detail_rows,
        key=lambda r: (
            int(r.get("group_seq", 0)),
            _subitem_sort_key(r.get("subitem", "")),
            str(r.get("start_time", "")),
        ),
    )
    payload = [
        {
            "label": (
                f'{r["group_seq_label"]} '
                f'{r["subitem"] or r["group"]} '
                f'{r["start_time"]}'
            ).strip(),
            "lat_min": float(_safe_float(r["lat_min"])),
            "lat_avg": float(_safe_float(r["lat_avg"])),
            "lat_p50": float(_safe_float(r["lat_p50"])),
            "lat_p95": float(_safe_float(r["lat_p95"])),
            "lat_p99": float(_safe_float(r["lat_p99"])),
            "lat_max": float(_safe_float(r["lat_max"])),
        }
        for r in sorted_rows
    ]
    return json.dumps(payload, ensure_ascii=False)


def render_html(
    detail_rows: list[dict],
    group_rows: list[dict],
    generated_at: str,
) -> str:
    drop_total_log = sum(r["drop_with_log"] for r in detail_rows)
    drop_total_silent = sum(r["drop_silent"] for r in detail_rows)
    return HTML_TEMPLATE.safe_substitute(
        generated_at=html.escape(generated_at),
        target_duration=TARGET_DURATION_SEC,
        n_total=len(detail_rows),
        n_groups=len(group_rows),
        kpi_section=_render_kpi_section(detail_rows, group_rows),
        group_table=_render_group_table(group_rows),
        detail_table=_render_detail_table(detail_rows),
        drop_table=_render_drop_table(detail_rows),
        drop_total_log=drop_total_log,
        drop_total_silent=drop_total_silent,
        drop_total_all=drop_total_log + drop_total_silent,
        groups_json=_build_groups_json(group_rows),
        details_single_json=_build_details_json(
            [r for r in detail_rows if not _is_distributed_row(r)]
        ),
        details_distributed_json=_build_details_json(
            [r for r in detail_rows if _is_distributed_row(r)]
        ),
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    runs = find_runs(SOURCE_ROOTS)
    print(f"扫描到候选运行目录 : {len(runs)}（已按 run 目录名去重，cleaned 优先）")

    rows: list[dict] = []
    skipped: list[tuple[Path, str]] = []
    for run_dir in runs:
        try:
            row = collect_one(run_dir)
        except Exception as exc:
            skipped.append((run_dir, f"解析失败: {exc}"))
            continue
        if row is None:
            skipped.append((run_dir, "duration != 300s，跳过"))
            continue
        rows.append(row)

    rows.sort(key=lambda r: r["start_time"])
    groups = aggregate_groups(rows)

    # 把组的"第N次"序号回填到每条明细行，明细表也能直接显示
    seq_map = {g["group"]: (g["seq"], g["seq_label"]) for g in groups}
    for r in rows:
        seq, label = seq_map.get(r["group"], (0, ""))
        r["group_seq"] = seq
        r["group_seq_label"] = label

    print(f"有效压测            : {len(rows)} 次，分为 {len(groups)} 个测试组")
    if skipped:
        print(f"跳过                : {len(skipped)} 个")
        for p, reason in skipped:
            try:
                rel = p.relative_to(ROOT)
            except ValueError:
                rel = p
            print(f"  - {rel}  ({reason})")

    if not rows:
        print("没有可汇总的报告，终止。", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / f"summary_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    detail_csv = out_dir / "detail.csv"
    group_csv = out_dir / "group.csv"
    summary_html = out_dir / "summary.html"

    write_csv(detail_csv, rows, DETAIL_HEADERS)
    write_csv(group_csv, groups, GROUP_HEADERS)
    summary_html.write_text(
        render_html(
            rows,
            groups,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
        encoding="utf-8",
    )

    print("-" * 72)
    print(f"输出目录            : {out_dir}")
    print(f"  汇总 HTML         : {summary_html}")
    print(f"  明细 CSV          : {detail_csv}")
    print(f"  组聚合 CSV        : {group_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
