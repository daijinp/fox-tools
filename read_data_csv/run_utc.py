"""
2025年12月3日 14:29:00
批量处理CSV文件工具

功能说明:
1. 读取 read_data_csv/input_utc 目录中的所有CSV文件
2. 处理 create_time: 将毫秒级时间戳转成 2025-12-03 04:06:47 UTC 格式(0区),列名为 time
3. 处理 content: 解析JSON并提取 $.endpoint 内容,列名为 endpoint
4. 处理 content: 解析JSON并提取 $.argument 内容,列名为 argument
   - 如果 argument 包含 formart_json.get_keys() 返回的所有标准key，则按标准顺序排序
   - 如果不包含所有标准key，则保持原样
5. 按照 create_time 从小到大排序
6. 输出结果到 read_data_csv/output_utc 目录,文件名格式: 原文件名_processed.csv
"""

import pandas as pd
import json
import os
import glob
from datetime import datetime, timezone
from formart_json import sort_dict_by_keys

# 定义UTC时区
UTC = timezone.utc

# 定义输入输出目录
input_dir = 'input_utc'
output_dir = 'output_utc'

# 确保输出目录存在
os.makedirs(output_dir, exist_ok=True)

# 获取input_utc目录下的所有CSV文件
csv_files = glob.glob(os.path.join(input_dir, '*.csv'))

if not csv_files:
    print(f"在 {input_dir} 目录中没有找到CSV文件！")
    exit(1)

print(f"找到 {len(csv_files)} 个CSV文件待处理\n")

# 处理 create_time: 将毫秒级时间戳转换为UTC格式化字符串
def convert_timestamp_utc(timestamp_ms):
    """将毫秒级时间戳转换为 '2025-12-03 04:06:47 UTC' 格式"""
    try:
        # 将毫秒转换为秒
        timestamp_sec = timestamp_ms / 1000
        # 创建UTC时区的datetime对象
        dt = datetime.fromtimestamp(timestamp_sec, tz=UTC)
        # 格式化为指定格式
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception as e:
        print(f"时间戳转换错误: {timestamp_ms}, 错误: {e}")
        return None

# 处理 content: 解析JSON并提取argument字段
def extract_argument(content_str):
    """从JSON字符串中提取argument字段，并根据标准key进行排序"""
    try:
        # 解析JSON字符串
        content_dict = json.loads(content_str)
        # 提取argument字段，如果不存在返回空字典
        argument = content_dict.get('argument', {})
        
        # 使用 sort_dict_by_keys 对字典进行排序处理
        # 如果包含所有标准key，则按标准顺序排序；否则原样返回
        sorted_argument = sort_dict_by_keys(argument)
        
        # 将argument转换为JSON字符串便于在CSV中查看
        return json.dumps(sorted_argument, ensure_ascii=False)
    except TypeError as e:
        # sort_dict_by_keys 可能抛出 TypeError
        print(f"排序错误: {e}")
        return None
    except Exception as e:
        print(f"JSON解析错误: {e}")
        return None

# 处理 content: 解析JSON并提取endpoint字段
def extract_endpoint(content_str):
    """从JSON字符串中提取endpoint字段"""
    try:
        # 解析JSON字符串
        content_dict = json.loads(content_str)
        # 提取endpoint字段，如果不存在返回None
        return content_dict.get('endpoint', None)
    except Exception as e:
        print(f"JSON解析错误: {e}")
        return None

# 处理每个CSV文件
for idx, input_file in enumerate(csv_files, 1):
    try:
        # 获取文件名（不含路径）
        filename = os.path.basename(input_file)
        print(f"[{idx}/{len(csv_files)}] 正在处理文件: {filename}")
        
        # 读取CSV文件
        df = pd.read_csv(input_file)
        print(f"    共读取 {len(df)} 条记录")
        
        # 应用转换
        print("    正在处理 create_time 字段...")
        df['time'] = df['create_time'].apply(convert_timestamp_utc)
        
        print("    正在处理 content 字段并提取 endpoint...")
        df['endpoint'] = df['content'].apply(extract_endpoint)
        
        print("    正在处理 content 字段并提取 argument...")
        df['argument'] = df['content'].apply(extract_argument)
        
        # 选择需要的列
        output_columns = ['create_time', 'time', 'endpoint', 'argument']
        df_output = df[output_columns]
        
        # 按照 create_time 从小到大排序
        print("    正在按 create_time 排序...")
        df_output = df_output.sort_values(by='create_time', ascending=True).reset_index(drop=True)
        
        # 生成输出文件路径
        output_filename = filename.replace('.csv', '_processed.csv')
        output_file = os.path.join(output_dir, output_filename)
        
        # 保存到新CSV
        print(f"    正在保存到文件: {output_filename}")
        df_output.to_csv(output_file, index=False, encoding='utf-8-sig')
        
        print(f"    ✓ 处理完成! 共处理 {len(df_output)} 条记录\n")
        
    except Exception as e:
        print(f"    ✗ 处理文件 {filename} 时出错: {e}\n")
        continue

print("=" * 60)
print(f"所有文件处理完成! 结果已保存到 {output_dir} 目录")


