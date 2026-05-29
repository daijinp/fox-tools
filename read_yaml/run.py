"""
读取每个协议的setting文件,找到所有alias为以下keys的值,然后根据alias的值找到对应的version,然后写入csv文件

ExportLimit,MinSoc,MinSocOnGrid,MaxSoc,GridCode,WorkMode,ActivePowerLimit,ExportLimitPower,EpsOutPut,ECOMode
"""
import yaml
import os
import csv
import re
from pathlib import Path
from collections import defaultdict

# setting_keys = ['export_limit', 
#              'min_soc', 
#              'min_soc_on_grid', 
#              'max_soc', 
#              'grid_code', 
#              'work_mode', 
#              'active_power_limit', 
#              'export_limit_power', 
#              'eps_output', 
#              'eco_mode']
setting_keys = ['export_limit',
                'minsoc',
                'minsoc_ongrid',
                'grid_code',
                'work_mode',
                'active_power_limit',
                'export_limit_power',
                'eps_output',
                'eco_mode'
                ]

setting_keys_read_name = ['MaximumSoC']

def find_all_aliases(data, aliases=None):
    """
    递归查找字典中所有的 alias 值
    """
    if aliases is None:
        aliases = []
    
    if isinstance(data, dict):
        if 'alias' in data:
            aliases.append(data['alias'])
        for value in data.values():
            find_all_aliases(value, aliases)
    elif isinstance(data, list):
        for item in data:
            find_all_aliases(item, aliases)
    
    return aliases


def find_all_names(data, names=None):
    """
    递归查找字典中所有的 name 值（包括 en 和 zh_CN）
    """
    if names is None:
        names = []
    
    if isinstance(data, dict):
        if 'name' in data:
            name_dict = data['name']
            if isinstance(name_dict, dict):
                # 获取 en 和 zh_CN 的值
                if 'en' in name_dict:
                    names.append(name_dict['en'])
                if 'zh_CN' in name_dict:
                    names.append(name_dict['zh_CN'])
        for value in data.values():
            find_all_names(value, names)
    elif isinstance(data, list):
        for item in data:
            find_all_names(item, names)
    
    return names


def main():
    # 读取setting文件夹下的所有yml文件,转成dict
    setting_dir = Path(__file__).parent / 'setting'
    # 使用字典存储每个 setting_key 对应的所有 version
    setting_versions = {key: [] for key in setting_keys}
    # 使用字典存储每个 setting_keys_read_name 对应的所有 version
    setting_versions_by_name = {key: [] for key in setting_keys_read_name}
    
    # 遍历所有 yml 文件
    for yml_file in setting_dir.glob('*.yml'):
        try:
            with open(yml_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data:
                continue
            
            # 获取 version
            version = data.get('version', '')
            
            # 查找所有 alias 值
            aliases = find_all_aliases(data)
            
            # 查找所有 name 值
            names = find_all_names(data)
            
            # 遍历setting_keys得到 setting_key, 然后查找dict中的所有alias的值
            # 如果这个setting_key存在于这个dict中,则获取这个dict的version值
            for setting_key in setting_keys:
                if setting_key in aliases:
                    if version not in setting_versions[setting_key]:
                        setting_versions[setting_key].append(version)
            
            # 遍历setting_keys_read_name得到 setting_key, 然后查找dict中的所有name的值
            # 如果这个setting_key存在于这个dict中,则获取这个dict的version值
            for setting_key in setting_keys_read_name:
                if setting_key in names:
                    if version not in setting_versions_by_name[setting_key]:
                        setting_versions_by_name[setting_key].append(version)
        
        except Exception as e:
            print(f"Error processing {yml_file}: {e}")
            continue
    
    # 最后将结果写在csv中,格式为: setting_key,versions
    output_file = Path(__file__).parent / 'results.csv'
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['setting_key', 'versions'])
        # 按 setting_keys 的顺序写入，每个 setting_key 一行，versions 用逗号分隔
        for setting_key in setting_keys:
            versions_str = ','.join(sorted(setting_versions[setting_key]))
            writer.writerow([setting_key, versions_str])
        # 按 setting_keys_read_name 的顺序写入
        for setting_key in setting_keys_read_name:
            versions_str = ','.join(sorted(setting_versions_by_name[setting_key]))
            writer.writerow([setting_key, versions_str])
    
    print(f"结果已保存到 {output_file}")
    total_count = sum(len(versions) for versions in setting_versions.values())
    total_count += sum(len(versions) for versions in setting_versions_by_name.values())
    print(f"共找到 {total_count} 条记录")


