"""Вход через аккаунт Hik-Connect (P2P/облако Hikvision) и список привязанных устройств (без GTK).

Hik-Connect не публикует открытого API. Модуль повторяет запросы мобильного
приложения так же, как открытая интеграция Home Assistant «hikconnect»:

* ``POST /v3/users/login/v2`` — вход (пароль передаётся как MD5-хеш, как в приложении);
  код 1100 означает, что аккаунт обслуживает другой регион — запрос повторяется на
  ``loginArea.apiDomain``;
* ``PUT /v3/apigateway/login`` — продление сессии по ``refreshSessionId``;
* ``GET /v3/userdevices/v1/devices/pagelist`` — устройства аккаунта с состоянием и адресами;
* ``GET /v3/userdevices/v1/cameras/info`` — каналы устройства.

Облако даёт список устройств, их состояние, серийные номера, модели, каналы и
адреса (локальный IP, внешний IP, порты). Само видео через P2P-ретранслятор
Hikvision идёт по закрытому зашифрованному протоколу, поэтому при импорте
устройство добавляется в каталог как обычный Hikvision ISAPI/RTSP по локальному
или внешнему адресу, а для просмотра нужен логин и пароль самого устройства.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import sentinel_devmgmt as devmgmt
import sentinel_storage as storage

PROVIDER = "hikconnect"
SECRET_NAMESPACE = "hikconnect"
DEFAULT_API = "https://api.hik-connect.com"
REGIONS = (
    ("auto", "Международный (регион определяется автоматически)", DEFAULT_API),
    ("ru", "Россия (hik-connectru.com)", "https://api.hik-connectru.com"),
    ("custom", "Другой адрес сервера…", ""),
)
DEVICE_FILTER = "TIME_PLAN,CONNECTION,SWITCH,STATUS,STATUS_EXT,WIFI,NODISTURB,P2P,KMS,HIDDNS"
PAGE_LIMIT = 50
CLIENT_TYPE = "55"
MAX_REDIRECTS = 3
REFRESH_MARGIN = 3600   # продлевать сессию за час до истечения JWT
ACCESS_HINTS = ("DS-K", "ACCESS", "INTERCOM", "VIS", "DOOR")


class HikConnectError(RuntimeError):
    """Ошибка облака Hik-Connect с понятным пользователю текстом."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class WrongCredentials(HikConnectError):
    pass


class CaptchaRequired(HikConnectError):
    pass


class SessionExpired(HikConnectError):
    pass


_META_MESSAGES = {
    1013: (WrongCredentials, "Неверный логин или пароль Hik-Connect"),
    1014: (WrongCredentials, "Неверный логин или пароль Hik-Connect"),
    1015: (CaptchaRequired, "Hik-Connect требует ввести код с картинки. Войдите один раз в приложение "
                            "Hik-Connect или на сайте, затем повторите вход."),
    1016: (WrongCredentials, "Аккаунт Hik-Connect заблокирован после неудачных попыток. Повторите позже."),
    1021: (WrongCredentials, "Аккаунт Hik-Connect не найден в этом регионе. Выберите другой сервер."),
    1060: (HikConnectError, "Hik-Connect требует подтвердить вход на новом устройстве (код из SMS/email). "
                            "Подтвердите вход в приложении Hik-Connect и повторите."),
    2003: (HikConnectError, "Устройство не в сети"),
    2009: (HikConnectError, "Устройство не в сети"),
    70002: (HikConnectError, "Устройство не в сети"),
}


def password_hash(password: str) -> str:
    """Пароль в формате мобильного приложения (MD5 hex). Сам пароль не передаётся."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()  # noqa: S324 — требование протокола


def jwt_expiry(token: str) -> float | None:
    """Время истечения (``exp``) из JWT-сессии без проверки подписи."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return float(value["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def normalize_api_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_API
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Адрес сервера Hik-Connect должен начинаться с https://")
    return f"https://{parsed.netloc}"


