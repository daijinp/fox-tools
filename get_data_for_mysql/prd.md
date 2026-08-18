# sql
## 获取全部数据的SN
SELECT
    d.device_id,
    d.device_sn,
    v.protocol_version,
    v.master_version,
    d.product_type
FROM devices d
    INNER JOIN versions v ON d.device_id = v.model_id
WHERE
    device_sn in ('xxxx') ;


## 从每一个协议版本中获取一个在线device_id 放在get_ui配置文件中
SELECT
    v.protocol_version,
    MIN(d.device_id) AS device_id
FROM devices d
    INNER JOIN versions v ON d.device_id = v.model_id
WHERE
    d.device_sn in ('xxxx') AND d.communication = 0
GROUP BY v.protocol_version;

# 需求
写一个脚本放在datagrip中，使用绝对路径获取E:\work_code\fox-tools\get_data_for_mysql\sns 文件夹下相匹配的数据
以上两个sql替换  ('xxxx')  位置