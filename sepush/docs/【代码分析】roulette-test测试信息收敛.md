# roulette-test 代码分析：测试信息收敛

> 分析目录：`sepush/rice-code/roulette-test`  
> 分析日期：2026-08-20  
> 分析目的：只提取黑盒和端到端测试需要的信息，不进行全面代码评审。

## 1. 结论

当前代码足以确定后端主链路、数据库表字段、Job Handler、请求重试、计划转换和强充恢复规则。无需再向开发零散询问这些内容。

当前仓库只有 `roulette` 后端，没有 rice/APP/Web 前端代码，因此页面入口、展示规则和用户实际看到的结果仍需通过测试环境界面确认。

## 2. 已确认的外部 API

| 用途 | 实际地址 |
| --- | --- |
| 国家停电状态 | `GET https://developer.sepush.co.za/business/3.0/status` |
| 停电时刻表 | `GET https://developer.sepush.co.za/business/3.0/schedule?id=<scheduleId>` |

- 请求头使用 `token`；
- 业务代码通过 `ESKOM_TOKEN` 注入 Token；
- `/status` 和 `/schedule` 地址当前写在 `EskomUrls` 常量中；
- 测试构造器可传 URL，但实际 Job 使用默认常量，因此测试环境暂时不能只靠环境变量切到外部 Mock。

## 3. 已确认的 Job

| Handler | 用途 |
| --- | --- |
| `eskominfoJob` | 查询 `plants.block_id`、转换 Schedule ID、请求 `/schedule`、转换并写入计划表 |
| `eskomPushMessageJob` | 请求 `/status`，执行推送和强充逻辑 |
| `eskomPushMessageTestJob` | 接受 Job 参数中的 status JSON，仅针对配置的测试 plant 执行消息链路 |
| `addregion` | 已硬禁用，执行时直接拒绝 |
| `removeDeviceController` | 强充结束后按 plant ID 恢复设备原设置；由程序动态创建一次性 XXL-JOB |
| `removeSingleDeviceController` | 按 device SN 恢复单台设备设置 |

测试环境 XXL-JOB 管理台已确认：

| Job ID | Handler | cron | 当前状态 | 本次用途 |
| --- | --- | --- | --- | --- |
| 532 | `eskominfoJob` | `0 0 10 */2 * ?` | STOP | 真实计划拉取；本次不手工调用 |
| 531 | `eskomPushMessageJob` | `0 0 * * * ?` | STOP | 真实状态推送；本次不手工调用 |
| 475 | `eskomPushMessageTestJob` | `0 0 0/1 * * ?` | STOP | 本次唯一手工调用的 Mock 任务 |

按 Quartz 语义，532 是每隔两天的 10:00，531 和 475 都是每小时整点；具体时区取决于 XXL-JOB 服务配置。截图中三个任务均为 STOP，因此当前不会自动执行，本次保持 STOP 并只手工触发 475。

测试 profile 的默认 XXL-JOB 信息：

- 执行器：`xxl-job-executor-sample`；
- 执行器端口：8030；
- 日志保留：30 天；
- 动态恢复任务使用执行器组 ID 1；
- 动态任务策略为 FIRST 路由、串行执行、失败重试 0 次。

## 4. 已确认的数据库表和字段

### 4.1 电站

| 表 | 关键字段 | 用途 |
| --- | --- | --- |
| `plants` | `id` | 自增主键 |
| `plants` | `plant_id` | 业务电站 ID |
| `plants` | `block_id` | 历史 v2 区域 ID；Mapper 直接查询，实体类未声明该字段 |
| `plants` | `country` | 入库任务只选 `ZA` |
| `plants` | `iana_timezone` | 推送、强充和恢复使用的电站时区 |

### 4.2 停电计划

| 表 | 字段 | 用途 |
| --- | --- | --- |
| `ns_power_outage_info` | `id` | 自增主键 |
|  | `block_id` | 保留原历史 ID，用于与 `plants` 关联 |
|  | `stages` | 固定 8 级二维 JSON 数组 |
|  | `day` | `yyyy-MM-dd` 日期 |
|  | `stage` | 已推送/处理的 Stage 标记 |

代码实体没有更新时间字段，需要更新时间验收时应查询实际表结构。

### 4.3 用户设置和推送资格

| 表 | 用途 |
| --- | --- |
| `ns_power_outage_settings` | 用户/电站停电储备、提前充电时分、首次状态和推送开关 |
| `msg_push_device` | 推送设备及 registration ID |
| `msg_push_relations` | 用户的消息订阅关系和开关 |
| `msg_push_type` | 消息类型代码 |

