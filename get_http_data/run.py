import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
import urllib3

from fr_requests import fr_requests

urllib3.disable_warnings()

_base_dir = os.path.dirname(os.path.abspath(__file__))
_config_dir = os.path.join(_base_dir, 'config')

with open(os.path.join(_config_dir, 'config.json'), 'r', encoding='utf-8') as _f:
    config = json.load(_f)

_setting_cfg = config.get('get_setting', {})

_DEFAULT_SOURCE_HEADERS = [
    'device_id', 'device_sn', 'protocol_version', 'master_version'
]


def _resolve_input_csv():
    input_file = _setting_cfg.get('input_file', '').strip()
    if not input_file:
        raise Exception('config.json 中未配置 get_setting.input_file')

    candidates = []
    if os.path.isabs(input_file):
        candidates.append(input_file)
    else:
        candidates.append(os.path.join(_base_dir, input_file))
        for name in os.listdir(_base_dir):
            full_path = os.path.join(_base_dir, name)
            if os.path.isdir(full_path):
                candidates.append(os.path.join(full_path, input_file))

    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)

    raise Exception(f'输入文件不存在: {input_file}')


def _read_device_csv(file_path):
    devices = []
    with open(file_path, 'r', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))

    source_headers = None
    if rows and rows[0] and rows[0][0].strip().lower() == 'device_id':
        source_headers = [cell.strip() for cell in rows.pop(0)]

        # fail.csv 会作为下一轮的输入，末尾的旧错误信息不是源数据列。
        if source_headers and source_headers[-1].lower() == 'err_info':
            source_headers.pop()
            rows = [row[:-1] for row in rows]

    max_columns = max((len(row) for row in rows if row), default=0)
    if source_headers is None:
        source_headers = _DEFAULT_SOURCE_HEADERS[:max_columns]
        source_headers.extend(
            f'source_column_{index}'
            for index in range(len(source_headers) + 1, max_columns + 1)
        )
    elif len(source_headers) < max_columns:
        source_headers.extend(
            f'source_column_{index}'
            for index in range(len(source_headers) + 1, max_columns + 1)
        )

    for row in rows:
        if not row or not row[0].strip():
            continue
        padded_row = row + [''] * (len(source_headers) - len(row))
        devices.append({
            'device_id': row[0].strip(),
            'device_sn': row[1].strip() if len(row) > 1 else '',
            'protocol_version': row[2].strip() if len(row) > 2 else '',
            'master_version': row[3].strip() if len(row) > 3 else '',
            # 输出时使用完整原始行，保留第 5 列及后面的所有附加列。
            'source_row': padded_row,
        })
    return devices, source_headers


def _to_csv_line(row):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(row)
    return output.getvalue()


class BatchState:
    def __init__(self, output_dir, source_headers):
        self.output_dir = output_dir
        self.source_headers = source_headers
        self.log_lock = threading.Lock()
        self.success_lock = threading.Lock()
        self.fail_lock = threading.Lock()
        self.success_count = 0
        self.fail_count = 0
        success_path = os.path.join(output_dir, 'success.csv')
        self.success_header_written = (
            os.path.exists(success_path) and os.path.getsize(success_path) > 0
        )
        self.fail_header_written = False


def _write_logs(state, message, log_files):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    line = f'[{timestamp}] {message}\n'
    with state.log_lock:
        for name in log_files:
            file_path = os.path.join(state.output_dir, name)
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(line)


