# eskomPushMessageTestJob 参数说明

## 1. 任务边界

本任务不调用 Eskom API，也不写入停电计划。它把 XXL-JOB 的任务参数解析成 status JSON，再结合 `ns_power_outage_info` 中已有计划执行：

```text
Mock status 参数
→ 读取配置的测试 plant
→ 读取该 plant 当天、对应 Stage 的停电时段
→ APP 通知
→ 设备强充设置
→ 创建结束恢复任务
```

目标电站取自 roulette 服务配置 `eskom.plantId`，不是 JSON 参数。因此执行前必须确认它等于 PM 提供设备所属的 `plant_id`。

## 2. 推荐参数

一次只传一个 Stage。粘贴到 XXL-JOB 前，将所有 `YYYY-MM-DD` 替换成测试电站当地日期：

```json
{
  "status": {
    "eskom": {
      "name": "Eskom",
      "stage": "2",
      "stage_updated": "YYYY-MM-DDT08:00:00+02:00",
      "next_stages": [
        {
          "stage": "2",
          "stage_start_timestamp": "YYYY-MM-DDT20:00:00+02:00"
        }
      ]
    },
    "capetown": {
      "name": "Cape Town",
      "stage": "0",
      "stage_updated": "YYYY-MM-DDT08:00:00+02:00",
      "next_stages": []
    }
  }
}
```

当前代码只使用 `status.eskom.next_stages` 中的 Stage 和日期。时间戳的时分不会让 Job 等待；手工触发后会立即处理，因此测试时间由手工执行时刻决定。

## 3. 不要直接使用旧日期

开发提供的示例日期是 `2026-08-18`。在 `2026-08-20` 或之后直接执行时：

- 通知判断会因事件早于当天零点而跳过；
- 强充判断会因事件日期不是当天而跳过。

必须在每次执行前改成测试当天。

## 4. 建议执行顺序

1. 先用 Stage 2 参数手工执行一次；
2. 查看 XXL-JOB 日志、APP 通知、数据库 `stage` 标记、设备强充设置和动态恢复 Job；
3. 原样再执行一次，验证当日同 Stage 去重；
4. 把参数改成 Stage 3 再执行，验证 Stage 变化后允许再次通知并更新强充计划；
5. 等待或手工核对恢复任务，确认设备恢复到原设置；
6. 任务 475 保持 STOP，不改为自动调度。

如果一次参数同时放 Stage 2 和 Stage 3，代码会在同一次 Job 中连续处理两次，最终状态容易被第二次覆盖，不适合作为第一轮正常用例。
