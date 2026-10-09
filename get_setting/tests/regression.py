import asyncio
import contextlib
import csv
import io
import json
import os
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
