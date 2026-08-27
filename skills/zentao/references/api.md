# ZenTao 22.4 REST API notes

The configured server exposes ZenTao under `/zentao/` and REST endpoints under `/zentao/api.php/v1`.

- Create token: `POST /tokens` with JSON `account` and `password`.
- Read bug: `GET /bugs/{id}` with the token in the `Token` header.
- Batch audit is implemented as ordered calls to `GET /bugs/{id}` so the script can validate every exact ID and product before producing a report.
- List product bugs: `GET /products/{productID}/bugs?page={page}&limit={limit}`.
- Create product bug: `POST /products/{productID}/bugs`.
- Delete bug: `DELETE /bugs/{id}`. ZenTao v1 returns `{"message":"success"}`; deleted records may remain recoverable through the administrator recycle mechanism.

The local FoxCloud2.0 template was discovered from bugs 3672 and 3673:

- product: 33 (`FoxCloud2.0`)
- project: 57 (`FoxCloud2.0`)
- module: 121 (`接口`)
- opened build: 1 (`test`)
- assignee: `linziliang` (`林子良`)
- type: `codeerror`

Never write the returned API token to disk or include credentials in diagnostic output.

## Local command safety

- `validate`, `get`, and `audit` are read-only.
- `create` and `delete` are dry-run unless `--commit` is supplied.
- Preparing a test verification result does not authorize changing the BUG lifecycle state.
- Use `config.example.yml` when documenting setup; never copy the real `config.yml`.
