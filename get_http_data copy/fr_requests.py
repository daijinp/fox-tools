import hashlib
import json
import os
import time

import requests
import urllib3

_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'config.json')
with open(_config_path, 'r', encoding='utf-8') as _f:
    _config = json.load(_f)

debug = _config.get('debug', False)
sleep_time = _config.get('sleep_time', 0)
domain = _config['domain']
urllib3.disable_warnings()


class GetAuth:
    def get_signature(self, token, path, lang='en', timestamp=None):
        """
        This function is used to generate a signature consisting of URL, token, and timestamp, and return a dictionary containing the signature and other information.
            :param token: your key
            :param path:  your request path
            :param lang: language, default is English.
            :return: with authentication header
        """
        timestamp = round(time.time() * 1000) if timestamp is None else int(timestamp)
        signature = fr'{path}\r\n{token}\r\n{timestamp}'

        result = {
            'token': token,
            'lang': lang,
            'timestamp': str(timestamp),
            'signature': self.md5c(text=signature),
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/117.0.0.0 Safari/537.36'
        }
        return result, signature

    @staticmethod
    def md5c(text="", _type="lower"):
        res = hashlib.md5(text.encode(encoding='UTF-8')).hexdigest()
        if _type.__eq__("lower"):
            return res
        else:
            return res.upper()



def fr_requests(method, path, token='', param=None, timeout=300):
    url = domain + path
    lang = _config.get('lang', 'en')
    fixed_timestamp = _config.get('fixed_timestamp')
    headers, signature_raw = GetAuth().get_signature(
        token=token,
        path=path,
        lang=lang,
        timestamp=fixed_timestamp,
    )

    if param is None:
        param = {}

    method = method.lower()
    if method == 'get' and param:
        request_param = {'params': param}
    elif method == 'get':
        request_param = {}
    elif method == 'post':
        request_param = {'json': param}
    else:
        raise Exception('request method error')

    if sleep_time:
        time.sleep(sleep_time)

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        timeout=timeout,
        verify=False,
        **request_param,
    )
    if debug:
        result = {
            'url': url,
            'method': method,
            'param': param,
            'signature_raw': signature_raw,
            'headers': headers,
            'response': response.text,
        }
        print(json.dumps(result, indent=1, ensure_ascii=False))
        print('-------------------------' * 5)
    return response
