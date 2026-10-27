"""FoxESS Cloud client. No imports from the other tool or its credentials."""

import hashlib
import random
import threading
import time
from datetime import date
from urllib.parse import urlsplit

import requests


CONCURRENCY = 14
PAGE_SIZE = 50
LOGIN_PATH = "/c/v0/user/login"
ALARM_PATH = "/c/v0/alarm/query"
DEFAULT_CONFIG = {"domain": "https://www.foxesscloud.com", "lang": "en"}


class ApiError(Exception):
    pass


class QueryCancelled(Exception):
    pass


def validate_config(config):
    domain = str(config.get("domain", DEFAULT_CONFIG["domain"])).rstrip("/")
    url = urlsplit(domain)
    if (url.scheme not in ("http", "https") or not url.netloc
            or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/")):
        raise ValueError("config.json 中 domain 必须是完整的服务地址，不包含接口路径。")
    # Plain HTTP is only allowed for local integration tests.
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("服务地址必须使用 HTTPS。")
    lang = config.get("lang", "en")
    if not isinstance(lang, str) or not lang.strip():
        raise ValueError("config.json 中 lang 必须是非空字符串。")
    return {"domain": domain, "lang": lang.strip()}


def auth_headers(token, path, lang="en", timestamp=None, suffix=None):
    """Preserve get_setting/fr_requests.py's literal backslash-r/backslash-n signing."""
    timestamp = str(round(time.time() * 1000) if timestamp is None else timestamp)
    signature_text = f"{path}\\r\\n{token}\\r\\n{lang}\\r\\n{timestamp}"
    digest = hashlib.md5(signature_text.encode("utf-8")).hexdigest()
    return {
        "token": token,
        "lang": lang,
        "timestamp": timestamp,
        "signature": f"{digest}.{random.randint(0, 999999) if suffix is None else suffix}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/117.0.0.0 Safari/537.36",
    }


def alarm_body(sn, begin, end):
    def parts(value):
        return {"year": str(value.year), "month": str(value.month), "day": str(value.day)}

    if not isinstance(begin, date) or not isinstance(end, date) or begin > end:
        raise ValueError("开始日期不能晚于结束日期。")
    return {
        "condition": {"deviceSN": sn, "alarmType": 0},
        "beginDate": parts(begin),
        "endDate": parts(end),
        "currentPage": 1,
        "pageSize": PAGE_SIZE,
    }


class FoxClient:
    """Each of the 14 worker threads owns a separate HTTP session."""

    def __init__(self, config, cancel=None):
        self.config = validate_config(config)
        self.cancel = cancel if cancel is not None else threading.Event()
        self.token = ""
        self._local = threading.local()
        self._sessions = []
        self._lock = threading.Lock()

    def _session(self):
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            with self._lock:
                self._sessions.append(self._local.session)
        return self._local.session

    def _post(self, path, body, retry=True):
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            if self.cancel.is_set():
                raise QueryCancelled()
            try:
                response = self._session().post(
                    self.config["domain"] + path,
                    json=body,
                    headers=auth_headers(self.token, path, self.config["lang"]),
                    timeout=(10, 30),
                    allow_redirects=False,
                )
            except requests.exceptions.SSLError as exc:
                raise ApiError("HTTPS 证书校验失败，请检查证书或网络代理。") from exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 == attempts:
                    raise ApiError("网络连接失败或请求超时。") from exc
            except requests.RequestException as exc:
                raise ApiError("请求失败，请检查网络连接。") from exc
            else:
                with response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt + 1 == attempts:
                            raise ApiError(f"服务暂不可用（HTTP {response.status_code}）。")
                    elif not 200 <= response.status_code < 300:
                        raise ApiError(f"接口请求失败（HTTP {response.status_code}）。")
                    else:
                        try:
                            data = response.json()
                        except ValueError as exc:
                            raise ApiError("接口响应不是有效的 JSON。") from exc
                        if not isinstance(data, dict) or "errno" not in data:
                            raise ApiError("接口响应缺少 errno。")
                        if type(data["errno"]) is not int or data["errno"] != 0:
                            # Do not log bodies, tokens, or server-echoed credentials.
                            code = data["errno"]
                            code = str(code) if type(code) is int else "未知"
                            raise ApiError(f"接口返回业务错误（errno={code}）。")
                        if not isinstance(data.get("result"), dict):
                            raise ApiError("接口响应缺少 result 对象。")
                        return data["result"]
            if self.cancel.wait(0.5 * (attempt + 1)):
                raise QueryCancelled()
        raise ApiError("接口请求失败。")

    def login(self, username, password):
        result = self._post(LOGIN_PATH, {
            "user": username,
            "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
        }, retry=False)
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise ApiError("登录响应缺少 token。")
        self.token = token

    def query(self, sn, begin, end):
        if not self.token:
            raise ApiError("请先登录。")
        result = self._post(ALARM_PATH, alarm_body(sn, begin, end))
        rows, total = result.get("data"), result.get("total")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ApiError("告警响应 data 格式错误。")
        if type(total) is not int or total < len(rows):
            raise ApiError("告警响应 total 格式错误。")
        required = ("plantName", "deviceSN", "moduleSN", "alarmCode", "content", "time")
        for row in rows:
            if any(key not in row for key in required):
                raise ApiError("告警响应缺少导出字段。")
            if row["deviceSN"] != sn:
                raise ApiError("接口返回的 deviceSN 与查询 SN 不一致。")
        if result.get("currentPage", 1) != 1:
            raise ApiError("接口返回的页码不是第一页。")
        return result

    def close(self):
        for session in self._sessions:
            session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
