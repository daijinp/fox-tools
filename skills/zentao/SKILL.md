---
name: zentao
description: Audit, read, draft, create, and explicitly authorized delete ZenTao bugs through the workspace's authenticated REST API configuration. Use for 禅道 BUG lookup, test-side status checks, ID/title inventories, verification-result preparation, deduplication, authorized submission, or authorized cleanup in FoxCloud2.0.
---

# ZenTao

Use `scripts/zentao.py` for deterministic API operations. Credentials come only from `config.yml`; never print, copy, commit, or persist the password or API token. This skill supports the tester's BUG-record workflow; do not infer authorization to fix code, resolve a BUG, or close a BUG from a request to prepare verification text.

## Workflow

1. Confirm `config.yml` is ignored by Git.
2. Run `validate` to authenticate and read the template bug without mutation.
3. For status review, run `audit` with exact BUG IDs and, when known, `--expected-product`. Prefer Markdown output for documents and JSON for automation.
4. Build verification conclusions from test evidence. Mark a BUG as verified only when current evidence proves the fix; otherwise write a pending-retest template and its acceptance criteria.
5. For submission, prepare a UTF-8 JSON array containing independent bug records.
6. Run `create` without `--commit` to validate fields and check exact-title duplicates.
7. Create bugs only when the user has explicitly authorized the external mutation. Add `--commit`; stop on the first API failure.
8. Read every created bug back and report its ID, title, assignee, product, project, and module.

For deletion, first fetch and display every exact ID. Require the expected product, opener, and exact count to match before mutation. Delete only with explicit user authorization and `--commit`; never infer a range from nearby IDs.

Default FoxCloud2.0 template: bug 3672. It supplies product, project, module, build, type, severity, priority, and assignee. Pass explicit overrides only when requested.

```powershell
python skills/zentao/scripts/zentao.py validate --template-bug 3672
python skills/zentao/scripts/zentao.py audit --ids 3672 3673 --expected-product 33 --format markdown
python skills/zentao/scripts/zentao.py create --input "sepush/docs/【禅道提交数据】南非停电通知3.0.json" --template-bug 3672
python skills/zentao/scripts/zentao.py create --input "sepush/docs/【禅道提交数据】南非停电通知3.0.json" --template-bug 3672 --assigned-to linziliang --commit
python skills/zentao/scripts/zentao.py delete --ids 3674 3675 --expected-product 33 --confirm-count 2
python skills/zentao/scripts/zentao.py delete --ids 3674 3675 --expected-product 33 --confirm-count 2 --commit
```

The create command skips exact-title duplicates in the target product. Do not bypass this check for retries.

`audit` is read-only and preserves the requested ID order. It returns status, resolution, assignee, scope, severity, priority, opener, resolution/closure metadata, and edit time. Do not treat `Active` as proof that a fix failed; correlate it with the latest test record.

Keep reusable verification documents separate from API credentials. A verification document may contain BUG IDs, titles, log IDs, test data counts, expected behavior, and conclusions, but must not contain passwords, API tokens, registration IDs, plant IDs, device IDs, or user IDs.

For endpoint details or adapting to another ZenTao version, read [references/api.md](references/api.md).
