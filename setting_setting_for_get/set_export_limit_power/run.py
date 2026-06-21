import ast
import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

import aiofiles
import aiohttp
import urllib3
from jsonpath import jsonpath

_tool_dir = os.path.dirname(os.path.abspath(__file__))
_base_dir = os.path.dirname(_tool_dir)
_get_setting_dir = os.path.join(_base_dir, 'get_setting')
_get_setting_config_dir = os.path.join(_get_setting_dir, 'config')
_tool_config_dir = os.path.join(_tool_dir, 'config')
_data_dir = os.path.join(_tool_dir, 'device_data')
_output_dir = os.path.join(_tool_dir, 'output')

sys.path.insert(0, _get_setting_dir)
from fr_requests import fr_requests, GetAuth  # noqa: E402

urllib3.disable_warnings()

with open(os.path.join(_get_setting_config_dir, 'config.json'), 'r', encoding='utf-8') as _f:
    app_config = json.load(_f)

with open(os.path.join(_tool_config_dir, 'config.json'), 'r', encoding='utf-8') as _f:
    tool_config = json.load(_f)

_app_setting_cfg = app_config.get('get_setting', {})
_target_name = tool_config.get('target_name', 'ExportLimitedPower')
_target_value_text = str(tool_config.get('target_value', '800'))
_target_value = Decimal(_target_value_text)
_country_code_match = tool_config.get('country_code_match', 'DE').upper()
_success_info_value_match = tool_config.get('success_info_value_match', 'VDE4105_DE')


class _BatchState:
    def __init__(self):
        self.log_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()
        self.set_records = []
        self.skip_records = []
        self.fail_records = []
        self.discard_records = []


def _merge_records(target_state, batch_state):
    target_state.set_records.extend(batch_state.set_records)
    target_state.skip_records.extend(batch_state.skip_records)
    target_state.discard_records.extend(batch_state.discard_records)


def login():
    body = app_config['login']
    response = fr_requests('post', path='/c/v0/user/login', param=body)
    data = response.json()
    if data.get('errno') != 0:
        raise Exception(f'登录失败: {data}')
    print(f'登录成功, user: {body["user"]}')
    return data['result']['token']


def _load_getui_devices():
    devices = []
    csv_path = os.path.join(_get_setting_config_dir, 'getui_config.csv')
    with open(csv_path, 'r', encoding='utf-8') as f:
        for row in csv.reader(f):
            if row:
                devices.append({
                    'protocol_version': row[0].strip(),
                    'id': row[1].strip()
                })
    return devices


def _find_prop_key_recursive(properties, target_name):
    for prop in properties:
        if prop.get('name') == target_name:
            return prop.get('key')
        sub_props = prop.get('properties')
        if sub_props:
            key = _find_prop_key_recursive(sub_props, target_name)
            if key:
                return key
    return None


def _load_export_limit_mapping(token, needed_protocols):
    devices = _load_getui_devices()
    mapping_by_protocol = {}

    for device in devices:
        protocol = device['protocol_version']
        if protocol in mapping_by_protocol:
            continue
        if needed_protocols and protocol not in needed_protocols:
            continue

        response = fr_requests(
            'get',
            path='/generic/v0/device/setting/ui',
            token=token,
            param={'id': device['id']}
        )
        data = response.json()
        if data.get('errno') != 0:
            print(f'getui 跳过设备 {device["id"]} ({protocol}): {data.get("msg", data)}')
            continue

        parameters = jsonpath(data, '$.result.parameters')
        parameters = parameters[0] if parameters else []
        if not parameters:
            print(f'getui 跳过设备 {device["id"]} ({protocol}): parameters 为空')
            continue

        for group in parameters:
            response_key = _find_prop_key_recursive(
                group.get('properties', []),
                _target_name
            )
            if not response_key:
                continue

            mapping_by_protocol[protocol] = {
                'protocol_version': protocol,
                'request_key': group['key'],
                'response_name': _target_name,
                'response_key': response_key
            }
            break

    mapping_path = os.path.join(_tool_config_dir, 'export_limit_protocol_key_mapping.json')
    with open(mapping_path, 'w', encoding='utf-8') as f:
        json.dump(list(mapping_by_protocol.values()), f, ensure_ascii=False, indent=2)
    print(f'{_target_name} key 获取完成, 共 {len(mapping_by_protocol)} 个协议, 已保存到: {mapping_path}')
    return mapping_by_protocol


