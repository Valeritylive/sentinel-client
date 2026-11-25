"""Клиент REST API Sentinel Server (без GTK): вход, устройства, Hik-Connect, поток событий."""
from __future__ import annotations

import http.client
import ipaddress
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

API = "/api/v1"


PLAIN_HTTP_SSL_REASONS = ("RECORD_LAYER_FAILURE", "WRONG_VERSION_NUMBER", "UNKNOWN_PROTOCOL", "HTTP_REQUEST",
                          "PACKET_LENGTH_TOO_LONG")
PLAIN_HTTP_HINT = ("Сервер работает без шифрования (HTTP). Укажите адрес с http://, например "
                   "http://127.0.0.1:8765 — точный адрес показан в «Настройках Sentinel Server».")


class RemoteError(RuntimeError):
    def __init__(self, message, status=None, plain_http=False, tls_required=False):
        super().__init__(message)
        self.status = status
        self.plain_http = plain_http       # https-запрос ушёл на сервер без TLS
        self.tls_required = tls_required   # http-запрос ушёл на сервер с TLS (он оборвал соединение)


def has_scheme(url: str) -> bool:
    return "://" in (url or "")


def _is_local_host(host):
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_plain_http_error(reason):
    return isinstance(reason, ssl.SSLError) and (
        getattr(reason, "reason", None) in PLAIN_HTTP_SSL_REASONS
        or any(r in str(reason) for r in PLAIN_HTTP_SSL_REASONS))


def normalize_server_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Укажите адрес сервера")
    if "://" not in url:
        # сервер на этом же компьютере по умолчанию слушает 127.0.0.1 без TLS
        host = url.split("/", 1)[0].rsplit(":", 1)[0] if not url.startswith("[") else url[1:url.find("]")]
        url = ("http://" if _is_local_host(host) else "https://") + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Адрес сервера: https://хост:порт")
    if parsed.port is None:
        url = f"{parsed.scheme}://{parsed.hostname}:8765" if ":" not in parsed.hostname \
            else f"{parsed.scheme}://[{parsed.hostname}]:8765"
    return url


class ServerClient:
    def __init__(self, base_url, token="", verify_tls=True, ca_file=None, timeout=20):
        self.base_url = normalize_server_url(base_url)
        self.token = token
        self.timeout = timeout
        self.user = None
        context = None
        if self.base_url.startswith("https://"):
            context = ssl.create_default_context(cafile=ca_file) if verify_tls else ssl._create_unverified_context()
        handlers = [urllib.request.ProxyHandler({})]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------ HTTP
    def request(self, method, path, body=None, params=None, timeout=None):
        url = self.base_url + API + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (json.JSONDecodeError, AttributeError):
                message = None
            raise RemoteError(message or f"Сервер ответил HTTP {exc.code}", exc.code) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if _is_plain_http_error(reason):
                raise RemoteError(PLAIN_HTTP_HINT, plain_http=True) from exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise RemoteError("Сертификат сервера не прошёл проверку. Установите сертификат или "
                                  "укажите файл CA.") from exc
            raise RemoteError(f"Нет связи с сервером: {reason}") from exc
        except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError) as exc:
            if url.startswith("http://"):
                raise RemoteError("Сервер оборвал соединение: вероятно, он работает с шифрованием — укажите "
                                  "адрес с https://", tls_required=True) from exc
            raise RemoteError(f"Нет связи с сервером: {exc}") from exc
        except ssl.SSLError as exc:
            if _is_plain_http_error(exc):
                raise RemoteError(PLAIN_HTTP_HINT, plain_http=True) from exc
            raise RemoteError(f"Ошибка TLS: {exc}") from exc
        except TimeoutError as exc:
            raise RemoteError("Сервер не ответил вовремя") from exc
        return json.loads(raw or b"{}")

    # ------------------------------------------------------------ сессия
    def health(self):
        return self.request("GET", "/health")

    def login(self, username, password):
        result = self.request("POST", "/auth/login", {"username": username, "password": password})
        self.token = result["token"]
        self.user = result["user"]
        return result

    def logout(self):
        if self.token:
            try:
                self.request("POST", "/auth/logout", {})
            except RemoteError:
                pass
        self.token = ""

    def system(self):
        return self.request("GET", "/system")

    # ------------------------------------------------------------ данные
    def devices(self):
        return self.request("GET", "/devices")["items"]

    def stream_url(self, device_id, stream=None):
        return self.request("GET", f"/devices/{int(device_id)}/stream", params={"stream": stream})["url"]

    def alarms(self, status=None, limit=200):
        return self.request("GET", "/alarms", params={"status": status, "limit": limit})

    def acknowledge(self, ids, remarks=None):
        return self.request("POST", "/alarms/ack", {"ids": list(ids), "remarks": remarks})

    def events(self, limit=200):
        return self.request("GET", "/events", params={"limit": limit})["items"]

    # ------------------------------------------------------------ Hik-Connect
    def hikconnect_accounts(self):
        return self.request("GET", "/hikconnect/accounts")["items"]

    def hikconnect_login(self, account, password, region="auto", api_url=None, remember_password=False):
        return self.request("POST", "/hikconnect/accounts", {"account": account, "password": password,
                                                             "region": region, "api_url": api_url,
                                                             "remember_password": remember_password},
                            timeout=90)

    def hikconnect_sync(self, account_id, password=None):
        return self.request("POST", f"/hikconnect/accounts/{int(account_id)}/sync", {"password": password},
                            timeout=90)

    def hikconnect_devices(self, account_id=None):
        return self.request("GET", "/hikconnect/devices", params={"account_id": account_id})["items"]

    def hikconnect_import(self, serials, username="", password="", address="auto", area="Hik-Connect",
                          account_id=None):
        return self.request("POST", "/hikconnect/import", {"serials": list(serials), "username": username,
                                                           "password": password, "address": address,
                                                           "area": area, "account_id": account_id})

    def hikconnect_remove(self, account_id):
        return self.request("DELETE", f"/hikconnect/accounts/{int(account_id)}")

    # ------------------------------------------------------------ поток событий
    def listen(self, on_message, stop: threading.Event, on_state=None, reconnect=3.0):
        """Слушать SSE в текущем потоке до stop; переподключается сам, продолжает с последнего id."""
        last_id = 0
        while not stop.is_set():
            request = urllib.request.Request(self.base_url + API + "/stream", headers={
                "Accept": "text/event-stream", "Authorization": f"Bearer {self.token}",
                "Last-Event-ID": str(last_id)})
            try:
                with self.opener.open(request, timeout=60) as response:
                    if on_state:
                        on_state(True, "")
                    event = {}
                    for raw in response:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line:
                            if event.get("data"):
                                try:
                                    message = json.loads(event["data"])
                                    last_id = int(message.get("id", last_id))
                                    on_message(message)
                                except (json.JSONDecodeError, ValueError):
                                    pass
                            event = {}
                        elif line.startswith(":"):
                            continue
                        else:
                            key, _sep, value = line.partition(":")
                            value = value[1:] if value.startswith(" ") else value
                            event[key] = event.get(key, "") + value if key == "data" else value
            except urllib.error.HTTPError as exc:
                if on_state:
                    on_state(False, f"HTTP {exc.code}")
                if exc.code in (401, 403):
                    return
            except Exception as exc:
                if on_state:
                    on_state(False, str(exc))
            stop.wait(reconnect)