`ns_power_outage_settings` 的关键字段包括 `plant_id`、`user_id`、`power_outage_reserve`、`preChargeH`、`preChargeM`、`first_time`、`pushFlag`。

### 4.4 强充记录

| 表 | 字段 | 用途 |
| --- | --- | --- |
| `forcecharge_device_log` | `device_sn` | 设备 SN |
|  | `end_time` | 强充结束的毫秒时间戳 |
|  | `stages` | 实际应用的时段 JSON |
|  | `state` | `false` 表示尚未恢复，恢复后更新为 `true` |

强充设备设置通过 MicroBus 下发，并通过 Redis 保存原设置；数据库表记录执行状态，不是设备指令队列表。

## 5. 已确认的计划同步规则

- 从 `plants` 查询 `country='ZA'` 且非空的 distinct `block_id`；
- v2 ID 取前两个短横线段，例如 `eskde-10-fourways` → `eskde-10`；
- 空值、`null`、无短横线、含下划线、含空白或空段 ID 被拒绝；
- 相同 Schedule ID 每轮只请求一次，再回填所有原 `block_id`；
- 单轮最多 1000 个 distinct Schedule；
- 某 block 已存在的未来计划日期超过 3 天时，本轮整体跳过该 block；
- 已存在的日期不重复生成入库行；
- 输出固定 8 个 Stage，每一级包含之前所有等级的并集；
- 时段格式为 `HH:mm-HH:mm`；
- 允许结束时间位于次日，不允许跨两天；
- 日期、时间戳、Stage 名称或范围非法时拒绝整个 Schedule 的转换结果。

## 6. 已确认的异常策略

| 场景 | 当前代码行为 |
| --- | --- |
| 400/404 | 作为 Schedule 不存在，跳过该 Schedule，不重试 |
| 401/403 | 终止本轮计划同步或通知链路 |
| 429 | 记录 `x-ratelimit-reset` 并终止本轮，不自动等待或创建恢复任务 |
| 5xx | 最多额外重试 2 次，总请求最多 3 次；失败后跳过该 Schedule |
| IOException | 最多额外重试 2 次；失败后跳过该 Schedule |
| 3xx、408、422 等 | 不重试 |
| 200 空 body、非 JSON、无 schedule days | 视为无效响应并跳过 |
| 单 Schedule 转换失败 | 跳过失败项，其他成功结果仍可批量入库 |

## 7. 已确认的强充和恢复规则

- 只有 `ns_power_outage_settings.power_outage_reserve=1` 的南非电站进入强充范围；
- 每个电站可以配置 `preChargeH/preChargeM`；空值时默认提前 3 小时 0 分钟；
- 设备工作模式设置为 `ForceCharge`；代码没有设置固定目标 SOC；
- 原 `segmented_time_mode1` 和 `segmented_time_enable` 设置保存在 Redis；
- 强充结束时动态创建 `removeDeviceController` XXL-JOB；
- 恢复任务参数是 plant ID；
- 恢复任务把设备的分段时间和总开关还原为 Redis 备份，并把强充记录 `state` 更新为 `true`；
- 跨午夜时恢复 Job 安排到电站时区的次日结束时间。

## 8. 已确认的通知去重

- 推送需要用户系统消息开关开启并且电站存在电池；
- 当日同一推送对象、同一 Stage 不重复推送；
- Stage 发生变化时允许再次推送；
- 去重状态保存在进程内 `MESSAGE_MAP`，不是数据库持久化状态；应用重启后的重复通知风险需要通过黑盒回归确认。

## 9. 自动化测试情况

代码中共有 58 个带 `@Test` 的测试方法，重点覆盖：

- ID 转换；
- Stage 1～8 累加、去重、跨天和非法数据；
- Schedule 请求去重、回填、已有日期和单轮上限；
- HTTP 状态、重试、限流头、空/非法响应和 Token 请求头；
- status 解析；
- `addregion` 双入口禁用；
- 无计划时不推送、不强充。

本地执行 `mvnw.cmd test` 未进入测试阶段，原因是缺少公司私有 Maven 依赖：`foxess:util`、`foxess:pushcenter`、`foxess:microbus`。需要公司 Maven settings/私服后才能得到真实通过结果。

## 10. 当前测试执行范围和剩余输入

开发已确认本次在测试环境只手工调用 `eskomPushMessageTestJob`，不调用 532/531，也不搭建外部 Mock Server。该任务从 XXL-JOB 参数读取 status JSON，并结合数据库已有停电计划执行 APP 通知、强充和恢复链路。

当前只剩以下输入：

