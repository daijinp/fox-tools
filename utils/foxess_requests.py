import json
import time

import urllib3
import requests

from utils.get_auth import GetAuth
from config.config import domain, key, sleep_time, debug

urllib3.disable_warnings()


def fr_requests(method, path, param=None):
    url = domain + path
    headers = GetAuth().get_signature(token=key, path=path)
    time.sleep(sleep_time)

    if method == 'get':
        response = requests.get(url=url, params=param, headers=headers, verify=False, timeout=60)

    elif method == 'post':
        response = requests.post(url=url, json=param, headers=headers, verify=False, timeout=60)
    else:
        raise Exception('request method error')

    if debug:
        result = {'url': url, 'method': method, 'param': param, 'headers': headers, 'response': response.text}
        print(json.dumps(result, indent=1))
        print('-------------------------'*5)
    return response
