# 采集器或者 HUB 证书下载
import json
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_CONFIG_NAME = "config.json"
DEFAULT_PORT = 14435
DEFAULT_PATH = "/aba/bcb/cac"
DEFAULT_USER_AGENT = "esp-idf/1.0 esp32"
DEFAULT_SAVE_NAME = "downloaded_cert.bin"
DEFAULT_JSON_NAME = "cert_response.json"


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_config_path() -> Path:
    return get_app_dir() / DEFAULT_CONFIG_NAME


def load_config() -> dict:
    config_path = get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_url(domain: str, sn: str, a_param: str, port: int, path: str) -> str:
    return f"https://{domain}:{port}{path}?a={a_param}&b={sn}"


def create_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    if verify_ssl:
        return ssl.create_default_context()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def request_cert(
    domain: str,
    sn: str,
    a_param: str,
    port: int,
    path: str,
    user_agent: str,
    timeout: int,
    verify_ssl: bool,
):
    url = build_url(domain=domain, sn=sn, a_param=a_param, port=port, path=path)
    headers = {
        "Host": f"{domain}:{port}",
        "User-Agent": user_agent,
    }
    request = Request(url, headers=headers)
    context = create_ssl_context(verify_ssl)
    return urlopen(request, timeout=timeout, context=context)


def get_filename_from_headers(headers) -> str:
    content_disposition = headers.get("Content-Disposition", "")
    if "filename=" in content_disposition:
        filename = content_disposition.split("filename=", 1)[1].strip().strip('"')
        if filename:
            return Path(filename).name
    return DEFAULT_SAVE_NAME


def should_save_file(headers, body: bytes) -> bool:
    content_type = (headers.get("Content-Type") or "").lower()
    content_disposition = (headers.get("Content-Disposition") or "").lower()

    if "attachment" in content_disposition or "filename=" in content_disposition:
        return True

    text_types = ("text/", "application/json", "application/xml", "application/javascript")
    if any(content_type.startswith(item) for item in text_types):
        return False

    if not content_type:
        try:
            body.decode("utf-8")
            return False
        except UnicodeDecodeError:
            return True

    return True


def save_json_response(response_text: str) -> Path:
    save_path = Path.cwd() / DEFAULT_JSON_NAME
    save_path.write_text(response_text, encoding="utf-8")
    return save_path


def save_cert_field(cert_value: str) -> Path:
    suffix = ".pem" if "BEGIN CERTIFICATE" in cert_value else ".txt"
    save_path = Path.cwd() / f"downloaded_cert{suffix}"
    save_path.write_text(cert_value, encoding="utf-8")
    return save_path


def main() -> int:
    try:
        config = load_config()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Config file format error: {exc}", file=sys.stderr)
        return 1

    domain = config.get("domain", "").strip()
    sn = config.get("sn", "").strip()
    a_param = config.get("a_param", "").strip()
    port = int(config.get("port", DEFAULT_PORT))
    path = config.get("path", DEFAULT_PATH)
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
    timeout = int(config.get("timeout", 15))
    verify_ssl = bool(config.get("verify_ssl", False))

    if not domain or not sn or not a_param:
        print("Please fill in domain, sn and a_param in config.json.", file=sys.stderr)
        return 1

    url = build_url(domain=domain, sn=sn, a_param=a_param, port=port, path=path)
    print(f"Config file: {get_config_path()}")
    print(f"Request URL: {url}")

    try:
        with request_cert(
            domain=domain,
            sn=sn,
            a_param=a_param,
            port=port,
            path=path,
            user_agent=user_agent,
            timeout=timeout,
            verify_ssl=verify_ssl,
        ) as response:
            body = response.read()
            headers = response.headers
            status = response.status
    except HTTPError as exc:
        print(f"HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        try:
            print(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            pass
        return 1
    except URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print("Response headers:")
    for key, value in headers.items():
        print(f"  {key}: {value}")

    if should_save_file(headers, body):
        save_path = Path.cwd() / get_filename_from_headers(headers)
        save_path.write_bytes(body)
        print(f"\nFile saved to: {save_path}")
        print(f"Saved size: {len(body)} bytes")
        return 0

    response_text = body.decode("utf-8", errors="replace")

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        print("\nResponse body:")
        print(response_text)
        return 0

    json_path = save_json_response(response_text)
    print(f"\nJSON saved to: {json_path}")

    errno_value = payload.get("errno")
    result = payload.get("result")
    cert_value = result.get("c") if isinstance(result, dict) else None

    if errno_value == 0 and cert_value:
        cert_path = save_cert_field(cert_value)
        print(f"Certificate content saved to: {cert_path}")
        print(f"Certificate text length: {len(cert_value)}")
    else:
        print("Response body:")
        print(response_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
