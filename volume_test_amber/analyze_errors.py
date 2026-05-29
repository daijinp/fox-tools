"""analyze_errors.py - 跨多次压测的错误 SN 横向对比分析。

扫描 reports/run_* 下所有 run 的 errors.log，按"SN + errno 一致性"维度把
失败过的 SN 分成 4 类：
  - hard_always    在所有测到它的 run 里都失败，且 errno 始终相同（强确定性问题）
  - always_varying 每次都失败但 errno 变化（服务端抖动 / 上下文相关）
  - flaky          在部分 run 失败
  - one_off        运行过至少 2 次，但只失败过 1 次

产物（默认 reports/_analysis/analysis_<时间戳>/）：
  - 控制台摘要 + Top-K SN 表
  - sn_summary.csv       每 SN 一行的汇总表
  - sn_by_run.csv        SN × run 的透视表
  - analysis.html        与 run 报告同风格的可视化报告

用法（使用项目自带 venv，纯 stdlib 无需 pip install）：
  E:\\MyCode\\pythonProject\\tools\\venv\\Scripts\\python.exe \\
      volume_test_amber\\analyze_errors.py
  # 或激活 venv 后:
  python volume_test_amber\\analyze_errors.py --last 5 --top 50
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_DIR_PATTERN = re.compile(r"^run_(\d{8}_\d{6})(?:_.*)?$")

CATEGORY_ORDER = ("hard_always", "always_varying", "flaky", "one_off")
CATEGORY_LABELS = {
    "hard_always": "硬性错误（每次 errno 相同）",
    "always_varying": "每次失败但 errno 变化",
    "flaky": "偶发失败 (flaky)",
    "one_off": "单次偶发",
}


# ---------------- 数据模型 ----------------


@dataclass
class FailureEntry:
    errno: int
    msg: str
    timestamp: str
    elapsed_ms: float


@dataclass
class RunInfo:
    run_id: str
    timestamp: datetime
    dir: Path
    device_count: int
    sn_file: str
    sn_start_index: int = 1
    population_order: list[str] = field(default_factory=list)
    population: set[str] = field(default_factory=set)
    failures: dict[str, FailureEntry] = field(default_factory=dict)
    errors_parsed: int = 0
    errors_skipped: int = 0


@dataclass
class SNStat:
    sn: str
    runs_tested: int = 0
    fail_count: int = 0
    distinct_errnos: set[int] = field(default_factory=set)
    # run_id -> errno（失败）或 None（测试且通过）；key 不存在 = 未测试
    errno_by_run: dict[str, Optional[int]] = field(default_factory=dict)
    last_msg: str = ""
    last_timestamp: str = ""

    @property
    def fail_rate(self) -> float:
        return (self.fail_count / self.runs_tested) if self.runs_tested else 0.0

    @property
    def category(self) -> str:
        return classify(self.fail_count, self.runs_tested, self.distinct_errnos)


def classify(fail_count: int, runs_tested: int, distinct_errnos: set[int]) -> str:
    # 顺序敏感：先判定两种"每次都挂"的场景，再判定 one_off，最后兜底 flaky
    if fail_count == runs_tested and runs_tested > 0:
        return "hard_always" if len(distinct_errnos) == 1 else "always_varying"
    if fail_count == 1 and runs_tested > 1:
        return "one_off"
    return "flaky"


# ---------------- I/O ----------------


def anchor(path_str: str, base: Path) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def read_sn_list(path: Path) -> list[str]:
    sns: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            for sep in (",", "\t"):
                if sep in s:
                    s = s.split(sep, 1)[0].strip()
                    break
            if s:
                sns.append(s)
    return sns


def parse_errors_log(path: Path) -> tuple[list[dict], int, int]:
    entries: list[dict] = []
    parsed = 0
    skipped = 0
    if not path.exists():
        return entries, 0, 0
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
                parsed += 1
            except json.JSONDecodeError as e:
                skipped += 1
                print(f"  [warn] {path.name}:{i} JSON 损坏已跳过: {e}", file=sys.stderr)
    return entries, parsed, skipped


def load_run(run_dir: Path, script_dir: Path) -> Optional[RunInfo]:
    m = RUN_DIR_PATTERN.match(run_dir.name)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        print(f"  [warn] 无法解析时间戳，跳过 {run_dir.name}", file=sys.stderr)
        return None

    cfg_path = run_dir / "config.snapshot.json"
    if not cfg_path.exists():
        print(f"  [warn] 缺少 config.snapshot.json，跳过 {run_dir.name}", file=sys.stderr)
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  [warn] config.snapshot.json 解析失败 {run_dir.name}: {e}", file=sys.stderr)
        return None

    device_count = int(cfg.get("device_count", 0) or 0)
    sn_file = str(cfg.get("sn_file", "")).strip()
    # sn_start_index 1-based；旧 run 没有该字段时按 1 处理，保持向后兼容
    sn_start_index = int(cfg.get("sn_start_index", 1) or 1)
    if sn_start_index < 1:
        print(
            f"  [warn] {run_dir.name} 的 sn_start_index={sn_start_index} 非法，已按 1 处理",
            file=sys.stderr,
        )
        sn_start_index = 1
    if device_count <= 0 or not sn_file:
        print(f"  [warn] 配置缺 device_count / sn_file，跳过 {run_dir.name}", file=sys.stderr)
        return None

    sn_path = anchor(sn_file, script_dir)
    if not sn_path.exists():
        print(f"  [warn] SN 文件不存在 {sn_path}，跳过 {run_dir.name}", file=sys.stderr)
        return None

    all_sns = read_sn_list(sn_path)
    start0 = sn_start_index - 1
    end0 = start0 + device_count
    if start0 >= len(all_sns):
        print(
            f"  [warn] {run_dir.name} 起始下标 {sn_start_index} 超出 SN 列表长度 {len(all_sns)}，跳过",
            file=sys.stderr,
        )
        return None
    if end0 > len(all_sns):
        # 历史 run 下标溢出 —— 分析阶段不终止，按实际可用长度截断并提示
        print(
            f"  [warn] {run_dir.name} 期望 SN 范围 #{sn_start_index}~#{end0} 超出列表长度 {len(all_sns)}，"
            f"population 按 #{sn_start_index}~#{len(all_sns)} 截断",
            file=sys.stderr,
        )
        end0 = len(all_sns)
    population_order = all_sns[start0:end0]
    population = set(population_order)

    info = RunInfo(
        run_id=run_dir.name,
        timestamp=ts,
        dir=run_dir,
        device_count=device_count,
        sn_file=sn_file,
        sn_start_index=sn_start_index,
        population_order=population_order,
        population=population,
    )

    entries, parsed, skipped = parse_errors_log(run_dir / "errors.log")
    info.errors_parsed = parsed
    info.errors_skipped = skipped

    for e in entries:
        sn = str(e.get("sn", "")).strip()
        if not sn:
            continue
        try:
            errno = int(e.get("errno", 0))
        except (TypeError, ValueError):
            errno = -999
        if errno == 0:
            # errors.log 里按约定不应有 errno=0；防御性忽略
            continue
        msg = str(e.get("msg", ""))
        timestamp = str(e.get("timestamp", ""))
        try:
            elapsed_ms = float(e.get("elapsed_ms", 0))
        except (TypeError, ValueError):
            elapsed_ms = 0.0
        info.failures[sn] = FailureEntry(errno, msg, timestamp, elapsed_ms)

    return info


def scan_runs(
    reports_root: Path,
    script_dir: Path,
    filter_runs: Optional[list[str]],
    last_n: Optional[int],
) -> list[RunInfo]:
    if not reports_root.exists():
        print(f"错误：reports 目录不存在：{reports_root}", file=sys.stderr)
        sys.exit(1)

    candidates: list[Path] = []
    for child in sorted(reports_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        if not RUN_DIR_PATTERN.match(child.name):
            continue
        if filter_runs and child.name not in filter_runs:
            continue
        candidates.append(child)

    if filter_runs:
        missing = set(filter_runs) - {c.name for c in candidates}
        if missing:
            print(f"错误：未找到指定 run：{', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(1)

    runs: list[RunInfo] = []
    for d in candidates:
        info = load_run(d, script_dir)
        if info is not None:
            runs.append(info)
    runs.sort(key=lambda r: r.timestamp)
    if last_n and last_n > 0:
        runs = runs[-last_n:]
    return runs


# ---------------- 聚合 ----------------


def consistency_check(runs: list[RunInfo]) -> dict:
    sn_files = {r.sn_file for r in runs}
    device_counts = {r.device_count for r in runs}
    warnings: list[str] = []
    if len(sn_files) > 1:
        warnings.append(
            f"sn_file 不一致：{sorted(sn_files)}（population 不同，runs_tested 会按各 run 实际情况精确计算）"
        )
    if len(device_counts) > 1:
        warnings.append(
            f"device_count 不一致：{sorted(device_counts)}（分母会因 run 而异）"
        )
    return {
        "sn_files": sn_files,
        "device_counts": device_counts,
        "warnings": warnings,
    }


def aggregate(runs: list[RunInfo]) -> dict[str, SNStat]:
    # 只为"至少失败过一次"的 SN 生成记录，避免巨量 SN 全部展开
    failed_sns: set[str] = set()
    for r in runs:
        failed_sns.update(r.failures.keys())

    stats: dict[str, SNStat] = {}
    for sn in failed_sns:
        s = SNStat(sn=sn)
        for r in runs:
            in_pop = sn in r.population
            fe = r.failures.get(sn)
            if in_pop:
                s.runs_tested += 1
                if fe is not None:
                    s.fail_count += 1
                    s.distinct_errnos.add(fe.errno)
                    s.errno_by_run[r.run_id] = fe.errno
                    if fe.timestamp >= s.last_timestamp:
                        s.last_timestamp = fe.timestamp
                        s.last_msg = fe.msg
                else:
                    s.errno_by_run[r.run_id] = None  # 测试了，通过
            else:
                if fe is not None:
                    # SN 不在声明的 population，但 errors.log 里出现了
                    # 认定它确实被测试且失败（population 估算不完美时兜底）
                    s.runs_tested += 1
                    s.fail_count += 1
                    s.distinct_errnos.add(fe.errno)
                    s.errno_by_run[r.run_id] = fe.errno
                    if fe.timestamp >= s.last_timestamp:
                        s.last_timestamp = fe.timestamp
                        s.last_msg = fe.msg
                # 否则 SN 压根没参与该 run，不写 errno_by_run
        stats[sn] = s
    return stats


# ---------------- 输出：控制台 ----------------


def format_errno_seq(stat: SNStat, runs: list[RunInfo], max_len: int = 30) -> str:
    parts: list[str] = []
    for r in runs:
        if r.run_id in stat.errno_by_run:
            v = stat.errno_by_run[r.run_id]
            parts.append("OK" if v is None else str(v))
        else:
            parts.append("-")
    s = ",".join(parts)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def print_console(
    runs: list[RunInfo],
    stats: dict[str, SNStat],
    cons: dict,
    top_k: int,
    out_dir: Path,
) -> None:
    print("=" * 72)
    print("跨 run 错误 SN 分析")
    print("=" * 72)
    if runs:
        print(f"扫描 run 数量 : {len(runs)}")
        print(
            f"时间跨度      : {runs[0].timestamp} ~ {runs[-1].timestamp}"
        )
        print(f"sn_file       : {', '.join(sorted(cons['sn_files'])) or '-'}")
        print(
            f"device_count  : {', '.join(str(x) for x in sorted(cons['device_counts'])) or '-'}"
        )
    for w in cons["warnings"]:
        print(f"[警告] {w}")
    print()

    print("-" * 72)
    print(f"{'run_id':<44} {'device_count':>12} {'fails':>6}")
    for r in runs:
        print(f"{r.run_id:<44} {r.device_count:>12} {len(r.failures):>6}")
    print()

    cat_count = Counter(s.category for s in stats.values())
    print("-" * 72)
    print("失败 SN 分类（仅包含至少失败过 1 次的 SN）:")
    print(f"  合计                                   : {len(stats)}")
    for c in CATEGORY_ORDER:
        print(f"  {CATEGORY_LABELS[c]:<38} : {cat_count.get(c, 0)}")
    print()

    if not stats:
        print("没有任何 SN 发生过 errno != 0 的错误。")
        print(f"产物目录      : {out_dir}")
        return

    ordered = sorted(stats.values(), key=lambda s: (-s.fail_count, s.sn))
    k = min(top_k, len(ordered))
    print("-" * 72)
    print(f"Top-{k} 失败 SN:")
    print(f"{'SN':<22} {'fail/tested':>12}  {'errno_seq':<32} {'category'}")
    for s in ordered[:k]:
        seq = format_errno_seq(s, runs, max_len=32)
        print(
            f"{s.sn:<22} {f'{s.fail_count}/{s.runs_tested}':>12}  {seq:<32} {s.category}"
        )
    print()
    print(f"产物目录      : {out_dir}")


# ---------------- 输出：CSV ----------------


def write_csv_summary(path: Path, stats: dict[str, SNStat]) -> None:
    ordered = sorted(stats.values(), key=lambda s: (-s.fail_count, s.sn))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "sn",
                "runs_tested",
                "fail_count",
                "fail_rate",
                "distinct_errnos_count",
                "distinct_errnos",
                "category",
                "last_msg",
            ]
        )
        for s in ordered:
            errnos_sorted = sorted(s.distinct_errnos)
            w.writerow(
                [
                    s.sn,
                    s.runs_tested,
                    s.fail_count,
                    f"{s.fail_rate:.4f}",
                    len(errnos_sorted),
                    "|".join(str(e) for e in errnos_sorted),
                    s.category,
                    s.last_msg,
                ]
            )


def write_csv_pivot(path: Path, runs: list[RunInfo], stats: dict[str, SNStat]) -> None:
    ordered = sorted(stats.values(), key=lambda s: (-s.fail_count, s.sn))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["# timestamp"]
            + [r.timestamp.strftime("%Y-%m-%d %H:%M:%S") for r in runs]
        )
        w.writerow(["# device_count"] + [r.device_count for r in runs])
        w.writerow(["sn"] + [r.run_id for r in runs])
        for s in ordered:
            row: list[str] = [s.sn]
            for r in runs:
                if r.run_id in s.errno_by_run:
                    v = s.errno_by_run[r.run_id]
                    row.append("OK" if v is None else str(v))
                else:
                    row.append("")
            w.writerow(row)


# ---------------- 输出：HTML ----------------


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8">
<title>压测错误 SN 横向对比 - {generated_at}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f4f6f9; color: #2c3e50; }}
  h1 {{ margin: 0 0 16px; }}
  h2 {{ margin: 24px 0 12px; color: #34495e; border-left: 4px solid #3498db; padding-left: 10px; }}
  .card {{ background: #fff; padding: 18px 22px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 18px; }}
  .metrics {{ display: flex; flex-wrap: wrap; gap: 18px; }}
  .metric {{ flex: 1 1 180px; background: #fafbfc; border: 1px solid #eceff1; border-radius: 6px; padding: 12px 14px; }}
  .metric .label {{ color: #7f8c8d; font-size: 12px; margin-bottom: 4px; }}
  .metric .value {{ font-size: 22px; font-weight: 600; color: #2c3e50; }}
  .ok {{ color: #27ae60 !important; }}
  .bad {{ color: #c0392b !important; }}
  .warn {{ color: #e67e22 !important; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ border: 1px solid #e1e4e8; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #ecf0f1; }}
  tr:nth-child(even) td {{ background: #fbfcfd; }}
  code {{ background: #ecf0f1; padding: 1px 6px; border-radius: 3px; font-size: 13px; }}
  .muted {{ color: #95a5a6; font-size: 12px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #ecf0f1; color: #34495e; }}
  .tag.bad {{ background: #fdecea; color: #c0392b; }}
  .tag.warn {{ background: #fef3e0; color: #b36a00; }}
  .tag.ok {{ background: #e6f7ea; color: #1e7a3c; }}
  .warning-box {{ background: #fff3cd; border: 1px solid #ffeeba; color: #856404; padding: 10px 14px; border-radius: 6px; margin-bottom: 12px; }}
  .small {{ font-size: 12px; color: #7f8c8d; }}
  td.cell-fail {{ background: #fdecea !important; color: #c0392b; font-weight: 600; text-align: center; }}
  td.cell-ok {{ background: #f0f8f3 !important; color: #27ae60; text-align: center; }}
  td.cell-na {{ background: #f7f7f7 !important; color: #aaa; text-align: center; }}
  .pivot-wrap {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>压测错误 SN 横向对比</h1>
<div class="muted">生成时间：{generated_at} &nbsp; 扫描 run：{run_count} 个</div>

{warnings_html}

<h2>总览</h2>
<div class="card">
  <div class="metrics">
    <div class="metric"><div class="label">扫描 run 数</div><div class="value">{run_count}</div></div>
    <div class="metric"><div class="label">失败 SN 总数</div><div class="value bad">{total_failed_sn}</div></div>
    <div class="metric"><div class="label">硬性错误 SN</div><div class="value bad">{cnt_hard_always}</div></div>
    <div class="metric"><div class="label">errno 变化 SN</div><div class="value warn">{cnt_always_varying}</div></div>
    <div class="metric"><div class="label">Flaky SN</div><div class="value warn">{cnt_flaky}</div></div>
    <div class="metric"><div class="label">单次偶发 SN</div><div class="value">{cnt_one_off}</div></div>
  </div>
  <div class="small" style="margin-top:12px">
    时间跨度：{time_range}<br>
    sn_file：{sn_files_html}<br>
    device_count：{device_counts_str}
  </div>
</div>

<h2>各次压测概览</h2>
<div class="card">
<table>
<thead><tr><th>run_id</th><th>时间</th><th>SN 范围</th><th>device_count</th><th>失败数</th><th>errors.log 条目</th></tr></thead>
<tbody>
{per_run_rows}
</tbody>
</table>
</div>

<h2>Top {top_n} 失败 SN</h2>
<div class="card">
<table>
<thead><tr><th>SN</th><th>fail / tested</th><th>fail rate</th><th>distinct errnos</th><th>类别</th><th>最近 msg</th></tr></thead>
<tbody>
{top_rows}
</tbody>
</table>
</div>

<h2>SN × Run 透视表（全部失败 SN，共 {total_failed_sn} 条）</h2>
<div class="card">
<div class="small" style="margin-bottom:8px">单元格含义：
<span class="tag bad">errno</span> 该 run 失败 ·
<span class="tag ok">OK</span> 该 run 测试且通过 ·
<span class="tag">-</span> 该 run 未测试此 SN
</div>
<div class="pivot-wrap">
<table>
<thead>{pivot_head}</thead>
<tbody>
{pivot_rows}
</tbody>
</table>
</div>
</div>

<h2>产物文件</h2>
<div class="card">
<ul>
<li><a href="sn_summary.csv">sn_summary.csv</a> — SN 汇总表</li>
<li><a href="sn_by_run.csv">sn_by_run.csv</a> — SN × run 透视表 CSV</li>
</ul>
</div>
</body>
</html>
"""