def _extract_success_info_value(success_info):
    if not success_info:
        return None
    try:
        parsed = ast.literal_eval(success_info)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict) or len(parsed) != 1:
        return None
    return str(next(iter(parsed.values()))).strip()


def _match_device(row):
    country_code = row.get('country_code', '').strip().upper()
    if country_code == _country_code_match:
        return True, 'country_code'

    success_info_value = _extract_success_info_value(row.get('success_info', ''))
    if success_info_value == _success_info_value_match:
        return True, 'success_info'

    return False, ''


def _read_candidates(input_csv):
    candidates = []
    total = 0
    with open(input_csv, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            matched, matched_by = _match_device(row)
            if not matched:
                continue
            row['matched_by'] = matched_by
            candidates.append(row)
    print(f'读取 {total} 条记录, 命中 {len(candidates)} 条')
    return candidates


async def _write_logs(state, message, log_files):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    line = f'[{timestamp}] {message}\n'
    async with state.log_lock:
        for name in log_files:
            async with aiofiles.open(os.path.join(_output_dir, name), 'a', encoding='utf-8') as f:
                await f.write(line)


def _base_record(row, mapping=None, round_num=None):
    record = {
        'device_id': row.get('device_id', ''),
        'device_sn': row.get('device_sn', ''),
        'protocol_version': row.get('protocol_version', ''),
        'master_version': row.get('master_version', ''),
        'country_code': row.get('country_code', ''),
        'matched_by': row.get('matched_by', ''),
    }
    if round_num is not None:
        record['round'] = round_num
    if mapping:
        record['get_key'] = mapping['request_key']
        record['set_key'] = mapping['response_key']
    return record


def _to_decimal(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


async def _process_device(session, semaphore, row, token, mapping_by_protocol, state,
                          round_num):
    device_id = row.get('device_id', '').strip()
    protocol = row.get('protocol_version', '').strip()
    mapping = mapping_by_protocol.get(protocol)

    async def fail(reason, log_files=None):
        await _write_logs(
            state,
            f'{device_id} 失败: {reason}',
            log_files or ['set_fail.log', 'all.log']
        )
        async with state.record_lock:
            record = _base_record(row, mapping, round_num)
            record['err_info'] = reason
            state.fail_records.append(record)

    async def discard(reason, current_value=''):
        await _write_logs(state, f'{device_id} 丢弃: {reason}', ['discard.log', 'all.log'])
        async with state.record_lock:
            record = _base_record(row, mapping, round_num)
            record['current_value'] = current_value
            record['discard_info'] = reason
            state.discard_records.append(record)

    if not device_id:
        return await fail('device_id 为空')
    if not mapping:
        return await fail(f'协议 {protocol} 未找到 {_target_name} key')

    async with semaphore:
        path = '/generic/v0/device/setting/get'
        url = app_config['domain'] + path
        headers = GetAuth().get_signature(token=token, path=path)
        params = {'id': device_id, 'key': mapping['request_key']}
        timeout = aiohttp.ClientTimeout(total=tool_config.get(
            'request_timeout',
            _app_setting_cfg.get('request_timeout', 300)
        ))

        try:
            async with session.get(url, params=params, headers=headers, ssl=False,
                                   timeout=timeout) as resp:
                if resp.status != 200:
                    return await fail(f'get HTTP {resp.status}')
                try:
                    data = await resp.json()
                except Exception as e:
                    return await fail(f'get 响应解析失败: {e}')
        except asyncio.TimeoutError:
            return await fail('get 请求超时')
        except Exception as e:
            return await fail(f'get 异常: {e}')

        if data.get('errno') != 0:
            return await fail(f'get errno={data.get("errno")}: {data.get("msg", "")}')

        values = data.get('result', {}).get('values', {})
        current_value = values.get(mapping['response_key'])
        current_decimal = _to_decimal(current_value)
        if current_decimal is None:
            return await discard('当前值无法转为数字', current_value)

        if current_decimal <= _target_value:
            await _write_logs(
                state,
                f'{device_id} 无需下发: 当前值 {current_value}',
                ['set_skip.log', 'all.log']
            )
            async with state.record_lock:
                record = _base_record(row, mapping, round_num)
                record['current_value'] = current_value
                record['skip_info'] = f'当前值 <= {_target_value_text}'
                state.skip_records.append(record)
            return

        path = '/generic/v0/device/setting/set'
        url = app_config['domain'] + path
        headers = GetAuth().get_signature(token=token, path=path)
        body = {
            'id': device_id,
            'key': mapping['response_key'],
            'values': {
                mapping['response_key']: _target_value_text
            }
        }

        try:
            async with session.post(url, json=body, headers=headers, ssl=False,
                                    timeout=timeout) as resp:
                if resp.status != 200:
                    return await fail(f'set HTTP {resp.status}')
                try:
                    set_data = await resp.json()
                except Exception as e:
                    return await fail(f'set 响应解析失败: {e}')
        except asyncio.TimeoutError:
            return await fail('set 请求超时')
        except Exception as e:
            return await fail(f'set 异常: {e}')

        if set_data.get('errno') != 0:
            return await fail(f'set errno={set_data.get("errno")}: {set_data.get("msg", "")}')

        await _write_logs(
            state,
            f'{device_id} 已下发: {current_value} -> {_target_value_text}',
            ['set_success.log', 'all.log']
        )
        async with state.record_lock:
            record = _base_record(row, mapping, round_num)
            record['old_value'] = current_value
            record['new_value'] = _target_value_text
            record['set_info'] = str(set_data)
            state.set_records.append(record)


def _save_records_csv(records, csv_path, extra_fields):
    fieldnames = [
        'device_id',
        'device_sn',
        'protocol_version',
        'master_version',
        'country_code',
        'matched_by',
        'round',
        'get_key',
        'set_key',
    ]
    fieldnames.extend(extra_fields)
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)
    print(f'保存记录到: {csv_path} ({len(records)} 条)')


async def _run_batch(rows, token, mapping_by_protocol, round_num):
    state = _BatchState()
    concurrency = tool_config.get('concurrency', _app_setting_cfg.get('concurrency', 200))
    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)

    print(f'第 {round_num} 轮开始处理 {len(rows)} 个设备, 并发: {concurrency}')
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _process_device(session, semaphore, row, token, mapping_by_protocol, state,
                            round_num)
            for row in rows
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    print(
        f'第 {round_num} 轮完成! '
        f'已下发: {len(state.set_records)}, '
        f'无需下发: {len(state.skip_records)}, '
        f'失败: {len(state.fail_records)}, '
        f'丢弃: {len(state.discard_records)}'
    )
    return state


