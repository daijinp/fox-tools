# 设备配置查询

在仓库的 Python 环境中运行 `get_setting/run.py`，或使用 PowerShell 在后台启动：

```powershell
& .\get_setting\start-background.ps1
```

后台启动后只检查进程是否立即退出，不等待查询完成。PID 和标准输出、错误日志路径保存在 `device_data/launch.json`；当前阶段和最近一次保存数量见 `device_data/run_status.json`。

`config/config.json` 保留现有登录、查询字段和输入文件配置。新增设置如下：

`getui.read_names` 支持字段名和以 `.` 连接的完整名称路径。例如分别读取两个同名的 `Status`：

```json
"read_names": [
  ["Modbus-RTU.Status", "Modbus-TCP.Status"]
]
```

路径按 UI 响应中的 `name` 精确匹配，区分大小写；更深层级可写成 `配置组.子组.字段`。
CSV 使用配置中的完整名称作为列名，上例会生成 `Modbus-RTU.Status`、`Modbus-TCP.Status` 两列，分别保留各自的值。
原来的 `WorkMode` 等单独字段名仍然可用；如果同一名称匹配到不同 KEY，则该候选设备的映射生成失败并记录原因，继续按候选设备和协议跳过规则处理，避免同名值被覆盖。此时需改用完整路径。
内层数组仅用于整理查询项，不表示父子路径。查询列名变化会创建独立检查点，不复用旧列的结果。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `getui.max_attempts_per_protocol` | 5 | 每个协议最多尝试的候选设备数 |
| `getui.request_timeout` | 30 | 单次 UI 请求超时秒数 |
| `get_setting.write_batch_size` | 500 | 每批提交的结果数量 |
| `get_setting.flush_interval` | 5 | 未满一批时，最多等待多少秒就提交 |
| `get_setting.max_retries` | 15 | 首次读取后的最大重试轮数，可设 0～15 |
| `get_setting.resume` | true | 恢复相同输入和查询字段的已保存进度 |

getui 正常返回非空 `parameters`，但未匹配到目标 KEY 时，立即跳过该协议，不再尝试该协议的其他设备。
跳过原因、实际尝试次数和剩余候选数记录在 `config/skip_protocol_reasons.json`；后续配置读取也跳过该协议。
接口失败、超时、解析异常或 `parameters` 为空时，仍按 `max_attempts_per_protocol` 尝试其他候选设备。

输入逐行读取；工作协程和队列数量固定，不随输入行数增加。SQLite 检查点在 `device_data/checkpoints/`，每批先提交检查点，再刷新 CSV 和日志。突然中断后，已提交的设备不会重复查询；尚未提交或正在请求的少量设备可能重查。

`success.csv` 持续增加成功记录；`fail.csv` 在查询期间追加失败记录，每轮结束后整理为仍失败的设备。成功结果包含按实际 `request_key` 分组的 `success_info` JSON 数组。

再次运行时，从检查点恢复 CSV 并继续未完成设备；同一设备、同一协议的重复输入不会重复生成结果。失败设备按轮次重试，首次读取后最多重试 15 轮，某轮没有成功设备时提前结束。errno=41935 视为离线，记录到 fail.csv 后不再自动重试，断点恢复也跳过这些设备。重启沿用已保存的轮数，不会重置上限；需要全新读取时使用 resume=false。旧检查点中的离线失败会在迁移时识别。上次自动跳过的协议会重新获取 UI，已有成功结果保留。

输入内容、查询字段、服务地址、登录用户或永久跳过协议发生变化，会创建独立检查点。现有结果先复制到 `device_data/archive/`，避免混入新任务。要对同一份输入重新查询全部设备，可临时设置 `resume=false`。不要删除检查点目录，否则不能恢复进度。

同一结果目录只允许一个查询进程运行。正常运行时修改并发或保存间隔，需要重启后生效。