def _append_success(state, record):
    csv_path = os.path.join(state.output_dir, 'success.csv')
    with state.success_lock:
        lines = []
        if not state.success_header_written:
            lines.append(_to_csv_line(state.source_headers + ['response_json']))
            state.success_header_written = True
        lines.append(_to_csv_line(record['source_row'] + [record['response_json']]))
        with open(csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            f.writelines(lines)
        state.success_count += 1


def _append_fail(state, record):
    csv_path = os.path.join(state.output_dir, 'fail.csv')
    with state.fail_lock:
        lines = []
        if not state.fail_header_written:
            lines.append(_to_csv_line(state.source_headers + ['err_info']))
            state.fail_header_written = True
        lines.append(_to_csv_line(record['source_row'] + [record['err_info']]))
        with open(csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            f.writelines(lines)
        state.fail_count += 1


def _prepare_output_files(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    for file_name in ['success.csv', 'fail.csv', 'success.log', 'fail.log', 'all.log']:
        file_path = os.path.join(output_dir, file_name)
        if os.path.exists(file_path):
            os.remove(file_path)


def _reset_fail_outputs(output_dir):
    for file_name in ['fail.csv', 'fail.log']:
        file_path = os.path.join(output_dir, file_name)
        if os.path.exists(file_path):
            os.remove(file_path)


def _build_failure(device_data, reason):
    return {
        'source_row': device_data['source_row'],
        'err_info': reason,
    }


def _process_device(device_data, token, state):
    device_id = device_data['device_id']
    device_sn = device_data['device_sn']
    path = '/op/v2/device/scheduler/get'
    payload = {'deviceSN': device_sn}

    try:
        timeout = _setting_cfg.get('request_timeout', 300)
        response = fr_requests('post', path, token=token, param=payload, timeout=timeout)

        if response.status_code != 200:
            reason = f'HTTP {response.status_code}'
            _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
            _append_fail(state, _build_failure(device_data, reason))
            return

        try:
            data = response.json()
        except Exception as e:
            reason = f'响应解析失败: {e}'
            _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
            _append_fail(state, _build_failure(device_data, reason))
            return

        if data.get('errno') != 0:
            reason = f'errno={data.get("errno")}: {data.get("msg", "")}'
            _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
            _append_fail(state, _build_failure(device_data, reason))
            return

        _write_logs(state, f'{device_id} 成功', ['success.log', 'all.log'])
        _append_success(state, {
            'source_row': device_data['source_row'],
            'response_json': json.dumps(data, ensure_ascii=False, separators=(',', ':')),
        })
    except requests.Timeout:
        reason = '请求超时'
        _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
        _append_fail(state, _build_failure(device_data, reason))
    except requests.RequestException as e:
        reason = f'网络异常: {e}'
        _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
        _append_fail(state, _build_failure(device_data, reason))
    except Exception as e:
        reason = f'异常: {e}'
        _write_logs(state, f'{device_id} 失败: {reason}', ['fail.log', 'all.log'])
        _append_fail(state, _build_failure(device_data, reason))


def _run_batch(input_csv, token, output_dir, reset_fail_outputs=False):
    devices, source_headers = _read_device_csv(input_csv)
    if not devices:
        print('没有设备需要处理')
        return 0, 0

    if reset_fail_outputs:
        _reset_fail_outputs(output_dir)

    concurrency = _setting_cfg.get('concurrency', 300)
    print(f'输入文件: {input_csv}')
    print(f'输出目录: {output_dir}')
    print(f'开始处理 {len(devices)} 个设备, 并发: {concurrency}')

    state = BatchState(output_dir, source_headers)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(_process_device, device_data, token, state)
            for device_data in devices
        ]
        for future in as_completed(futures):
            future.result()

    print(f'本轮完成! 成功: {state.success_count}, 失败: {state.fail_count}')
    return state.success_count, state.fail_count


def run():
    token = config.get('token', '').strip()
    if not token:
        raise Exception('config.json 中未配置 token')

    input_csv = _resolve_input_csv()
    output_dir = os.path.dirname(input_csv)
    _prepare_output_files(output_dir)

    round_num = 1
    current_input = input_csv
    fail_csv = os.path.join(output_dir, 'fail.csv')

    while True:
        print(f'\n{"=" * 20} 第 {round_num} 轮处理 {"=" * 20}')
        success_count, fail_count = _run_batch(
            current_input,
            token,
            output_dir,
            reset_fail_outputs=round_num > 1,
        )

        if fail_count == 0:
            print('\n所有设备处理成功!')
            break

        if success_count > 0:
            wait_seconds = 3
            print(f'\n{success_count} 成功, {fail_count} 失败, {wait_seconds} 秒后准备重试...')
            time.sleep(wait_seconds)
            current_input = fail_csv
            round_num += 1
            continue

        print(f'\n全部失败 ({fail_count} 个), 停止重试')
        break

    print('\n处理完成!')


if __name__ == '__main__':
    run()