def _int(value, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _first(mapping, keys, default=0):
    for key in keys:
        value = _int(mapping.get(key))
        if value:
            return value
    return default


def _keyed(response, key):
    """Блоки ответа ``connectionInfos`` и т.п.: словарь serial → данные (иногда список)."""
    value = response.get(key) or {}
    if isinstance(value, list):
        return {item.get("deviceSerial"): item for item in value if isinstance(item, dict)}
    return value if isinstance(value, dict) else {}


def device_category(info) -> str:
    text = " ".join(str(info.get(k, "")) for k in ("deviceType", "deviceCategory", "deviceSubCategory")).upper()
    return "access" if any(hint in text for hint in ACCESS_HINTS) else "video"


def normalize_device(info, connection=None, status=None, hiddns=None, wifi=None):
    """Запись устройства Hik-Connect в собственном формате Sentinel."""
    connection, status, hiddns, wifi = connection or {}, status or {}, hiddns or {}, wifi or {}
    if "globalStatus" in status:
        online = _int(status.get("globalStatus")) == 1
    else:
        online = _int(info.get("status")) == 1
    return {
        "serial": str(info.get("deviceSerial", "")),
        "full_serial": str(info.get("fullSerial") or info.get("deviceSerial", "")),
        "name": str(info.get("name") or info.get("deviceName") or info.get("deviceSerial", "")),
        "model": str(info.get("deviceType") or info.get("model") or ""),
        "firmware": str(info.get("version") or ""),
        "online": online,
        "local_ip": str(connection.get("localIp") or wifi.get("address") or ""),
        "wan_ip": str(connection.get("netIp") or connection.get("wanIp") or ""),
        "domain": str(hiddns.get("domain") or ""),
        "http_port": _first(connection, ("localHttpPort",), 80),
        "rtsp_port": _first(connection, ("localRtspPort",), 554),
        "sdk_port": _first(connection, ("localCmdPort", "localStreamPort"), 8000),
        "wan_http_port": _first(connection, ("netHttpPort",), 0) or _first(hiddns, ("mappingHttpPort",), 0),
        "wan_rtsp_port": _first(connection, ("netRtspPort",), 0),
        "wan_sdk_port": _first(connection, ("netCmdPort", "netStreamPort"), 0)
        or _first(hiddns, ("mappingCmdPort",), 0),
        "category": device_category(info),
        "cameras": [],
    }


def normalize_camera(info):
    channel_info = info.get("deviceChannelInfo") or {}
    signal = channel_info.get("signalStatus")
    return {
        "channel": _int(info.get("channelNo"), 1),
        "name": str(info.get("cameraName") or ""),
        "visible": info.get("isShow", 1) not in (0, "0", False),
        "online": signal is None or _int(signal) == 1,
    }


class HikConnectClient:
    """Синхронный клиент облака Hik-Connect (только stdlib)."""

    def __init__(self, api_url=DEFAULT_API, opener=None, timeout=15, feature_code=None):
        self.api_url = normalize_api_url(api_url)
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self.feature_code = feature_code or secrets.token_hex(16)
        self.session_id = ""
        self.refresh_id = ""

    # ------------------------------------------------------------ состояние
    def state(self) -> dict:
        return {"api_url": self.api_url, "session_id": self.session_id, "refresh_id": self.refresh_id,
                "feature_code": self.feature_code}

    @classmethod
    def from_state(cls, state, **kwargs):
        client = cls(state.get("api_url") or DEFAULT_API, feature_code=state.get("feature_code"), **kwargs)
        client.session_id = state.get("session_id", "")
        client.refresh_id = state.get("refresh_id", "")
        return client

    @property
    def logged_in(self) -> bool:
        return bool(self.session_id)

    def expires_at(self) -> float | None:
        return jwt_expiry(self.session_id) if self.session_id else None

    def needs_refresh(self, now=None, margin=REFRESH_MARGIN) -> bool:
        expiry = self.expires_at()
        if expiry is None:          # непрозрачный токен: продлеваем только по ошибке 401
            return not self.session_id
        return expiry - (now if now is not None else time.time()) < margin

    # ------------------------------------------------------------ HTTP
    def _headers(self, auth=True):
        headers = {"clientType": CLIENT_TYPE, "lang": "en-US", "featureCode": self.feature_code,
                   "Accept": "application/json", "User-Agent": "Sentinel-VMS"}
        if auth and self.session_id:
            headers["sessionId"] = self.session_id
        return headers

    def _request(self, method, path, params=None, form=None, auth=True, check=True):
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = self._headers(auth)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SessionExpired("Сессия Hik-Connect истекла, войдите снова", 401) from exc
            raise HikConnectError(f"Сервер Hik-Connect ответил HTTP {exc.code}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise HikConnectError(f"Нет связи с сервером Hik-Connect: {exc.reason}") from exc
        except TimeoutError as exc:
            raise HikConnectError("Сервер Hik-Connect не ответил вовремя") from exc
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HikConnectError("Сервер Hik-Connect вернул не JSON — проверьте адрес сервера") from exc
        if not isinstance(body, dict):
            raise HikConnectError("Неожиданный ответ Hik-Connect")
        if check:
            self._check(body)
        return body

    @staticmethod
    def meta_code(body) -> int:
        meta = body.get("meta") or {}
        return _int(meta.get("code"), 0) if isinstance(meta, dict) else 0

    def _check(self, body):
        code = self.meta_code(body)
        if code in (0, 200):
            return
        if code in (1001, 1002, 1003, 401):
            raise SessionExpired("Сессия Hik-Connect истекла, войдите снова", code)
        error_class, message = _META_MESSAGES.get(code, (HikConnectError, None))
        if message is None:
            meta = body.get("meta") or {}
            text = meta.get("message") or meta.get("moreInfo") or "неизвестная ошибка"
            message = f"Hik-Connect: {text} (код {code})"
        raise error_class(message, code)

    # ------------------------------------------------------------ сессия
    def login(self, account: str, password: str) -> dict:
        account = (account or "").strip()
        if not account or not password:
            raise WrongCredentials("Введите логин (email/телефон) и пароль Hik-Connect")
        form = {"account": account, "password": password_hash(password), "featureCode": self.feature_code}
        for _attempt in range(MAX_REDIRECTS + 1):
            body = self._request("POST", "/v3/users/login/v2", form=form, auth=False, check=False)
            code = self.meta_code(body)
            area = body.get("loginArea") or {}
            domain = area.get("apiDomain") if isinstance(area, dict) else None
            if code == 1100 and domain:
                self.api_url = normalize_api_url(domain)
                continue
            self._check(body)
            session = body.get("loginSession") or {}
            self.session_id = session.get("sessionId", "")
            self.refresh_id = session.get("rfSessionId", "")
            if not self.session_id:
                raise HikConnectError("Hik-Connect не выдал сессию")
            if domain:
                self.api_url = normalize_api_url(domain)
            return body.get("loginUser") or {}
        raise HikConnectError("Слишком много перенаправлений между регионами Hik-Connect")

    def refresh(self) -> None:
        if not self.refresh_id:
            raise SessionExpired("Нет токена продления сессии, войдите снова")
        body = self._request("PUT", "/v3/apigateway/login",
                             form={"refreshSessionId": self.refresh_id, "featureCode": self.feature_code},
                             auth=False)
        session = body.get("sessionInfo") or body.get("loginSession") or {}
        self.session_id = session.get("sessionId", "") or self.session_id
        self.refresh_id = session.get("refreshSessionId") or session.get("rfSessionId") or self.refresh_id
        if not self.session_id:
            raise SessionExpired("Hik-Connect не продлил сессию")

    def ensure_session(self, now=None) -> bool:
        """Продлить сессию, если она скоро истечёт. True — если продлевали."""
        if not self.session_id:
            raise SessionExpired("Вход в Hik-Connect не выполнен")
        if self.needs_refresh(now):
            self.refresh()
            return True
        return False

    def logout(self) -> None:
        self.session_id = ""
        self.refresh_id = ""

    # ------------------------------------------------------------ данные
    def devices(self, with_cameras=True) -> list[dict]:
        result = []
        offset = 0
        for _page in range(100):
            body = self._request("GET", "/v3/userdevices/v1/devices/pagelist",
                                 params={"groupId": -1, "limit": PAGE_LIMIT, "offset": offset,
                                         "filter": DEVICE_FILTER})
            connections = _keyed(body, "connectionInfos")
            statuses = _keyed(body, "statusInfos")
            hiddns = _keyed(body, "hiddnsInfos")
            wifi = _keyed(body, "wifiInfos")
            infos = body.get("deviceInfos") or []
            for info in infos:
                serial = info.get("deviceSerial")
                if not serial:
                    continue
                result.append(normalize_device(info, connections.get(serial), statuses.get(serial),
                                               hiddns.get(serial), wifi.get(serial)))
            page = body.get("page") or {}
            if not infos or not page.get("hasNext"):
                break
            offset += len(infos)
        if with_cameras:
            for device in result:
                try:
                    device["cameras"] = self.cameras(device["serial"])
                except SessionExpired:
                    raise
                except HikConnectError:
                    device["cameras"] = []
        return result

    def cameras(self, serial: str) -> list[dict]:
        body = self._request("GET", "/v3/userdevices/v1/cameras/info", params={"deviceSerial": serial})
        cameras = [normalize_camera(c) for c in body.get("cameraInfos") or [] if isinstance(c, dict)]
        return sorted(cameras, key=lambda c: c["channel"])


# ==================================================================== сохранение
def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_accounts():
    with storage.database() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM cloud_accounts WHERE provider=? ORDER BY account", (PROVIDER,))]


def get_account(account_id):
    with storage.database() as db:
        row = db.execute("SELECT * FROM cloud_accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def upsert_account(account, api_url, remember_password=False, auto_sync=True) -> int:
    with storage.database() as db:
        db.execute("""INSERT INTO cloud_accounts(provider,account,api_url,remember_password,auto_sync)
            VALUES(?,?,?,?,?) ON CONFLICT(provider,account) DO UPDATE SET api_url=excluded.api_url,
            remember_password=excluded.remember_password, auto_sync=excluded.auto_sync""",
                   (PROVIDER, account, api_url, int(bool(remember_password)), int(bool(auto_sync))))
        return db.execute("SELECT id FROM cloud_accounts WHERE provider=? AND account=?",
                          (PROVIDER, account)).fetchone()[0]


def _update_account(account_id, **values):
    if not values:
        return
    columns = ",".join(f"{k}=?" for k in values)
    with storage.database() as db:
        db.execute(f"UPDATE cloud_accounts SET {columns} WHERE id=?", (*values.values(), account_id))


def delete_account(account_id) -> None:
    account = get_account(account_id)
    if not account:
        return
    try:
        storage.delete_json_secret(SECRET_NAMESPACE, account["account"])
    except storage.CredentialStoreError:
        pass
    with storage.database() as db:
        db.execute("DELETE FROM cloud_devices WHERE account_id=?", (account_id,))
        db.execute("DELETE FROM cloud_accounts WHERE id=?", (account_id,))


def save_session(account, client, password=None, remember=False) -> None:
    value = client.state()
    if remember and password:
        value["password"] = password
    else:
        previous = None
        if remember:
            try:
                previous = storage.load_json_secret(SECRET_NAMESPACE, account)
            except storage.CredentialStoreError:
                previous = None
        if previous and previous.get("password"):
            value["password"] = previous["password"]
    storage.save_json_secret(SECRET_NAMESPACE, account, value)


def load_session(account):
    return storage.load_json_secret(SECRET_NAMESPACE, account)


def save_devices_snapshot(account_id, devices) -> None:
    stamp = _now()
    with storage.database() as db:
        db.execute("DELETE FROM cloud_devices WHERE account_id=?", (account_id,))
        for d in devices:
            db.execute("""INSERT OR REPLACE INTO cloud_devices(account_id,serial,full_serial,name,model,firmware,
                online,local_ip,wan_ip,domain,http_port,rtsp_port,sdk_port,wan_http_port,wan_rtsp_port,wan_sdk_port,
                category,cameras,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (account_id, d["serial"], d["full_serial"], d["name"], d["model"], d["firmware"],
                        int(d["online"]), d["local_ip"], d["wan_ip"], d["domain"], d["http_port"], d["rtsp_port"],
                        d["sdk_port"], d["wan_http_port"], d["wan_rtsp_port"], d["wan_sdk_port"], d["category"],
                        json.dumps(d.get("cameras") or [], ensure_ascii=False), stamp))


def load_cloud_devices(account_id=None):
    query = """SELECT c.*, a.account FROM cloud_devices c JOIN cloud_accounts a ON a.id=c.account_id"""
    params = ()
    if account_id is not None:
        query += " WHERE c.account_id=?"
        params = (account_id,)
    with storage.database() as db:
        rows = [dict(r) for r in db.execute(query + " ORDER BY a.account, c.name", params)]
        imported = {r[0] for r in db.execute("SELECT DISTINCT cloud_serial FROM devices WHERE cloud_serial<>''")}
    for row in rows:
        row["online"] = bool(row["online"])
        try:
            row["cameras"] = json.loads(row["cameras"] or "[]")
        except json.JSONDecodeError:
            row["cameras"] = []
        row["imported"] = row["serial"] in imported
    return rows


# ==================================================================== сценарии
def login_account(account, password, api_url=DEFAULT_API, remember_password=False, client_factory=None):
    """Войти, запомнить аккаунт и сессию, загрузить устройства. Возвращает (account_id, devices)."""
    factory = client_factory or HikConnectClient
    client = factory(normalize_api_url(api_url))
    client.login(account, password)
    account_id = upsert_account(account.strip(), client.api_url, remember_password)
    save_session(account.strip(), client, password, remember_password)
    devices = _fetch_and_store(account_id, client)
    return account_id, devices


def _fetch_and_store(account_id, client):
    try:
        devices = client.devices()
    except HikConnectError as exc:
        _update_account(account_id, last_error=str(exc))
        raise
    save_devices_snapshot(account_id, devices)
    _update_account(account_id, last_sync=_now(), last_error="", api_url=client.api_url)
    return devices


def restore_client(account_row, password=None, client_factory=None, force=False):
    """Клиент с действующей сессией: продление по токену, иначе повторный вход по паролю.

    force=True — сервер отверг текущую сессию: сразу продлеваем токеном или входим заново.
    """
    factory = client_factory or HikConnectClient
    state = load_session(account_row["account"]) or {}
    client = (factory.from_state(state) if state.get("session_id")
              else factory(account_row.get("api_url") or DEFAULT_API))
    secret_password = password or state.get("password")
    try:
        if client.logged_in and force:
            client.refresh()
            return client
        if client.logged_in:
            client.ensure_session()
            return client
    except SessionExpired:
        pass
    except HikConnectError:
        if not secret_password:
            raise
    if not secret_password:
        raise SessionExpired(f"Сессия Hik-Connect для {account_row['account']} истекла: введите пароль снова")
    client.login(account_row["account"], secret_password)
    return client


def sync_account(account_id, password=None, client_factory=None):
    account = get_account(account_id)
    if not account:
        raise HikConnectError("Аккаунт Hik-Connect не найден")
    try:
        client = restore_client(account, password, client_factory)
    except HikConnectError as exc:
        _update_account(account_id, last_error=str(exc))
        raise
    save_session(account["account"], client, password, bool(account["remember_password"]))
    try:
        return _fetch_and_store(account_id, client)
    except SessionExpired:
        client = restore_client(account, password, client_factory, force=True)
        save_session(account["account"], client, password, bool(account["remember_password"]))
        return _fetch_and_store(account_id, client)


def import_template(cloud_device, address="auto", area=""):
    """Шаблон устройства каталога, номера каналов и их имена для облачного устройства.

    address: "local" — локальный IP, "wan" — внешний адрес/HiDDNS с пробросом портов,
    "auto" — локальный, если известен, иначе внешний.
    """
    local = cloud_device.get("local_ip")
    remote = cloud_device.get("domain") or cloud_device.get("wan_ip")
    use_wan = address == "wan" or (address == "auto" and not local)
    if use_wan:
        if not remote:
            raise ValueError(f"{cloud_device['name']}: облако не сообщило внешний адрес")
        host = remote
        http_port = cloud_device.get("wan_http_port") or cloud_device.get("http_port") or 80
        rtsp_port = cloud_device.get("wan_rtsp_port") or cloud_device.get("rtsp_port") or 554
        sdk_port = cloud_device.get("wan_sdk_port") or cloud_device.get("sdk_port") or 8000
    else:
        if not local:
            raise ValueError(f"{cloud_device['name']}: облако не сообщило локальный адрес "
                             "(устройство не в сети?)")
        host = local
        http_port = cloud_device.get("http_port") or 80
        rtsp_port = cloud_device.get("rtsp_port") or 554
        sdk_port = cloud_device.get("sdk_port") or 8000
    cameras = [c for c in cloud_device.get("cameras") or [] if c.get("visible", True)]
    channels = sorted({int(c["channel"]) for c in cameras}) or [1]
    names = {int(c["channel"]): c["name"] for c in cameras if c.get("name")}
    template = {
        "name": cloud_device["name"], "device_name": cloud_device["name"], "host": host,
        "protocol": "hikvision", "sdk_port": int(sdk_port), "http_port": int(http_port),
        "rtsp_port": int(rtsp_port), "stream": "main", "category": cloud_device.get("category", "video"),
        "serial_no": cloud_device.get("full_serial") or cloud_device["serial"],
        "device_model": cloud_device.get("model", ""), "firmware": cloud_device.get("firmware", ""),
        "group_name": area, "channel_names": names, "cloud_serial": cloud_device["serial"],
        "door_count": 1 if cloud_device.get("category") == "access" else 0,
    }
    return template, channels


def store_device(device, credentials, channels):
    """Записать устройство с каналами в каталог (без GTK; используется сервером)."""
    if not channels or len(channels) > 512:
        raise ValueError("Устройство вернуло недопустимое число каналов")
    group_id = storage.add_group(device["group_name"]) if device.get("group_name") else None
    names = device.get("channel_names") or {}
    ids = []
    with storage.database() as db:
        for channel in channels:
            title = devmgmt.channel_title(device["name"], channel, names, single=len(channels) == 1)
            cursor = db.execute(
                """INSERT INTO devices(name,host,sdk_port,http_port,rtsp_port,channel,stream,protocol,group_id,
                serial_no,device_model,host_name,category,door_count,firmware,status,cloud_serial)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (title, device["host"], device["sdk_port"], device["http_port"], device["rtsp_port"], channel,
                 device.get("stream", "main"), device.get("protocol", "hikvision"), group_id,
                 device.get("serial_no", ""), device.get("device_model", ""),
                 device.get("device_name") or device["name"], device.get("category", "video"),
                 int(device.get("door_count") or 0), device.get("firmware", ""), device.get("status", ""),
                 device.get("cloud_serial", "")))
            ids.append(cursor.lastrowid)
    if credentials and credentials.get("username"):
        storage.save_credentials(device, credentials)
    return ids


def mark_cloud_serial(device_ids, serial) -> None:
    with storage.database() as db:
        db.executemany("UPDATE devices SET cloud_serial=? WHERE id=?", ((serial, i) for i in device_ids))


def import_devices(serials, credentials, address="auto", area="Hik-Connect", store=None, account_id=None):
    """Импорт облачных устройств в каталог. Возвращает (добавлено, [ошибки])."""
    store = store or store_device
    wanted = set(serials)
    added, errors = 0, []
    for cloud in load_cloud_devices(account_id):
        if cloud["serial"] not in wanted:
            continue
        wanted.discard(cloud["serial"])
        if cloud["imported"]:
            errors.append(f"{cloud['name']}: уже в каталоге")
            continue
        try:
            template, channels = import_template(cloud, address, area)
            template["status"] = "online" if cloud["online"] else "offline"
            ids = store(template, credentials, channels)
            mark_cloud_serial(ids, cloud["serial"])
            added += 1
        except sqlite3.IntegrityError:
            errors.append(f"{cloud['name']}: адрес и каналы уже добавлены")
        except (ValueError, storage.CredentialStoreError) as exc:
            errors.append(str(exc))
    errors.extend(f"{serial}: нет в списке аккаунта" for serial in sorted(wanted))
    return added, errors


def apply_cloud_status() -> int:
    """Перенести состояние «в сети / не в сети» из облака в каталог для импортированных устройств."""
    with storage.database() as db:
        rows = db.execute("SELECT serial, online FROM cloud_devices").fetchall()
        changed = 0
        for row in rows:
            cursor = db.execute("UPDATE devices SET status=?, status_checked_at=? WHERE cloud_serial=? AND status<>?",
                                ("online" if row["online"] else "offline", _now(), row["serial"],
                                 "online" if row["online"] else "offline"))
            changed += cursor.rowcount
    return changed


def account_for_serial(serial):
    """Аккаунт Hik-Connect, к которому привязано устройство с этим серийным номером."""
    with storage.database() as db:
        row = db.execute("""SELECT a.* FROM cloud_devices c JOIN cloud_accounts a ON a.id=c.account_id
            WHERE c.serial=? ORDER BY a.last_sync DESC LIMIT 1""", (serial,)).fetchone()
    return dict(row) if row else None


def cloud_session(serial, client_factory=None) -> dict:
    """Действующая сессия аккаунта устройства для облачного видео (продлевается при необходимости)."""
    account = account_for_serial(serial)
    if not account:
        raise HikConnectError("Устройство не найдено в аккаунтах Hik-Connect: синхронизируйте аккаунт")
    before = (load_session(account["account"]) or {}).get("session_id")
    client = restore_client(account, client_factory=client_factory)
    if client.session_id != before:
        save_session(account["account"], client, remember=bool(account["remember_password"]))
    return client.state()

# ==================================================================== подсказки при ошибке видео
_CONNECT_HINTS = ("resource", "ресурс", "could not connect", "connection refused", "timed out", "timeout",
                  "no route", "unreachable", "не удалось открыть")
_LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"))
_AUTH_HINTS = ("401", "unauthorized", "not authorized", "authentication", "авториз")


def is_private_host(host) -> bool:
    """Адрес из частной сети (192.168.x.x, 10.x, 172.16-31.x, 169.254.x.x)."""
    try:
        address = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return any(address in network for network in _LAN_NETWORKS)


def cloud_status_map() -> dict:
    """{серийный номер: в сети ли устройство по данным Hik-Connect}."""
    with storage.database() as db:
        return {r["serial"]: bool(r["online"]) for r in db.execute("SELECT serial, online FROM cloud_devices")}


def stream_failure_hint(device, message) -> str:
    """Понятное объяснение ошибки потока для устройства, импортированного из Hik-Connect.

    Пустая строка — устройство не облачное или причина не распознана.
    """
    if not device or not device.get("cloud_serial"):
        return ""
    text = str(message or "").lower()
    name = device.get("host_name") or device.get("name") or device.get("cloud_serial")
    host = device.get("host", "")
    if any(h in text for h in _AUTH_HINTS):
        return (f"{name}: регистратор отклонил логин или пароль. Нужны учётные данные самого регистратора "
                "(обычно admin), а не пароль аккаунта Hik-Connect. Укажите их в «Система › Устройства».")
    if any(h in text for h in _CONNECT_HINTS):
        if is_private_host(host):
            return (f"{name}: нет связи с {host}:{device.get('rtsp_port', 554)}. Это локальный адрес сети "
                    "объекта из Hik-Connect, напрямую он доступен только через VPN или после проброса "
                    "RTSP-порта (укажите внешний адрес в «Система › Устройства»). Без этого видео "
                    "идёт через облако Hik-Connect.")
        return (f"{name}: нет связи с {host}:{device.get('rtsp_port', 554)}. Проверьте, что RTSP-порт "
                "проброшен на роутере объекта, или подключитесь к его сети через VPN.")
    return ""
