"""Bounded-memory result persistence and recovery for device reads."""

import csv
import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime


BASE_FIELDS = ['device_id', 'device_sn', 'protocol_version', 'master_version']


def write_json_atomic(path, payload):
    temp_path = f'{path}.tmp'
    with open(temp_path, 'w', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write('\n')
    os.replace(temp_path, path)


class RunLock:
    """Prevent two processes from writing the same result files."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, 'a+b')
        try:
            self.handle.seek(0)
            if not self.handle.read(1):
                self.handle.write(b'0')
                self.handle.flush()
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError('已有查询进程正在运行，不能重复启动') from exc
        return self

    def __exit__(self, *args):
        if self.handle:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()


class Checkpoint:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA cache_size=-8192')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS results (
            device_id TEXT NOT NULL, protocol_version TEXT NOT NULL,
            status TEXT NOT NULL, round_num INTEGER NOT NULL, record TEXT NOT NULL,
            PRIMARY KEY (device_id, protocol_version))''')
        self.connection.execute('''CREATE INDEX IF NOT EXISTS results_status_round
            ON results(status, round_num)''')
        self.connection.commit()
        self.totals = {'success': 0, 'fail': 0}
        self.totals.update(self.connection.execute(
            'SELECT status, COUNT(*) FROM results GROUP BY status'))

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.connection.execute(
                'SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, **values):
        with self.lock, self.connection:
            self.connection.executemany(
                'INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)',
                [(key, json.dumps(value, ensure_ascii=False))
                 for key, value in values.items()])

    def completed_keys(self, devices, round_num):
        """Successes and this round's failures must not be requested again."""
        keys = list(dict.fromkeys((device[0], device[2]) for device in devices))
        completed = set()
        with self.lock:
            for start in range(0, len(keys), 200):
                chunk = keys[start:start + 200]
                placeholders = ','.join('(?, ?)' for _ in chunk)
                parameters = [value for key in chunk for value in key]
                parameters.append(round_num)
                rows = self.connection.execute(
                    f'''SELECT device_id, protocol_version FROM results
                        WHERE (device_id, protocol_version) IN (VALUES {placeholders})
                        AND (status='success' OR round_num>=?)''', parameters)
                completed.update(rows)
        return completed

    def save_batch(self, results, round_num):
        accepted = []
        seen = set()
        changes = {'success': 0, 'fail': 0}
        with self.lock:
            with self.connection:
                for result in results:
                    record = result['record']
                    key = (record['device_id'], record['protocol_version'])
                    if key in seen:
                        continue
                    seen.add(key)
                    previous = self.connection.execute(
                        'SELECT status, round_num FROM results WHERE device_id=? '
                        'AND protocol_version=?', key).fetchone()
                    if previous and (previous[0] == 'success' or previous[1] >= round_num):
                        continue
                    self.connection.execute(
                        'INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)',
                        (*key, result['status'], round_num,
                         json.dumps(record, ensure_ascii=False)))
                    if previous:
                        changes[previous[0]] -= 1
                    changes[result['status']] += 1
                    accepted.append(result)
            for status, change in changes.items():
                self.totals[status] += change
        return accepted

    def counts(self, round_num=None):
        with self.lock:
            if round_num is None:
                return dict(self.totals)
            query = 'SELECT status, COUNT(*) FROM results'
            parameters = ()
            if round_num is not None:
                query += ' WHERE round_num=?'
                parameters = (round_num,)
            rows = self.connection.execute(query + ' GROUP BY status', parameters)
            counts = {'success': 0, 'fail': 0}
            counts.update(rows)
            return counts

    def iter_records(self, status):
        with self.lock:
            cursor = self.connection.execute(
                'SELECT record FROM results WHERE status=? ORDER BY rowid', (status,))
            try:
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        yield json.loads(row[0])
            finally:
                cursor.close()

    def close(self):
        with self.lock:
            self.connection.close()


class ResultWriter:
    def __init__(self, checkpoint, output_dir, columns, run_id):
        self.checkpoint = checkpoint
        self.output_dir = output_dir
        self.columns = columns
        self.run_id = run_id
        self.handles = {}
        self.writers = {}
        self.lock = threading.RLock()

    def fields(self, status):
        return (BASE_FIELDS + self.columns + ['success_info'] if status == 'success'
                else BASE_FIELDS + ['err_info'])

    def row(self, record, status):
        row = dict(record)
        if status == 'success':
            for column in self.columns:
                row.setdefault(column, 'N/A')
        return row

    def export(self, statuses=('success', 'fail')):
        """Recover CSVs from committed data without duplicating partial writes."""
        for status in statuses:
            path = os.path.join(self.output_dir, f'{status}.csv')
            temp_path = f'{path}.tmp'
            with open(temp_path, 'w', encoding='utf-8-sig', newline='') as output:
                writer = csv.DictWriter(output, fieldnames=self.fields(status),
                                        extrasaction='ignore')
                writer.writeheader()
                for record in self.checkpoint.iter_records(status):
                    writer.writerow(self.row(record, status))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)

    def prepare(self):
        marker_path = os.path.join(self.output_dir, 'active_run.json')
        previous_id = None
        if os.path.exists(marker_path):
            with open(marker_path, encoding='utf-8') as source:
                previous_id = json.load(source).get('run_id')
        if previous_id != self.run_id:
            old_files = [os.path.join(self.output_dir, name) for name in
                         ('success.csv', 'fail.csv', 'success.log', 'fail.log', 'all.log')]
            old_files = [path for path in old_files if os.path.isfile(path)]
            if old_files:
                stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                archive = os.path.join(self.output_dir, 'archive', stamp)
                os.makedirs(archive)
                for path in old_files:
                    shutil.copy2(path, archive)
                print(f'已有结果已备份到: {archive}', flush=True)
            for name in ('success.log', 'fail.log', 'all.log'):
                with open(os.path.join(self.output_dir, name), 'w', encoding='utf-8'):
                    pass
        write_json_atomic(marker_path, {'run_id': self.run_id,
                                       'checkpoint': self.checkpoint.path})
        self.export()

    def open(self):
        for status in ('success', 'fail'):
            handle = open(os.path.join(self.output_dir, f'{status}.csv'),
                          'a', encoding='utf-8', newline='')
            self.handles[f'{status}.csv'] = handle
            self.writers[status] = csv.DictWriter(
                handle, fieldnames=self.fields(status), extrasaction='ignore')
        for name in ('success.log', 'fail.log', 'all.log'):
            self.handles[name] = open(os.path.join(self.output_dir, name),
                                      'a', encoding='utf-8')

    def save_batch(self, results, round_num):
        with self.lock:
            accepted = self.checkpoint.save_batch(results, round_num)
            for result in accepted:
                status = result['status']
                record = result['record']
                self.writers[status].writerow(self.row(record, status))
                info = record['success_info'] if status == 'success' else record['err_info']
                stamp = datetime.now().isoformat(timespec='milliseconds')
                line = f'[{stamp}] {record["device_id"]} {status}: {info}\n'
                self.handles[f'{status}.log'].write(line)
                self.handles['all.log'].write(line)
            for handle in self.handles.values():
                handle.flush()
            return self.checkpoint.counts()

    def close(self):
        with self.lock:
            for handle in self.handles.values():
                handle.close()
            self.handles.clear()
            self.writers.clear()
