"""Серверное ядро Sentinel без GTK: фоновые службы и шина событий.

Работает в ``sentinel-server`` (и может использоваться любым другим
процессом без графики):

* подписка на поток событий ISAPI каждого физического Hikvision-устройства;
* правила тревог из общей базы: запись тревоги, снимок (ISAPI), запись (FFmpeg),
  тревожный выход с автозакрытием, PTZ-пресет, email;
* резервная запись по расписанию (те же планы, что в клиенте);
* проверка состояния устройств, синхронизация времени;
* периодическая синхронизация аккаунтов Hik-Connect и перенос их состояния в каталог;
* обслуживание хранилища (сроки хранения событий, тревог, журнала, записей).

Все изменения публикуются в ``EventBus`` — оттуда их получают подключённые
клиенты (Server-Sent Events).
"""
from __future__ import annotations

import collections
import logging
import queue
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import sentinel_alarms as alarms
import sentinel_hikconnect as hikconnect
import sentinel_layouts as layouts
import sentinel_people as people
import sentinel_recording as recording
import sentinel_storage as storage
from sentinel_protocols import (FfmpegJob, authenticated_opener, build_time_xml, iter_alert_payloads,
                                parse_device_info)

log = logging.getLogger("sentinel.engine")

ALARM_READ_TIMEOUT = 45
RECONNECT_MIN, RECONNECT_MAX = 2, 60
DEDUP_SECONDS = 10


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def endpoint_key(device):
    return device["host"], int(device.get("http_port", 80))


def public_device(device):
    """Устройство для API: без секретов."""
    keys = ("id", "name", "host", "host_name", "sdk_port", "http_port", "rtsp_port", "channel", "stream",
            "protocol", "group_id", "serial_no", "device_model", "firmware", "category", "status",
            "status_checked_at", "door_count", "alarm_outputs", "cloud_serial", "area")
    return {k: device.get(k) for k in keys if k in device}


def rtsp_url(device, credentials, stream=None):
    """RTSP-адрес живого потока (Hikvision или сохранённый пользовательский RTSP)."""
    user = urllib.parse.quote(credentials.get("username", ""), safe="")
    password = urllib.parse.quote(credentials.get("password", ""), safe="")
    if not user:
        raise ValueError("Для устройства не сохранено имя пользователя")
    auth = f"{user}:{password}@"
    if device["protocol"] == "hikvision":
        suffix = layouts.STREAM_SUFFIX.get(stream or device.get("stream") or "main", 1)
        return (f"rtsp://{auth}{device['host']}:{device['rtsp_port']}/Streaming/Channels/"
                f"{int(device['channel']) * 100 + suffix}")
    parsed = urllib.parse.urlparse(credentials.get("rtsp_url", ""))
    if parsed.scheme != "rtsp" or parsed.hostname != device["host"]:
        raise ValueError("Сохранённый RTSP-адрес не совпадает с устройством")
    return parsed._replace(netloc=auth + parsed.netloc).geturl()


def isapi_request(device, credentials, path, method="GET", body=None, content_type="application/xml",
                  timeout=12):
    url = f"http://{device['host']}:{device.get('http_port', 80)}{path}"
    opener = authenticated_opener(url, credentials["username"], credentials.get("password", ""))
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": content_type})
    with opener.open(request, timeout=timeout) as response:
        return response.read()


class EventBus:
    """Потокобезопасная рассылка сообщений с короткой историей (для переподключения SSE)."""

    def __init__(self, history=500):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._history = collections.deque(maxlen=history)
        self._seq = 0

    def publish(self, kind, data):
        with self._lock:
            self._seq += 1
            message = {"id": self._seq, "type": kind, "time": now_iso(), "data": data}
            self._history.append(message)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                pass  # медленный клиент пропускает сообщения, а не тормозит сервер
        return message

    def subscribe(self, last_id=0, maxsize=1000):
        q = queue.Queue(maxsize=maxsize)
        with self._lock:
            for message in self._history:
                if message["id"] > last_id:
                    q.put_nowait(message)
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    @property
    def last_id(self):
        with self._lock:
            return self._seq

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


