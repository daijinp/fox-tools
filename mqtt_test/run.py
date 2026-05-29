#!/usr/bin/env python3
"""
MQTT TLS 双向认证发送 & 接收脚本（Callback API V2）
- 发送指定的 topic 和 payload
- 订阅响应主题，打印所有接收到的消息
证书路径可通过命令行参数覆盖
"""

import json
import logging
import ssl
import sys
import os
import time
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# ---------- 日志格式 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mqtt_sender")

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))

# ---------- MQTT 连接参数 ----------
#BROKER = "139.224.232.119"
BROKER = "emqx-svc.foxess-cloud.svc.cluster.local"
PORT = 30082
USERNAME = "foxessTest"
PASSWORD = "<>foxess"                    # 请确认密码是否包含尖括号

# 默认 TLS 证书路径（请根据实际情况修改）
CA_CERTS = None
CERTFILE = r".\server.pem"
KEYFILE = r".\server.key"

# ---------- 发布主题与负载 ----------
TOPIC = "/kp23bhcpmt91n2v8/R250312J0069/rtg/status/meter/lems/antireflux/-1/-1/1"
PAYLOAD = {
    "0": 0,
    "1": -1,
    "2": -1,
    "3": 1,
    "4": 1778574886,
    "110110004": 0,
    "110110005": 0,
    "110110006": 0,
    "110110007": 0,
    "110110008": 0,
    "110110009": 0,
    "110110010": 0,
    "110110011": 0,
    "110110012": 0,
    "110110013": 0,
    "110110014": 0,
    "110110015": 0,
    "110110016": 0,
    "110110017": 0,
    "110110018": 0,
    "110110019": 0,
    "110110020": 0,
    "110110021": 0,
    "110110026": 0,
    "110110027": 0,
    "110110028": 0,
    "110110029": 0,
    "110110066": 0,
    "110110067": 0
}

# ---------- 订阅响应主题 ----------
# 监听该设备的所有消息（通配符 #）；若只想监听特定响应，改为具体主题如 TOPIC + "/response"
SUB_TOPICS = [
    "/kp23bhcpmt91n2v8/R250312J0069/#",
]
SUB_QOS = 1
# 发送后等待接收的时间（秒）
WAIT_AFTER_PUB = 10


def resolve_path(path_value):
    if not path_value:
        return path_value

    if os.path.isabs(path_value) and os.path.exists(path_value):
        return path_value

    candidates = [
        path_value,
        os.path.join(APP_DIR, os.path.basename(path_value)),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.basename(path_value)),
    ]

    for candidate in candidates:
        normalized = os.path.abspath(candidate)
        if os.path.exists(normalized):
            return normalized

    return os.path.abspath(path_value)


# ---------- MQTT 回调 (V2 API) ----------
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logger.info("✅ 已连接到 MQTT broker")
        # 连接成功后订阅响应主题
        for topic in SUB_TOPICS:
            client.subscribe(topic, qos=SUB_QOS)
            logger.info(f"  订阅 topic: {topic} (QoS {SUB_QOS})")
    else:
        logger.error(f"❌ 连接失败，返回码: {reason_code} ({mqtt.connack_string(reason_code)})")


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    """订阅确认回调"""
    logger.info(f"📡 订阅确认: mid={mid}, 结果码={reason_code_list}")


def on_message(client, userdata, msg):
    """接收到消息时，打印完整 topic 和 payload"""
    try:
        payload_str = msg.payload.decode("utf-8")
        # 尝试格式化 JSON，方便阅读
        try:
            payload_obj = json.loads(payload_str)
            formatted = json.dumps(payload_obj, indent=2)
            logger.info(f"\n📩 收到消息 [{msg.topic}] (QoS={msg.qos}):\n{formatted}")
        except json.JSONDecodeError:
            logger.info(f"\n📩 收到消息 [{msg.topic}] (QoS={msg.qos}): {payload_str}")
    except Exception as e:
        logger.warning(f"📩 收到消息但解码失败: {e}, 原始 bytes: {msg.payload}")


def on_publish(client, userdata, mid, reason_code, properties):
    logger.debug(f"消息已发送，mid={mid}, reason_code={reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        logger.warning(f"⚠️ 意外断开，返回码: {reason_code}")
    else:
        logger.info("🔌 已正常断开连接")


# ---------- 主逻辑 ----------
def main(certfile=None, keyfile=None):
    # 优先使用传入的参数，否则使用默认值
    cert_path = resolve_path(certfile or CERTFILE)
    key_path = resolve_path(keyfile or KEYFILE)

    # 检查证书文件是否存在
    if not os.path.exists(cert_path):
        logger.error(f"❌ 证书文件不存在: {cert_path}")
        sys.exit(1)
    if not os.path.exists(key_path):
        logger.error(f"❌ 私钥文件不存在: {key_path}")
        sys.exit(1)

    # 创建 Client（使用 V2 回调 API）
    client = mqtt.Client(
        client_id="foxess_sender",
        callback_api_version=CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311
    )

    # 用户名/密码
    client.username_pw_set(USERNAME, PASSWORD)

    # 配置 TLS
    client.tls_set(
        ca_certs=CA_CERTS,
        certfile=cert_path,
        keyfile=key_path,
        cert_reqs=ssl.CERT_NONE if CA_CERTS is None else ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS
    )

    # 绑定回调（新增 on_subscribe 和 on_message）
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_publish = on_publish
    client.on_disconnect = on_disconnect

    # 连接
    logger.info(f"正在连接到 {BROKER}:{PORT} ...")
    try:
        client.connect(BROKER, PORT, keepalive=60)
    except Exception as e:
        logger.exception("连接失败，请检查地址、证书或网络状态")
        sys.exit(1)

    client.loop_start()

    # 等待连接成功
    timeout = 5
    start = time.time()
    while not client.is_connected():
        if time.time() - start > timeout:
            logger.error("等待连接超时")
            client.loop_stop()
            sys.exit(1)
        time.sleep(0.1)

    # 发布消息
    payload_str = json.dumps(PAYLOAD)
    logger.info(f"\n📤 发送 topic: {TOPIC}")
    logger.info(f"📤 发送内容: {payload_str[:200]}{'...' if len(payload_str) > 200 else ''}")
    logger.info(f"负载长度: {len(payload_str)} 字节")

    info = client.publish(TOPIC, payload_str, qos=1)
    info.wait_for_publish()
    logger.info("✔️ 消息已成功发出")

    # 等待一段时间接收响应
    logger.info(f"⏳ 等待 {WAIT_AFTER_PUB} 秒接收响应...\n")
    time.sleep(WAIT_AFTER_PUB)

    # 断开连接
    client.loop_stop()
    client.disconnect()
    logger.info("🏁 程序结束")


if __name__ == "__main__":
    # 支持命令行传入证书路径：python mqtt_send.py [certfile] [keyfile]
    cert_arg = sys.argv[1] if len(sys.argv) > 1 else None
    key_arg = sys.argv[2] if len(sys.argv) > 2 else None
    main(certfile=cert_arg, keyfile=key_arg)
