#!/usr/bin/env python3
"""ZenTao bug helper for safe test-side lookup, audit, creation, and cleanup."""

from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "config.yml"


def parse_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        elif value.startswith('"') and value.endswith('"'):
            value = json.loads(value)
        values[key.strip().upper()] = value
    missing = [key for key in ("URL", "USER", "PASSWORD") if not values.get(key)]
    if missing:
        raise ValueError(f"config.yml 缺少配置项：{', '.join(missing)}")
    return values


def api_base(configured_url: str) -> str:
    parts = urllib.parse.urlsplit(configured_url.rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/api.php/v1"):
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if path.endswith("/zentao"):
        app_path = path
    elif path in ("", "/"):
        app_path = "/zentao"
    else:
        app_path = f"{path}/zentao"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"{app_path}/api.php/v1", "", ""))


class ZenTaoClient:
    def __init__(self, config: dict[str, str], timeout: int = 30) -> None:
        self.base = api_base(config["URL"])
        self.timeout = timeout
        self.token = self._request(
            "POST",
            "/tokens",
            {"account": config["USER"], "password": config["PASSWORD"]},
            authenticated=False,
        )["token"]

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self.base}{path}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Token"] = self.token
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("message") or parsed.get("error") or detail
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"ZenTao API {method} {path} 失败：HTTP {exc.code}，{detail}") from exc
        return json.loads(text) if text else {}

    def get_bug(self, bug_id: int) -> dict[str, Any]:
        return self._request("GET", f"/bugs/{bug_id}")

    def list_product_bugs(self, product_id: int) -> list[dict[str, Any]]:
        bugs: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request("GET", f"/products/{product_id}/bugs?page={page}&limit=100")
            batch = result.get("bugs", [])
            bugs.extend(batch)
            if not batch or len(bugs) >= int(result.get("total", len(bugs))):
                return bugs
            page += 1

    def create_bug(self, product_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/products/{product_id}/bugs", payload)

    def delete_bug(self, bug_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/bugs/{bug_id}")


def build_steps(record: dict[str, Any]) -> str:
    sections = [
        ("问题描述", record.get("description")),
        ("复现步骤", record.get("reproduction")),
        ("实际结果", record.get("actual")),
        ("预期结果", record.get("expected")),
        ("影响范围", record.get("impact")),
        ("测试证据", record.get("evidence")),
        ("备注", record.get("note")),
    ]
    output: list[str] = []
    for heading, value in sections:
        if not value:
            continue
        escaped = html.escape(str(value)).replace("\n", "<br>")
        output.append(f"<p><strong>{heading}：</strong><br>{escaped}</p>")
    return "\n".join(output)


def template_payload(template: dict[str, Any], record: dict[str, Any], assigned_to: str | None) -> dict[str, Any]:
    opened_build = []
    for build in template.get("openedBuild") or []:
        opened_build.append(str(build.get("id") if isinstance(build, dict) else build))
    assignee = assigned_to or (template.get("assignedTo") or {}).get("account")
    return {
        "title": record["title"],
        "branch": record.get("branch", template.get("branch", 0)),
        "module": record.get("module", template.get("module", 0)),
        "project": record.get("project", template.get("project", 0)),
        "execution": record.get("execution", template.get("execution", 0)),
        "openedBuild": record.get("openedBuild", opened_build),
        "assignedTo": record.get("assignedTo", assignee),
        "type": record.get("type", template.get("type", "codeerror")),
        "severity": int(record.get("severity", template.get("severity", 3))),
        "pri": int(record.get("pri", template.get("pri", 3))),
        "steps": build_steps(record),
        "keywords": record.get("keywords", "南非停电 Eskom 3.0"),
    }


def selected_bug(bug: dict[str, Any]) -> dict[str, Any]:
    assigned = bug.get("assignedTo") or {}
    opened_by = bug.get("openedBy") or {}
    resolved_by = bug.get("resolvedBy") or {}
    closed_by = bug.get("closedBy") or {}

    def account(value: Any) -> Any:
        return value.get("account") if isinstance(value, dict) else value

    def realname(value: Any) -> str:
        return value.get("realname", "") if isinstance(value, dict) else ""

    return {
        "id": bug.get("id"),
        "title": bug.get("title"),
        "product": bug.get("product"),
        "productName": bug.get("productName"),
        "project": bug.get("project"),
        "projectName": bug.get("projectName"),
        "module": bug.get("module"),
        "moduleTitle": bug.get("moduleTitle"),
        "assignedTo": account(assigned),
        "assignedName": realname(assigned),
        "status": bug.get("status"),
        "resolution": bug.get("resolution"),
        "severity": bug.get("severity"),
        "pri": bug.get("pri"),
        "type": bug.get("type"),
        "openedBy": account(opened_by),
        "openedName": realname(opened_by),
        "openedDate": bug.get("openedDate"),
        "resolvedBy": account(resolved_by),
        "resolvedName": realname(resolved_by),
        "resolvedDate": bug.get("resolvedDate"),
        "closedBy": account(closed_by),
        "closedName": realname(closed_by),
        "closedDate": bug.get("closedDate"),
        "lastEditedDate": bug.get("lastEditedDate"),
    }


def audit_bugs(client: ZenTaoClient, bug_ids: list[int], expected_product: int | None) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(bug_ids))
    if len(ids) != len(bug_ids):
        raise ValueError("审计列表包含重复 ID")
    results: list[dict[str, Any]] = []
    for bug_id in ids:
        bug = selected_bug(client.get_bug(bug_id))
        if expected_product is not None and int(bug.get("product") or 0) != expected_product:
            raise ValueError(f"BUG {bug_id} 产品={bug.get('product')}，不是预期的 {expected_product}")
        results.append(bug)
    return results


def audit_markdown(bugs: list[dict[str, Any]]) -> str:
    def cell(value: Any) -> str:
        if value is None or value == "":
            return "-"
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| ID | 标题 | 状态 | 解决方案 | 指派给 | 产品 / 项目 / 模块 |",
        "|---:|---|---|---|---|---|",
    ]
    for bug in bugs:
        scope = " / ".join(
            cell(value)
            for value in (bug.get("productName"), bug.get("projectName"), bug.get("moduleTitle"))
        )
        assignee = bug.get("assignedName") or bug.get("assignedTo")
        lines.append(
            f"| {cell(bug.get('id'))} | {cell(bug.get('title'))} | {cell(bug.get('status'))} | "
            f"{cell(bug.get('resolution'))} | {cell(assignee)} | {scope} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="ZenTao REST bug helper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--template-bug", type=int, default=3672)

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("--id", type=int, required=True)

    audit_parser = subparsers.add_parser("audit", help="批量只读核对 BUG 并输出 JSON 或 Markdown")
    audit_parser.add_argument("--ids", type=int, nargs="+", required=True)
    audit_parser.add_argument("--expected-product", type=int)
    audit_parser.add_argument("--format", choices=("json", "markdown"), default="json")

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--input", type=Path, required=True)
    create_parser.add_argument("--template-bug", type=int, default=3672)
    create_parser.add_argument("--assigned-to")
    create_parser.add_argument("--max-items", type=int)
    create_parser.add_argument("--commit", action="store_true")

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("--ids", type=int, nargs="+", required=True)
    delete_parser.add_argument("--expected-product", type=int, required=True)
    delete_parser.add_argument("--expected-opened-by")
    delete_parser.add_argument("--confirm-count", type=int, required=True)
    delete_parser.add_argument("--commit", action="store_true")

    args = parser.parse_args()
    config = parse_config(args.config)
    client = ZenTaoClient(config, timeout=int(config.get("TIMEOUT", "30")))

    if args.command == "validate":
        print(json.dumps({"authenticated": True, "template": selected_bug(client.get_bug(args.template_bug))}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "get":
        print(json.dumps(selected_bug(client.get_bug(args.id)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "audit":
        bugs = audit_bugs(client, args.ids, args.expected_product)
        if args.format == "markdown":
            print(audit_markdown(bugs))
        else:
            print(json.dumps(bugs, ensure_ascii=False, indent=2))
        return 0
    if args.command == "delete":
        ids = list(dict.fromkeys(args.ids))
        if len(ids) != len(args.ids):
            raise ValueError("删除列表包含重复 ID")
        if len(ids) != args.confirm_count:
            raise ValueError(f"删除数量={len(ids)}，与 --confirm-count={args.confirm_count} 不一致")
        expected_opener = args.expected_opened_by or config["USER"]
        targets: list[dict[str, Any]] = []
        for bug_id in ids:
            bug = client.get_bug(bug_id)
            opener = bug.get("openedBy") or {}
            opener_account = opener.get("account") if isinstance(opener, dict) else opener
            if int(bug.get("product", 0)) != args.expected_product:
                raise ValueError(f"BUG {bug_id} 产品={bug.get('product')}，不是预期的 {args.expected_product}")
            if opener_account != expected_opener:
                raise ValueError(f"BUG {bug_id} 创建人={opener_account}，不是预期的 {expected_opener}")
            targets.append({"id": bug_id, "title": bug.get("title"), "product": bug.get("product"), "openedBy": opener_account})
        if not args.commit:
            print(json.dumps({"status": "dry-run", "count": len(targets), "targets": targets}, ensure_ascii=False, indent=2))
            return 0
        deleted: list[dict[str, Any]] = []
        for target in targets:
            response = client.delete_bug(target["id"])
            message = response.get("message") or response.get("status")
            if str(message).lower() not in ("success", "ok"):
                raise RuntimeError(f"BUG {target['id']} 删除响应异常：{response}")
            try:
                remaining = client.get_bug(target["id"])
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
            else:
                if not remaining.get("deleted"):
                    raise RuntimeError(f"BUG {target['id']} 删除后仍可作为有效记录读取")
            deleted.append({"status": "deleted", **target})
        print(json.dumps(deleted, ensure_ascii=False, indent=2))
        return 0

    records = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(records, list) or not records:
        raise ValueError("输入必须是非空 JSON 数组")
    if args.max_items is not None:
        if args.max_items <= 0:
            raise ValueError("--max-items 必须大于 0")
        records = records[: args.max_items]
    template = client.get_bug(args.template_bug)
    product_id = int(template["product"])
    existing = {bug.get("title"): bug.get("id") for bug in client.list_product_bugs(product_id)}
    results: list[dict[str, Any]] = []
    seen_input: set[str] = set()
    for index, record in enumerate(records, start=1):
        title = str(record.get("title", "")).strip()
        if not title:
            raise ValueError(f"第 {index} 条缺少 title")
        if title in seen_input:
            raise ValueError(f"输入中存在重复标题：{title}")
        seen_input.add(title)
        if title in existing:
            results.append({"status": "skipped-existing", "id": existing[title], "title": title})
            continue
        payload = template_payload(template, record, args.assigned_to)
        if not args.commit:
            results.append({"status": "dry-run", "title": title, "assignedTo": payload["assignedTo"], "product": product_id})
            continue
        response = client.create_bug(product_id, payload)
        created = response.get("bug", response) if isinstance(response, dict) else {}
        bug_id = created.get("id")
        if not bug_id:
            raise RuntimeError(f"创建后未返回 bug id：{title}")
        verified = client.get_bug(int(bug_id))
        results.append({"action": "created", **selected_bug(verified)})
        existing[title] = bug_id
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
