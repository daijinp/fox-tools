import os
import csv
import json
import asyncio
import hashlib
import time
import uuid
from itertools import islice
import aiohttp
import urllib3
from datetime import datetime
from jsonpath import jsonpath
from fr_requests import fr_requests, GetAuth
from checkpoint import Checkpoint, ResultWriter, RunLock, write_json_atomic

urllib3.disable_warnings()

_base_dir = os.path.dirname(os.path.abspath(__file__))
_config_dir = os.path.join(_base_dir, 'config')
_data_dir = os.path.join(_base_dir, 'device_data')
_config_path = os.path.join(_config_dir, 'config.json')

with open(_config_path, 'r', encoding='utf-8') as _f:
    config = json.load(_f)

_setting_cfg = config.setdefault('get_setting', {})


# ==================== login / get_ui_keys ====================


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


def _build_protocol_mappings(protocol, parameters, read_names):
    """从一个设备的 UI 参数中提取当前协议所需的 KEY 映射。"""
    requested_names = []
    for name_group in read_names:
        for name in name_group:
            if name not in requested_names:
                requested_names.append(name)

    names_set = set(requested_names)
    mappings = []
    mappings_by_request_key = {}
    for group in parameters:
        request_key = group.get('key')
        if not request_key:
            continue
        matched = _find_props_recursive(
            group.get('properties', []), names_set)
        if not matched:
            continue

        mapping = mappings_by_request_key.get(request_key)
        if mapping is None:
            mapping = {
                'protocol_version': protocol,
                'request_key': request_key,
                'response_names': [],
                'response_key': [],
            }
            mappings_by_request_key[request_key] = mapping
            mappings.append(mapping)

        matched_dict = dict(matched)
        existing_names = set(mapping['response_names'])
        for name in requested_names:
            if name in matched_dict and name not in existing_names:
                mapping['response_names'].append(name)
                mapping['response_key'].append(matched_dict[name])
                existing_names.add(name)
    return mappings