1. PM 提供的专用测试账号、电站和设备；
2. `plant_id`、`plant_sn`、`block_id`、`device_id`；
3. APP 登录和通知观察入口；
4. 确认部署环境的 `eskom.plantId` 指向该专用测试电站。这个目标由服务配置决定，不是 XXL-JOB 参数决定。

XXL-JOB 管理台、Admin 权限、任务 ID、任务状态和数据库读取能力均已具备，不再需要开发补充。

## 11. 需要开发处理的两项差异和一项安全问题

1. 技术文档写“429 等待 reset 后再重试”，但代码只记录 reset 并终止本轮，没有自动等待或恢复逻辑；需要确认由下一次 cron 自然恢复是否符合要求。
2. 技术文档写“批量 404 立即止损”，代码没有批量阈值和自动停 Job，当前行为是逐个跳过后继续；需要给出明确阈值和执行方式。
3. `application.yml` 为 Eskom Token 设置了硬编码默认值。该值不应继续留在仓库；建议删除默认值、改为环境变量必填，并更换现有 Token。本文和 `.env.example` 均未复制该凭证。

## 12. 测试环境数据库实测（2026-08-20）

使用现有 SSH/MySQL 配置在只读事务中检查 `ns_power_outage_info`：

- 数据库当前日期为 `2026-08-20`；
- 表字段为 `id, block_id, stages, day, stage`，没有创建/更新时间字段，因此只能确认计划日期，不能证明具体更新时间；
- 总记录 126267，distinct `block_id` 1895，计划日期范围 `2025-06-02`～`2026-08-25`；
- `2026-08-20`～`2026-08-25` 每天均有 269 条、对应 269 个 distinct `block_id`；
- 近期记录的日期、`block_id`、JSON 都合法，`stages` 均为固定 8 级；
- 当天 269 条记录的 Stage 2 和 Stage 3 均有非空时段，当前 `stage` 标记均为 0；
- 全历史存在 224 条非 8 级的旧记录，但近期窗口为 0，不影响本轮测试。

结论：公共近期数据可以支撑 Mock Job；拿到专用设备的 `block_id` 后，还需再做一次单电站核查，确认该 `block_id` 在测试当天有计划且与 `plant_id` 关联正确。

专用设备信息补齐后的首次核查结果：

- `block_id` 本身有效：在当前库的 `plants`、`ns_region` 和历史计划表中均存在；
- 该 `block_id` 在数据库当天没有计划；
- 配置的 `plant_id` 在当前库的 `plants.plant_id` 和主键 `plants.id` 中均不存在；
- 配置的 `device_id` 在当前库的 `devices.device_id` 和 `devices.device_sn` 中均不存在。

首次结果由数据库环境不一致导致。切换到 `sepush/config/config.json` 后复核：

- `plant_id`、`device_id` 和 `block_id` 均存在且相互关联；
- 测试当天到未来三天都有该 `block_id` 的 8 级计划，Stage 2/3 均非空；
- 电站时区、设备归属和 4 条电池记录正常；
- `power_outage_reserve=1`，强充已开启，提前时间为 5 小时 30 分钟；
- 当前电站不满足代码的 `plants.country='ZA'` 过滤；
- `pushFlag` 未开启，活跃 APP 推送关系为 0；
- 测试 `plant_id` 与仓库中的默认 `eskom.plantId` 不同，部署环境必须通过 `ESKOM_PLANTID` 覆盖后才能让 Mock Job 命中该电站。

后三项处理并复核通过前不得触发 Mock Job。

固定只读核查命令：

```powershell
go -C .\get_data_for_mysql run .\cmd\query_sepush -config .\config\config.json
```

开发提供的线上备用 SQL 已保存至 [`南非停电通知3.0只读核查.sql`](../sql/南非停电通知3.0只读核查.sql)，目前无需执行线上全量查询。

## 13. Mock Job 参数代码结论

- 真正参与逻辑的是 `status.eskom.next_stages[].stage` 和 `stage_start_timestamp`；
- `status.eskom.stage`、`stage_updated` 和 `name` 当前不参与判断；
- `status.capetown` 当前也不参与判断，代码两次调用都读取 `status.eskom`；
- 时间戳必须属于测试电站的测试当天，否则不会通知或强充；
- 一次执行中的多个 `next_stages` 会依次立即处理，不会等待时间戳到达；
- 为了结果单一、便于核对，正常用例每次只传一个 Stage，再分次测试 Stage 变化；
- Stage 必须是 1～8，并且所选 Stage 在该电站当天的 `stages` 中有非空时段。
