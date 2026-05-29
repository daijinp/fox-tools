"""
比对执行后的SN因为报错没有被执行的
"""

import csv
import json

with open('config/completed_sn.csv', 'r', encoding='utf-8') as file:
    csv_reader = csv.reader(file)
    for row in csv_reader:
        try:
            print(row[1])
        except IndexError:
            continue
