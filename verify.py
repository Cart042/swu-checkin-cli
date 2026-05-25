from des import des
import requests
import urllib.parse
import base64

def verify(username, password, timeout=10):
    try:
        from get_info import get_token
        token = get_token(username, password, timeout)
        return token is not None
    except Exception:
        return False