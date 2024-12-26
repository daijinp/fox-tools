import threading


class ThreadSafeList:
    def __init__(self):
        self.lock = threading.Lock()  # 创建锁
        self.data = []  # 共享数据：列表

    def append(self, value):
        with self.lock:  # 获取锁，确保操作是原子性的
            self.data.append(value)

    def get(self, index):
        with self.lock:  # 获取锁，确保读取操作是安全的
            return self.data[index]
