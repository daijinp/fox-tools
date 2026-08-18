import os
import csv
import json
import asyncio
import aiohttp
import aiofiles
import urllib3
from datetime import datetime
from jsonpath import jsonpath
from fr_requests import fr_requests, GetAuth

urllib3.disable_warnings()

_base_dir = os.path.dirname(os.path.abspath(__file__))
_config_dir = os.path.join(_base_dir, 'config')
_data_dir = os.path.join(_base_dir, 'device_data')

with open(os.path.join(_config_dir, 'config.json'), 'r', encoding='utf-8') as _f:
    config = json.load(_f)

_setting_cfg = config.get('get_setting', {})


# ==================== login / get_ui_keys ====================


def load_getui_devices():
    devices = []
    csv_path = os.path.join(_config_dir, 'getui_config.csv')
    with open(csv_path, 'r', encoding='utf-8') as f:
        for row in csv.reader(f):
            if row:
                devices.append({'protocol_version': row[0].strip(), 'id': row[1].strip()})
    return devices


def login():
    body = config['login']
    response = fr_requests('post', path='/c/v0/user/login', param=body)
    data = response.json()
    if data.get('errno') != 0:
        raise Exception(f'登录失败: {data}')
    token = data['result']['token']
    print(f'登录成功, user: {body["user"]}')
    return token


def _find_props_recursive(properties, names):
    """递归搜索所有层级的 properties，精确匹配 name，返回 (name, key) 列表"""
    results = []
    for prop in properties:
        if prop.get('name') in names:
            results.append((prop['name'], prop['key']))
        sub_props = prop.get('properties')
        if sub_props:
            results.extend(_find_props_recursive(sub_props, names))
    return results


