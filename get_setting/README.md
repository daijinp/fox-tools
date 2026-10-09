# 设备配置查询

在仓库的 Python 环境中运行 `get_setting/run.py`，或使用 PowerShell 在后台启动：

```powershell
& .\get_setting\start-background.ps1
```

后台启动后只检查进程是否立即退出，不等待查询完成。PID 和标准输出、错误日志路径保存在 `device_data/launch.json`；当前阶段和最近一次保存数量见 `device_data/run_status.json`。

`config/config.json` 保留现有登录、查询字段和输入文件配置。新增设置如下：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `getui.max_attempts_per_protocol` | 5 | 每个协议最多尝试的候选设备数 |
| `getui.request_timeout` | 30 | 单次 UI 请求超时秒数 |
| `get_setting.write_batch_size` | 500 | 每批提交的结果数量 |
| `get_setting.flush_interval` | 5 | 未满一批时，最多等待多少秒就提交 |
| `get_setting.resume` | true | 恢复相同输入和查询字段的已保存进度 |

输入逐行读取；工作协程和队列数量固定，不随输入行数增加。SQLite 检查点在 `device_data/checkpoints/`，每批先提交检查点，再刷新 CSV 和日志。突然中断后，已提交的设备不会重复查询；尚未提交或正在请求的少量设备可能重查。

`success.csv` 持续增加成功记录；`fail.csv` 在查询期间追加失败记录，每轮结束后整理为仍失败的设备。成功结果包含按实际 `request_key` 分组的 `success_info` JSON 数组。

再次运行时，从检查点恢复 CSV 并继续未完成设备；同一设备、同一协议的重复输入不会重复生成结果。失败设备按轮次重试，某轮全部失败时停止本次运行，下次启动可继续尝试。上次自动跳过的协议会重新获取 UI，已有成功结果保留。

输入内容、查询字段、服务地址、登录用户或永久跳过协议发生变化，会创建独立检查点。现有结果先复制到 `device_data/archive/`，避免混入新任务。要对同一份输入重新查询全部设备，可临时设置 `resume=false`。不要删除检查点目录，否则不能恢复进度。

同一结果目录只允许一个查询进程运行。正常运行时修改并发或保存间隔，需要重启后生效。
