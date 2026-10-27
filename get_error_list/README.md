# 设备告警导出工具

独立工具，沿用 `get_setting/fr_requests.py` 的 FoxESS Cloud 登录、MD5 密码参数及签名规则，不读取或修改其配置、账号或检查点。开发与构建使用上级 `venv`。

```powershell
& .\venv\Scripts\python.exe .\get_error_list\run.py
& .\venv\Scripts\python.exe -m unittest discover -s .\get_error_list\tests -p 'check_*.py' -v
& .\get_error_list\build.ps1
```

首次构建依赖：

```powershell
& .\venv\Scripts\python.exe -m pip install -r .\get_error_list\requirements-build.txt
```

在 `get_error_list/SN.csv` 填写单列、无表头的 SN。支持 UTF-8/BOM 和 GB18030，忽略空行并去重；错误列数或表头会阻止启动。发布包自带空白 SN.csv，不复制开发者的真实设备列表或登录信息。

`dist/FoxAlarmExport-win-x64_日期_时间/` 是可交付目录，同时生成同名 ZIP。每次构建使用新目录，不混入以前运行的数据。整个目录解压后运行 `FoxAlarmExport.exe`。使用当前 64 位 Python 以 PyInstaller 单文件、无控制台方式构建，包含 Python、Tk 和 HTTP 依赖。见 [PyInstaller 打包说明](https://pyinstaller.org/en/stable/operating-mode.html)。客户使用说明见 [使用说明.txt](使用说明.txt)。

## 接口与边界

- 登录：`POST /c/v0/user/login`，原始密码转小写 MD5 后提交。
- 告警：`POST /c/v0/alarm/query`；`condition={deviceSN, alarmType:0}`；年月日字符串；顶层 `currentPage=1, pageSize=50`。
- 并发写死为 14，最多保持 14 个在途任务，每个工作线程使用独立 Session；结果由单个线程串行写入，逐 SN 刷新。
- 每个 SN 仅查询第一页，不自动翻页；总数大于实际返回数时明确记为未完整导出，包含服务端实际返回少于 50 条的情况。
- 网络错误、HTTP 429 和临时 5xx 最多尝试 3 次；业务错误不自动重试，登录只尝试 1 次。启用 HTTPS 证书校验。
- 提供停止功能，等待在途请求结束；单次连接/读取超时为 10/30 秒，不进行无限重试。
- 不调用响应中的 `downloadUrl`，不附带其他业务接口。

## 字段映射

| 导出列 | 数据来源 |
| --- | --- |
| 序号 | 全局连续编号 |
| 电站 | `plantName` |
| 逆变器SN | `deviceSN` |
| 采集器SN | `moduleSN` |
| 告警编号 | `alarmCode` |
| 告警内容 | `content` |
| 报错时间 | `time` |

文件职责：`api.py` 负责认证、请求和响应校验；`exporter.py` 负责 SN、14 并发调度及结果文件；`credentials.py` 负责 Windows DPAPI 保存；`run.py` 负责界面。新增同系统查询时可复用客户端、日期控件和凭据存储，再替换查询体与字段映射。
