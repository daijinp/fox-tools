"""
    2025年2月24日,中性用户 批量创建电站
"""
import csv

# 数组分组
batch_create_plant_file = './config/datas/batch_create_plant.csv'

with open(batch_create_plant_file, 'r', encoding='utf-8') as file:
    csv_reader = csv.reader(file)
    for row in csv_reader:
        module_sn = row[0]

        plant_name = module_sn
        # 国家
        country = row[1]
        # 时区
        time_zone = row[2]

        city = row[4]
        address = row[5]

        param = {
            "attachment": {
                "nmi": ""
            },
            "electricmeterSN": "",
            "groups": [
                ""
            ],
            "devices": [
                {
                    "sn": module_sn,
                    "key": ""
                }
            ],
            "timezone": time_zone,
            "daylight": "",
            "agent": "DC_zz1",
            "pileSN": "",
            "position": {
                "format": "dd",
                "pid": "",
                "x": "",
                "y": ""
            },
            "countryInfo": {
                "code": "BR"
            },
            "details": {
                "model": 1,
                "name": module_sn,
                "type": 1,
                "country": "中国",
                "countryCode": country,
                "city": city,
                "address": address,
                "price": "1",
                "currency": row[8],
                "capacity": "",
                "quantity": "",
                "stationID": "",
                "owner": "",
                "createdDate": "",
                "postcode": "0",
                "systemCapacity": "1",
                "isAutoAppend": False
            },
            "layoutByMini": {
                "direction": 1,
                "arrange": []
            },
            "ukattachment": []
        }