def _category_tag(category: str) -> str:
    cls = {
        "hard_always": "bad",
        "always_varying": "warn",
        "flaky": "warn",
        "one_off": "",
    }.get(category, "")
    return f'<span class="tag {cls}">{category}</span>'


def write_html(
    path: Path,
    runs: list[RunInfo],
    stats: dict[str, SNStat],
    cons: dict,
    top_k: int,
) -> None:
    total_failed_sn = len(stats)
    cat_count = Counter(s.category for s in stats.values())
    ordered = sorted(stats.values(), key=lambda s: (-s.fail_count, s.sn))

    if runs:
        time_range = (
            f"{runs[0].timestamp.strftime('%Y-%m-%d %H:%M:%S')} "
            f"~ {runs[-1].timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        time_range = "-"

    warnings_html = ""
    if cons["warnings"]:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in cons["warnings"])
        warnings_html = (
            f'<div class="warning-box"><b>一致性警告：</b><ul>{items}</ul></div>'
        )

    per_run_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(r.run_id)}</code></td>"
        f"<td>{r.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</td>"
        f"<td>#{r.sn_start_index} ~ #{r.sn_start_index + r.device_count - 1}</td>"
        f"<td>{r.device_count}</td>"
        f"<td>{len(r.failures)}</td>"
        f"<td>{r.errors_parsed}"
        f"{f' <span class=\"warn\">(损坏 {r.errors_skipped})</span>' if r.errors_skipped else ''}"
        "</td></tr>"
        for r in runs
    )

    top_n = min(top_k, len(ordered))
    top_rows_list: list[str] = []
    for s in ordered[:top_n]:
        errnos_sorted = sorted(s.distinct_errnos)
        top_rows_list.append(
            "<tr>"
            f"<td><code>{html.escape(s.sn)}</code></td>"
            f"<td>{s.fail_count} / {s.runs_tested}</td>"
            f"<td>{s.fail_rate * 100:.1f}%</td>"
            f"<td>{','.join(str(e) for e in errnos_sorted)}</td>"
            f"<td>{_category_tag(s.category)}</td>"
            f"<td>{html.escape(s.last_msg[:200])}</td>"
            "</tr>"
        )
    top_rows = "\n".join(top_rows_list) or "<tr><td colspan=\"6\" class=\"muted\">无失败 SN</td></tr>"

    th_cells = ["<th>SN</th>"] + [
        "<th>"
        f"<div><code>{html.escape(r.run_id)}</code></div>"
        f"<div class=\"small\">{r.timestamp.strftime('%m-%d %H:%M')} · n={r.device_count}</div>"
        "</th>"
        for r in runs
    ]
    pivot_head = f"<tr>{''.join(th_cells)}</tr>"

    pivot_rows_list: list[str] = []
    for s in ordered:
        row = [f"<td><code>{html.escape(s.sn)}</code></td>"]
        for r in runs:
            if r.run_id in s.errno_by_run:
                v = s.errno_by_run[r.run_id]
                if v is None:
                    row.append('<td class="cell-ok">OK</td>')
                else:
                    row.append(f'<td class="cell-fail">{v}</td>')
            else:
                row.append('<td class="cell-na">-</td>')
        pivot_rows_list.append(f"<tr>{''.join(row)}</tr>")
    pivot_rows = "\n".join(pivot_rows_list) or "<tr><td class=\"muted\">无数据</td></tr>"

    sn_files_html = (
        ", ".join(f"<code>{html.escape(x)}</code>" for x in sorted(cons["sn_files"]))
        or "-"
    )
    device_counts_str = (
        ", ".join(str(x) for x in sorted(cons["device_counts"])) or "-"
    )

    content = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        run_count=len(runs),
        total_failed_sn=total_failed_sn,
        cnt_hard_always=cat_count.get("hard_always", 0),
        cnt_always_varying=cat_count.get("always_varying", 0),
        cnt_flaky=cat_count.get("flaky", 0),
        cnt_one_off=cat_count.get("one_off", 0),
        time_range=time_range,
        sn_files_html=sn_files_html,
        device_counts_str=device_counts_str,
        per_run_rows=per_run_rows,
        top_n=top_n,
        top_rows=top_rows,
        pivot_head=pivot_head,
        pivot_rows=pivot_rows,
        warnings_html=warnings_html,
    )
    path.write_text(content, encoding="utf-8")


