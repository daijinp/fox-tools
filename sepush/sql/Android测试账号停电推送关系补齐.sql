-- Android 测试账号：补齐南非停电通知订阅关系
-- 适用环境：测试环境 foxess 库
-- 本脚本不新增 msg_push_device；它使用 APP 已自动登记的 Android registration_id。
-- 执行前将 <TEST_PLANT_ID> 替换为本地 .env 中的 TEST_PLANT_ID。

START TRANSACTION;

SET @test_plant_id = '<TEST_PLANT_ID>';

-- 1. 从测试电站取得所属用户。
SET @test_user_id = (
  SELECT p.related_userid
  FROM plants p
  WHERE p.plant_id = @test_plant_id
  LIMIT 1
);

-- 2. 使用该用户最近更新且启用的 Android 推送 registration_id。
SET @test_registration_id = (
  SELECT d.registration_id
  FROM msg_push_device d
  WHERE d.user_id = @test_user_id
    AND d.category = 'android'
    AND d.state = 1
  ORDER BY d.updated_date DESC, d.id DESC
  LIMIT 1
);

-- 3. 写入代码实际使用的两种停电消息关系。
-- 唯一索引为 registration_id + user_id + msg_type_id，重复执行不会生成重复数据。
INSERT INTO msg_push_relations (
  registration_id,
  user_id,
  msg_type_id,
  state
)
SELECT
  @test_registration_id,
  @test_user_id,
  t.msg_type_id,
  1
FROM msg_push_type t
WHERE t.msg_type_code IN (
    'power_outage_reserve_notification',
    'power_outage_reserve_alert'
  )
  AND t.state = 1
  AND @test_user_id IS NOT NULL
  AND @test_registration_id IS NOT NULL
ON DUPLICATE KEY UPDATE state = 1;

-- 4. 提交前核对：应返回 2 行，relation_state/device_state/type_state 均为 1。
SELECT
  t.msg_type_code,
  r.state AS relation_state,
  d.state AS device_state,
  t.state AS type_state,
  d.category
FROM msg_push_relations r
INNER JOIN msg_push_type t
        ON t.msg_type_id = r.msg_type_id
INNER JOIN msg_push_device d
        ON d.registration_id = r.registration_id
       AND d.user_id = r.user_id
WHERE r.user_id = @test_user_id
  AND r.registration_id = @test_registration_id
  AND t.msg_type_code IN (
    'power_outage_reserve_notification',
    'power_outage_reserve_alert'
  )
ORDER BY t.msg_type_code;

-- 确认上面的查询结果正确后执行 COMMIT；否则执行 ROLLBACK。
-- 不要同时执行下面两条。
-- COMMIT;
-- ROLLBACK;
