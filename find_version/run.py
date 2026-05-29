"""这个脚本是将setting_key和protocol_version写入到数据库中"""
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MySQL 数据库操作脚本
"""

import pymysql
import csv
import os
from collections import defaultdict

# ==================== 数据库配置 ====================
# 请填写您的数据库连接信息
DB_CONFIG = {
    'host': '10.3.62.229',  # 数据库IP地址，例如: '127.0.0.1' 或 'localhost'
    'port': 3306,  # 数据库端口，默认3306
    'user': 'root',  # 数据库用户名，例如: 'root'
    'password': 'mysql@foxess',  # 数据库密码
    'database': 'foxess',  # 数据库名称，例如: 'foxess'
    'charset': 'utf8mb4'
}


def read_product_type_csv(csv_file='product_type.csv'):
    """
    读取 product_type.csv 文件，生成一对多的数组
    
    Args:
        csv_file: CSV文件路径
        
    Returns:
        list: 格式为 [{'protocol': '协议版本', 'product_types': ['产品类型1', '产品类型2', ...]}, ...]
    """
    # 获取当前脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, csv_file)
    
    # 使用 defaultdict 来收集一对多的关系
    protocol_dict = defaultdict(list)
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            csv_reader = csv.reader(f)
            for row in csv_reader:
                if len(row) >= 2:
                    protocol = row[0].strip()
                    product_type = row[1].strip()
                    
                    # 只添加非空的数据
                    if protocol and product_type:
                        protocol_dict[protocol].append(product_type)
        
        # 转换为目标数组格式
        result = []
        for protocol, product_types in sorted(protocol_dict.items()):
            result.append({
                'protocol': protocol,
                'product_types': product_types
            })
        
        return result
        
    except FileNotFoundError:
        print(f"错误：找不到文件 {csv_path}")
        return []
    except Exception as e:
        print(f"读取CSV文件时出错: {e}")
        return []


def read_key_protocol_csv(csv_file='key-协议版本.csv'):
    """
    读取 key-协议版本.csv 文件
    
    Args:
        csv_file: CSV文件路径
        
    Returns:
        list: 格式为 [{'setting_key': 'xxx', 'protocol_version': 'xxx'}, ...]
              每个 setting_key 对应的多个 protocol_version 会被拆分为多条记录
    """
    # 获取当前脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, csv_file)
    
    result = []
    
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            csv_reader = csv.reader(f)
            # 跳过标题行
            next(csv_reader, None)
            
            for row in csv_reader:
                if len(row) >= 2:
                    setting_key = row[0].strip()
                    protocol_versions_str = row[1].strip()
                    
                    # 只处理非空的数据
                    if setting_key and protocol_versions_str:
                        # 将逗号分隔的协议版本拆分
                        protocol_versions = [pv.strip() for pv in protocol_versions_str.split(',')]
                        
                        # 为每个协议版本创建一条记录
                        for protocol_version in protocol_versions:
                            if protocol_version:  # 确保不是空字符串
                                result.append({
                                    'setting_key': setting_key,
                                    'protocol_version': protocol_version
                                })
        
        return result
        
    except FileNotFoundError:
        print(f"错误：找不到文件 {csv_path}")
        return []
    except Exception as e:
        print(f"读取CSV文件时出错: {e}")
        return []


def insert_key_protocol_data(cursor, data_list):
    """
    批量插入数据到 key_for_protocol_version 表
    
    Args:
        cursor: 数据库游标
        data_list: 要插入的数据列表
        
    Returns:
        int: 成功插入的记录数
    """
    if not data_list:
        return 0
    
    # 插入 SQL
    insert_sql = """
        INSERT INTO key_for_protocol_version (setting_key, protocol_version)
        VALUES (%s, %s)
    """
    
    inserted_count = 0
    
    try:
        for item in data_list:
            cursor.execute(insert_sql, (item['setting_key'], item['protocol_version']))
            inserted_count += 1
            
            # 每100条显示一次进度
            if inserted_count % 100 == 0:
                print(f"  已插入 {inserted_count}/{len(data_list)} 条记录")
        
        return inserted_count
        
    except pymysql.Error as e:
        print(f"插入数据时出错: {e}")
        raise


def main():
    """
    主函数
    """
    # 读取 key-协议版本.csv 数据
    print("正在读取 key-协议版本.csv 文件...")
    key_protocol_data = read_key_protocol_csv()
    
    if not key_protocol_data:
        print("没有读取到数据")
        return
    
    print(f"成功读取并解析 {len(key_protocol_data)} 条记录")
    print("\n数据示例（前10条）：")
    for i, item in enumerate(key_protocol_data[:10], 1):
        print(f"  {i}. setting_key: {item['setting_key']}, protocol_version: {item['protocol_version']}")
    
    # 询问用户确认
    confirm = input(f"\n是否继续插入这 {len(key_protocol_data)} 条记录到数据库？(yes/no): ")
    if confirm.lower() not in ['yes', 'y']:
        print("操作已取消")
        return

    # 连接数据库
    connection = None
    try:
        print("\n正在连接数据库...")
        connection = pymysql.connect(**DB_CONFIG)
        cursor = connection.cursor()
        print(f"数据库连接成功: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
        
        # 清空表（可选）
        clear_table = input("\n是否先清空 key_for_protocol_version 表？(yes/no): ")
        if clear_table.lower() in ['yes', 'y']:
            print("正在清空表...")
            cursor.execute("TRUNCATE TABLE key_for_protocol_version")
            connection.commit()
            print("表已清空")
        
        # 插入数据
        print(f"\n正在插入 {len(key_protocol_data)} 条记录...")
        inserted_count = insert_key_protocol_data(cursor, key_protocol_data)
        
        # 提交事务
        connection.commit()
        print(f"\n插入完成！共成功插入 {inserted_count} 条记录")
        
        # 验证插入结果
        cursor.execute("SELECT COUNT(*) FROM key_for_protocol_version")
        total_count = cursor.fetchone()[0]
        print(f"表中当前共有 {total_count} 条记录")
        
    except pymysql.Error as e:
        print(f"数据库错误: {e}")
        if connection:
            connection.rollback()
            print("已回滚事务")
    except Exception as e:
        print(f"发生错误: {e}")
        if connection:
            connection.rollback()
            print("已回滚事务")
    finally:
        # 关闭数据库连接
        if connection:
            connection.close()
            print("\n数据库连接已关闭")


if __name__ == "__main__":
    main()
