"""
2024年9月6日15:37:46
这个脚本筛选出需要清理数据的SN
"""
import datetime
from multiprocessing import Lock

import jsonpath

from utils import foxess_requests as fr
import csv
import concurrent.futures
import threading

# Get the current date
current_date = datetime.datetime.now()
# Format the month and day with leading zeros if needed
month = f"{current_date.month:02d}"
day = f"{current_date.day:02d}"

# check后【不需要】清理数据的SN的log文件
check_not_mark_log_file = f'check_not_mark_log_{month}{day}.csv'
# 请求失败的log文件
check_request_fail_log_file = f'check_request_fail_log_{month}{day}.csv'
# 请求失败的SN
check_request_fail_sn_file = f'check_request_fail_sn_{month}{day}.csv'
# check后【需要】执行清理的SN的log文件
check_mark_log_file = f'check_mark_log_{month}{day}.csv'
# check后【需要】执行清理的SN
check_mark_sn_file = f'check_mark_sn_{month}{day}.csv'
# 已经执行过的SN ---》记录跑完的, 用来比对原文件的SN
check_used_sn_file = f'check_used_sn_{month}{day}.csv'

file_lock = threading.Lock()


# def lock_log(log_name, log_data):
#     print(log_data)
#     if isinstance(log_data, list):
#         _data = [s.replace('\n', '') if isinstance(s, str) else s for s in log_data]
#     else:
#         _data = log_data.replace("\n", "").replace("\r", "")
#     # 创建一个锁对象
#     file_lock = Lock()
#
#     file_lock.acquire()  # 在写入前获取锁
#     try:
#         with open(log_name, mode='a', newline='') as file:
#             writer = csv.writer(file)
#             writer.writerow(_data)
#     finally:
#         file_lock.release()  # 写入后释放锁


def lock_log(log_name, log_data):
    print(log_data)
    if isinstance(log_data, list):
        _data = [s.replace('\n', '') if isinstance(s, str) else s for s in log_data]
    else:
        _data = log_data.replace("\n", "").replace("\r", "")

    with file_lock:
        with open(log_name, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(_data)


# 数组分组
def split_array(arr, chunk_size):
    return [arr[i:i + chunk_size] for i in range(0, len(arr), chunk_size)]


def device_report_query(param):
    path = '/imp/v0/device/check'
    response = fr.fr_requests('post', path, param)
    return {'request_params': param, 'response': response}


def run(data):
    index = 0
    for item in data:
        try:
            try:
                # 请求参数是
                r = device_report_query(item)
                response = r['response']
                request_params = r['request_params']
            except Exception as e:
                response = None
                raise e
        except Exception as e:
            print(f'发生了错误: {e},检查到了第{index}个设备,设备信息: {item},响应:{response}')
            if response:
                log = [datetime.datetime.now(), item['deviceSN'], f'执行脚本时发生了错误：{e}', f'响应:{response.text}']
            else:
                log = [datetime.datetime.now(), item['deviceSN'], f'执行脚本时发生了错误：{e}']
            # 把执行失败的log 写入到check_request_fail_sn_file
            lock_log(check_request_fail_log_file, log)

            # 把执行失败的SN 写入到check_request_fail_sn_file
            lock_log(check_request_fail_sn_file, [item['deviceSN']])
            continue

        if jsonpath.jsonpath(response.json(), '$..errno'):
            errno = jsonpath.jsonpath(response.json(), '$..errno')[0]
        else:
            errno = 9

        if errno != 0:
            print(item['deviceSN'], f'取消标记SN')
            one_line_response = response.text.replace('\n', '').strip()
            log_data = [datetime.datetime.now(), item['deviceSN'],
                        f'取消标记SN,耗时{response.elapsed.total_seconds()}s,'
                        f'状态码为{response.status_code}',
                        f'响应:{one_line_response}']
            lock_log(check_not_mark_log_file, log_data)

        elif errno == 9:
            # 这个分支处理的是请求出错的情况
            print(item['deviceSN'], '请求出错了，记录一下')
            # 将响应内容转为一行字符串，即去除换行符
            one_line_response = response.text.replace('\n', '').strip()
            # 把执行失败的log 写入到check_request_fail_sn_file
            log_data = [datetime.datetime.now(), item['deviceSN'],
                        f'请求出错',
                        f'请求:{request_params}',
                        f'响应:{one_line_response}']
            lock_log(check_request_fail_log_file, log_data)

            # 把执行失败的SN 写入到check_request_fail_sn_file
            lock_log(check_request_fail_sn_file, [item['deviceSN']])
        else:
            # result = jsonpath.jsonpath(response.json(), '$..result')
            # if result:
            #     print(item['deviceSN'], f'取消标记SN')
            #     log_data = [datetime.datetime.now(), item['deviceSN'],
            #                          f'取消标记SN,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}']
            #     lock_log(check_not_mark_log_file, log_data)
            # else:
            #     print(item['deviceSN'], '满足条件标记SN')
            #     # 将响应内容转为一行字符串，即去除换行符
            #     one_line_response = response.text.replace('\n', '').strip()
            #     log_data = [datetime.datetime.now(), item['deviceSN'],
            #                          f'执行失败,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}',
            #                          f'请求:{request_params}',
            #                          f'响应:{one_line_response}']
            #     lock_log(check_mark_log_file, log_data)
            #
            #     # 需要清理数据的SN放入到这里
            #     lock_log(check_mark_sn_file, [item['deviceSN']])

            print(item['deviceSN'], '满足条件标记SN')
            # 将响应内容转为一行字符串，即去除换行符
            one_line_response = response.text.replace('\n', '').strip()
            # 判断 result的值，写入csv。方便排序
            try:
                result = jsonpath.jsonpath(response.json(), '$..result')[0]
                if result is None:
                    result = -1
                elif result is False:
                    result = -2
            except Exception as e:
                result = -999

            log_data = [datetime.datetime.now(), item['deviceSN'],
                        result,
                        f'请求:{request_params}',
                        f'响应:{one_line_response}',
                        {fr.domain},
                        f'满足条件标记SN,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}']
            lock_log(check_mark_log_file, log_data)

            # 需要清理数据的SN放入到这里
            lock_log(check_mark_sn_file, [item['deviceSN']])

        # 记录库一下跑完的, 用来比对原文件的SN
        lock_log(check_used_sn_file, [item['deviceSN']])

        index += 1
        # 整千的时候打印
        if index % 1000 == 0:
            print(f'第{index}个设备')
    print(f'检查完毕,检查了{index}设备')


if __name__ == '__main__':
    with open('config/processing_sn.csv', 'r') as file:
        csv_reader = csv.reader(file)
        # 将csv_reader转为list后随机获取X个设备
        # list_csv_reader = random.sample(list(csv_reader), 1200)
        # 执行全部
        list_csv_reader = csv.reader(file)
        data = []
        for row in list_csv_reader:
            data.append({'deviceSN': row[0]})

    input(f'执行的SN数量:{len(data)}')

    # 将数组分组,启用17个线程。需要符合2N+1的规则
    iterables = split_array(data, (int(len(data) / 17) + 1))
    print(f'一共有{len(iterables)}组数据')
    with concurrent.futures.ThreadPoolExecutor(max_workers=17) as executor:
        executor.map(run, iterables)
