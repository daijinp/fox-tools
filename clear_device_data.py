"""
2024年5月6日10:25:33
这个脚本校验了欧服的设备的历史数据的迁移
"""
import datetime
import random
import time

import jsonpath

from utils import foxess_requests as fr
import csv
import concurrent.futures

fail_log_file = 'fail_log_0906.csv'
success_log_file = 'success_log_0906.csv'
empty_data_file = 'empty_data_sn_0906.csv'


# 数组分组
def split_array(arr, chunk_size):
    return [arr[i:i + chunk_size] for i in range(0, len(arr), chunk_size)]


def device_report_query(param):
    # path = '/imp/v0/device/clearAll'
    # response = fr.fr_requests('post', path, param)
    # return {'request_params': param, 'response': response}
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

            with open(fail_log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(log)
            continue

        # if jsonpath.jsonpath(response.json(), '$..errno'):
        #     res = response.json()
        # else:
        #     res = None
        errno = jsonpath.jsonpath(response.json(), '$..errno')[0]
        if errno == 0:
            print(item['deviceSN'], f'执行成功')
            with open(success_log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([datetime.datetime.now(), item['deviceSN'],
                                 f'执行成功,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}'])
        else:
            print(item['deviceSN'], '执行失败')
            # 将响应内容转为一行字符串，即去除换行符
            one_line_response = response.text.replace('\n', '').strip()
            with open(fail_log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([datetime.datetime.now(), item['deviceSN'],
                                 f'执行失败,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}',
                                 f'请求:{request_params}',
                                 f'响应:{one_line_response}'])
            with open(empty_data_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([item['deviceSN']])

        index += 1
        # 整千的时候打印
        if index % 1000 == 0:
            print(f'第{index}个设备')
    print(f'检查完毕,检查了{index}设备')


if __name__ == '__main__':
    with open('config/clear_deviceSN_data.csv', 'r') as file:
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