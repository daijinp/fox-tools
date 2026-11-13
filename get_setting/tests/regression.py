import asyncio
import contextlib
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
from collections import Counter, deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run
from checkpoint import Checkpoint, ResultWriter, RunLock


class Response:
    status = 200

    def __init__(self, fixture, payload):
        self.fixture, self.payload = fixture, payload

    async def __aenter__(self):
        self.fixture.active += 1
        self.fixture.max_active = max(self.fixture.max_active, self.fixture.active)
        return self

    async def __aexit__(self, *args):
        self.fixture.active -= 1

    async def json(self):
        await asyncio.sleep(self.fixture.delay)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, fixture):
        self.fixture = fixture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, *, params, **kwargs):
        key = (params['id'], params['key'])
        self.fixture.calls.append(key)
        self.fixture.max_tasks = max(self.fixture.max_tasks, len(asyncio.all_tasks()))
        responses = self.fixture.responses.get(key)
        payload = responses.popleft() if responses else self.fixture.good()
        return Response(self.fixture, payload)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fox-stream-regression-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config_dir, self.data_dir = self.base / 'config', self.base / 'data'
        self.config_dir.mkdir()
        self.data_dir.mkdir()
        self.cfg = {
            'domain': 'https://offline.invalid', 'login': {'user': 'offline'},
            'getui': {'max_attempts_per_protocol': 5, 'read_names': [['GridCode'], ['Switch']]},
            'get_setting': {'input_file': 'input.csv', 'concurrency': 3,
                            'write_batch_size': 3, 'flush_interval': 0.02,
                            'request_timeout': 2, 'resume': True,
                            'skip_protocols': [], 'persistent_skip_protocols': []},
        }
        self.config_path = self.config_dir / 'config.json'
        self.config_path.write_text(json.dumps(self.cfg), encoding='utf-8')
        self.calls, self.responses = [], {}
        self.active = self.max_active = self.max_tasks = 0
        self.delay = 0
        self.output = io.StringIO()
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(contextlib.redirect_stdout(self.output))
        contexts.enter_context(patch.multiple(run, config=self.cfg,
            _setting_cfg=self.cfg['get_setting'], _base_dir=str(self.base),
            _config_dir=str(self.config_dir), _config_path=str(self.config_path),
            _data_dir=str(self.data_dir)))
        contexts.enter_context(patch.object(run.aiohttp, 'TCPConnector', return_value=None))
        contexts.enter_context(patch.object(run.aiohttp, 'ClientSession',
                                           side_effect=lambda **kwargs: Session(self)))
        self.stores = []
        self.addCleanup(self.close_stores)

    def close_stores(self):
        for store in self.stores:
            store.close()

    def store(self, name='state.sqlite3'):
        store = Checkpoint(str(self.data_dir / 'checkpoints' / name))
        self.stores.append(store)
        return store

    def devices(self, rows):
        with (self.data_dir / 'input.csv').open('w', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow(['device_id', 'device_sn', 'protocol_version', 'master_version'])
            writer.writerows((device, device + '-sn', protocol, '1.0') for device, protocol in rows)

    def mapping(self, protocol='P_OK', key='switch', field='Switch'):
        return {'protocol_version': protocol, 'request_key': key,
                'response_names': [field], 'response_key': [key]}

    def mappings(self, rows=None):
        (self.config_dir / 'protocol_key_mapping.json').write_text(
            json.dumps(rows if rows is not None else [self.mapping()]), encoding='utf-8')

    def good(self):
        return {'errno': 0, 'result': {'values': {'switch': 1, 'grid': 27}}}

    def records(self, name):
        with (self.data_dir / name).open(encoding='utf-8-sig', newline='') as source:
            return list(csv.DictReader(source))

    def test_retry_skips_missing_protocol_and_preserves_multi_key_success(self):
        self.devices([('one', 'P_OK'), ('missing', 'P_NONE'), ('two', 'P_OK')])
        self.mappings([self.mapping(), self.mapping(key='grid', field='GridCode')])
        self.responses[('two', 'switch')] = deque([
            {'errno': 1, 'msg': 'temporary'}, self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'run-one'))
        records = self.records('success.csv')
        self.assertEqual({row['device_id'] for row in records}, {'one', 'two'})
        self.assertTrue(all(len(json.loads(row['success_info'])) == 2 for row in records))
        self.assertEqual(Counter(device for device, key in self.calls), {'one': 2, 'two': 3})
        self.assertEqual(self.records('fail.csv'), [])
        reasons = json.loads((self.config_dir / 'skip_protocol_reasons.json').read_text(encoding='utf-8'))
        self.assertEqual(reasons['skipped_protocols'][0]['protocol_version'], 'P_NONE')

    def test_streaming_tasks_are_bounded_and_duplicate_input_is_not_requested_twice(self):
        self.cfg['get_setting']['write_batch_size'] = 100
        self.devices(((f'device-{index % 3000}', 'P_OK') for index in range(6000)))
        self.mappings()
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'bounded'))
        self.assertEqual(store.counts(), {'success': 3000, 'fail': 0})
        self.assertEqual(len(self.calls), 3000)
        self.assertLessEqual(self.max_active, 3)
        self.assertLessEqual(self.max_tasks, 10)
        self.assertEqual(len(self.records('success.csv')), 3000)

    def test_interrupt_resume_recovers_csv_and_only_requeries_uncommitted_devices(self):
        self.cfg['get_setting'].update(concurrency=1, write_batch_size=1)
        self.delay = 0.05
        self.devices([('one', 'P_OK'), ('two', 'P_OK'), ('three', 'P_OK')])
        self.mappings()
        store = self.store()

        async def interrupt():
            task = asyncio.create_task(run.get_setting('offline', store, 'resume'))
            while store.counts()['success'] < 1:
                await asyncio.sleep(0.005)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(interrupt())
        self.assertGreaterEqual(store.counts()['success'], 1)
        before = store.counts()['success']
        (self.data_dir / 'success.csv').write_text('corrupted partial CSV', encoding='utf-8')
        self.delay = 0
        asyncio.run(run.get_setting('offline', store, 'resume'))
        self.assertEqual(store.counts(), {'success': 3, 'fail': 0})
        self.assertEqual(Counter(device for device, key in self.calls)['one'], 1)
        self.assertEqual(len(self.records('success.csv')), 3)
        self.assertLessEqual(len(self.calls), 3 + 1)
        self.assertGreaterEqual(before, 1)
        calls = len(self.calls)
        asyncio.run(run.get_setting('offline', store, 'resume'))
        self.assertEqual(len(self.calls), calls)

    def test_flush_interval_saves_before_the_round_finishes(self):
        self.cfg['get_setting'].update(concurrency=1, write_batch_size=500, flush_interval=0.01)
        self.delay = 0.03
        self.devices([('one', 'P_OK'), ('two', 'P_OK'), ('three', 'P_OK')])
        self.mappings()
        store = self.store()

        async def check():
            task = asyncio.create_task(run.get_setting('offline', store, 'timed'))
            while store.counts()['success'] == 0:
                await asyncio.sleep(0.002)
            self.assertFalse(task.done())
            self.assertEqual(self.records('success.csv')[0]['device_id'], 'one')
            await task

        asyncio.run(check())

    def test_failure_round_stops_and_restart_can_retry(self):
        self.devices([('one', 'P_OK')])
        self.mappings()
        self.responses[('one', 'switch')] = deque([{'errno': 1}, self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'retry'))
        self.assertFalse(store.get_meta('complete'))
        self.assertEqual(store.get_meta('round_num'), 2)
        asyncio.run(run.get_setting('offline', store, 'retry'))
        self.assertEqual(store.counts(), {'success': 1, 'fail': 0})
        self.assertEqual(self.records('fail.csv'), [])


    def test_offline_devices_are_not_retried_in_later_rounds_or_after_restart(self):
        self.devices([('offline', 'P_OK'), ('offline-string', 'P_OK'),
                      ('flaky', 'P_OK'), ('good', 'P_OK')])
        self.mappings()
        self.responses[('offline', 'switch')] = deque([{'errno': 41935}])
        self.responses[('offline-string', 'switch')] = deque([{'errno': '41935'}])
        self.responses[('flaky', 'switch')] = deque([{'errno': 1}, self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'offline'))
        self.assertEqual(store.counts(), {'success': 2, 'fail': 2})
        self.assertEqual(store.retryable_failures(), 0)
        self.assertEqual(Counter(self.calls), {
            ('offline', 'switch'): 1, ('offline-string', 'switch'): 1,
            ('flaky', 'switch'): 2, ('good', 'switch'): 1})
        failures = self.records('fail.csv')
        self.assertEqual({row['device_id'] for row in failures}, {'offline', 'offline-string'})
        self.assertTrue(all('41935' in row['err_info'] for row in failures))
        reopened = self.store()
        calls = len(self.calls)
        asyncio.run(run.get_setting('offline', reopened, 'offline'))
        self.assertEqual(len(self.calls), calls)

    def test_retry_limit_stops_despite_progress_and_survives_restart(self):
        self.cfg['get_setting']['max_retries'] = 2
        self.devices([(str(index), 'P_OK') for index in range(5)])
        self.mappings()
        for index in range(5):
            self.responses[(str(index), 'switch')] = deque(
                [{'errno': 1}] * index + [self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'limited'))
        self.assertEqual(store.counts(), {'success': 3, 'fail': 2})
        self.assertEqual(Counter(self.calls)[('4', 'switch')], 3)
        status = json.loads((self.data_dir/'run_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['stop_reason'], 'retry_limit')
        self.assertEqual(status['max_retries'], 2)
        calls = len(self.calls)
        reopened = self.store()
        asyncio.run(run.get_setting('offline', reopened, 'limited'))
        self.assertEqual(len(self.calls), calls)

    def test_default_allows_at_most_fifteen_retry_rounds(self):
        self.assertEqual(run._options()['max_retries'], 15)
        self.devices([(str(index), 'P_OK') for index in range(18)])
        self.mappings()
        for index in range(18):
            self.responses[(str(index), 'switch')] = deque(
                [{'errno': 1}] * index + [self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'default-limit'))
        self.assertEqual(store.counts(), {'success': 16, 'fail': 2})
        self.assertEqual(max(Counter(self.calls).values()), 16)

    def test_zero_retry_limit_runs_only_initial_round(self):
        self.cfg['get_setting']['max_retries'] = 0
        self.devices([('good', 'P_OK'), ('failure', 'P_OK')])
        self.mappings()
        self.responses[('failure', 'switch')] = deque([{'errno': 1}, self.good()])
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'no-retries'))
        self.assertEqual(store.counts(), {'success': 1, 'fail': 1})
        self.assertEqual(len(self.calls), 2)

    def test_retry_limit_rejects_invalid_options(self):
        for value in (-1, 16, True, 1.5, '5', None):
            with self.subTest(value=value):
                self.cfg['get_setting']['max_retries'] = value
                with self.assertRaises(ValueError):
                    run._options()

    def test_legacy_offline_failures_migrate_as_nonretryable(self):
        path = self.data_dir/'legacy.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE results(device_id TEXT,protocol_version TEXT,status TEXT,"
                       "round_num INTEGER,record TEXT,PRIMARY KEY(device_id,protocol_version))")
            for device, errno in [('offline', 41935), ('temporary', 41203)]:
                db.execute('INSERT INTO results VALUES (?,?,?,?,?)',
                           (device, 'P_OK', 'fail', 1, json.dumps(
                               {'device_id': device, 'protocol_version': 'P_OK',
                                'err_info': f'key=switch errno={errno}: error'})))
            db.commit()
        with contextlib.closing(Checkpoint(str(path))) as store:
            devices = [('offline', '', 'P_OK', ''), ('temporary', '', 'P_OK', '')]
            self.assertEqual(store.completed_keys(devices, 2), {('offline', 'P_OK')})
            self.assertEqual(store.retryable_failures(), 1)
            self.assertEqual(store.save_batch([{'status': 'success',
                             'record': {'device_id': 'offline', 'protocol_version': 'P_OK'}}], 3), [])
            self.assertEqual(store.counts(), {'success': 0, 'fail': 2})


    def test_first_match_output_columns_survive_csv_and_checkpoint_resume(self):
        self.cfg['getui']['read_names'] = [['Status']]
        self.devices([('one', 'P_ONE'), ('two', 'P_TWO')])
        parameters = [
            {'name': 'Modbus-RTU', 'key': 'rtu', 'properties': [{'name': 'Status', 'key': 'switch'}]},
            {'name': 'Modbus-TCP', 'key': 'tcp', 'properties': [{'name': 'Status', 'key': 'grid'}]},
        ]
        mappings = (run._build_protocol_mappings('P_ONE', parameters, [['Status']]) +
                    run._build_protocol_mappings('P_TWO', list(reversed(parameters)), [['Status']]))
        self.mappings(mappings)
        store = self.store()
        asyncio.run(run.get_setting('offline', store, 'first-matches'))
        self.assertEqual(self.calls, [('one', 'rtu'), ('two', 'tcp')])
        rows = {row['device_id']: row for row in self.records('success.csv')}
        self.assertNotIn('Status', rows['one'])
        self.assertEqual(rows['one']['Modbus-RTU.Status'], '1')
        self.assertEqual(rows['two']['Modbus-TCP.Status'], '27')
        expected = ['Modbus-RTU.Status', 'Modbus-TCP.Status']
        self.assertEqual(store.get_meta('output_columns'), expected)
        calls = len(self.calls)
        # Simulate CSV recovery after an interrupted export.
        (self.data_dir/'success.csv').write_text('partial', encoding='utf-8')
        reopened = self.store()
        asyncio.run(run.get_setting('offline', reopened, 'first-matches'))
        self.assertEqual(len(self.calls), calls)
        recovered = {row['device_id']: row for row in self.records('success.csv')}
        self.assertEqual(recovered, rows)

    def test_input_and_fields_select_independent_checkpoints(self):
        self.devices([('one', 'P_OK')])
        store, first_id = run._prepare_checkpoint()
        self.stores.append(store)
        other, same_id = run._prepare_checkpoint()
        self.stores.append(other)
        self.assertEqual(first_id, same_id)
        self.cfg['getui']['read_names'].append(['Other'])
        changed, changed_id = run._prepare_checkpoint()
        self.stores.append(changed)
        self.assertNotEqual(first_id, changed_id)
        self.devices([('two', 'P_OK')])
        changed_input, changed_input_id = run._prepare_checkpoint()
        self.stores.append(changed_input)
        self.assertNotEqual(changed_id, changed_input_id)

    def test_new_task_archives_existing_outputs(self):
        original = b'old results must be preserved'
        (self.data_dir / 'success.csv').write_bytes(original)
        writer = ResultWriter(self.store(), str(self.data_dir), ['Switch'], 'new-run')
        writer.prepare()
        archives = list((self.data_dir / 'archive').glob('*/success.csv'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), original)
        self.assertEqual(self.records('success.csv'), [])

    def test_store_reopen_recovers_committed_results_and_counts(self):
        path = str(self.data_dir / 'checkpoints' / 'reopen.sqlite3')
        store = Checkpoint(path)
        record = {'device_id': 'one', 'protocol_version': 'P_OK', 'success_info': '[]'}
        store.save_batch([{'status': 'success', 'record': record}], 1)
        store.close()
        reopened = Checkpoint(path)
        self.stores.append(reopened)
        self.assertEqual(reopened.counts(), {'success': 1, 'fail': 0})
        self.assertEqual(reopened.completed_keys([('one', '', 'P_OK', '')], 2), {('one', 'P_OK')})

    def test_lock_rejects_second_writer_and_releases_after_exit(self):
        path = str(self.data_dir / 'run.lock')
        with RunLock(path):
            with self.assertRaisesRegex(RuntimeError, '已有查询进程'):
                with RunLock(path):
                    pass
        with RunLock(path):
            pass

    def test_existing_config_edits_survive_skip_updates(self):
        latest = json.loads(self.config_path.read_text())
        latest['get_setting']['concurrency'] = 99
        self.config_path.write_text(json.dumps(latest), encoding='utf-8')
        run._set_skip_protocols(['P_MISSING'])
        stored = json.loads(self.config_path.read_text())
        self.assertEqual(stored['get_setting']['concurrency'], 99)
        self.assertEqual(stored['get_setting']['skip_protocols'], ['P_MISSING'])

    def test_writer_failure_aborts_without_hanging_or_marking_task_complete(self):
        self.devices(((f'device-{index}', 'P_OK') for index in range(50)))
        self.mappings()
        store = self.store()
        with patch.object(ResultWriter, 'save_batch', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                asyncio.run(asyncio.wait_for(run.get_setting('offline', store, 'failed-write'), 2))
        self.assertFalse(store.get_meta('complete', False))
        self.assertEqual(store.counts(), {'success': 0, 'fail': 0})

    def test_main_refreshes_skipped_ui_protocol_without_repeating_saved_successes(self):
        self.devices([('one', 'P_OK')])
        good_ui = {'errno': 0, 'result': {'parameters': [
            {'key': 'switch', 'properties': [{'name': 'Switch', 'key': 'switch'}]},
        ]}}
        payloads = deque([{'errno': 1}, good_ui])

        class UIResponse:
            def json(self):
                return payloads.popleft()

        with patch.object(run, 'login', return_value='offline') as login_mock:
            with patch.object(run, 'fr_requests', side_effect=lambda *a, **kw: UIResponse()) as ui_mock:
                run.main()
                self.assertEqual(len(self.calls), 0)
                run.main()
                self.assertEqual(self.calls, [('one', 'switch')])
                run.main()
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(ui_mock.call_count, 2)
                self.assertEqual(login_mock.call_count, 2)
        self.assertEqual(len(self.records('success.csv')), 1)

    def test_million_row_ui_scan_has_bounded_memory_and_only_five_requests(self):
        self.devices(((f'device-{index}', 'P_BIG') for index in range(1_000_000)))
        calls = []

        def request(*args, **kwargs):
            calls.append(kwargs['param']['id'])
            self.assertEqual(kwargs['max_retries'], 1)
            return type('UIResponse', (), {'json': lambda self: {'errno': 1}})()

        tracemalloc.start()
        try:
            with patch.object(run, 'fr_requests', side_effect=request):
                self.assertEqual(run.get_ui_keys('offline'), [])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(calls), 5)
        self.assertLess(peak, 16 * 1024 * 1024)
        payload = json.loads((self.config_dir / 'skip_protocol_reasons.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['skipped_protocols'][0]['device_count'], 1_000_000)
        print(f'Million-row UI scan peak Python allocation: {peak / 1024 / 1024:.2f} MiB', file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
