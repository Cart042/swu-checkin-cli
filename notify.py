import os
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
import logging

logger = logging.getLogger("swu.notify")

def send_dingtalk(token, secret, title, content):
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    if secret:
        timestamp = str(round(time.time() * 1000))
        secret_enc = secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url += f"&timestamp={timestamp}&sign={sign}"
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "msgtype": "text",
        "text": {
            "content": f"{title}\n\n{content}"
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        return r.json().get("errcode") == 0
    except Exception as e:
        logger.error(f"发送钉钉推送失败: {e}")
        return False

def send_qywx(key, title, content):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "msgtype": "text",
        "text": {
            "content": f"{title}\n\n{content}"
        }
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        return r.json().get("errcode") == 0
    except Exception as e:
        logger.error(f"发送企业微信群机器人推送失败: {e}")
        return False

def send_bark(key, url, title, content):
    base_url = url.rstrip("/") if url else "https://api.day.app"
    request_url = f"{base_url}/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(content)}"
    try:
        r = requests.get(request_url, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"发送Bark推送失败: {e}")
        return False

def send_serverchan(key, title, content):
    url = f"https://sctapi.ftqq.com/{key}.send"
    payload = {
        "title": title,
        "desp": content
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.json().get("code") == 0
    except Exception as e:
        logger.error(f"发送Server酱推送失败: {e}")
        return False

def send_pushdeer(key, title, content):
    url = "https://api2.pushdeer.com/message/push"
    payload = {
        "pushkey": key,
        "text": title,
        "desp": content
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.json().get("code") == 0
    except Exception as e:
        logger.error(f"发送PushDeer推送失败: {e}")
        return False

def send_push(title, content):
    dingtalk_token = os.getenv("PUSH_DINGTALK_TOKEN", "").strip()
    dingtalk_secret = os.getenv("PUSH_DINGTALK_SECRET", "").strip()
    qywx_key = os.getenv("PUSH_QYWX_KEY", "").strip()
    bark_key = os.getenv("PUSH_BARK_KEY", "").strip()
    bark_url = os.getenv("PUSH_BARK_URL", "").strip()
    serverchan_key = os.getenv("PUSH_SERVERCHAN_KEY", "").strip()
    pushdeer_key = os.getenv("PUSH_PUSHDEER_KEY", "").strip()
    
    sent = False
    
    if dingtalk_token:
        logger.info("正在发送钉钉机器人推送...")
        if send_dingtalk(dingtalk_token, dingtalk_secret, title, content):
            logger.info("钉钉机器人推送成功")
            sent = True
            
    if qywx_key:
        logger.info("正在发送企业微信群机器人推送...")
        if send_qywx(qywx_key, title, content):
            logger.info("企业微信群机器人推送成功")
            sent = True
            
    if bark_key:
        logger.info("正在发送Bark推送...")
        if send_bark(bark_key, bark_url, title, content):
            logger.info("Bark推送成功")
            sent = True
            
    if serverchan_key:
        logger.info("正在发送Server酱推送...")
        if send_serverchan(serverchan_key, title, content):
            logger.info("Server酱推送成功")
            sent = True
            
    if pushdeer_key:
        logger.info("正在发送PushDeer推送...")
        if send_pushdeer(pushdeer_key, title, content):
            logger.info("PushDeer推送成功")
            sent = True
            
    if not sent:
        if not any([dingtalk_token, qywx_key, bark_key, serverchan_key, pushdeer_key]):
            logger.info("未配置任何推送通道 (如 PUSH_DINGTALK_TOKEN, PUSH_BARK_KEY 等)，跳过推送。")
        else:
            logger.warning("推送配置存在，但发送失败。")
