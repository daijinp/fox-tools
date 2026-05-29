# 对话记录（2026-05-15）

## 1. 初始需求

用户最初希望将现有 MQTT Python 脚本打包为 Linux 可执行文件，并且希望在 Linux 上可以直接通过：

```bash
./xxx
```

方式运行。

最开始产出过一个 `.pyz` 版本，但该方案依赖目标机器存在 Python 运行环境，不满足“直接执行、内置运行环境”的要求。

## 2. 方案调整：改为 Go 实现

随后决定将原 Python MQTT 脚本改写为 Go 程序，以便更方便地交叉编译为 Linux 单文件可执行程序。

完成的事项：

- 将原始 MQTT TLS 双向认证发送逻辑迁移到 Go
- 支持 Linux `amd64` 二进制编译
- 打包产物可直接在 Linux 上执行，不依赖目标机安装 Python

相关文件：

- `main.go`
- `go.mod`
- `dist/mqtt_test_linux_amd64`

## 3. 路径问题修复

在 Linux 环境首次运行时，出现如下问题：

```text
ERROR certificate file not found: .\server.pem
```

原因是 Go 版本中保留了 Windows 风格路径 `.\server.pem`，Linux 下无法正确识别。

已修复内容：

- 默认证书路径改为 `server.pem` / `server.key`
- 增加路径归一化逻辑，兼容 Windows 和 Linux

重新编译后，生成了新的 Linux 可执行文件。

## 4. 端口修改后重新打包

用户修改了端口号，希望重新打包。

确认结果：

- `main.go` 中端口已修改为 `8883`
- 已基于最新源码重新编译 Linux 可执行文件

## 5. 压测脚本需求

之后用户提出将 Go 脚本改造成性能测试脚本，要求如下：

1. 配置文件中可调整 `qps`
2. 增加 9 个 topic，与原 topic 共 10 个 topic
3. 每个 topic 的发送信息一一对应，来源于 `message.json`
4. 需要记录 MQTT 订阅断开信息，便于排查

在实现前，针对未确定事项进行了确认：

### 确认项

1. `qps` 为总 QPS，由 10 个 topic 平摊
2. 压测只需要按时长运行，单位为秒，不需要总消息数
3. `message.json` 第 3 个 topic 前存在多余空格，需要自动去掉

用户确认以上三点后开始改造代码。

## 6. 压测版程序实现

当前 Go 程序已经改造成 MQTT 压测程序，具备以下能力：

- 从配置文件 `mqtt_perf_config.json` 读取运行参数
- 从 `message.json` 读取 10 组 `topic + payload`
- 按 `total_qps` 进行总速率控制
- 通过轮询方式将总 QPS 平摊到 10 个 topic
- 根据 `duration_seconds` 控制压测持续时长
- 自动订阅 `sub_topics`
- 记录连接、断连、重连、订阅失败、每秒统计信息
- 日志同时输出到终端和日志文件

相关文件：

- `main.go`
- `mqtt_perf_config.json`
- `message.json`

## 7. 配置文件说明

用户询问了 `mqtt_perf_config.json` 的含义。

已解释的关键字段包括：

- `broker`：MQTT broker 地址
- `port`：MQTT TLS 端口
- `username` / `password`：认证信息
- `client_id`：客户端 ID
- `cert_file` / `key_file`：客户端证书和私钥
- `ca_cert_file`：CA 证书路径
- `insecure_skip_verify`：是否跳过服务端证书校验
- `sub_topics`：订阅主题列表
- `qos`：MQTT 协议层的 QoS，不是 JSON payload 内容
- `total_qps`：10 个 topic 合计总发送速率
- `duration_seconds`：压测时长（秒）
- `log_file`：日志文件路径
- `message_file`：消息定义文件路径
- `stats_interval_seconds`：统计日志输出间隔
- `connect_timeout_seconds`：连接超时时间
- `reconnect_interval_seconds`：断连重连间隔

## 8. 关于 `qos` 的说明

用户进一步确认：

### 问题 1

```json
"qos": 1,
"total_qps": 10
```

是否与“10 个 topic 都要发送”冲突。

结论：

- 不冲突
- `total_qps = 10` 表示 10 个 topic 合计每秒发送 10 条
- 当前程序会轮询 10 个 topic，因此 10 个都会被使用
- 长期平均下来，大致相当于每个 topic 每秒 1 条

### 问题 2

用户询问 `qos` 是否指 JSON 数据本身。

结论：

- 不是
- `qos` 是 MQTT 协议层的消息服务质量等级
- `payload` 才是 `message.json` 中的 JSON 数据内容

即：

- `topic`：发到哪个主题
- `qos`：按什么级别投递
- `payload`：发什么数据

## 9. 当前产物

当前目录中的核心文件包括：

- `main.go`
- `go.mod`
- `go.sum`
- `message.json`
- `mqtt_perf_config.json`
- `server.pem`
- `server.key`
- `dist/mqtt_test_linux_amd64`

## 10. 当前程序运行方式

Linux 上运行方式：

```bash
chmod +x mqtt_test_linux_amd64
./mqtt_test_linux_amd64
```

或者显式指定配置文件：

```bash
./mqtt_test_linux_amd64 ./mqtt_perf_config.json
```

## 11. 后续可继续扩展的方向

后续如果需要，可以继续做这些增强：

- 将 topic 调度改为“每秒内更严格均匀分配”
- 支持命令行覆盖配置文件参数
- 增加更详细的压测统计
- 按大小或日期切分日志
- 增加订阅消息内容采样输出

