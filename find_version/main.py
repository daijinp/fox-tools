#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MySQL 数据库操作脚本
"""

import pymysql

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


def main():
    """
    主函数
    """
    connection = None
    try:
        # 连接数据库
        print("正在连接数据库...")
        connection = pymysql.connect(**DB_CONFIG)
        cursor = connection.cursor()
        print(f"数据库连接成功: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    
        
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
            print("数据库连接已关闭")


if __name__ == "__main__":
    main()
