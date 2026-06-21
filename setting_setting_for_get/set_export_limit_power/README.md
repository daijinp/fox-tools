# ExportLimitedPower 下发脚本使用说明

这个脚本用于筛选需要处理的德国/VDE4105_DE 设备，重新读取设备的 `ExportLimitedPower`，当当前值大于配置阈值时，通过 `/generic/v0/device/setting/set` 下发为目标值。

## 目录结构

```text
set_export_limit_power/
  config/
    config.json
    export_limit_protocol_key_mapping.json  # 运行时自动生成
  device_data/
    success.csv                             # 默认输入文件
  output/
    set_success.csv
    set_skip.csv
    set_fail.csv
    discard.csv
    *.log
  run.py
```

## 输入文件

默认读取：

```text
setting_setting_for_get/set_export_limit_power/device_data/success.csv
```

请把 `get_setting` 生成的 `success.csv` 复制到上面的 `device_data` 目录。

输入文件需要包含这些列：

```text
device_id,device_sn,protocol_version,master_version,country_code,success_info
```

其中 `success_info` 是 `get_setting` 获取 `GridCode` 后生成的字段，例如：

```text
{'i103__grid_code__safety': 'VDE4105_DE'}
```

## 命中规则

脚本会按下面顺序判断设备是否需要处理：

1. 如果 `country_code` 等于配置里的 `country_code_match`，默认是 `DE`，直接命中。
2. 如果不是 `DE`，再判断 `success_info` 里唯一 k-v 的 value 是否等于配置里的 `success_info_value_match`，默认是 `VDE4105_DE`。
3. 两个条件都不满足，则跳过该设备。

## 配置文件

配置文件位置：

```text
setting_setting_for_get/set_export_limit_power/config/config.json
```

当前配置项：

```json
{
  "input_file": "success.csv",
  "target_name": "ExportLimitedPower",
  "target_value": "800",
  "country_code_match": "DE",
  "success_info_value_match": "VDE4105_DE",
  "concurrency": 200,
  "request_timeout": 300,
  "max_retry_rounds": 3
}
```

说明：

- `input_file`：默认输入文件名，放在 `device_data` 目录下。
- `target_name`：需要从 setting UI 中查找的字段名，默认 `ExportLimitedPower`。
- `target_value`：超过该值时下发的新值，默认 `800`。
- `country_code_match`：国家代码命中值，默认 `DE`。
- `success_info_value_match`：GridCode 命中值，默认 `VDE4105_DE`。
- `concurrency`：并发数。
- `request_timeout`：单个请求超时时间，单位秒。
- `max_retry_rounds`：失败后的最大重试轮数。默认 `3` 表示首轮后最多再重试 3 轮，总共最多 4 轮。

登录账号、域名、`getui_config.csv` 仍复用：

```text
setting_setting_for_get/get_setting/config/
```

不会覆盖 `get_setting/config/protocol_key_mapping.json`。

## 运行命令

在 PowerShell 中运行：

```powershell
cd E:\work_code\fox-tools\setting_setting_for_get
..\venv\Scripts\python.exe .\set_export_limit_power\run.py
```

也可以临时指定输入文件，不使用配置里的 `input_file`：

```powershell
cd E:\work_code\fox-tools\setting_setting_for_get
..\venv\Scripts\python.exe .\set_export_limit_power\run.py .\get_setting\device_data\success.csv
```

## 执行流程

1. 读取 `success.csv`。
2. 按国家代码/`success_info` 筛选命中设备。
3. 登录 FoxESS Cloud。
4. 根据 `getui_config.csv` 中的样例设备，调用 `/generic/v0/device/setting/ui` 动态查找 `ExportLimitedPower` 的 key。
5. 将查到的 key 保存到：

```text
set_export_limit_power/config/export_limit_protocol_key_mapping.json
```

6. 对命中设备调用 `/generic/v0/device/setting/get` 获取当前 `ExportLimitedPower`。
7. 当前值大于 `target_value` 时，调用 `/generic/v0/device/setting/set` 下发为 `target_value`。
8. 失败设备按 `max_retry_rounds` 自动重试。

## 输出文件

输出目录：

```text
setting_setting_for_get/set_export_limit_power/output/
```

文件说明：

- `set_success.csv`：已成功下发的设备。
- `set_skip.csv`：当前值小于或等于目标值，无需下发的设备。
- `set_fail.csv`：最终重试后仍失败的设备。
- `discard.csv`：当前值无法转成数字，被丢弃的设备。
- `set_success.log`：下发成功日志。
- `set_skip.log`：无需下发日志。
- `set_fail.log`：失败日志。
- `discard.log`：丢弃日志。
- `all.log`：全部处理日志。

CSV 输出里包含 `round` 字段，可以看到该设备是在第几轮处理成功、跳过、失败或丢弃。

## 注意事项

- 脚本会实际调用 `/generic/v0/device/setting/set` 下发设置，运行前请确认输入文件和配置正确。
- `discard.csv` 不参与重试，因为这类设备的当前值无法转数字。
- 每次运行会覆盖 `output` 下同名 CSV 文件；日志文件会追加写入。
- 如果修改了 `target_name`，请确认 `getui_config.csv` 里对应协议的样例设备可以在 setting UI 中找到这个字段。
