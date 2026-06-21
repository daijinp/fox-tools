import json
import time
import os
import urllib3
import requests
import hashlib
import random

_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'config.json')
with open(_config_path, 'r', encoding='utf-8') as _f:
    _config = json.load(_f)

debug = _config.get('debug', False)
sleep_time = _config.get('sleep_time', 0)
domain = _config['domain']


class GetAuth:

    def get_signature(self, token, path, lang='en'):
        timestamp = round(time.time() * 1000)
        signature = fr'{path}\r\n{token}\r\n{lang}\r\n{timestamp}'

        result = {
            'token': token,
            'lang': lang,
            'timestamp': str(timestamp),
            'signature': self.md5c(text=signature) + f'.{random.randint(0, 999999)}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
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


def fr_requests(method, path, token='', param=None, max_retries=3):
    url = domain + path
    headers = GetAuth().get_signature(token=token, path=path)
    time.sleep(sleep_time)

    for attempt in range(1, max_retries + 1):
        try:
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
        except requests.exceptions.SSLError:
            if attempt < max_retries:
                wait = attempt * 2
                print(f'SSL 错误, {wait}s 后第 {attempt + 1} 次重试: {path}')
                time.sleep(wait)
            else:
                raise
