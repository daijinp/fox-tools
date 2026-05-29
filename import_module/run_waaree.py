import requests
import csv
import logging
import time
from typing import List
import hashlib
from datetime import datetime
import json

# 配置日志文件
logging.basicConfig(
    filename="import_sn3.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def md5c(text="", _type="lower"):
    res = hashlib.md5(text.encode(encoding='UTF-8')).hexdigest()
    if _type.__eq__("lower"):
        return res
    else:
        return res.upper()

# API 配置
API_URL = "https://digital.waaree.com/imp/v0/module/importModule"
key = "kKXG8A072CX9sM3fCk4s3s5g6dZIwZGelPqsmGhUP1fqrDxFn72HAxBFc2j0DChv"


def read_sn_from_csv(file_path: str) -> List[str]:
    """从 CSV 文件读取 SN（每行一个 SN）"""
    sn_list = []
    with open(file_path, mode="r", encoding="utf-8") as file:
        reader = csv.reader(file)
        for row in reader:
            if row:  # 跳过空行
                sn_list.append(row[0].strip())  # 假设 SN 在第一列
    return sn_list


def send_sn_batches(sn_list: List[str], batch_size: int = 3000):
    """分批发送 SN 到 API，并记录日志"""
    total_sn = len(sn_list)
    for i in range(0, total_sn, batch_size):
        input(123)
        batch = sn_list[i:i + batch_size]
        batch_number = (i // batch_size) + 1
        payload = {
            "moduleList": batch
        }

        # 记录开始请求
        logging.info(f"开始发送第 {batch_number} 批 | SN 数量: {len(batch)}")
        print(f"正在发送第 {batch_number} 批，SN 数量: {len(batch)}")

        try:
            timestamp = round(time.time() * 1000)
            signature = fr'{key};{timestamp}'
            headers = {
                "Content-Type": "application/json",
                'token': md5c(text=signature),
                'timestamp': str(timestamp)
            }

            # 发送请求
            response = requests.post(
                API_URL,
                json=payload,
                headers=headers,
                # timeout=30
            )
            response.raise_for_status()  # 检查 HTTP 错误

            # 记录成功日志
            logging.info(
                f"第 {batch_number} 批成功 | "
                f"状态码: {response.status_code} | "
                f"响应: {response.text[:200]}..."  # 截断长响应
            )
            print(f"成功 | 状态码: {response.status_code}")

        except requests.exceptions.RequestException as e:
            # 记录失败日志
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                error_msg += f" | 响应: {e.response.text[:200]}..."
            logging.error(
                f"第 {batch_number} 批失败 | "
                f"错误: {error_msg}"
            )
            print(f"失败 | 错误: {error_msg}")

        except Exception as e:
            # 捕获其他异常（如 JSON 解析错误）
            logging.error(
                f"第 {batch_number} 批发生未知错误 | "
                f"错误类型: {type(e).__name__} | "
                f"详情: {str(e)}"
            )
            print(f"严重错误: {e}")


if __name__ == "__main__":
    csv_file = "waaree3.csv"  # CSV 文件路径
    try:
        sn_list = read_sn_from_csv(csv_file)
        print(f"从 CSV 读取到 {len(sn_list)} 个 SN")
        logging.info(f"=== 开始导入任务，总 SN 数量: {len(sn_list)} ===")

        send_sn_batches(sn_list, batch_size=3000)

        logging.info("=== 所有批次导入完成 ===")
        print("所有批次导入完成！")

    except Exception as e:
        logging.critical(f"脚本全局错误: {str(e)}", exc_info=True)
        print(f"脚本崩溃: {e}")
