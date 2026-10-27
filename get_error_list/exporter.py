"""Bounded concurrent queries, with all output serialized by one writer."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime
import csv
import json
from pathlib import Path
import threading

from api import CONCURRENCY, PAGE_SIZE, QueryCancelled


COLUMNS = (
    ("序号", None), ("电站", "plantName"), ("逆变器SN", "deviceSN"),
    ("采集器SN", "moduleSN"), ("告警编号", "alarmCode"),
    ("告警内容", "content"), ("报错时间", "time"),
)


def read_sns(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("未找到 SN.csv，请在程序同目录创建该文件，每行填写一个 SN，无表头。")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("SN.csv 编码无法识别，请保存为 UTF-8 CSV。") from exc
    import io
    sns, seen = [], set()
    for number, row in enumerate(csv.reader(io.StringIO(text, newline="")), 1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != 1 or not row[0].strip():
            raise ValueError(f"SN.csv 第 {number} 行不是单列 SN，请删除多余列。")
        sn = row[0].strip()
        if sn.lower() in ("sn", "devicesn", "device_sn", "逆变器sn"):
            raise ValueError(f"SN.csv 第 {number} 行包含表头，请删除表头。")
        if any(character.isspace() for character in sn):
            raise ValueError(f"SN.csv 第 {number} 行的 SN 包含空白字符。")
        if sn not in seen:
            seen.add(sn)
            sns.append(sn)
    if not sns:
        raise ValueError("SN.csv 没有有效 SN，请每行填写一个 SN，无表头。")
    return sns


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_alarms(client, sns, begin, end, output_root, cancel=None, emit=None):
    cancel = cancel if cancel is not None else threading.Event()
    emit = emit or (lambda event: None)
    if begin > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if not sns or len(sns) != len(set(sns)):
        raise ValueError("SN 列表不能为空或包含重复项。")
    started = datetime.now()
    directory = Path(output_root) / ("results_" + started.strftime("%Y%m%d_%H%M%S_%f"))
    directory.mkdir(parents=True, exist_ok=False)
    emit({"type": "output", "path": str(directory)})
    summary = {
        "started_at": started.isoformat(timespec="seconds"),
        "begin_date": begin.isoformat(), "end_date": end.isoformat(),
        "concurrency": CONCURRENCY, "page_size": PAGE_SIZE,
        "total_devices": len(sns), "queried_devices": 0, "successful_devices": 0,
        "failed_devices": 0, "incomplete_devices": 0, "exported_alarms": 0,
        "cancelled": False, "not_queried_devices": len(sns),
    }
    completed = set()
    try:
        with ExitStack() as stack:
            files = [stack.enter_context((directory / filename).open(
                "w", encoding="utf-8-sig", newline="")) for filename in
                ("告警列表.csv", "查询明细.csv", "失败SN.csv")]
            alarms, detail, failures = [csv.writer(handle) for handle in files]
            alarms.writerow([title for title, _ in COLUMNS])
            detail.writerow(["逆变器SN", "查询状态", "告警总数", "本次导出数", "说明"])
            log = stack.enter_context((directory / "运行日志.log").open("w", encoding="utf-8"))

            def report(message):
                line = f"[{datetime.now():%H:%M:%S}] {message}"
                log.write(line + "\n")
                log.flush()
                emit({"type": "log", "message": line})

            report(f"开始查询：{len(sns)} 个 SN，并发 {CONCURRENCY}，每个 SN 第一页 {PAGE_SIZE} 条。")
            positions = iter(enumerate(sns))
            with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="alarm") as pool:
                pending = {}

                def fill():
                    while len(pending) < CONCURRENCY and not cancel.is_set():
                        item = next(positions, None)
                        if item is None:
                            break
                        index, sn = item
                        pending[pool.submit(client.query, sn, begin, end)] = (index, sn)

                fill()
                while pending:
                    done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=lambda value: pending[value][0]):
                        _, sn = pending.pop(future)
                        try:
                            result = future.result()
                        except QueryCancelled:
                            continue
                        except Exception as exc:
                            # Only trusted client messages are shown; unknown exceptions are not echoed.
                            from api import ApiError
                            reason = str(exc) if isinstance(exc, ApiError) else f"查询异常（{type(exc).__name__}）。"
                            summary["failed_devices"] += 1
                            failures.writerow([sn])
                            detail.writerow([csv_cell(sn), "失败", "", 0, reason])
                            report(f"{sn} 查询失败：{reason}")
                        else:
                            rows = result["data"]
                            for row in rows:
                                summary["exported_alarms"] += 1
                                alarms.writerow([summary["exported_alarms"]] + [
                                    csv_cell(row[key]) for _, key in COLUMNS[1:]])
                            total, count = result["total"], len(rows)
                            summary["successful_devices"] += 1
                            incomplete = total > count
                            if incomplete:
                                summary["incomplete_devices"] += 1
                                note = f"接口共 {total} 条，仅返回第一页 {count} 条；本工具不自动翻页。"
                                status = "未完整导出"
                            else:
                                note, status = "", "无告警" if total == 0 else "成功"
                            detail.writerow([csv_cell(sn), status, total, count, note])
                            report(f"{sn} {status}：导出 {count} / {total} 条。" + note)
                        completed.add(sn)
                        summary["queried_devices"] += 1
                        for handle in files:
                            handle.flush()
                        emit({"type": "progress", "done": len(completed), "total": len(sns)})
                    fill()
            for sn in sns:
                if sn not in completed:
                    detail.writerow([csv_cell(sn), "未查询", "", 0, "用户取消，本 SN 未完成查询。"])
            report(f"结束：已查询 {len(completed)}/{len(sns)} 个，导出 {summary['exported_alarms']} 条告警，"
                   f"失败 {summary['failed_devices']} 个，未完整导出 {summary['incomplete_devices']} 个。")
    finally:
        summary["cancelled"] = cancel.is_set()
        summary["not_queried_devices"] = len(sns) - len(completed)
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        summary["complete"] = (len(completed) == len(sns) and not summary["failed_devices"]
                               and not summary["incomplete_devices"] and not cancel.is_set())
        (directory / "汇总.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory, summary
