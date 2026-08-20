-- 南非停电通知 3.0 只读核查 SQL
-- 来源：开发提供。默认仅作为线上问题定位和上线前数据核查备用。
-- 所有语句均为 SELECT；执行前仍应确认当前连接环境。

-- 1. block_id 数量及转换后的 Schedule ID 数量
SELECT
  COUNT(DISTINCT block_id) AS distinct_block_count,
  COUNT(DISTINCT SUBSTRING_INDEX(block_id, '-', 2)) AS distinct_schedule_count
FROM plants
WHERE country = 'ZA'
  AND block_id IS NOT NULL
  AND block_id NOT IN ('', 'null');

-- 2. 每个 Schedule ID 映射的 block_id 和电站数量
SELECT
  SUBSTRING_INDEX(block_id, '-', 2) AS schedule_id,
  COUNT(DISTINCT block_id) AS mapped_block_count,
  COUNT(*) AS plant_count
FROM plants
WHERE country = 'ZA'
  AND block_id IS NOT NULL
  AND block_id NOT IN ('', 'null')
GROUP BY SUBSTRING_INDEX(block_id, '-', 2)
ORDER BY mapped_block_count DESC, schedule_id;

-- 3. 当前所有 distinct block_id
SELECT DISTINCT
  block_id AS block_id_distinct
FROM plants
WHERE country = 'ZA'
  AND block_id IS NOT NULL
  AND block_id NOT IN ('', 'null')
ORDER BY block_id_distinct;

-- 4. ns_region 全量数据（结果可能很大，非必要不执行）
SELECT *
FROM ns_region;

-- 5. ns_region 中 Area ID 和转换后的 Schedule ID 数量
SELECT
  COUNT(DISTINCT SUBSTRING_INDEX(area_id, '-', 2)) AS distinct_schedule_count,
  COUNT(DISTINCT area_id) AS distinct_area_count
FROM ns_region;

-- 6. ns_region 中所有 distinct Schedule ID
SELECT DISTINCT
  SUBSTRING_INDEX(area_id, '-', 2) AS schedule_id
FROM ns_region
ORDER BY schedule_id;

-- 7. 近期停电计划质量检查（测试环境优先使用）
SELECT
  day,
  COUNT(*) AS plan_rows,
  COUNT(DISTINCT block_id) AS distinct_block_count,
  SUM(CASE WHEN JSON_VALID(stages) = 1 AND JSON_LENGTH(stages) = 8 THEN 0 ELSE 1 END) AS invalid_stage_rows
FROM ns_power_outage_info
WHERE STR_TO_DATE(day, '%Y-%m-%d') BETWEEN DATE_SUB(CURDATE(), INTERVAL 3 DAY)
                                          AND DATE_ADD(CURDATE(), INTERVAL 7 DAY)
GROUP BY day
ORDER BY STR_TO_DATE(day, '%Y-%m-%d');
