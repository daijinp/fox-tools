# 流向图速率设置报文分析（`report_interval_sec = 60`）

## 1. 结论

代码确实会在一次 `Hub.SetPostRate` 调用中先设置采集器，再设置逆变器。

当设备协议首字节为 **`P`、`Q`、`K` 或 `I`** 时，正好对应“采集器一条、逆变器一条”：

| 目标 | Hub 功能码 | 原始 payload（十六进制） | Base64 `data` |
|---|---:|---|---|
| 采集器 | `0x3B` | `0F 00 3C 00 00` | `DwA8AAA=` |
| 逆变器 | `0x12` | `01 06 9C 4E 00 3C` | `AQacTgA8` |

因此，用“功能码 + 解码后的 payload”简写，两条核心报文是：

```text
发给采集器：0x3B | 0F 00 3C 00 00
发给逆变器：0x12 | 01 06 9C 4E 00 3C
```

需要注意：`0x3B`、`0x12` 在代码中是 Hub 请求的独立 `code` 字段，**不包含在 Base64 的 `data` 字段内**。以上用 `|` 拼接只是为了直观展示，不代表代码真的把功能码拼进 payload。

## 2. 输入值如何转换

本文按用户给出的 60 秒分析。源码中的配置字段实际拼写为 `report_interval_sec`；60 转成 `uint16` 大端字节是：

```text
60(十进制) = 0x003C = 00 3C
```

业务层固定将持续时间设为 `0`，表示永久生效：

```go
ReportDurationSec: 0
```

因此持续时间的 `uint16` 大端字节为 `00 00`；在 A/B 协议的 `uint32` 字段中则为 `00 00 00 00`。

代码位置：

- `friday/internal/biz/devicereport/usecase.go:284-292`
- `friday/internal/data/devicereport_providers.go:90-100`

## 3. 发给采集器的报文

采集器报文由 `commonProtocolHandler.setDataloggerInterval` 构造，对所有进入 `Hub.SetPostRate` 的协议都先尝试下发。

### 3.1 payload 结构

| 偏移 | 长度 | 值 | 含义 |
|---:|---:|---|---|
| 0 | 1 字节 | `0F` | 固定子命令 |
| 1 | 2 字节 | `00 3C` | 上报间隔，`uint16` 大端，60 秒 |
| 3 | 2 字节 | `00 00` | 持续时间，`uint16` 大端，0 表示永久 |

组合结果：

```text
功能码：0x3B（JSON 十进制为 59）
payload：0F 00 3C 00 00
Base64：DwA8AAA=
```

源码证据：`friday/internal/pkg/hub/protocol_handler.go:53-62`。

## 4. 发给逆变器的报文

逆变器 payload 取决于实时数据中 `protocol` 字符串的首字节，不能脱离协议类型只给出唯一答案。

### 4.1 P/Q/K/I 协议：一条 `0x12` 报文

`P`、`Q`、`K`、`I` 都调用 `setRateWithHybrid`，写寄存器 40014。

```text
40014(十进制) = 0x9C4E = 9C 4E
60(十进制)    = 0x003C = 00 3C
```

payload 结构：

| 偏移 | 长度 | 值 | 含义 |
|---:|---:|---|---|
| 0 | 1 字节 | `01` | 固定设备/从站标识 |
| 1 | 1 字节 | `06` | 内层写单寄存器操作码 |
| 2 | 2 字节 | `9C 4E` | 寄存器地址 40014，大端 |
| 4 | 2 字节 | `00 3C` | 间隔 60 秒，大端 |

组合结果：

```text
功能码：0x12（JSON 十进制为 18）
payload：01 06 9C 4E 00 3C
Base64：AQacTgA8
```

源码证据：

- `friday/internal/pkg/hub/protocol_handler.go:65-75`
- `friday/internal/pkg/hub/protocol_p.go:12-14`
- `friday/internal/pkg/hub/protocol_q.go:13-15`
- `friday/internal/pkg/hub/protocol_k.go:11-13`
- `friday/internal/pkg/hub/protocol_I.go:10-12`

### 4.2 A/B 协议：逆变器实际发两条 `0x12` 报文

A/B 协议并不是只给逆变器发一条。代码先写持续时间寄存器 50653，再写间隔寄存器 50652；第一条失败后不会继续第二条。

第一条，持续时间为 0：

```text
50653(十进制) = 0xC5DD

功能码：0x12
payload：01 10 C5 DD 00 00 00 00
Base64：ARDF3QAAAAA=
```

第二条，间隔为 60 秒：

```text
50652(十进制) = 0xC5DC

功能码：0x12
payload：01 06 C5 DC 00 3C
Base64：AQbF3AA8
```

所以 A/B 协议的一次完整调用共有三次 `SetRaw`：

1. 采集器 `0x3B`；
2. 逆变器持续时间 `0x12`；
3. 逆变器上报间隔 `0x12`。

源码证据：`friday/internal/pkg/hub/protocol_ab.go:14-38`。

### 4.3 D/H 协议：逆变器不发 `0x12`

D/H 协议的采集器部分仍然先发 `0x3B`，但逆变器部分不调用 `SetRaw(0x12, ...)`。它先通过 Hub 获取 `grid_code` 配置，然后把 `grid_code__data_interval` 更新为字符串 `"60"`，再调用 Hub 的 `/setting/set` 接口。

