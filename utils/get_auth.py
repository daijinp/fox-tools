import hashlib
import time


class GetAuth:

    def get_signature(self, token, path, lang='en'):
        """
        This function is used to generate a signature consisting of URL, token, and timestamp, and return a dictionary containing the signature and other information.
            :param token: your key
            :param path:  your request path
            :param lang: language, default is English.
            :return: with authentication header
        """
        timestamp = round(time.time() * 1000) - 50000
        # 开放API
        # signature = fr'{path}\r\n{token}\r\n{timestamp}'
        # 欧服
        # signature = fr'{path}\r\n{token}\r\n{lang}\r\n{timestamp}'
        # result = {
        #     'token': token,
        #     'lang': lang,
        #     'timestamp': str(timestamp),
        #     'signature': self.md5c(text=signature),
        #     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
        #                   'Chrome/117.0.0.0 Safari/537.36'
        # }
        # check 和 clear token计算方式: 密钥+";"+时间戳 MD5加密
        signature = fr'{token}{";"}{timestamp}'
        result = {
            'token': self.md5c(text=signature),
            'lang': lang,
            'timestamp': str(timestamp),
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


