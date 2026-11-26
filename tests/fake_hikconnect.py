"""Имитация облачного сервиса P2P для тестов: opener, совместимый с urllib."""
import base64
import io
import json
import time
import urllib.parse


def make_jwt(exp):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'HS256'})}.{part({'exp': exp})}.sig"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeCloud:
    """Два региона: глобальный перенаправляет аккаунт на apiieu."""

    def __init__(self, account="user@example.com", password="secret", region="https://apiieu.hik-connect.com"):
        self.account = account
        self.password_md5 = __import__("hashlib").md5(password.encode()).hexdigest()
        self.region = region
        self.requests = []
        self.session = make_jwt(time.time() + 7200)
        self.devices = [
            {"deviceSerial": "Q12345678", "fullSerial": "DS-7608NI-K2/P0820201234CCRRQ12345678WCVU",
             "name": "Дом NVR", "deviceType": "DS-7608NI-K2", "version": "V4.30.085", "deviceCategory": "NVR"},
            {"deviceSerial": "K00000001", "fullSerial": "DS-K1T341AMF0000K00000001", "name": "Терминал",
             "deviceType": "DS-K1T341AMF", "version": "V3.2", "deviceCategory": "AccessControl"},
        ]
        self.connections = {
            "Q12345678": {"localIp": "192.168.1.50", "netIp": "203.0.113.7", "localRtspPort": 554,
                          "netRtspPort": 10554, "localCmdPort": 8000, "netCmdPort": 18000, "netHttpPort": 8080},
            "K00000001": {"localIp": "192.168.1.60"},
        }
        self.statuses = {"Q12345678": {"globalStatus": 1}, "K00000001": {"globalStatus": 0}}
        self.cameras = {"Q12345678": [
            {"cameraId": "a", "cameraName": "Ворота", "channelNo": 1, "isShow": 1,
             "deviceChannelInfo": {"signalStatus": 1}},
            {"cameraId": "b", "cameraName": "Двор", "channelNo": 2, "isShow": 1,
             "deviceChannelInfo": {"signalStatus": 0}},
            {"cameraId": "c", "cameraName": "Скрытая", "channelNo": 3, "isShow": 0},
        ]}
        self.expire_next_devices = False

    def _json(self, value):
        return FakeResponse(json.dumps(value).encode())

    def open(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        base = f"{url.scheme}://{url.netloc}"
        query = dict(urllib.parse.parse_qsl(url.query))
        form = dict(urllib.parse.parse_qsl((request.data or b"").decode()))
        headers = {k.lower(): v for k, v in request.header_items()}
        self.requests.append((request.get_method(), base, url.path, query, form, headers))
        if url.path == "/v3/users/login/v2":
            if form.get("account") != self.account or form.get("password") != self.password_md5:
                return self._json({"meta": {"code": 1013}})
            if base != self.region:
                return self._json({"meta": {"code": 1100}, "loginArea": {"apiDomain": self.region}})
            return self._json({"meta": {"code": 200}, "loginSession": {"sessionId": self.session,
                                                                      "rfSessionId": "refresh-1"},
                               "loginArea": {"apiDomain": self.region}})
        if url.path == "/v3/apigateway/login":
            if form.get("refreshSessionId") != "refresh-1":
                return self._json({"meta": {"code": 1002}})
            self.session = make_jwt(time.time() + 7200)
            return self._json({"meta": {"code": 200}, "sessionInfo": {"sessionId": self.session,
                                                                     "refreshSessionId": "refresh-1"}})
        if headers.get("sessionid") != self.session:
            return self._json({"meta": {"code": 1002}})
        if url.path == "/v3/userdevices/v1/devices/pagelist":
            if self.expire_next_devices:
                self.expire_next_devices = False
                return self._json({"meta": {"code": 1002}})
            offset = int(query.get("offset", 0))
            page = self.devices[offset:offset + 1]          # по одному на страницу — проверка пагинации
            return self._json({"meta": {"code": 200}, "deviceInfos": page,
                               "connectionInfos": {d["deviceSerial"]: self.connections.get(d["deviceSerial"], {})
                                                   for d in page},
                               "statusInfos": {d["deviceSerial"]: self.statuses[d["deviceSerial"]] for d in page},
                               "page": {"hasNext": offset + 1 < len(self.devices)}})
        if url.path == "/v3/userdevices/v1/cameras/info":
            return self._json({"meta": {"code": 200}, "cameraInfos": self.cameras.get(query["deviceSerial"], [])})
        return self._json({"meta": {"code": 404}})


class FakeSecrets:
    """Подмена storage._secret_tool в памяти."""

    def __init__(self):
        self.items = {}

    def __call__(self, action, device, secret=None):
        key = (device["host"], int(device.get("http_port", 80)))
        if action == "store":
            self.items[key] = secret
        elif action == "lookup":
            return self.items.get(key)
        elif action == "clear":
            self.items.pop(key, None)
        return None
