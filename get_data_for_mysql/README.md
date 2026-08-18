# 通过 SSH 隧道查询设备数据

这个 Go 工具直接读取 `sns` 目录中的 CSV，通过 SSH 私钥隧道连接 MySQL，并且只执行 `SELECT`。不需要 DataGrip，也不需要开启 `local_infile` 或 `allowLoadLocalInfile`。

## 配置

编辑 `config/config.json`，并把 SSH 私钥放在同一个目录：

```text
get_data_for_mysql/config/
├── config.json
├── id_rsa
└── known_hosts  # 首次成功连接时自动生成
```

配置中的相对路径都以 `config.json` 所在目录为基准。关键字段：

- `ssh.address`：SSH 服务器的 `host:port`。
- `ssh.username`：SSH 用户名。
- `ssh.private_key`：私钥文件名，默认是同目录的 `id_rsa`。
- `ssh.private_key_passphrase`：私钥密码；没有则留空。
- `ssh.known_hosts_file`：服务器主机密钥记录文件；无需手工创建或填写指纹。
- `mysql.address`：从 SSH 服务器访问 MySQL 的 `host:port`，经常是 `127.0.0.1:3306`。
- `mysql.username`、`mysql.password`、`mysql.database`：MySQL 账号、密码和库名，建议使用只读账号。

真实的 `config.json`、私钥和自动生成的 `known_hosts` 会被 `.gitignore` 忽略。
首次连接会自动信任服务器并生成 `known_hosts`；后续连接如果检测到服务器密钥变化，会拒绝连接并提示检查。

## 运行

在项目根目录执行：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_devices
```

也可以指定另一份配置：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_devices -config E:\path\to\config.json
```

程序会把配置中的每个 CSV 作为独立分组，只取第一列 SN，并在各自分组内去除 BOM、空值和重复值。不同源 CSV 的 SN 和结果不会合并。程序只使用一个 MySQL 连接串行执行固定的 `SELECT`：每批最多查询 `200` 个 SN，批次之间默认等待 `1` 秒。

正式查询前，程序会检查 `devices(device_sn)` 和 `versions(model_id)` 是否为索引首列，并对首批查询执行 `EXPLAIN`。如果执行计划显示全表扫描、全索引扫描或没有使用索引，程序会直接退出，不读取业务数据。

全部设备和在线协议设备共用同一轮分批查询，在线设备结果在 Go 中汇总，不会为第二份结果再次查询数据库。

结果按“源 CSV 文件名 + 结果类型”写入 `output.directory`。默认两个源文件会生成四个文件：

- `单相_all_device_data.csv`
- `单相_online_device_by_protocol.csv`
- `三相_all_device_data.csv`
- `三相_online_device_by_protocol.csv`

旧版本生成的 `all_device_data.csv` 和 `online_device_by_protocol.csv` 如果存在，会在四个新文件全部写入后移动到 `output/legacy_combined/`，不会删除。

四个输出 CSV 都不包含表头，第一行就是查询数据；没有结果时生成空文件。