async def _run(input_csv):
    os.makedirs(_output_dir, exist_ok=True)
    candidates = _read_candidates(input_csv)
    if not candidates:
        print('没有命中的设备需要处理')
        return

    token = login()
    needed_protocols = {row.get('protocol_version', '').strip() for row in candidates}
    mapping_by_protocol = _load_export_limit_mapping(token, needed_protocols)

    total_state = _BatchState()
    current_rows = candidates
    max_retry_rounds = int(tool_config.get('max_retry_rounds', 3))
    max_rounds = max_retry_rounds + 1

    for round_num in range(1, max_rounds + 1):
        batch_state = await _run_batch(
            current_rows,
            token,
            mapping_by_protocol,
            round_num
        )
        _merge_records(total_state, batch_state)
        current_rows = batch_state.fail_records

        if not current_rows:
            break
        if round_num < max_rounds:
            print(f'第 {round_num} 轮仍有 {len(current_rows)} 个失败, 准备重试...')

    total_state.fail_records = current_rows

    _save_records_csv(
        total_state.set_records,
        os.path.join(_output_dir, 'set_success.csv'),
        ['old_value', 'new_value', 'set_info']
    )
    _save_records_csv(
        total_state.skip_records,
        os.path.join(_output_dir, 'set_skip.csv'),
        ['current_value', 'skip_info']
    )
    _save_records_csv(
        total_state.fail_records,
        os.path.join(_output_dir, 'set_fail.csv'),
        ['err_info']
    )
    _save_records_csv(
        total_state.discard_records,
        os.path.join(_output_dir, 'discard.csv'),
        ['current_value', 'discard_info']
    )

    print(
        '处理完成! '
        f'已下发: {len(total_state.set_records)}, '
        f'无需下发: {len(total_state.skip_records)}, '
        f'失败: {len(total_state.fail_records)}, '
        f'丢弃: {len(total_state.discard_records)}'
    )


def _resolve_config_input_csv():
    input_file = tool_config.get('input_file', 'success.csv')
    if os.path.isabs(input_file):
        return input_file
    return os.path.join(_data_dir, input_file)


def _resolve_input_csv():
    if len(sys.argv) > 1:
        return os.path.abspath(sys.argv[1])
    return _resolve_config_input_csv()


if __name__ == '__main__':
    _input_csv = _resolve_input_csv()
    if not os.path.exists(_input_csv):
        raise Exception(f'输入文件不存在: {_input_csv}')
    asyncio.run(_run(_input_csv))
