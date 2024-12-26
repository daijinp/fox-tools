from datetime import datetime
import csv
"""
比对xxx_initial_sn_... xxx_used_sn_...文件中的SN差异,获取到日志中没有的log继续执行

比对后会输入到f'{name}_initial_sn_{month}{day}.csv'文件中，手动复制到 processing_sn.csv然后继续执行对应的处理脚本就行
"""
current_date = datetime.now()
month = f"{current_date.month:02d}"
day = f"{current_date.day:02d}"

use = input('1.check\n2.clear\n')
if use == '1':
    name = 'check'
elif use == '2':
    name = 'clear'
else:
    print('输入错误')
    exit()

# 已经执行过的SN
_used_sn_file = f'{name}_used_sn_{month}{day}.csv'
# csv_name
_initial_sn_file = f'{name}_initial_sn_{month}{day}.csv'

with open('config/data_deviceSN.csv', 'r') as file:
    csv_reader = csv.reader(file)
    # 将csv_reader转为list后随机获取X个设备
    # list_csv_reader = random.sample(list(csv_reader), 1200)
    # 执行全部
    list_csv_reader = csv.reader(file)
    initial_data = []
    for row in list_csv_reader:
        initial_data.append({'deviceSN': row[0]})

with open(_used_sn_file, 'r') as file:
    csv_reader = csv.reader(file)
    # 将csv_reader转为list后随机获取X个设备
    # list_csv_reader = random.sample(list(csv_reader), 1200)
    # 执行全部
    list_csv_reader = csv.reader(file)
    used_data = []
    for row in list_csv_reader:
        used_data.append({'deviceSN': row[0]})


result = [x for x in initial_data if x not in used_data]
for item in result:
    with open(_initial_sn_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([item['deviceSN']])