def get_ui_keys(token):
    """
    遍历 getui_config.csv 中的设备，对每个 read_names 分组找到 request_key 和 response_key 列表。
    按 protocol_version 去重，返回 PROTOCOL_KEY_MAPPING
    """
    devices = load_getui_devices()
    read_names = config['getui']['read_names']
    seen = set()
    result = []
    first_saved = False
    for device in devices:
        response = fr_requests('get', path='/generic/v0/device/setting/ui', token=token,
                               param={'id': device['id']})
        data = response.json()
        if data.get('errno') != 0:
            print(f'getui 跳过设备 {device["id"]} ({device["protocol_version"]}): '
                  f'{data.get("msg", data)}')
            continue
        parameters = jsonpath(data, '$.result.parameters')[0]
        if not parameters:
            print(f'getui 跳过设备 {device["id"]} ({device["protocol_version"]}): '
                  f'parameters 为空')
            continue
        if not first_saved:
            first_saved = True
            with open(os.path.join(_base_dir, 'gitui_res.json'), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        for name_group in read_names:
            names_set = set(name_group)
            for group in parameters:
                matched = _find_props_recursive(group.get('properties', []), names_set)
                if not matched:
                    continue
                dedup_key = (device['protocol_version'], group['key'])
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                matched_dict = dict(matched)
                ordered_names = [n for n in name_group if n in matched_dict]
                result.append({
                    'protocol_version': device['protocol_version'],
                    'request_key': group['key'],
                    'response_names': ordered_names,
                    'response_key': [matched_dict[n] for n in ordered_names]
                })
    mapping_path = os.path.join(_config_dir, 'protocol_key_mapping.json')
    with open(mapping_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'get_ui_keys 完成, 共匹配 {len(result)} 组, 已保存到: {mapping_path}')
    return result


# ==================== get_setting (async) ====================


class _BatchState:
    """一轮批处理的共享状态"""
    def __init__(self):
        self.log_lock = asyncio.Lock()
        self.record_lock = asyncio.Lock()
        self.fail_records = []
        self.success_records = []
        self.success_count = 0


def _read_device_csv(file_path):
    """读取设备 CSV（取前 4 列），自动跳过 device_id 表头行和 skip_protocols 中的协议版本，忽略多余列"""
    skip_protocols = set(_setting_cfg.get('skip_protocols', []))
    devices = []
    skipped_count = 0
    with open(file_path, 'r', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            if row[0].strip().lower() == 'device_id':
                continue
            protocol = row[2].strip() if len(row) > 2 else ''
            if protocol in skip_protocols:
                skipped_count += 1
                continue
            devices.append((
                row[0].strip(),
                row[1].strip() if len(row) > 1 else '',
                protocol,
                row[3].strip() if len(row) > 3 else '',
            ))
    if skipped_count:
        print(f'已跳过 {skipped_count} 条记录 (协议版本在 skip_protocols 中: {sorted(skip_protocols)})')
    return devices


def _validate_protocols(devices, mapping_by_protocol):
    """校验所有设备的 protocol_version 都有对应 mapping，否则抛出严重错误停止脚本"""
    device_protocols = {d[2] for d in devices}
    missing = device_protocols - set(mapping_by_protocol.keys())
    if missing:
        raise Exception(
            f'严重错误: 以下协议版本未找到 KEY，请检查 config/getui_config.csv: {sorted(missing)}'
        )


def _get_input_csv_path():
    """获取并校验待处理设备 CSV 路径。"""
    input_file = _setting_cfg.get('input_file', '')
    if not input_file:
        raise Exception('config.json 中未配置 get_setting.input_file')

    input_csv = os.path.join(_data_dir, input_file)
    if not os.path.exists(input_csv):
        raise Exception(f'输入文件不存在: {input_csv}')
    return input_csv


def precheck():
    """在登录和拉取 UI 数据前，检查待处理协议是否已配置取 KEY 设备。"""
    input_csv = _get_input_csv_path()
    devices = _read_device_csv(input_csv)
    configured_protocols = {
        device['protocol_version'] for device in load_getui_devices()
    }
    device_protocols = {device[2] for device in devices}
    missing = device_protocols - configured_protocols
    if missing:
        raise Exception(
            '预检失败: 以下协议版本未在 config/getui_config.csv 中配置: '
            f'{sorted(missing)}'
        )
    print(f'预检通过, 共 {len(device_protocols)} 个协议版本')


async def _write_logs(state, message, log_files, output_dir):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    line = f'[{timestamp}] {message}\n'
    async with state.log_lock:
        for name in log_files:
            async with aiofiles.open(os.path.join(output_dir, name), 'a', encoding='utf-8') as f:
                await f.write(line)


def _save_records_csv(records, csv_path, info_field, value_columns=None,
                      append=False):
    if not records:
        return
    exists = os.path.exists(csv_path)
    with open(csv_path, 'a' if append else 'w', encoding='utf-8-sig', newline='') as f:
        if not append or not exists:
            header = 'device_id,device_sn,protocol_version,master_version'
            if value_columns:
                header += ',' + ','.join(value_columns)
            f.write(f'{header},{info_field}\n')
        for r in records:
            line = f'{r["device_id"]},{r["device_sn"]},' \
                   f'{r["protocol_version"]},{r["master_version"]}'
            if value_columns:
                for col in value_columns:
                    val = str(r.get(col, 'N/A')).replace('\n', ' ').replace(',', ';')
                    line += f',{val}'
            info = str(r[info_field]).replace('\n', ' ').replace(',', ';')
            f.write(f'{line},{info}\n')
    print(f'{"追加" if append else "保存"}记录到: {csv_path} ({len(records)} 条)')


async def _process_device(session, semaphore, device_data, token,
                          mapping_by_protocol, state, output_dir):
    device_id, device_sn, pv, mv = device_data
    mapping = mapping_by_protocol[pv]

    async def fail(reason):
        await _write_logs(state, f'{device_id} 失败: {reason}',
                          ['fail.log', 'all.log'], output_dir)
        async with state.record_lock:
            state.fail_records.append({
                'device_id': device_id, 'device_sn': device_sn,
                'protocol_version': pv, 'master_version': mv,
                'err_info': reason
            })

    async with semaphore:
        try:
            path = '/generic/v0/device/setting/get'
            url = config['domain'] + path
            headers = GetAuth().get_signature(token=token, path=path)
            param = {'id': device_id, 'key': mapping['request_key']}

            timeout = aiohttp.ClientTimeout(
                total=_setting_cfg.get('request_timeout', 300))
            async with session.get(url, params=param, headers=headers,
                                   ssl=False, timeout=timeout) as resp:
                if resp.status != 200:
                    return await fail(f'HTTP {resp.status}')

                try:
                    data = await resp.json()
                except Exception as e:
                    return await fail(f'响应解析失败: {e}')

                if data.get('errno') != 0:
                    return await fail(
                        f'errno={data.get("errno")}: {data.get("msg", "")}')

                values = data.get('result', {}).get('values', {})
                names = mapping['response_names']
                keys = mapping['response_key']
                name_values = {}
                key_values = {}
                for name, key in zip(names, keys):
                    val = values.get(key, 'N/A')
                    name_values[name] = val
                    key_values[key] = val

                await _write_logs(state, f'{device_id} 成功: {key_values}',
                                  ['success.log', 'all.log'], output_dir)
                async with state.record_lock:
                    state.success_records.append({
                        'device_id': device_id, 'device_sn': device_sn,
                        'protocol_version': pv, 'master_version': mv,
                        **name_values,
                        'success_info': str(key_values)
                    })
                    state.success_count += 1

        except asyncio.TimeoutError:
            await fail('请求超时')
        except Exception as e:
            await fail(f'异常: {e}')


def _get_value_columns():
    """从 read_names 配置中提取动态列名（保持配置顺序）"""
    columns = []
    for group in config.get('getui', {}).get('read_names', []):
        for name in group:
            if name not in columns:
                columns.append(name)
    return columns


async def _run_batch(input_csv, token, mapping_by_protocol, output_dir):
    state = _BatchState()
    devices = _read_device_csv(input_csv)
    if not devices:
        print('没有设备需要处理')
        return 0, 0

    concurrency = _setting_cfg.get('concurrency', 2000)
    print(f'开始处理 {len(devices)} 个设备, 并发: {concurrency}')

    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            _process_device(session, semaphore, d, token,
                            mapping_by_protocol, state, output_dir)
            for d in devices
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    value_columns = _get_value_columns()
    _save_records_csv(state.fail_records,
                      os.path.join(output_dir, 'fail.csv'), 'err_info')
    _save_records_csv(state.success_records,
                      os.path.join(output_dir, 'success.csv'), 'success_info',
                      value_columns=value_columns, append=True)

    print(f'本轮完成! 成功: {state.success_count}, 失败: {len(state.fail_records)}')
    return state.success_count, len(state.fail_records)


def _load_protocol_key_mapping():
    """从 config/protocol_key_mapping.json 加载协议-key 映射"""
    mapping_path = os.path.join(_config_dir, 'protocol_key_mapping.json')
    if not os.path.exists(mapping_path):
        raise Exception(
            f'映射文件不存在: {mapping_path}，请先运行 get_ui_keys 生成')
    with open(mapping_path, 'r', encoding='utf-8') as f:
        return json.load(f)


async def get_setting(token):
    """异步批量读取设备配置，自动重试失败设备"""
    input_csv = _get_input_csv_path()

    os.makedirs(_data_dir, exist_ok=True)
    protocol_key_mapping = _load_protocol_key_mapping()
    mapping_by_protocol = {m['protocol_version']: m for m in protocol_key_mapping}

    _validate_protocols(_read_device_csv(input_csv), mapping_by_protocol)

    round_num = 1
    current_input = input_csv
    fail_csv = os.path.join(_data_dir, 'fail.csv')

    while True:
        print(f'\n{"=" * 20} 第 {round_num} 轮处理 {"=" * 20}')
        success, fail_count = await _run_batch(
            current_input, token, mapping_by_protocol, _data_dir)

        if fail_count == 0:
            print('\n所有设备处理成功!')
            break

        if success > 0:
            print(f'\n{success} 成功, {fail_count} 失败, 准备重试...')
            current_input = fail_csv
            round_num += 1
        else:
            print(f'\n全部失败 ({fail_count} 个), 停止重试')
            break

    print('\n处理完成!')


if __name__ == '__main__':
    precheck()
    _token = login()
    get_ui_keys(_token)
    asyncio.run(get_setting(_token))
