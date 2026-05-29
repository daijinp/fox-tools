"""
2024年5月6日10:25:33
这个脚本校验了欧服的设备的历史数据的迁移
"""
import datetime
import random

import jsonpath

from utils import foxess_requests as fr
import csv
import concurrent.futures

log_file = 'log_0527.csv'
empty_data_file = 'empty_data_sn_0527.csv'


# 数组分组
def split_array(arr, chunk_size):
    return [arr[i:i + chunk_size] for i in range(0, len(arr), chunk_size)]


def device_report_query(begin_date, end_date, sn):
    path = '/c/v0/device/report/query'
    # {"year": 2024, "month": 5, "day": 2}
    param = {"pageSize": 10, "currentPage": 1, "sn": sn, "odmSN": "",
             "beginDate": begin_date, "endDate": end_date}
    response = fr.fr_requests('post', path, param)
    if response.status_code == 200:
        if response.json()['errno'] == 40261:
            param['endDate']['day'] -= 1
            response = fr.fr_requests('post', path, param)
    return {'request_params': param, 'response': response}


def run(data):
    index = 0
    for item in data:
        # 将时间戳转换为年月日时分秒
        bd = datetime.datetime.fromtimestamp(item['begin_date'] / 1000)
        ed = datetime.datetime.fromtimestamp(item['end_date'] / 1000)
        try:
            try:
                # 请求参数是
                r = device_report_query(begin_date={"year": bd.year, "month": bd.month, "day": bd.day},
                                        end_date={"year": ed.year, "month": ed.month, "day": ed.day},
                                        sn=item['sn'])
                response = r['response']
                request_params = r['request_params']
            except Exception as e:
                response = None
                raise e
        except Exception as e:
            print(f'发生了错误: {e},检查到了第{index}个设备,设备信息: {item},响应:{response}')
            if response:
                log = [datetime.datetime.now(), item['sn'], f'执行脚本时发生了错误：{e}', f'响应:{response.text}']
            else:
                log = [datetime.datetime.now(), item['sn'], f'执行脚本时发生了错误：{e}']

            with open(log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(log)
            continue

        if jsonpath.jsonpath(response.json(), '$..data'):
            res = jsonpath.jsonpath(response.json(), '$..data')[0]
        else:
            res = None
        if res:
            print(item['sn'], f'数据不为空')
            with open(log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([datetime.datetime.now(), item['sn'],
                                 f'数据不为空,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}'])
        else:
            print(item['sn'], '并网后两周内的数据为空')
            # 将响应内容转为一行字符串，即去除换行符
            one_line_response = response.text.replace('\n', '').strip()
            with open(log_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([datetime.datetime.now(), item['sn'],
                                 f'并网后两周内的数据为空,耗时{response.elapsed.total_seconds()}s,状态码为{response.status_code}',
                                 f'请求：{request_params}',
                                 f'响应:{one_line_response}'])
            with open(empty_data_file, mode='a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([item['sn'], item['id'], item['begin_date'], item['end_date']])

        index += 1
        # 整千的时候打印
        if index % 1000 == 0:
            print(f'第{index}个设备')
    print(f'检查完毕,检查了{index}设备')


"""
随机抽查X个设备
"""
if __name__ == '__main__':
    # 0508执行 device_data_available.csv 中所有SN  时间：2022-8-15(1660492800000)  到2022-8-29(1661702400000)
    with open('config/device_data_available.csv', 'r') as file:
        csv_reader = csv.reader(file)
        # 将csv_reader转为list后随机获取X个设备
        list_csv_reader = random.sample(list(csv_reader), 1200)
        data = []
        for row in list_csv_reader:
            # 添加时间段 2022-7-20 ~ 2022-8-3
            data.append({'sn': row[0].strip(), 'id': row[2],
                         'begin_date': 1658246400000,
                         'end_date': 1659542400000})
            # 添加时间段 2022-8-25 ~ 2022-9-5
            data.append({'sn': row[0].strip(), 'id': row[2],
                         'begin_date': 1661356800000,
                         'end_date': 1662393600000})
            # 添加时间段 2023-1-1 ~ 2023-1-10
            data.append({'sn': row[0].strip(), 'id': row[2],
                         'begin_date': 1672502400000,
                         'end_date': 1673366400000})

    input(f'执行的SN数量:{len(data)}')
    # 将数组分组,启用17个线程。需要符合2N+1的规则
    iterables = split_array(data, (int(len(data) / 17) + 1))
    print(f'一共有{len(iterables)}组数据')
    with concurrent.futures.ThreadPoolExecutor(max_workers=17) as executor:
        executor.map(run, iterables)