class Engine:
    def __init__(self, bus=None, receive_events=True, status_interval=300, cloud_interval=600,
                 recording_enabled=True, credentials_loader=None, hikconnect_factory=None):
        self.bus = bus or EventBus()
        self.receive_events = receive_events
        self.status_interval = status_interval
        self.cloud_interval = cloud_interval
        self.recording_enabled = recording_enabled
        self.credentials_loader = credentials_loader or storage.load_saved_credentials
        self.hikconnect_factory = hikconnect_factory
        self.started_at = time.time()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads = []
        self.event_workers: dict[tuple, threading.Event] = {}
        self.backup_jobs: dict[int, FfmpegJob] = {}
        self._recent: dict[tuple, float] = {}
        self._credentials: dict[tuple, dict] = {}
        self.cloud_errors: dict[int, str] = {}

    # ------------------------------------------------------------ жизненный цикл
    def start(self):
        for name, interval, func, delay in (
                ("subscriptions", 60, self.sync_event_subscriptions, 0),
                ("status", self.status_interval, self.check_devices, 5),
                ("cloud", self.cloud_interval, self.sync_cloud_accounts, 10),
                ("recorder", 30, self.recorder_tick, 15),
                ("time-sync", 600, self.time_sync_tick, 30),
                ("maintenance", 3600, self.maintenance_tick, 60)):
            thread = threading.Thread(target=self._loop, args=(name, interval, func, delay), daemon=True,
                                      name=f"sentinel-{name}")
            thread.start()
            self._threads.append(thread)
        log.info("Службы запущены")

    def stop(self):
        self._stop.set()
        with self._lock:
            for stop in self.event_workers.values():
                stop.set()
            self.event_workers.clear()
            for job in self.backup_jobs.values():
                job.stop()
            self.backup_jobs.clear()

    def _loop(self, name, interval, func, delay):
        if self._stop.wait(delay):
            return
        while not self._stop.is_set():
            try:
                func()
            except Exception:   # служба не должна падать из-за одного сбоя
                log.exception("Служба %s завершилась с ошибкой", name)
            if self._stop.wait(interval):
                return

    # ------------------------------------------------------------ устройства
    def devices(self):
        return storage.load_devices()

    def endpoints(self):
        result = {}
        for device in self.devices():
            if device.get("protocol") == "hikvision":
                result.setdefault(endpoint_key(device), device)
        return result

    def credentials_for(self, device, cached=True):
        key = endpoint_key(device)
        if cached and key in self._credentials:
            return self._credentials[key]
        try:
            credentials = self.credentials_loader(device)
        except Exception as exc:
            log.warning("Нет доступа к паролю %s:%s: %s", key[0], key[1], exc)
            credentials = None
        if credentials:
            self._credentials[key] = credentials
        return credentials

    def forget_credentials(self, device=None):
        if device is None:
            self._credentials.clear()
        else:
            self._credentials.pop(endpoint_key(device), None)

    def set_status(self, host, http_port, status, info=None):
        stamp = now_iso()
        with storage.database() as db:
            before = {r["status"] for r in db.execute(
                "SELECT status FROM devices WHERE host=? AND http_port=?", (host, http_port))}
            db.execute("UPDATE devices SET status=?, status_checked_at=? WHERE host=? AND http_port=?",
                       (status, stamp, host, http_port))
            info = info or {}
            if info.get("firmwareVersion"):
                db.execute("UPDATE devices SET firmware=? WHERE host=? AND http_port=?",
                           (info["firmwareVersion"], host, http_port))
            if info.get("serialNumber"):
                db.execute("UPDATE devices SET serial_no=? WHERE host=? AND http_port=? AND serial_no=''",
                           (info["serialNumber"], host, http_port))
        if before != {status}:
            self.bus.publish("device_status", {"host": host, "http_port": http_port, "status": status})

    def probe_status(self, device):
        credentials = self.credentials_for(device)
        info = {}
        try:
            if credentials:
                info = parse_device_info(isapi_request(device, credentials, "/ISAPI/System/deviceInfo", timeout=6))
            else:
                with socket.create_connection(endpoint_key(device), timeout=3):
                    pass
            return "online", info
        except urllib.error.HTTPError:
            return "online", info
        except Exception:
            return "offline", info

    def check_devices(self, devices=None):
        endpoints = {}
        for device in devices or self.devices():
            if device.get("protocol") == "hikvision":
                endpoints.setdefault(endpoint_key(device), device)
        results = []
        for key, device in endpoints.items():
            if self._stop.is_set():
                break
            status, info = self.probe_status(device)
            self.set_status(key[0], key[1], status, info)
            results.append({"host": key[0], "http_port": key[1], "status": status})
        return results

    # ------------------------------------------------------------ события устройств
    def sync_event_subscriptions(self):
        enabled = self.receive_events and storage.get_setting("receive_events", "1") == "1"
        wanted = self.endpoints() if enabled else {}
        with self._lock:
            for key in list(self.event_workers):
                if key not in wanted:
                    self.event_workers.pop(key).set()
            for key, device in wanted.items():
                if key in self.event_workers:
                    continue
                credentials = self.credentials_for(device)
                if not credentials:
                    continue
                stop = threading.Event()
                self.event_workers[key] = stop
                threading.Thread(target=self._event_worker, args=(device, dict(credentials), stop), daemon=True,
                                 name=f"events-{key[0]}").start()
        return len(self.event_workers)

    def _event_worker(self, device, credentials, stop):
        url = f"http://{device['host']}:{device.get('http_port', 80)}/ISAPI/Event/notification/alertStream"
        delay = RECONNECT_MIN
        offline = False
        while not stop.is_set() and not self._stop.is_set():
            response = None
            try:
                opener = authenticated_opener(url, credentials["username"], credentials.get("password", ""))
                response = opener.open(urllib.request.Request(url, headers={"Accept": "multipart/mixed"}),
                                       timeout=ALARM_READ_TIMEOUT)
                delay = RECONNECT_MIN
                if offline:
                    offline = False
                    self.set_status(device["host"], int(device.get("http_port", 80)), "online")
                for payload in iter_alert_payloads(response, stop):
                    event = alarms.parse_event_xml(payload, device)
                    if event:
                        self.handle_event(event)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    log.warning("%s: поток событий недоступен (HTTP %s)", device["host"], exc.code)
                    self.bus.publish("notice", {"message": f"{device['host']}: события недоступны "
                                                           f"(HTTP {exc.code})", "error": True})
                    with self._lock:
                        if self.event_workers.get(endpoint_key(device)) is stop:
                            self.event_workers.pop(endpoint_key(device), None)
                    self.forget_credentials(device)
                    return
            except Exception:
                if not offline and not stop.is_set():
                    offline = True
                    self.set_status(device["host"], int(device.get("http_port", 80)), "offline")
                    self.handle_event({"time": now_iso(), "device_id": device["id"],
                                       "device_name": device.get("host_name") or device["host"], "channel": "",
                                       "raw_type": "device_offline", "event_type": "offline", "category": "event",
                                       "state": "active", "plate": "", "details": {}})
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
            if stop.wait(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX)

    def handle_event(self, event):
        """Журнал → правила → тревоги и действия → рассылка клиентам. Возвращает id тревог."""
        key = (event.get("device_id"), str(event.get("channel")), event.get("event_type"), event.get("plate"))
        stamp = time.monotonic()
        with self._lock:
            if event.get("event_type") != "user" and stamp - self._recent.get(key, -1e9) < DEDUP_SECONDS:
                self._recent[key] = stamp
                return []
            self._recent[key] = stamp
            if len(self._recent) > 2000:
                for stale in [k for k, t in self._recent.items() if stamp - t > DEDUP_SECONDS]:
                    self._recent.pop(stale, None)
        if event.get("plate"):
            lists = people.vehicle_index().get(alarms.plate_key(event["plate"]), [])
            if lists:
                names = {v["id"]: v["name"] for v in people.load_vehicle_lists()}
                event["matched_list"] = ", ".join(names.get(i, str(i)) for i in lists)
        event_id = alarms.store_event(event) if event.get("state") != "inactive" else None
        self.bus.publish("event", dict(event, id=event_id, title=alarms.event_title(event["event_type"])))
        with storage.database() as db:
            holidays = [dict(r) for r in db.execute("SELECT * FROM holidays")]
        records = []
        for rule in alarms.match_rules(event, alarms.load_rules(True), people.vehicle_index(), people.get_schedule,
                                       holidays=holidays):
            record_id = alarms.create_alarm_record(rule, event, event_id)
            records.append(record_id)
            self._run_linkage(rule, event, record_id)
            self.bus.publish("alarm", alarms.get_alarm_record(record_id))
        return records

    def _device(self, device_id):
        return next((d for d in self.devices() if d["id"] == device_id), None)

    def _run_linkage(self, rule, event, record_id):
        picture = None
        for action, params in alarms.linkage_plan(rule, event):
            try:
                if action == "capture":
                    picture = self.capture_snapshot(event.get("device_id"), record_id)
                elif action == "record":
                    self.record_clip(event.get("device_id"), params["seconds"])
                elif action == "alarm_output":
                    self.trigger_alarm_output(params["device_id"], params["output"], params["auto_close"])
                elif action == "ptz":
                    device = self._device(params.get("device_id"))
                    credentials = self.credentials_for(device) if device else None
                    if device and credentials:
                        path = (f"/ISAPI/PTZCtrl/channels/{device['channel']}/presets/"
                                f"{int(params.get('preset', 1))}/goto")
                        threading.Thread(target=self._quiet_isapi, args=(device, credentials, path, "PUT"),
                                         daemon=True).start()
                elif action == "email":
                    self._send_email(rule, event, picture, params["to"])
                # «popup» на сервере — это рассылка тревоги клиентам (publish выше)
            except Exception as exc:
                log.warning("Действие %s правила %s: %s", action, rule.get("name"), exc)
                self.bus.publish("notice", {"message": f"Действие «{action}» правила «{rule['name']}»: {exc}",
                                            "error": True})

    def _quiet_isapi(self, device, credentials, path, method, body=None):
        try:
            isapi_request(device, credentials, path, method, body)
        except Exception as exc:
            log.warning("%s %s: %s", device["host"], path, exc)

    def capture_snapshot(self, device_id, record_id=None):
        device = self._device(device_id)
        credentials = self.credentials_for(device) if device else None
        if not device or not credentials or device["protocol"] != "hikvision":
            return None
        data = isapi_request(device, credentials, f"/ISAPI/Streaming/channels/{int(device['channel']) * 100 + 1}"
                                                  "/picture", timeout=10)
        folder = recording.ensure_private_dir(storage.DATA / "alarms")
        path = folder / datetime.now().strftime(f"alarm_{record_id or 0}_%Y%m%d_%H%M%S.jpg")
        path.write_bytes(data)
        if record_id:
            alarms.update_alarm_record(record_id, picture_path=str(path))
        return str(path)

    def record_clip(self, device_id, seconds):
        device = self._device(device_id)
        credentials = self.credentials_for(device) if device else None
        ffmpeg = shutil.which("ffmpeg")
        if not device or not credentials or not ffmpeg:
            return None
        folder = recording.ensure_private_dir(storage.DATA / "alarms")
        output = folder / datetime.now().strftime(f"alarm_{device_id}_%Y%m%d_%H%M%S.mkv")
        job = FfmpegJob(ffmpeg, rtsp_url(dict(device, stream="main"), credentials), str(output))
        timer = threading.Timer(max(5, int(seconds)), job.stop)
        timer.daemon = True
        timer.start()
        return str(output)

    def trigger_alarm_output(self, device_id, output, auto_close=0):
        device = self._device(device_id)
        if device is None:
            raise ValueError("Устройство тревожного выхода удалено")
        credentials = self.credentials_for(device)
        if not credentials:
            raise ValueError(f"Нет сохранённого пароля для {device['host']}")
        path = alarms.alarm_output_path(output)
        isapi_request(device, credentials, path, "PUT", alarms.alarm_output_xml(True))
        alarms.log_operation("server", f"Тревожный выход {output} включён", device["host"])
        if auto_close:
            timer = threading.Timer(int(auto_close), self._quiet_isapi,
                                    args=(device, credentials, path, "PUT", alarms.alarm_output_xml(False)))
            timer.daemon = True
            timer.start()

    def _send_email(self, rule, event, picture, recipients):
        config = storage.get_json_setting("smtp", dict(alarms.DEFAULT_SMTP))
        try:
            secret = storage.load_saved_credentials({"host": "smtp:" + config.get("server", ""), "http_port": 0})
            password = secret.get("password") if secret else None
        except Exception:
            password = None
        subject = f"[Sentinel] {rule['name']}: {event.get('device_name', '')}"
        threading.Thread(target=alarms.send_email,
                         args=(config, password, recipients, subject, alarms.email_body(rule, event), picture),
                         daemon=True).start()

    # ------------------------------------------------------------ резервная запись и обслуживание
    def recorder_tick(self):
        if not self.recording_enabled:
            return
        ffmpeg = shutil.which("ffmpeg")
        plans = {p.device_id: p for p in recording.enabled_plans()}
        moment = datetime.now().astimezone()
        root = recording.storage_root()
        with self._lock:
            for device_id, job in list(self.backup_jobs.items()):
                plan = plans.get(device_id)
                if plan is None or not recording.should_record(plan, moment) or job.poll() is not None:
                    job.stop()
                    self.backup_jobs.pop(device_id, None)
            if not ffmpeg:
                return
            devices = {d["id"]: d for d in self.devices()}
            for device_id, plan in plans.items():
                if device_id in self.backup_jobs or not recording.should_record(plan, moment):
                    continue
                device = devices.get(device_id)
                credentials = self.credentials_for(device) if device else None
                if not device or not credentials:
                    continue
                try:
                    folder = recording.ensure_private_dir(recording.camera_folder(root, device_id))
                    self.backup_jobs[device_id] = FfmpegJob(
                        ffmpeg, rtsp_url(dict(device, stream=plan.stream), credentials), str(folder),
                        command=lambda list_path, f=folder: recording.segment_command(ffmpeg, list_path, f))
                except (OSError, ValueError) as exc:
                    log.warning("Резервная запись %s: %s", device["name"], exc)

    def time_sync_tick(self):
        settings = storage.get_json_setting("time_sync", {"enabled": False, "hours": 24})
        if not settings.get("enabled"):
            return
        last = float(storage.get_setting("server_last_time_sync", "0") or 0)
        if time.time() - last < int(settings.get("hours", 24)) * 3600:
            return
        storage.set_setting("server_last_time_sync", time.time())
        for device in self.endpoints().values():
            credentials = self.credentials_for(device)
            if credentials:
                self._quiet_isapi(device, credentials, "/ISAPI/System/time", "PUT",
                                  build_time_xml(datetime.now().astimezone()))

    def maintenance_tick(self):
        settings = recording.retention_settings()
        alarms.purge_older_than(settings["events_days"], settings["alarms_days"], settings["log_days"])
        if self.recording_enabled:
            recording.cleanup(recording.storage_root(), recording.enabled_plans(),
                              min_free_bytes=int(settings.get("min_free_gb", 5)) * 1024 ** 3)

    # ------------------------------------------------------------ Hik-Connect
    def sync_cloud_accounts(self, account_id=None, password=None):
        """Обновить устройства аккаунтов Hik-Connect. Возвращает {account_id: число устройств | ошибка}."""
        result = {}
        accounts = ([hikconnect.get_account(account_id)] if account_id is not None
                    else [a for a in hikconnect.load_accounts() if a["auto_sync"]])
        for account in accounts:
            if not account:
                continue
            try:
                devices = hikconnect.sync_account(account["id"], password, self.hikconnect_factory)
                result[account["id"]] = len(devices)
                self.cloud_errors.pop(account["id"], None)
            except hikconnect.HikConnectError as exc:
                result[account["id"]] = str(exc)
                self.cloud_errors[account["id"]] = str(exc)
                if account_id is not None:
                    raise
        if result:
            hikconnect.apply_cloud_status()
            self.bus.publish("cloud_sync", {"accounts": result})
        return result

    # ------------------------------------------------------------ сводка
    def summary(self):
        devices = self.devices()
        physical = {endpoint_key(d) for d in devices}
        return {
            "uptime_seconds": int(time.time() - self.started_at),
            "channels": len(devices),
            "devices": len(physical),
            "online": len({endpoint_key(d) for d in devices if d.get("status") == "online"}),
            "event_subscriptions": len(self.event_workers),
            "backup_recordings": sum(1 for j in self.backup_jobs.values() if j.poll() is None),
            "unacknowledged_alarms": alarms.unacknowledged_count(),
            "clients": self.bus.subscriber_count(),
            "cloud_accounts": len(hikconnect.load_accounts()),
        }