# ---------------- 入口 ----------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="跨多次压测的错误 SN 横向对比分析",
    )
    p.add_argument(
        "--reports",
        type=Path,
        default=None,
        help="reports 目录（默认：脚本同级的 reports/）",
    )
    p.add_argument(
        "--last",
        type=int,
        default=0,
        help="只分析最近 N 次 run（0=全部）",
    )
    p.add_argument(
        "--runs",
        nargs="+",
        default=None,
        help="指定若干 run 目录名进行分析（可与 --last 叠加：先筛再取最近）",
    )
    p.add_argument(
        "--top",
        type=int,
        default=20,
        help="控制台打印 Top-K 失败 SN（默认 20）",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="分析产物目录（默认：<reports>/_analysis/analysis_<时间戳>/）",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    reports_root = (args.reports or (SCRIPT_DIR / "reports")).resolve()
    print(f"脚本目录      : {SCRIPT_DIR}")
    print(f"reports 目录  : {reports_root}")

    runs = scan_runs(reports_root, SCRIPT_DIR, args.runs, args.last or None)
    if not runs:
        print("没有找到可分析的 run_* 目录。", file=sys.stderr)
        return 1

    cons = consistency_check(runs)
    stats = aggregate(runs)

    if args.output_dir:
        out_dir = args.output_dir.resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = reports_root / "_analysis" / f"analysis_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print_console(runs, stats, cons, args.top, out_dir)

    summary_csv = out_dir / "sn_summary.csv"
    pivot_csv = out_dir / "sn_by_run.csv"
    html_path = out_dir / "analysis.html"

    write_csv_summary(summary_csv, stats)
    write_csv_pivot(pivot_csv, runs, stats)
    write_html(html_path, runs, stats, cons, max(args.top, 50))

    print("-" * 72)
    print(f"sn_summary.csv : {summary_csv}")
    print(f"sn_by_run.csv  : {pivot_csv}")
    print(f"analysis.html  : {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