def get_ui_keys(token):
    """
    按协议分组，在 max_attempts_per_protocol 上限内依次尝试候选设备。
    找到目标 KEY 后停止尝试；候选耗尽或达到上限仍失败则自动跳过该协议。
    """
    getui_cfg = config['getui']
    max_attempts = getui_cfg.get('max_attempts_per_protocol', 5)
    if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or max_attempts <= 0):
        raise ValueError('getui.max_attempts_per_protocol 必须为大于 0 的整数')
    ui_timeout = getui_cfg.get('request_timeout', 30)
    if (isinstance(ui_timeout, bool) or not isinstance(ui_timeout, (int, float))
            or ui_timeout <= 0):
        raise ValueError('getui.request_timeout 必须大于 0')

    input_csv = _get_input_csv_path()
    devices_by_protocol = {}
    protocol_counts = {}
    extra_candidates = set()
    missing_protocol_devices = []
    for device_id, _, protocol, _ in _iter_device_csv(input_csv):
        if not protocol:
            if len(missing_protocol_devices) < 10:
                missing_protocol_devices.append(device_id)
            continue
        protocol_counts[protocol] = protocol_counts.get(protocol, 0) + 1
        candidates = devices_by_protocol.setdefault(protocol, [])
        if device_id in candidates:
            continue
        if len(candidates) < max_attempts:
            candidates.append(device_id)
        else:
            extra_candidates.add(protocol)
    if missing_protocol_devices:
        raise ValueError(f'以下设备缺少第三列协议版本: {missing_protocol_devices}')

    read_names = getui_cfg['read_names']
    result = []
    failed_protocols = []
    failed_protocol_details = {}
    first_saved = False
    for protocol, candidate_ids in devices_by_protocol.items():
        attempt_limit = min(len(candidate_ids), max_attempts)
        protocol_mappings = None
        failure_counts = {}
        last_failure = None
        print(f'getui 协议 {protocol}: 共 {protocol_counts[protocol]} 条设备记录，'
              f'最多尝试 {attempt_limit} 个')

        def record_failure(category, device_id, detail=None):
            nonlocal last_failure
            failure_counts[category] = failure_counts.get(category, 0) + 1
            last_failure = {
                'device_id': device_id,
                'reason': category,
            }
            if detail:
                last_failure['detail'] = str(detail)

        for attempt, device_id in enumerate(candidate_ids[:attempt_limit], start=1):
            try:
                response = fr_requests(
                    'get', path='/generic/v0/device/setting/ui', token=token,
                    param={'id': device_id}, max_retries=1,
                    timeout=ui_timeout)
                data = response.json()
                if data.get('errno') != 0:
                    reason = data.get('msg', data)
                    record_failure('接口返回失败', device_id, reason)
                    print(f'getui 协议 {protocol} 第 {attempt}/{attempt_limit} '
                          f'个设备 {device_id} 失败: {reason}')
                    continue

                parameter_matches = jsonpath(data, '$.result.parameters')
                parameters = parameter_matches[0] if parameter_matches else None
                if not parameters:
                    record_failure('parameters 为空', device_id)
                    print(f'getui 协议 {protocol} 第 {attempt}/{attempt_limit} '
                          f'个设备 {device_id} 失败: parameters 为空')
                    continue

                candidate_mappings = _build_protocol_mappings(
                    protocol, parameters, read_names)
                if not candidate_mappings:
                    record_failure('未找到目标 KEY', device_id)
                    print(f'getui 协议 {protocol} 第 {attempt}/{attempt_limit} '
                          f'个设备 {device_id} 失败: 未找到目标 KEY')
                    continue

                protocol_mappings = candidate_mappings
                result.extend(candidate_mappings)
                print(f'getui 协议 {protocol} 获取成功: 使用第 '
                      f'{attempt}/{attempt_limit} 个设备 {device_id}')
                if not first_saved:
                    first_saved = True
                    with open(os.path.join(_base_dir, 'gitui_res.json'), 'w',
                              encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                break
            except Exception as exc:
                record_failure('请求或解析异常', device_id, exc)
                print(f'getui 协议 {protocol} 第 {attempt}/{attempt_limit} '
                      f'个设备 {device_id} 异常: {exc}')

        if protocol_mappings is None:
            remaining_count = (protocol_counts[protocol] - attempt_limit
                               if protocol in extra_candidates else 0)
            reason = (
                f'达到 getui 尝试上限（{max_attempts} 次），仍未生成目标 KEY 映射'
                if remaining_count else '所有候选设备均未生成目标 KEY 映射'
            )
            failed_protocols.append(protocol)
            failed_protocol_details[protocol] = {
                'stage': 'getui',
                'reason': reason,
                'candidate_count': protocol_counts[protocol],
                'device_count': protocol_counts[protocol],
                'selected_candidate_count': len(candidate_ids),
                'attempted_count': attempt_limit,
                'max_attempts_per_protocol': max_attempts,
                'remaining_candidate_count': remaining_count,
                'failure_counts': failure_counts,
                'last_failure': last_failure,
            }
            print(f'getui 跳过协议 {protocol}: {reason}，'
                  f'已尝试 {attempt_limit} 个设备，'
                  '将加入 skip_protocols，继续处理其他协议')

    mapping_path = os.path.join(_config_dir, 'protocol_key_mapping.json')
    with open(mapping_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    _set_skip_protocols(failed_protocols)
    reason_path = _save_skip_protocol_reasons(failed_protocol_details)
    success_protocols = len(devices_by_protocol) - len(failed_protocols)
    print(f'get_ui_keys 完成, 成功 {success_protocols} 个协议/'
          f'{len(result)} 组映射, 失败 {len(failed_protocols)} 个协议, '
          f'已保存到: {mapping_path}')
    print(f'跳过协议原因已保存到: {reason_path}')
    return result


# ==================== get_setting (async) ====================


def _iter_device_csv(file_path, report_skipped=True):
    """读取设备 CSV，并跳过自动或永久配置的协议版本。"""
    automatic_skips = set(_setting_cfg.get('skip_protocols', []))
    persistent_skips = set(
        _setting_cfg.get('persistent_skip_protocols', []))
    skip_protocols = automatic_skips | persistent_skips
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
            yield (
                row[0].strip(),
                row[1].strip() if len(row) > 1 else '',
                protocol,
                row[3].strip() if len(row) > 3 else '',
            )
    if skipped_count and report_skipped:
        print(f'已跳过 {skipped_count} 条记录 '
              f'(自动跳过: {sorted(automatic_skips)}, '
              f'永久跳过: {sorted(persistent_skips)})')



def _get_input_csv_path():
    """获取并校验待处理设备 CSV 路径。"""
    input_file = _setting_cfg.get('input_file', '')
    if not input_file:
        raise Exception('config.json 中未配置 get_setting.input_file')

    input_csv = os.path.join(_data_dir, input_file)
    if not os.path.exists(input_csv):
        raise Exception(f'输入文件不存在: {input_csv}')
    return input_csv



def _set_skip_protocols(protocols):
    """更新内存及 config.json 中的自动跳过协议列表。"""
    normalized = sorted({protocol for protocol in protocols if protocol})
    _setting_cfg['skip_protocols'] = normalized
    with open(_config_path, encoding='utf-8') as source:
        latest = json.load(source)
    latest.setdefault('get_setting', {})['skip_protocols'] = normalized
    write_json_atomic(_config_path, latest)



def _save_skip_protocol_reasons(failed_protocol_details, merge=False):
    """保存跳过协议的原因；爬取阶段合并记录，保留 getui 阶段的原因。"""
    records = []
    for protocol in sorted(set(
            _setting_cfg.get('persistent_skip_protocols', []))):
        records.append({
            'protocol_version': protocol,
            'skip_source': 'persistent_skip_protocols',
            'reason': '配置为永久跳过',
        })
    for protocol in sorted(failed_protocol_details):
        details = failed_protocol_details[protocol]
        records.append({
            'protocol_version': protocol,
            'skip_source': 'skip_protocols',
            'reason': '所有候选设备均未生成目标 KEY 映射',
            **details,
        })

    reason_path = os.path.join(_config_dir, 'skip_protocol_reasons.json')
    if merge and os.path.exists(reason_path):
        with open(reason_path, 'r', encoding='utf-8') as f:
            previous = json.load(f)
        records_by_protocol = {
            (record['skip_source'], record['protocol_version']): record
            for record in previous.get('skipped_protocols', [])
        }
        for record in records:
            records_by_protocol[
                (record['skip_source'], record['protocol_version'])] = record
        records = list(records_by_protocol.values())
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'requested_names': config.get('getui', {}).get('read_names', []),
        'skipped_protocols': records,
    }
    with open(reason_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return reason_path



def _clear_skip_protocols():
    """每次启动时仅清空自动跳过协议，保留永久跳过协议。"""
    previous = list(_setting_cfg.get('skip_protocols', []))
    if previous:
        _set_skip_protocols([])
        print(f'启动清理: 已清空上次的 skip_protocols: {sorted(previous)}')
    else:
        _setting_cfg['skip_protocols'] = []
        print('启动清理: skip_protocols 原本为空')



def _get_value_columns():
    """从 read_names 配置中提取动态列名（保持配置顺序）"""
    columns = []
    for group in config.get('getui', {}).get('read_names', []):
        for name in group:
            if name not in columns:
                columns.append(name)
    return columns



def _load_protocol_key_mapping():
    """从 config/protocol_key_mapping.json 加载协议-key 映射"""
    mapping_path = os.path.join(_config_dir, 'protocol_key_mapping.json')
    if not os.path.exists(mapping_path):
        raise Exception(
            f'映射文件不存在: {mapping_path}，请先运行 get_ui_keys 生成')
    with open(mapping_path, 'r', encoding='utf-8') as f:
        return json.load(f)



def _options():
    values = {
        'concurrency': _setting_cfg.get('concurrency', 12),
        'write_batch_size': _setting_cfg.get('write_batch_size', 500),
        'flush_interval': _setting_cfg.get('flush_interval', 5),
        'request_timeout': _setting_cfg.get('request_timeout', 300),
        'resume': _setting_cfg.get('resume', True),
    }
    for key in ('concurrency', 'write_batch_size'):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'get_setting.{key} 必须为大于 0 的整数')
    for key in ('flush_interval', 'request_timeout'):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f'get_setting.{key} 必须大于 0')
    if not isinstance(values['resume'], bool):
        raise ValueError('get_setting.resume 必须为 true 或 false')
    return values


