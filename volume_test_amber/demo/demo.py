import json
import time
import urllib3
import requests
import hashlib
import os
from json import JSONDecodeError

"""debug mode"""
debug = True
"""request interval (second)"""
sleep_time = 0
"""domain name, do not modify it unless necessary"""
domain = 'https://www.foxesscloud.com'
"""your key"""
key = '3b8c6b63-7fb0-42c0-b475-eb7288cb0baa'


class GetAuth:

    def get_signature(self, token, path, lang='en'):
        """
        This function is used to generate a signature consisting of URL, token, and timestamp, and return a dictionary containing the signature and other information.
            :param token: your key
            :param path:  your request path
            :param lang: language, default is English.
            :return: with authentication header
        """
        timestamp = round(time.time() * 1000)
        signature = fr'{path}\r\n{token}\r\n{timestamp}'

        result = {
            'token': token,
            'lang': lang,
            'timestamp': str(timestamp),
            'signature': self.md5c(text=signature),
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/117.0.0.0 Safari/537.36'
        }
        return result

    @staticmethod
    def md5c(text="", _type="lower"):
        res = hashlib.md5(text.encode(encoding='UTF-8')).hexdigest()
        if _type.__eq__("lower"):
            return res
        else:
            return res.upper()


urllib3.disable_warnings()


def fr_requests(method, path, param=None):
    url = domain + path
    headers = GetAuth().get_signature(token=key, path=path)

    time.sleep(sleep_time)

    if method == 'get':
        response = requests.get(url=url, params=param, headers=headers, verify=False)

    elif method == 'post':
        response = requests.post(url=url, json=param, headers=headers, verify=False)
    else:
        raise Exception('request method error')

    if debug:
        result = {'url': url, 'method': method, 'param': param, 'headers': headers, 'response': response.text}
        print(json.dumps(result, indent=1))
        print('-------------------------' * 5)
    return response


class Device:

    @staticmethod
    def device_v2_scheduler_get():
        path = '/op/v2/device/scheduler/get'
        request_param= {"deviceSN": "60MJ30303APP010"}
        response = fr_requests('post', path, request_param)
        return response

    @staticmethod
    def device_v2_scheduler_set():
        path = '/op/v2/device/scheduler/enable'
        request_param= {
                        "groups": [
                            {
                            "fdPwr": 0,
                            "fdSoc": 6,
                            "enable": 0,
                            "maxSoc": 80,
                            "endHour": 3,
                            "workMode": "ForceDischarge",
                            "endMinute": 4,
                            "startHour": 1,
                            "startMinute": 2,
                            "minSocOnGrid": 5
                            },
                            {
                            "fdPwr": 2422,
                            "fdSoc": 7,
                            "enable": 1,
                            "maxSoc": 92,
                            "endHour": 3,
                            "workMode": "Feedin",
                            "endMinute": 30,
                            "startHour": 2,
                            "startMinute": 22,
                            "minSocOnGrid": 6
                            }
                        ],
                        "deviceSN": "your_device_sn"
                        }
        response = fr_requests('post', path, request_param)
        return response



if __name__ == '__main__':


    device = Device()
    device.device_v2_scheduler_get()
    # device.device_v2_scheduler_set()