def extract_version_prefix(version_str):
    """
    从版本号中提取前缀（去掉最后3个字符，并将前面的英文字母大写）
    例如: 'r105xx' -> 'R105', 'zaa100xx' -> 'ZAA100'
    """
    if len(version_str) <= 3:
        return version_str.upper()
    # 去掉最后3个字符
    prefix = version_str[:-3]
    # 将前面的英文字母大写
    return prefix.upper()


def extract_series_and_number(version_str):
    """
    从版本号中提取系列和数字部分
    例如: 'R105xx' -> ('R', 105), 'ZAA100xx' -> ('ZAA', 100)
    """
    # 去掉最后3个字符（xx）
    prefix = version_str[:-3] if len(version_str) > 3 else version_str
    
    # 使用正则表达式分离字母和数字
    match = re.match(r'^([A-Za-z]+)(\d+)$', prefix.upper())
    if match:
        series = match.group(1)
        number = int(match.group(2))
        return series, number
    return None, None


def process_csv_comparison():
    """
    处理 CSV 文件比对
    1. 统计 setting 文件夹下 .yml 文件的数量
    2. 读取 results.csv 中的 version，进行比对
    3. 将结果写入新的 CSV
    """
    setting_dir = Path(__file__).parent / 'setting'
    results_file = Path(__file__).parent / 'results.csv'
    output_file = Path(__file__).parent / 'comparison_results.csv'
    
    # 1. 统计 setting 文件夹下所有 .yml 文件的数量
    total_file_count = len(list(setting_dir.glob('*.yml')))
    print(f"setting 文件夹下共有 {total_file_count} 个 .yml 文件")
    
    # 2. 读取 results.csv 文件
    csv_results = []
    with open(results_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            setting_key = row['setting_key']
            versions_str = row['versions']
            # 解析版本号列表
            versions = [v.strip() for v in versions_str.split(',') if v.strip()]
            csv_results.append({
                'setting_key': setting_key,
                'versions': versions
            })
    
    # 3. 比对每个 setting_key 的版本号
    comparison_results = []
    
    for item in csv_results:
        setting_key = item['setting_key']
        csv_versions = item['versions']  # 完整的版本号列表，如 ['R105xx', 'R106xx', ...]
        
        # 提取版本号的前缀（去重）用于数量比对
        csv_version_prefixes = set()
        for version in csv_versions:
            prefix = extract_version_prefix(version)
            csv_version_prefixes.add(prefix)
        
        csv_version_count = len(csv_version_prefixes)
        
        # 比对数量
        if csv_version_count == total_file_count:
            # 数量一致，结果为 "all"
            result = "all"
        elif csv_version_count < total_file_count:
            # 数量少于全部文件数量，找出每个系列的最低版本号（使用完整版本号）
            result = find_min_versions_by_series(csv_versions)
        else:
            # 数量多于全部文件数量（理论上不应该发生）
            result = "all"
        
        comparison_results.append({
            'setting_key': setting_key,
            'result': result
        })
    
    # 4. 将结果写入新的 CSV
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['setting_key', 'result'])
        for item in comparison_results:
            writer.writerow([item['setting_key'], item['result']])
    
    print(f"比对结果已保存到 {output_file}")


def find_min_versions_by_series(csv_versions):
    """
    从 CSV 版本号中找出每个系列的最低版本号（完整版本号）
    参数: csv_versions - 完整版本号列表，如 ['R105xx', 'R106xx', 'A100xx', ...]
    返回: 每个系列的最低完整版本号，逗号分隔
    """
    # 按系列分组，找出每个系列的最低版本号
    series_versions = defaultdict(list)
    for version in csv_versions:
        # version 是完整版本号（如 'R105xx'），提取系列和数字部分
        series, number = extract_series_and_number(version)
        if series and number is not None:
            series_versions[series].append((version, number))
    
    # 找出每个系列的最低版本号（按数字部分比对）
    min_versions = []
    for series in sorted(series_versions.keys()):
        versions = series_versions[series]
        # 按数字部分排序，取最低版本（完整版本号）
        versions.sort(key=lambda x: x[1])
        min_versions.append(versions[0][0])  # 返回完整版本号
    
    # 返回最低版本号列表（逗号分隔，完整版本号）
    if min_versions:
        return ','.join(min_versions)
    else:
        return "all"


if __name__ == '__main__':
    # 运行原有的 main 函数
    # main()
    
    # 运行 CSV 比对处理
    # print("\n开始处理 CSV 比对...")
    # process_csv_comparison()

    # 读取setting中的文件名
    setting_dir = Path(__file__).parent / 'setting'
    for file in setting_dir.glob('*.yml'):
        # 不换行
        print(file.name[:-4], end=' ')
