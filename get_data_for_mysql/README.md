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

## 大批量查询、中断恢复

默认开启每批落盘和断点续跑，原有配置和运行命令均可继续使用：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_devices
```

- 每批完整读取成功后，将该批结果写入临时文件，刷新到磁盘，再提交为独立断点文件。没有匹配结果的成功批次也会记录；读到半批时出错不会提交半成品。
- 断点位于 `output.directory/checkpoints/query_devices/<任务哈希>/`。进程退出、断网或电脑重启后，重新执行相同命令即可跳过已提交批次，继续未完成批次。请保留该目录。未提交的当前批次会重新查询。
- 任务身份包含按顺序去重后的 SN 列表、源文件路径、分组、每批大小、查询 SQL、SSH/MySQL 目标和用户。改变这些内容会自动使用新的断点目录；改变密码、超时或重试配置不影响续跑。旧断点不会自动删除。
- 临时网络错误、连接中断和超时默认最多尝试 `5` 次（包含首次），重试间隔为 `2/4/8/16` 秒；更多尝试时等待上限为 `30` 秒。每次失败会关闭现有连接，下次重新建立 SSH/MySQL 连接，并重新检查索引及首批执行计划。认证失败、SQL 错误和执行计划不安全等问题直接停止。
- 每次尝试默认最多 `60` 秒，包含连接、检查和整批读取。超时或按 `Ctrl+C` 会关闭 SSH 传输，避免 SSH 通道读取一直等待。SSH 握手另受 `ssh.timeout_seconds` 限制。
- 每个分组完成后立即导出其两个 CSV。单个 CSV 通过同目录临时文件替换，写入失败时保留原文件；两个 CSV 不是一起替换，中途停止后重新执行可从断点重新生成完整结果。
- 同一输出目录同时只允许一个进程运行，进程异常退出后操作系统会释放锁，无需手动删除 `.query_devices.lock`。
- 断点带校验和，损坏时会停止并报告文件位置，避免悄悄导出错误数据。

可选配置（省略时使用下面的默认值）：

```json
"query": {
  "batch_size": 200,
  "batch_interval_seconds": 1,
  "timeout_seconds": 60,
  "max_attempts": 5,
  "retry_interval_seconds": 2
}
```

**已完成的任务也会复用断点，不会自动刷新数据库当前状态。** 若要全部重新获取，执行一次：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_devices -fresh
```

`-fresh` 会先将当前输入对应的旧断点目录重命名归档，再开始新一轮查询。若这一轮中断，后续用原命令续跑，**不要再次添加 `-fresh`**。

查询仍逐批执行，不是数据库的一致性快照。跨时间续跑时，已有批次保留原查询时的状态，未完成批次读取恢复时的状态（包括在线状态）；需要整体刷新时使用 `-fresh`。几十万条结果会按单个分组在内存中汇总、排序后导出，断点额外占用磁盘空间。

## 南非停电通知只读核查

核查 `ns_power_outage_info` 的近期计划和数据格式：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_sepush -config .\config\config.json
```

拿到测试电站的历史 `block_id` 后，可只核查该区域：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_sepush -config .\config\config.json -block-id eskde-10-fourways
```

同时核查电站、设备归属、当天计划、强充设置和 APP 推送关系：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_sepush -config .\config\config.json `
  -plant-id <plant_id> -block-id <block_id> -device-id <device_id>
```

该命令只包含固定的 `SELECT`，并在只读事务中执行，不提供任意 SQL 输入。