def _publish_status(phase, **details):
    write_json_atomic(os.path.join(_data_dir, 'run_status.json'), {
        'pid': os.getpid(), 'phase': phase,
        'updated_at': datetime.now().isoformat(timespec='seconds'), **details,
    })


def _prepare_checkpoint():
    options = _options()
    input_csv = _get_input_csv_path()
    digest = hashlib.sha256()
    with open(input_csv, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    signature = {
        'version': 1, 'input_sha256': digest.hexdigest(),
        'input_file': os.path.abspath(input_csv), 'domain': config['domain'],
        'user': config.get('login', {}).get('user'),
        'columns': _get_value_columns(),
        'persistent_skip_protocols': sorted(_setting_cfg.get('persistent_skip_protocols', [])),
    }
    run_id = hashlib.sha256(json.dumps(
        signature, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    if not options['resume']:
        run_id += '-' + uuid.uuid4().hex[:8]
    checkpoint = Checkpoint(os.path.join(_data_dir, 'checkpoints', run_id + '.sqlite3'))
    if checkpoint.get_meta('signature') is None:
        checkpoint.set_meta(signature=signature, round_num=1, complete=False)
    print(f'任务: {run_id[:12]}, 已保存进度: {checkpoint.counts()}', flush=True)
    return checkpoint, run_id


def _record_missing_protocols(counts):
    if not counts:
        return
    _set_skip_protocols(set(_setting_cfg.get('skip_protocols', [])) | set(counts))
    _save_skip_protocol_reasons({protocol: {
        'stage': 'get_setting',
        'reason': 'protocol_key_mapping.json 中没有对应协议的 KEY 映射',
        'device_count': count,
    } for protocol, count in counts.items()}, merge=True)


async def _process_device(session, device_data, token, mapping_by_protocol):
    device_id, device_sn, pv, mv = device_data
    base = {'device_id': device_id, 'device_sn': device_sn,
            'protocol_version': pv, 'master_version': mv}

    def fail(reason):
        return {'status': 'fail', 'record': {**base, 'err_info': reason}}

    try:
        path = '/generic/v0/device/setting/get'
        url = config['domain'] + path
        timeout = aiohttp.ClientTimeout(total=_setting_cfg.get('request_timeout', 300))
        name_values, success_info = {}, []
        for mapping in mapping_by_protocol[pv]:
            request_key = mapping['request_key']
            headers = GetAuth().get_signature(token=token, path=path)
            try:
                async with session.get(url, params={'id': device_id, 'key': request_key},
                                       headers=headers, ssl=False, timeout=timeout) as resp:
                    if resp.status != 200:
                        return fail(f'key={request_key} HTTP {resp.status}')
                    try:
                        data = await resp.json()
                    except Exception as exc:
                        return fail(f'key={request_key} 响应解析失败: {exc}')
            except asyncio.TimeoutError:
                return fail(f'key={request_key} 请求超时')
            except Exception as exc:
                return fail(f'key={request_key} 请求异常: {exc}')
            if data.get('errno') != 0:
                return fail(f'key={request_key} errno={data.get("errno")}: {data.get("msg", "")}')
            values = data.get('result', {}).get('values', {})
            key_values = {}
            for name, key in zip(mapping['response_names'], mapping['response_key']):
                value = values.get(key, 'N/A')
                name_values[name] = value
                key_values[key] = value
            success_info.append({'request_key': request_key, 'values': key_values})
        return {'status': 'success', 'record': {
            **base, **name_values,
            'success_info': json.dumps(success_info, ensure_ascii=False),
        }}
    except Exception as exc:
        return fail(f'异常: {exc}')


async def _run_batch(input_csv, token, mapping_by_protocol, checkpoint, writer,
                     round_num, options):
    concurrency = options['concurrency']
    batch_size = options['write_batch_size']
    devices_queue = asyncio.Queue(maxsize=concurrency * 2)
    results_queue = asyncio.Queue(maxsize=batch_size * 2)
    inflight = set()
    stats = {'scheduled': 0, 'already_processed': 0, 'unmapped': 0}

    async def produce():
        iterator = _iter_device_csv(input_csv)
        missing_counts = {}
        try:
            while True:
                chunk = await asyncio.to_thread(lambda: list(islice(iterator, 500)))
                if not chunk:
                    break
                completed = await asyncio.to_thread(
                    checkpoint.completed_keys, chunk, round_num)
                new_missing = False
                for device in chunk:
                    protocol = device[2]
                    if not mapping_by_protocol.get(protocol):
                        new_missing |= protocol not in missing_counts
                        missing_counts[protocol] = missing_counts.get(protocol, 0) + 1
                        stats['unmapped'] += 1
                        continue
                    key = (device[0], protocol)
                    if key in completed or key in inflight:
                        stats['already_processed'] += 1
                        continue
                    inflight.add(key)
                    completed.add(key)
                    await devices_queue.put(device)
                    stats['scheduled'] += 1
                if new_missing:
                    _record_missing_protocols(missing_counts)
            _record_missing_protocols(missing_counts)
        finally:
            iterator.close()
        for _ in range(concurrency):
            await devices_queue.put(None)

    async def consume(session):
        while True:
            device = await devices_queue.get()
            if device is None:
                await results_queue.put(None)
                return
            result = await _process_device(session, device, token, mapping_by_protocol)
            await results_queue.put(result)

    async def persist():
        buffer = []
        finished = 0
        deadline = time.monotonic() + options['flush_interval']

        async def flush():
            nonlocal buffer, deadline
            if buffer:
                batch, buffer = buffer, []
                totals = await asyncio.to_thread(writer.save_batch, batch, round_num)
                for result in batch:
                    record = result['record']
                    inflight.discard((record['device_id'], record['protocol_version']))
                print(f'已保存: 成功 {totals["success"]}, 失败 {totals["fail"]}, '
                      f'本轮已调度 {stats["scheduled"]}', flush=True)
                _publish_status('querying', round_num=round_num, **totals, **stats)
            deadline = time.monotonic() + options['flush_interval']

        while finished < concurrency:
            try:
                result = await asyncio.wait_for(
                    results_queue.get(), timeout=max(0.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                await flush()
                continue
            if result is None:
                finished += 1
            else:
                buffer.append(result)
            if len(buffer) >= batch_size or time.monotonic() >= deadline:
                await flush()
        await flush()

    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency)
    print(f'第 {round_num} 轮: 并发 {concurrency}, 每 {batch_size} 条或 '
          f'{options["flush_interval"]} 秒保存一次', flush=True)
    writer.open()
    tasks = []
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [asyncio.create_task(produce()), asyncio.create_task(persist())]
            tasks.extend(asyncio.create_task(consume(session)) for _ in range(concurrency))
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await asyncio.to_thread(writer.close)
    await asyncio.to_thread(writer.export, ('fail',))
    counts = checkpoint.counts(round_num)
    print(f'本轮完成: 成功 {counts["success"]}, 失败 {counts["fail"]}, {stats}', flush=True)
    return counts['success'], counts['fail']


async def get_setting(token, checkpoint, run_id):
    options = _options()
    mapping_by_protocol = {}
    for mapping in _load_protocol_key_mapping():
        mapping_by_protocol.setdefault(mapping['protocol_version'], []).append(mapping)
    writer = ResultWriter(checkpoint, _data_dir, _get_value_columns(), run_id)
    await asyncio.to_thread(writer.prepare)
    if checkpoint.get_meta('complete', False):
        print('此任务已经完成；无需重复查询。需要重新查询时设置 resume=false。', flush=True)
        _publish_status('complete', run_id=run_id, **checkpoint.counts())
        return
    round_num = checkpoint.get_meta('round_num', 1)
    while True:
        checkpoint.set_meta(round_num=round_num)
        _publish_status('querying', run_id=run_id, round_num=round_num, **checkpoint.counts())
        success, fail_count = await _run_batch(
            _get_input_csv_path(), token, mapping_by_protocol, checkpoint,
            writer, round_num, options)
        checkpoint.set_meta(automatic_skips=_setting_cfg.get('skip_protocols', []))
        if fail_count == 0 or success == 0:
            remaining_failures = checkpoint.counts()['fail']
            phase = 'complete' if remaining_failures == 0 else 'finished_with_failures'
            # Failed devices can be attempted again after a restart.
            checkpoint.set_meta(complete=remaining_failures == 0,
                                round_num=round_num if remaining_failures == 0 else round_num + 1)
            _publish_status(phase, run_id=run_id, **checkpoint.counts())
            print(f'处理结束: {checkpoint.counts()}，跳过原因见配置目录的原因文件。', flush=True)
            return
        round_num += 1
        print(f'准备第 {round_num} 轮重试；已成功设备不会重复请求。', flush=True)


def main():
    os.makedirs(_data_dir, exist_ok=True)
    with RunLock(os.path.join(_data_dir, 'run.lock')):
        checkpoint = None
        try:
            _publish_status('initializing')
            checkpoint, run_id = _prepare_checkpoint()
            previous_skips = checkpoint.get_meta('automatic_skips', [])
            if checkpoint.get_meta('complete', False) and not previous_skips:
                asyncio.run(get_setting(None, checkpoint, run_id))
                return
            _publish_status('logging_in', run_id=run_id)
            token = login()
            mappings = checkpoint.get_meta('protocol_key_mapping')
            if mappings is None or previous_skips:
                if checkpoint.get_meta('complete', False):
                    checkpoint.set_meta(complete=False,
                                        round_num=checkpoint.get_meta('round_num', 1) + 1)
                _clear_skip_protocols()
                _publish_status('getui', run_id=run_id)
                mappings = get_ui_keys(token)
                with open(os.path.join(_config_dir, 'skip_protocol_reasons.json'),
                          encoding='utf-8') as source:
                    reasons = json.load(source)
                checkpoint.set_meta(protocol_key_mapping=mappings,
                                    automatic_skips=_setting_cfg.get('skip_protocols', []),
                                    skip_reasons=reasons)
            else:
                print('恢复同一任务，使用已保存的协议映射。', flush=True)
                _set_skip_protocols(checkpoint.get_meta('automatic_skips', []))
                write_json_atomic(os.path.join(_config_dir, 'protocol_key_mapping.json'), mappings)
                write_json_atomic(os.path.join(_config_dir, 'skip_protocol_reasons.json'),
                                  checkpoint.get_meta('skip_reasons'))
            asyncio.run(get_setting(token, checkpoint, run_id))
        except BaseException as exc:
            _publish_status('interrupted' if isinstance(exc, KeyboardInterrupt) else 'error',
                            error=str(exc))
            raise
        finally:
            if checkpoint:
                checkpoint.close()


if __name__ == '__main__':
    main()
