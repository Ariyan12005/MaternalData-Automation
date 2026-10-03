"""Authenticated encryption with externally supplied keys."""
import base64
import hashlib
import hmac
import json
import os
from cryptography.fernet import Fernet

class Cipher:
    def __init__(self, key=None):
        key = key or os.environ.get("DAYONE_ENCRYPTION_KEY")
        if not key:
            raise ValueError("Configure DAYONE_ENCRYPTION_KEY with a Fernet key")
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self.index_key = base64.urlsafe_b64decode(key)

    def encrypt(self, data):
        return self.fernet.encrypt(data)

    def decrypt(self, data):
        return self.fernet.decrypt(bytes(data))

    def seal(self, value):
        return self.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def open(self, data):
        return json.loads(self.decrypt(data))

    def index(self, value):
        return hmac.new(self.index_key, value.encode(), hashlib.sha256).hexdigest()
