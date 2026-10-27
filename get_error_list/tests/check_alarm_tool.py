import csv
from datetime import date
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

from api import ALARM_PATH, ApiError, CONCURRENCY, FoxClient, alarm_body, auth_headers, validate_config
from credentials import CredentialStore
from exporter import COLUMNS, export_alarms, read_sns
from run import AlarmApp

BEGIN, END = date(2026, 10, 20), date(2026, 10, 27)


def sample_rows(sn):
    return [
        {"alarmCode": 141, "alarmType": 1, "content": "Gfci故障 ↑", "deviceSN": sn,
         "moduleSN": "609Q1EYF48CB489", "plantName": "Днепр Александр ", "time": "2026-10-22 22:14:21 EEST+0300"},
        {"alarmCode": 141, "alarmType": 1, "content": "Gfci故障 ↓", "deviceSN": sn,
         "moduleSN": "609Q1EYF48CB489", "plantName": "Днепр Александр ", "time": "2026-10-22 22:19:23 EEST+0300"},
    ]


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


class FixtureServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


class FixtureHandler(BaseHTTPRequestHandler):
    calls = []
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.calls.append((self.path, body, dict(self.headers)))
        if self.path.endswith("/login"):
            result = {"token": "local-test-token"}
        else:
            sn = body["condition"]["deviceSN"]
            if sn == "FAIL":
                return self.reply({"errno": 4012, "msg": "must not echo body or credentials"})
            if sn == "RETRY":
                with self.lock:
                    count = sum(item[1].get("condition", {}).get("deviceSN") == sn for item in self.calls)
                if count < 3:
                    return self.reply({"error": "busy"}, 503)
            rows = sample_rows(sn)
            result = {"total": len(rows), "data": rows, "currentPage": 1, "pageSize": 50}
            if sn == "ZERO":
                result.update(total=0, data=[])
            if sn == "TRUNC":
                result.update(total=65, data=[dict(rows[0]) for _ in range(50)])
            if sn == "SHORT":
                result.update(total=12, pageSize=10, data=[dict(rows[0]) for _ in range(10)])
            if sn == "BADFIELDS":
                del rows[0]["time"]
            if sn == "BADSN":
                rows[0]["deviceSN"] = "ANOTHER-SN"
        self.reply({"errno": 0, "result": result})

    def reply(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class AlarmToolChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer(("127.0.0.1", 0), FixtureHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.config = {"domain": f"http://127.0.0.1:{cls.server.server_port}", "lang": "en"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with FixtureHandler.lock:
            FixtureHandler.calls.clear()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, cancel=None):
        client = FoxClient(self.config, cancel)
        self.addCleanup(client.close)
        client.login("fixture-user", "fixture-原始-password")
        return client

    def test_request_and_existing_signature_contract(self):
        body = alarm_body("603J303048EP059", BEGIN, END)
        self.assertEqual(body, {
            "condition": {"deviceSN": "603J303048EP059", "alarmType": 0},
            "beginDate": {"year": "2026", "month": "10", "day": "20"},
            "endDate": {"year": "2026", "month": "10", "day": "27"},
            "currentPage": 1, "pageSize": 50,
        })
        headers = auth_headers("test-token", ALARM_PATH, "en", 1234567, 42)
        expected = hashlib.md5((ALARM_PATH + r"\r\ntest-token\r\nen\r\n1234567").encode()).hexdigest() + ".42"
        self.assertEqual(headers["signature"], expected)
        self.assertEqual(headers["timestamp"], "1234567")

    def test_real_http_login_and_query(self):
        result = self.client().query("603J303048EP059", BEGIN, END)
        self.assertEqual(result["data"], sample_rows("603J303048EP059"))
        login, query = FixtureHandler.calls
        self.assertEqual(login[1], {"user": "fixture-user", "password": hashlib.md5("fixture-原始-password".encode()).hexdigest()})
        self.assertEqual(query[0], ALARM_PATH)
        self.assertEqual(query[1]["pageSize"], 50)
        self.assertEqual(query[2]["token"], "local-test-token")

    def test_sn_encoding_blank_lines_and_deduplication(self):
        path = self.root / "SN.csv"
        path.write_text("\n603J303048EP059\n\n603J303048EP059\n 603J303048EP060 \n", encoding="utf-8-sig")
        self.assertEqual(read_sns(path), ["603J303048EP059", "603J303048EP060"])
        path.write_bytes("603J303048EP059\r\n".encode("gb18030"))
        self.assertEqual(read_sns(path), ["603J303048EP059"])

    def test_invalid_sn_file_fails_before_query(self):
        path = self.root / "SN.csv"
        for content in ("", "SN\n603J303048EP059\n", "one,two\n", "has space\n"):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ValueError):
                read_sns(path)
        with self.assertRaises(ValueError):
            read_sns(self.root / "missing.csv")

    def test_sample_mapping_unicode_and_timezone(self):
        directory, summary = export_alarms(self.client(), ["603J303048EP059"], BEGIN, END, self.root)
        rows = read_csv(directory / "告警列表.csv")
        self.assertEqual(rows[0], [title for title, _ in COLUMNS])
        self.assertEqual(rows[1], ["1", "Днепр Александр ", "603J303048EP059", "609Q1EYF48CB489", "141", "Gfci故障 ↑", "2026-10-22 22:14:21 EEST+0300"])
        self.assertEqual(rows[2][0], "2")
        self.assertEqual(rows[2][5], "Gfci故障 ↓")
        self.assertTrue((directory / "告警列表.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(summary["complete"])
        self.assertEqual(len(list(directory.iterdir())), 5)

    def test_zero_alarms_keeps_header(self):
        directory, summary = export_alarms(self.client(), ["ZERO"], BEGIN, END, self.root)
        self.assertEqual(len(read_csv(directory / "告警列表.csv")), 1)
        self.assertEqual(read_csv(directory / "查询明细.csv")[1][1], "无告警")
        self.assertEqual(summary["exported_alarms"], 0)
        self.assertTrue(summary["complete"])

    def test_truncation_and_server_page_limit_are_explicit(self):
        directory, summary = export_alarms(self.client(), ["TRUNC", "SHORT"], BEGIN, END, self.root)
        self.assertEqual(summary["exported_alarms"], 60)
        self.assertEqual(summary["incomplete_devices"], 2)
        self.assertFalse(summary["complete"])
        self.assertTrue(all(row[1] == "未完整导出" for row in read_csv(directory / "查询明细.csv")[1:]))
        self.assertEqual(len([call for call in FixtureHandler.calls if call[0] == ALARM_PATH]), 2)

    def test_failed_sn_can_be_reused_as_input_without_header(self):
        directory, summary = export_alarms(self.client(), ["FAIL", "GOOD"], BEGIN, END, self.root)
        self.assertEqual(read_sns(directory / "失败SN.csv"), ["FAIL"])
        self.assertEqual(summary["failed_devices"], 1)
        self.assertEqual(summary["exported_alarms"], 2)
        log = (directory / "运行日志.log").read_text(encoding="utf-8")
        for secret in ("fixture-user", "fixture-原始-password", "local-test-token", "must not echo"):
            self.assertNotIn(secret, log)
        self.assertEqual(sum(call[1].get("condition", {}).get("deviceSN") == "FAIL" for call in FixtureHandler.calls), 1)

    def test_invalid_response_is_failure_instead_of_blank_success(self):
        directory, summary = export_alarms(self.client(), ["BADFIELDS", "BADSN"], BEGIN, END, self.root)
        self.assertEqual(summary["failed_devices"], 2)
        self.assertEqual(len(read_csv(directory / "告警列表.csv")), 1)

    def test_temporary_http_failure_retries_only_to_limit(self):
        self.assertEqual(self.client().query("RETRY", BEGIN, END)["total"], 2)
        self.assertEqual(sum(call[1].get("condition", {}).get("deviceSN") == "RETRY" for call in FixtureHandler.calls), 3)

    def test_fixed_14_concurrency_and_no_lost_or_duplicate_rows(self):
        lock, barrier = threading.Lock(), threading.Barrier(14)
        active, peak, calls = 0, 0, 0

        class ConcurrentClient:
            def query(inner, sn, *_):
                nonlocal active, peak, calls
                with lock:
                    active += 1
                    calls += 1
                    number = calls
                    peak = max(peak, active)
                if number <= 14:
                    barrier.wait(timeout=5)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return {"total": 2, "data": sample_rows(sn)}

        sns = [f"SN{index:05d}" for index in range(70)]
        directory, summary = export_alarms(ConcurrentClient(), sns, BEGIN, END, self.root)
        rows = read_csv(directory / "告警列表.csv")[1:]
        self.assertEqual(CONCURRENCY, 14)
        self.assertEqual(peak, 14)
        self.assertEqual(calls, len(sns))
        self.assertEqual(len(rows), 140)
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 141)))
        self.assertEqual(len({(row[2], row[5]) for row in rows}), 140)
        self.assertTrue(summary["complete"])

    def test_cancel_stops_scheduling_and_preserves_finished_rows(self):
        cancel = threading.Event()
        barrier = threading.Barrier(14)
        lock = threading.Lock()
        count = 0

        class SlowClient:
            def query(inner, sn, *_):
                nonlocal count
                with lock:
                    count += 1
                barrier.wait(timeout=5)
                return {"total": 2, "data": sample_rows(sn)}

        def emit(event):
            if event["type"] == "progress":
                cancel.set()

        directory, summary = export_alarms(SlowClient(), [f"SN{i}" for i in range(100)], BEGIN, END, self.root, cancel, emit)
        self.assertEqual(count, 14)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["not_queried_devices"], 86)
        self.assertEqual(summary["exported_alarms"], 28)
        self.assertEqual(sum(row[1] == "未查询" for row in read_csv(directory / "查询明细.csv")[1:]), 86)

    def test_windows_credentials_are_encrypted_and_scoped_by_domain(self):
        store = CredentialStore("https://example.test", self.root)
        store.save("local-user", "only-a-fixture-password")
        self.assertNotIn(b"only-a-fixture-password", store.path.read_bytes())
        self.assertNotIn(b"local-user", store.path.read_bytes())
        self.assertEqual(store.load(), ("local-user", "only-a-fixture-password"))
        self.assertIsNone(CredentialStore("https://other.test", self.root).load())
        store.path.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            store.load()
        store.delete()
        self.assertIsNone(store.load())

    def test_repeated_runs_create_separate_directories_and_formula_text_is_safe(self):
        class FormulaClient:
            def query(inner, sn, *_):
                row = sample_rows(sn)[0]
                row["plantName"] = "=1+1"
                return {"total": 1, "data": [row]}

        first, _ = export_alarms(FormulaClient(), ["SN1"], BEGIN, END, self.root)
        second, _ = export_alarms(FormulaClient(), ["SN1"], BEGIN, END, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(read_csv(first / "告警列表.csv")[1][1], "'=1+1")

    def test_public_config_rejects_unsafe_urls(self):
        for domain in ("http://example.test", "https://user:secret@example.test", "https://example.test/api", "not-url"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                validate_config({"domain": domain})

    def test_gui_worker_end_to_end_uses_local_fixture(self):
        # Exercise the app controller and its event loop against a real local HTTP fixture.
        import tkinter as tk
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "SN.csv").write_text("GUI-SN\nZERO\n", encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        try:
            app = AlarmApp(root, self.root, self.root / "credentials")
            app.username.set("fixture-user")
            app.password.set("fixture-原始-password")
            app.begin.value.set(BEGIN.isoformat())
            app.end.value.set(END.isoformat())
            app.remember.set(True)
            app.start()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                root.update()
                if app.worker and not app.worker.is_alive() and app.events.empty():
                    break
                time.sleep(0.01)
            self.assertFalse(app.worker.is_alive())
            self.assertTrue(app.last_output.is_dir())
            self.assertIn("查询完成", app.status.get())
            self.assertEqual(len(read_csv(app.last_output / "告警列表.csv")), 3)
            self.assertEqual(app.store.load(), ("fixture-user", "fixture-原始-password"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
