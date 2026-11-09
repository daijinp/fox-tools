"""Offline regression checks: python get_setting/read_paths_test.py."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run


def status_parameters():
    return [
        {'name': 'Modbus-RTU', 'key': 'q113__29', 'properties': [
            {'name': 'Status', 'key': 'q113__29__00'}]},
        {'name': 'Modbus-TCP', 'key': 'q113__30', 'properties': [
            {'name': 'Status', 'key': 'q113__30__00'}]},
    ]


READ_NAMES = [['Modbus-RTU.Status', 'Modbus-TCP.Status']]


class MappingTests(unittest.TestCase):
    def test_qualified_statuses_use_separate_keys_and_columns(self):
        mappings = run._build_protocol_mappings('Q113xx', status_parameters(), READ_NAMES)
        self.assertEqual(mappings, [
            {'protocol_version': 'Q113xx', 'request_key': 'q113__29',
             'response_names': ['Modbus-RTU.Status'], 'response_key': ['q113__29__00']},
            {'protocol_version': 'Q113xx', 'request_key': 'q113__30',
             'response_names': ['Modbus-TCP.Status'], 'response_key': ['q113__30__00']},
        ])

    def test_selecting_tcp_does_not_read_rtu(self):
        mappings = run._build_protocol_mappings(
            'Q113xx', status_parameters(), [['Modbus-TCP.Status']])
        self.assertEqual([m['request_key'] for m in mappings], ['q113__30'])

    def test_nested_paths_and_plain_names_are_supported(self):
        parameters = [{'name': 'Group', 'key': 'group', 'properties': [
            {'name': 'Child', 'key': 'child', 'properties': [
                {'name': 'Flag', 'key': 'flag'}]}]}]
        mappings = run._build_protocol_mappings(
            'Q113xx', parameters, [['Group.Child.Flag', 'Flag']])
        self.assertEqual(mappings[0]['response_names'], ['Group.Child.Flag', 'Flag'])
        self.assertEqual(mappings[0]['response_key'], ['flag', 'flag'])
        self.assertEqual(run._build_protocol_mappings(
            'Q113xx', parameters, [['Other.Child.Flag']]), [])

    def test_existing_plain_name_config_still_works(self):
        parameters = [{'name': 'OperationMode', 'key': 'mode', 'properties': [
            {'name': 'WorkMode', 'key': 'work_mode'}]},
            {'name': 'SegmentedTimeModeEnable', 'key': 'time', 'properties': [
                {'name': 'TimeModeFlag', 'key': 'time_flag'}]}]
        mappings = run._build_protocol_mappings(
            'Q113xx', parameters, [['WorkMode', 'TimeModeFlag']])
        self.assertEqual([m['response_names'] for m in mappings],
                         [['WorkMode'], ['TimeModeFlag']])
        self.assertEqual([m['response_key'] for m in mappings],
                         [['work_mode'], ['time_flag']])

    def test_ambiguous_plain_status_reports_an_error(self):
        with self.assertRaisesRegex(ValueError, 'Status.*多个 KEY'):
            run._build_protocol_mappings('Q113xx', status_parameters(), [['Status']])

    def test_ambiguous_names_within_one_request_report_an_error(self):
        parameters = status_parameters()[:1]
        parameters[0]['properties'].append({'name': 'Status', 'key': 'another_status'})
        with self.assertRaisesRegex(ValueError, '多个 KEY'):
            run._build_protocol_mappings('Q113xx', parameters, [['Modbus-RTU.Status']])

    def test_repeated_identical_target_is_deduplicated(self):
        parameters = status_parameters()[:1] * 2
        mappings = run._build_protocol_mappings(
            'Q113xx', parameters, [['Modbus-RTU.Status', 'Modbus-RTU.Status']])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]['response_key'], ['q113__29__00'])

    def test_changing_query_columns_creates_a_new_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'input.csv').write_text('id,sn,Q113xx,version\n', encoding='utf-8')
            with patch.object(run, '_data_dir', directory), patch.object(
                    run, '_setting_cfg', {'input_file': 'input.csv', 'resume': True}), patch.dict(
                    run.config, {'getui': {'read_names': [['WorkMode', 'TimeModeFlag']]}}):
                old_checkpoint, old_id = run._prepare_checkpoint()
                old_checkpoint.set_meta(protocol_key_mapping=[{'old': 'mapping'}], complete=True)
                old_checkpoint.close()
                run.config['getui'] = {'read_names': READ_NAMES}
                new_checkpoint, new_id = run._prepare_checkpoint()
                try:
                    self.assertNotEqual(old_id, new_id)
                    self.assertIsNone(new_checkpoint.get_meta('protocol_key_mapping'))
                    self.assertFalse(new_checkpoint.get_meta('complete'))
                    self.assertEqual(new_checkpoint.get_meta('signature')['columns'], READ_NAMES[0])
                finally:
                    new_checkpoint.close()


class GetUiTests(unittest.TestCase):
    def run_getui(self, responses, include_next_protocol=False):
        with tempfile.TemporaryDirectory() as directory:
            config = {'getui': {'max_attempts_per_protocol': 5, 'read_names': READ_NAMES},
                      'get_setting': {'input_file': 'input.csv'}}
            config_path = Path(directory, 'config.json')
            config_path.write_text(json.dumps(config), encoding='utf-8')
            devices = [f'device-{index},sn,ZAA10301,version\n' for index in range(8)]
            if include_next_protocol:
                devices.append('next-device,sn,Q113xx,version\n')
            Path(directory, 'input.csv').write_text(''.join(devices), encoding='utf-8')
            replies = [item if isinstance(item, Exception)
                       else Mock(json=Mock(return_value=item)) for item in responses]
            with patch.object(run, 'config', config), patch.object(
                    run, '_setting_cfg', config['get_setting']), patch.object(
                    run, '_config_path', str(config_path)), patch.object(
                    run, '_config_dir', directory), patch.object(
                    run, '_data_dir', directory), patch.object(
                    run, '_base_dir', directory), patch.object(
                    run, 'fr_requests', side_effect=replies) as request, patch('builtins.print'):
                result = run.get_ui_keys('test-token')
                requested_devices = [call.kwargs['param']['id'] for call in request.call_args_list]
            reasons = json.loads(Path(directory, 'skip_protocol_reasons.json').read_text(encoding='utf-8'))
            saved_config = json.loads(config_path.read_text(encoding='utf-8'))
            return result, requested_devices, reasons['skipped_protocols'], saved_config

    @staticmethod
    def ui_response(parameters):
        return {'errno': 0, 'result': {'parameters': parameters}}

    def test_missing_keys_skips_immediately_and_continues_next_protocol(self):
        missing = self.ui_response([{'name': 'Other', 'key': 'other', 'properties': []}])
        success = self.ui_response(status_parameters())
        result, devices, reasons, config = self.run_getui([missing, success], True)
        self.assertEqual(devices, ['device-0', 'next-device'])
        self.assertEqual(len(result), 2)
        self.assertEqual({item['protocol_version'] for item in result}, {'Q113xx'})
        reason, = reasons
        self.assertEqual(reason['reason'], '未找到目标 KEY，直接跳过该协议')
        self.assertEqual(reason['attempted_count'], 1)
        self.assertEqual(reason['remaining_candidate_count'], 7)
        self.assertEqual(reason['failure_counts'], {'未找到目标 KEY': 1})
        self.assertEqual(config['get_setting']['skip_protocols'], ['ZAA10301'])

    def test_missing_keys_after_request_failure_still_stops_immediately(self):
        missing = self.ui_response([{'name': 'Other', 'key': 'other', 'properties': []}])
        result, devices, reasons, _ = self.run_getui([
            {'errno': 1, 'msg': 'temporary failure'}, missing])
        self.assertEqual(result, [])
        self.assertEqual(devices, ['device-0', 'device-1'])
        reason, = reasons
        self.assertEqual(reason['attempted_count'], 2)
        self.assertEqual(reason['remaining_candidate_count'], 6)

    def test_request_and_empty_parameters_failures_can_recover(self):
        result, devices, reasons, config = self.run_getui([
            TimeoutError('timeout'), self.ui_response([]), self.ui_response(status_parameters())])
        self.assertEqual(devices, ['device-0', 'device-1', 'device-2'])
        self.assertEqual(len(result), 2)
        self.assertEqual(reasons, [])
        self.assertEqual(config['get_setting']['skip_protocols'], [])

    def test_request_failures_still_stop_at_attempt_limit(self):
        result, devices, reasons, _ = self.run_getui([{'errno': 1, 'msg': 'failure'}] * 5)
        self.assertEqual(result, [])
        self.assertEqual(len(devices), 5)
        reason, = reasons
        self.assertEqual(reason['attempted_count'], 5)
        self.assertEqual(reason['remaining_candidate_count'], 3)
        self.assertIn('达到 getui 尝试上限', reason['reason'])


class ResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_values_survive_request_processing_and_csv_export(self):
        mappings = run._build_protocol_mappings('Q113xx', status_parameters(), READ_NAMES)

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return {'errno': 0, 'result': {'values': self.values}}

        class Session:
            def __init__(self):
                self.request_keys = []

            def get(self, url, *, params, **kwargs):
                self.request_keys.append(params['key'])
                response = Response()
                response.values = ({'q113__29__00': 'enable'} if params['key'] == 'q113__29'
                                   else {'q113__30__00': 'disable'})
                return response

        session = Session()
        with patch.object(run, 'GetAuth') as auth:
            auth.return_value.get_signature.return_value = {}
            result = await run._process_device(
                session, ('device', 'sn', 'Q113xx', 'version'), 'test-token', {'Q113xx': mappings})
        self.assertEqual(result['status'], 'success')
        self.assertEqual(session.request_keys, ['q113__29', 'q113__30'])
        self.assertEqual(result['record']['Modbus-RTU.Status'], 'enable')
        self.assertEqual(result['record']['Modbus-TCP.Status'], 'disable')
        self.assertEqual(len(json.loads(result['record']['success_info'])), 2)

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = run.Checkpoint(str(Path(directory, 'checkpoint.sqlite3')))
            try:
                checkpoint.save_batch([result], 1)
                with patch.dict(run.config, {'getui': {'read_names': READ_NAMES}}):
                    writer = run.ResultWriter(checkpoint, directory, run._get_value_columns(), 'test')
                writer.export(('success',))
                with Path(directory, 'success.csv').open(encoding='utf-8-sig', newline='') as source:
                    row, = csv.DictReader(source)
                self.assertEqual(row['Modbus-RTU.Status'], 'enable')
                self.assertEqual(row['Modbus-TCP.Status'], 'disable')
            finally:
                checkpoint.close()


if __name__ == '__main__':
    unittest.main()
