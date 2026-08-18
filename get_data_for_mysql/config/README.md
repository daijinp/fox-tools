# 私有配置目录

这个目录用于放置本机专用的连接配置和 SSH 私钥，例如：

```text
config/
├── config.json
├── id_rsa
└── known_hosts  # 首次成功连接时自动生成
```

`config.json` 和私钥默认会被本目录的 `.gitignore` 忽略。请勿提交真实账号、密码或私钥。

- `private_key` 是相对于 `config.json` 的路径，所以默认的 `id_rsa` 就放在本目录。
- `mysql.address` 是从 SSH 服务器上访问 MySQL 的地址，常见值为 `127.0.0.1:3306`。
- 不需要手工填写服务器指纹。首次连接会信任服务器并写入 `known_hosts`，后续连接会自动校验。
- `query.batch_size` 默认且建议保持为 `200`；`batch_interval_seconds` 默认是 `1` 秒。
