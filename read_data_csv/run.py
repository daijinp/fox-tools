"""
2025年12月3日 14:29:00
读取CSV文件--foxess_user_log60HG502057KA138.csv
1.读取 create_time,写入新CSV
2.create_time为毫秒级时间戳,转成2025-12-03 04:06:47 CST+0800格式,写入新CSV,第一行命名为create_time_str
3.读取 content, 将内容转成dict,获取到 $.argument内容 ,写入新CSV,第一行命名为 content_new
"""

import pandas as pd
import json
from datetime import datetime, timezone, timedelta

# 定义CST时区 (UTC+8)
CST = timezone(timedelta(hours=8))

# 读取CSV文件
input_file = 'foxess_user_log60HG502057KA138.csv'
output_file = 'foxess_user_log60HG502057KA138_processed.csv'

print(f"正在读取文件: {input_file}")
df = pd.read_csv(input_file)

print(f"共读取 {len(df)} 条记录")

# 处理 create_time: 将毫秒级时间戳转换为格式化字符串
def convert_timestamp(timestamp_ms):
    """将毫秒级时间戳转换为 '2025-12-03 04:06:47 CST+0800' 格式"""
    try:
        # 将毫秒转换为秒
        timestamp_sec = timestamp_ms / 1000
        # 创建带时区的datetime对象
        dt = datetime.fromtimestamp(timestamp_sec, tz=CST)
        # 格式化为指定格式
        return dt.strftime('%Y-%m-%d %H:%M:%S CST+0800')
    except Exception as e:
        print(f"时间戳转换错误: {timestamp_ms}, 错误: {e}")
        return None

# 处理 content: 解析JSON并提取argument字段
def extract_argument(content_str):
    """从JSON字符串中提取argument字段"""
    try:
        # 解析JSON字符串
        content_dict = json.loads(content_str)
        # 提取argument字段，如果不存在返回空字典
        argument = content_dict.get('argument', {})
        # 将argument转换为JSON字符串便于在CSV中查看
        return json.dumps(argument, ensure_ascii=False)
    except Exception as e:
        print(f"JSON解析错误: {e}")
        return None

# 应用转换
print("正在处理 create_time 字段...")
df['create_time_str'] = df['create_time'].apply(convert_timestamp)

print("正在处理 content 字段并提取 argument...")
df['content_new'] = df['content'].apply(extract_argument)

# 选择需要的列并保存到新CSV
output_columns = ['create_time', 'create_time_str', 'content_new']
df_output = df[output_columns]

print(f"正在保存到文件: {output_file}")
df_output.to_csv(output_file, index=False, encoding='utf-8-sig')

print(f"处理完成! 共处理 {len(df_output)} 条记录")
print(f"输出文件: {output_file}")

# 显示前5条记录作为预览
print("\n前5条记录预览:")
print(df_output.head().to_string())