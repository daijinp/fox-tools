"""
2025年6月16日13:44:00
校验数据报表接口从 influx 到 clickhouse的逻辑
"""
import csv
import datetime
from ..utils import foxess_requests as fr

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
        ed = datetime.datetime.now() - datetime.timedelta(days= index)
        bd = ed - datetime.timedelta(days=1)
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


if __name__ == '__main__':
    with open('intserver.csv', 'r') as file:
        csv_reader = csv.reader(file)
        print(csv_reader)


    # input(f'执行的SN数量:{len(data)}')
    # # 将数组分组,启用17个线程。需要符合2N+1的规则
    # iterables = split_array(data, (int(len(data) / 17) + 1))
    # print(f'一共有{len(iterables)}组数据')
    # with concurrent.futures.ThreadPoolExecutor(max_workers=17) as executor:
    #     executor.map(run, iterables)
