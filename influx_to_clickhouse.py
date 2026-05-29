"""
2025年6月16日13:44:00
校验数据报表接口从 influx 到 clickhouse的逻辑
"""
import csv
import json
import datetime
import concurrent.futures
import pandas as pd
import jsonpath
import threading

from utils import foxess_requests as fr

# 全局锁确保线程安全写入
log_lock = threading.Lock()


def save_request_log(response_data: dict, log_file: str = "influx_to_clickhouse.log"):
    """
    将请求结果保存到JSON日志文件（线程安全）

    参数:
    response_data -- 要保存的响应数据（字典格式）
    log_file -- 日志文件路径（默认：requests.log）
    """
    # 添加时间戳
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "data": response_data
    }

    # JSON格式行
    json_line = json.dumps(log_entry, ensure_ascii=False)

    # 线程安全写入
    with log_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json_line + "\n")


# 数组分组
def split_array(arr, chunk_size):
    return [arr[i:i + chunk_size] for i in range(0, len(arr), chunk_size)]


def device_report_query(begin_date, end_date, sn):
    path = '/c/v0/device/report/query'
    new_data_param = {"pageSize": 10, "currentPage": 1, "sn": sn, "odmSN": "", "beginDate": begin_date,
                      "endDate": end_date}

    #  请求新库
    new_data_response = fr.fr_requests('post', path, new_data_param)

    old_data_param = new_data_param.copy()
    old_data_param.update({'testKey': 'CFo8awk@G6lNn^2&w7vM*JiNpV'})
    #  请求老库
    old_data_response = fr.fr_requests('post', path, old_data_param)

    print(f'请求结果：{old_data_response.text}')

    new_status_code = new_data_response.status_code
    old_status_code = old_data_response.status_code
    if new_status_code == 200:
        new_errno = jsonpath.jsonpath(new_data_response.json(), '$..errno')[0]
        if new_errno == 0:
            new_total = jsonpath.jsonpath(new_data_response.json(), '$.result.total')
        else:
            new_total = False
    else:
        new_total = False
        new_errno = False

    if old_status_code == 200:
        old_errno = jsonpath.jsonpath(old_data_response.json(), '$..errno')[0]
        if old_errno == 0:
            old_total = jsonpath.jsonpath(new_data_response.json(), '$.result.total')
        else:
            old_total = False
    else:
        old_total = False
        old_errno = False
    begin_time = f'{begin_date.get("year")}.{begin_date.get("month")}.{begin_date.get("day")}'
    end_time = f'{end_date.get("year")}.{end_date.get("month")}.{end_date.get("day")}'
    return {
        'sn': new_data_param.get('sn'),
        'time': f'{begin_time}-{end_time}',

        'new_status_code': new_status_code,
        'new_errno': new_errno,
        'new_total': new_total,

        'old_status_code': old_status_code,
        'old_errno': old_errno,
        'old_total': old_total}


def run(data):
    for item in data:
        # 将时间戳转换为年月日时分秒
        today = datetime.datetime.now()
        # 天维度：前X*15天数据
        for day_num in range(1, 10):
            bd = today - datetime.timedelta(days=day_num*14)
            ed = today - datetime.timedelta(days=(day_num-1)*14)
            result = device_report_query(begin_date={"year": bd.year, "month": bd.month, "day": bd.day},
                                         end_date={"year": ed.year, "month": ed.month, "day": ed.day},
                                         sn=item['sn'])
            save_request_log(result)


if __name__ == '__main__':
    with open('influx_to_clickhouse/intserver.csv', 'r') as file:
        csv_reader = csv.reader(file)
        list_csv_reader = list(csv_reader)
        data = []
        for row in list_csv_reader:
            data.append({'sn': row[0].strip()})

    input(f'执行的SN数量:{len(data)}')
    # 将数组分组,启用17个线程。需要符合2N+1的规则
    iterables = split_array(data, (int(len(data) / 17) + 1))
    print(f'一共有{len(iterables)}组数据')
    with concurrent.futures.ThreadPoolExecutor(max_workers=17) as executor:
        executor.map(run, iterables)

    # run([{'sn': '60UO1130465A005'}])
