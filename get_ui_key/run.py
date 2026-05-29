import json
import time
import urllib3
import requests
import hashlib
import os
from json import JSONDecodeError
from jsonpath import jsonpath

"""debug mode"""
debug = False
"""request interval (second)"""
sleep_time = 0
"""domain name, do not modify it unless necessary"""
domain = 'https://www.foxesscloud.com'
"""your key"""
key = 'admeyJpZCI6IjVkOGM5ODhiLTQ1NmQtNDAxZS04Nzk3LWI4YWRiODUzOWY1NCIsInNlY3JldCI6IjA4ZmY2YjRmNTU0MDI4NDIyODA3YjIwMDY5NDc3MTAyM2RjMzllM2Y5ODFmODVhZDNjMzdiNjJiODYxMTZhZmUiLCJwYXlsb2FkIjoiWHhhUGdsSEhFNHBadzFodU85blJkSUZteWJxU25IRnhQei9nc2pwZFhnTkhhaEZhYUFNbGQyY0ZMTWF3M2VPWkFXRkRwY3A0b29oMDE3clJiWld4NnNQQ2t2SWgyRTBiMFJPRGdUSEFOLzZWeUFYbFJGWnRheDE4ZU1sRnBGNC9DZGlhZk9oZFpPM2Q1YlJMWHVCUWg3V0ZhdVlvS1kwcTVqKytjVTloK2dLcDlmRDJjcEJXRjNPd2ZJOFBDMGE4ZkcwRGpueWdMOGpza2N2b0hLeFpOR1BvKzNjUFg0QTl3clhsdjk5SHJmTlBtTmYrdTJIZGppZVo3ekR1R3pFRiJ9'


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


def save_response_data(response, filename):
    """Create the 'data' directory if it doesn't exist"""
    os.makedirs('data', exist_ok=True)

    """Concatenate the directory path and filename to create the full file path"""
    file_path = os.path.join('data', filename)

    """Get the current timestamp"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    """Check if the status code is 200"""
    if response.status_code == 200:
        """Save the response data and timestamp to a dictionary"""
        try:
            response_json = response.json()
        except JSONDecodeError:
            response_json = response.text

        data = {
            'response': response_json,
            'timestamp': timestamp
        }
        """Write the dictionary as JSON to the file"""
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
    else:
        """Save the status code, response text, and timestamp to a dictionary"""
        data = {
            'status_code': response.status_code,
            'response_text': response.text,
            'timestamp': timestamp
        }
        """Write the dictionary as JSON to the file"""
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=4)

    print(f'{timestamp}: [{filename}] successfully saved')


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


def find_key_for_protocol_version(datas, name):
    result = []
    for data in datas:
        path = '/generic/v0/device/setting/ui'
        request_param = {'id': data.get('id')}
        response = fr_requests('get', path, request_param)
        # 使用jsonpath获取response中name对应的key
        parameters = jsonpath(response.json(), f'$.result.parameters')[0]
        if parameters is False:
            # 报一个异常打印 response.json()
            raise Exception(f'response: {response.json()}')
        for parameter_list in parameters:
            # parameters的下一级，需要每遍历这一级中每一个的 properties。是一个数组\
            for property in parameter_list.get('properties'):
                if property.get('name') == name:
                    result.append({'protocol_version': data.get('protocol_version'), 
                    'request_key': parameter_list.get('key'),
                    'response_key': property.get('key')})
    return result

if __name__ == '__main__':
    """
    SELECT
        device_id, device_sn, protocol_version, master_version
    FROM (
        SELECT
            d.device_id,
            d.device_sn,
            v.protocol_version,
            v.master_version,
            -- 下面的 ORDER BY 决定了如果有重复，取哪一条
            -- 这里假设取 device_id 最大的一条，你可以根据需要修改
            ROW_NUMBER() OVER (PARTITION BY v.protocol_version ORDER BY d.device_id DESC) as rn
        FROM devices d
        INNER JOIN versions v ON d.device_id = v.model_id
        WHERE v.protocol_version IN (
            'Q10100', 'Q10200', 'Q10300', 'Q10400', 'Q10500', 'Q10600',
            'Q10700', 'Q10800', 'Q10900', 'Q11000', 'Q11100', 'Q11200'
        )
        AND d.communication = 0
    ) t
    WHERE t.rn = 1;

    """
    datas = [{'protocol_version': 'Q10200', 'id': 'ff8fb637-a469-4c07-a3a7-6d7f98aa9063'},
             {'protocol_version': 'Q10300', 'id': 'ffe3bebc-a4d3-48f9-9cc1-686ee07c15e4'},
             {'protocol_version': 'Q10400', 'id': 'ffd8bd16-75e1-40c6-a8e4-3b82b68f75db'},
             {'protocol_version': 'Q10500', 'id': 'ffcc2089-6b4b-4466-ac98-afffaf30fa9c'},
             {'protocol_version': 'Q10600', 'id': 'fff2648f-2cbd-42cd-a3c1-d50b4a9b4d82'},
             {'protocol_version': 'Q10700', 'id': 'ffff31ef-c472-4f5c-b803-08a8c9ca2f19'},
             {'protocol_version': 'Q10800', 'id': 'ffe04532-b280-40b0-9a1b-0b86ed821fd1'},
             {'protocol_version': 'Q10900', 'id': 'ffedfe63-0510-4457-8023-433e304b73cd'},
             {'protocol_version': 'Q11000', 'id': 'fffde28e-bc65-4fed-a16e-ac30d10302b9'},
             {'protocol_version': 'Q11100', 'id': 'fff8c0ca-dea7-45a9-98eb-08fa58e344fe'},
             {'protocol_version': 'Q11200', 'id': 'fffe1847-543a-4059-b807-8f105972ebfc'}]
    keys = find_key_for_protocol_version(datas, 'BalanceLoadSwitch')
    print(keys)