因此，“逆变器功能码固定为 `0x12`”只适用于当前代码中的 A/B/P/Q/K/I，不适用于 D/H。

源码证据：

- `friday/internal/pkg/hub/protocol_d.go:13-15`
- `friday/internal/pkg/hub/protocol_h.go:11-13`
- `friday/internal/pkg/hub/protocol_handler.go:78-93`

### 4.4 其他协议首字节

当前 `GetProtocolHandler` 只支持 A、B、D、H、P、Q、K、I。其他首字节会被判定为不支持。由于采集器 `0x3B` 在协议路由前就已执行，此时可能出现“采集器已经下发，逆变器未下发”的部分成功状态。

这里还存在一处代码与测试不一致：上游 `extractProtocolVersion` 明确允许 S、T、W、Z 四类 8 字节协议字符串，`TestGetProtocolHandler_STWZ_useAB` 也期望这四类协议复用 A/B 处理器；但是当前实际 `GetProtocolHandler` 没有 S/T/W/Z 分支。因此应以当前运行代码为准：S/T/W/Z 会先尝试采集器 `0x3B`，随后返回逆变器协议不支持错误，不会发送 A/B 的两条 `0x12`。测试表达的预期尚未落实到生产路由代码。

源码证据：

- `friday/internal/biz/devicereport/usecase.go:62-95`
- `friday/internal/pkg/hub/protocol_handler.go:28-47`
- `friday/internal/pkg/hub/post_rate.go:18-32`
- `friday/internal/pkg/hub/post_rate_test.go:37-46`

## 5. 实际传输封装

### 5.1 microbus/Kafka 通道

当前依赖装配在 microbus 可用时优先走双向 microbus 消息。以设备序列号 `<SN>` 为占位符，P/Q/K/I 的两条 JSON 消息体分别是：

```json
{"sn":"","devSN":"<SN>","code":59,"data":"DwA8AAA=","timeout":30}
```

```json
{"sn":"","devSN":"<SN>","code":18,"data":"AQacTgA8","timeout":30}
```

发送资源为：

```text
$system/device/setting/bothWaySend/<SN>/c/v0/json
```

这里 JSON 的 `code` 是十进制，所以 59 等于 `0x3B`，18 等于 `0x12`。

源码证据：

- `friday/internal/pkg/hub/microbus_sender.go:79-89`
- `friday/internal/pkg/microbus/microbus.go:56-72`
- `friday/internal/pkg/microbus/microbus.go:100-145`

### 5.2 HTTP 回退通道

microbus 未配置时，代码向 Hub 的 `/setting/raw` 发送以下结构：

```json
{
  "devSN": "<SN>",
  "code": 59,
  "timeout": 10000,
  "data": "DwA8AAA="
}
```

逆变器消息只需把 `code` 改为 18，并按协议替换 `data`。`timeout` 单位是毫秒，默认 10000，但可被运行配置覆盖。

源码证据：`friday/internal/pkg/hub/hub.go:304-365`。

### 5.3 本仓库能够确认的报文边界

本仓库能够确认的是 Hub 请求中的 `code` 和 Base64 payload。Hub 收到请求后是否还会添加设备物理协议头、长度、序列号、校验和等，不在当前文件夹代码中，无法仅凭这里的源码还原。换言之，本文给出的是 friday 服务实际交给 Hub 的完整业务参数，不把仓库外 Hub 可能生成的链路层字节误当成本仓库报文。

另外，采集器和逆变器下发都调用 `SetRaw` 并传入同一个设备 SN；代码通过功能码区分处理目标，没有在这里提供一个独立的采集器 SN。

## 6. “双发”的两个不同含义

### 6.1 一次 Hub 调用内的双目标下发

`Hub.SetPostRate` 的确定顺序是：

```text
采集器 0x3B -> 根据 protocol 选择处理器 -> 逆变器设置
```

采集器下发失败不会阻止受支持协议的逆变器下发，最后用 `errors.Join` 合并两边错误。

### 6.2 general_forward 入口还存在重复触发

`general_forward.ProcessByMap` 对同一条实时消息既调用一次 `SubmitReportInterval`，后面又启动 goroutine 调用一次 `SetReportInterval`。这不是“采集器与逆变器各一份”的双发，而是同一套纠偏流程存在两条并发入口。

如果两条并发任务都基于相同旧状态通过判断，P/Q/K/I 理论上可能实际看到：

```text
0x3B + 0x12 + 0x3B + 0x12
```

也就是整组报文被重复，而不是只有预期的两条。`frank_energy` 入口只调用一次 `SubmitReportInterval`，没有这第二条入口。

源码证据：

- `friday/internal/biz/plugin/general_forward/plugin.go:247`
- `friday/internal/biz/plugin/general_forward/plugin.go:261-269`
- `friday/internal/biz/plugin/frank_energy/plugin.go:207-209`

## 7. 最终判断

若目标设备属于 P/Q/K/I 协议，用户问题的直接答案是：

```text
采集器：code=0x3B，data=DwA8AAA=
          解码后 0F 00 3C 00 00

逆变器：code=0x12，data=AQacTgA8
          解码后 01 06 9C 4E 00 3C
```

若设备属于 A/B 协议，则采集器报文相同，但逆变器会依次发送 `ARDF3QAAAAA=` 和 `AQbF3AA8` 两条 `0x12`。若属于 D/H，则逆变器部分不是 `0x12` 原始报文，而是 Hub 配置接口调用。
