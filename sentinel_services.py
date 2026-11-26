"""Фоновые службы главного окна (примесь к SentinelWindow).

* Подписка на поток событий ISAPI всех устройств Hikvision с сохранёнными паролями.
* Правила тревог: запись тревоги, всплывающее окно, снимок, тревожный выход с
  автозакрытием, PTZ-пресет, email.
* Резервная запись камер на этот ПК по расписанию + удаление по сроку хранения.
* Проверка состояния устройств (в сети / не в сети), команды дверей.

Сетевые операции выполняются в рабочих потоках; GTK трогается только через GLib.idle_add.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from gi.repository import GLib

import sentinel_alarms as alarms
import sentinel_arming as arming
import sentinel_archive as archive
import sentinel_people as people
import sentinel_recording as recording
import sentinel_storage as storage
from sentinel_protocols import FfmpegJob, authenticated_opener, iter_alert_payloads, parse_device_info

ALARM_READ_TIMEOUT = 45
RECONNECT_MIN, RECONNECT_MAX = 2, 60


class ServicesMixin:
    def _start_services(self):
        self.event_workers = {}      # (host, http_port) -> threading.Event
        self.backup_jobs = {}        # device_id -> FfmpegJob
        self.output_timers = []
        self.last_activity = time.monotonic()
        GLib.timeout_add_seconds(3, self._initial_services)
        GLib.timeout_add_seconds(30, self._recorder_tick)
        GLib.timeout_add_seconds(30, self._auto_lock_tick)
        GLib.timeout_add_seconds(3600, self._maintenance_tick)
        GLib.timeout_add_seconds(300, self._status_tick)
        GLib.timeout_add_seconds(600, self._time_sync_tick)
        self.last_time_sync = 0.0

    def _initial_services(self):
        if getattr(self, "_closing", False):
            return False
        policy_login = storage.get_json_setting("security", {}).get("require_login")
        import sentinel_security as security
        if policy_login and security.has_users():
            self._show_lock_screen("login")
        self.sync_event_subscriptions()
        self._recorder_tick()
        self._maintenance_tick()
        self._refresh_alarm_panel()
        self._refresh_event_panel()
        self.check_all_devices(quiet=True)
        try:
            import sentinel_cloud_ui
            sentinel_cloud_ui.restore_server_connection(self)
        except Exception as exc:   # автоподключение не должно мешать запуску
            self.notify(f"Сервер Sentinel: {exc}", error=True)
        return False

    def _stop_services(self):
        link = self.__dict__.get("server_link")
        if link is not None and link.stop is not None:
            link.stop.set()
        for stop in getattr(self, "event_workers", {}).values():
            stop.set()
        self.event_workers = {}
        for job in list(getattr(self, "backup_jobs", {}).values()):
            job.stop()
        self.backup_jobs = {}

    # ================================================================== события
    def _endpoints(self):
        endpoints = {}
        for device in self.devices:
            if device.get("protocol") == "hikvision":
                endpoints.setdefault((device["host"], int(device.get("http_port", 80))), device)
        return endpoints

    def sync_event_subscriptions(self):
        """Запустить/остановить потоки событий по настройке «Получать события»."""
        enabled = storage.get_setting("receive_events", "1") == "1"
        wanted = self._endpoints() if enabled else {}
        for key in list(self.event_workers):
            if key not in wanted:
                self.event_workers.pop(key).set()
        for key, device in wanted.items():
            if key in self.event_workers:
                continue
            credentials = self.session_credentials.get(device["id"])
            if not credentials:
                continue  # пароль появится после первого открытия камеры/предзагрузки хранилища
            stop = threading.Event()
            self.event_workers[key] = stop
            threading.Thread(target=self._event_worker, args=(device, dict(credentials), stop), daemon=True).start()
        self._set_event_status()

    def _set_event_status(self):
        if hasattr(self, "event_status"):
            self.event_status.set_text(f"Подписано устройств: {len(self.event_workers)}")

    def _event_worker(self, device, credentials, stop):
        url = f"http://{device['host']}:{device.get('http_port', 80)}/ISAPI/Event/notification/alertStream"
        delay = RECONNECT_MIN
        offline_reported = False
        while not stop.is_set():
            response = None
            try:
                opener = authenticated_opener(url, credentials["username"], credentials.get("password", ""))
                response = opener.open(urllib.request.Request(url, headers={"Accept": "multipart/mixed"}),
                                       timeout=ALARM_READ_TIMEOUT)
                delay = RECONNECT_MIN
                if offline_reported:
                    GLib.idle_add(self._set_device_status, device["host"], "online")
                    offline_reported = False
                for payload in iter_alert_payloads(response, stop):
                    event = alarms.parse_event_xml(payload, device)
                    if event:
                        GLib.idle_add(self.handle_event, event)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    GLib.idle_add(self.notify, f"{device['host']}: события недоступны (HTTP {exc.code})", True)
                    return
            except Exception:
                if not offline_reported and not stop.is_set():
                    offline_reported = True
                    GLib.idle_add(self._device_went_offline, device)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
            if stop.wait(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX)

    def _device_went_offline(self, device):
        if _cloud_online().get(device.get("cloud_serial")):
            return False    # регистратор из Hik-Connect недоступен напрямую, но в облаке он в сети
        self._set_device_status(device["host"], "offline")
        event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "device_id": device["id"],
                 "device_name": device.get("host_name") or device["host"], "channel": "", "raw_type": "device_offline",
                 "event_type": "offline", "category": "event", "state": "active", "plate": "", "details": {}}
        self.handle_event(event)
        return False

    DEDUP_SECONDS = 10

    def handle_event(self, event):
        """Главный поток: журнал → правила → тревоги и действия."""
        if getattr(self, "_closing", False):
            return False
        # Пока детектор активен, устройство повторяет событие раз в секунду — склеиваем повторы.
        key = (event.get("device_id"), str(event.get("channel")), event.get("event_type"), event.get("plate"))
        now = time.monotonic()
        recent = self.__dict__.setdefault("_recent_events", {})
        if event.get("event_type") != "user" and now - recent.get(key, -1e9) < self.DEDUP_SECONDS:
            recent[key] = now
            return False
        recent[key] = now
        if len(recent) > 2000:
            for stale in [k for k, t in recent.items() if now - t > self.DEDUP_SECONDS]:
                recent.pop(stale, None)
        if event.get("plate"):
            index = people.vehicle_index()
            lists = index.get(alarms.plate_key(event["plate"]), [])
            if lists:
                names = {v["id"]: v["name"] for v in people.load_vehicle_lists()}
                event["matched_list"] = ", ".join(names.get(i, str(i)) for i in lists)
        if event.get("state") != "inactive":
            event_id = alarms.store_event(event)
        else:
            event_id = None
        self.alarm_events.insert(0, {"device_id": event.get("device_id"), "device": event.get("device_name", ""),
                                     "type": alarms.event_title(event["event_type"]), "state": event.get("state", ""),
                                     "channel": event.get("channel", ""), "time": event["time"]})
        del self.alarm_events[200:]
        with storage.database() as db:
            holidays = [dict(r) for r in db.execute("SELECT * FROM holidays")]
        rules = alarms.match_rules(event, alarms.load_rules(True), people.vehicle_index(), people.get_schedule,
                                   holidays=holidays) if arming.event_raises_alarms(event) else []
        if event.get("state") != "inactive":
            self._flash_map_device(event.get("device_id"), alarm=bool(rules))
            if event.get("event_type") == "access" and hasattr(self, "_on_access_event"):
                self._on_access_event(event)
            if str(event.get("event_type", "")).startswith("anpr") and hasattr(self, "_on_parking_plate"):
                self._on_parking_plate(event)
        for rule in rules:
            record_id = alarms.create_alarm_record(rule, event, event_id)
            self._run_linkage(rule, event, record_id)
        if rules and hasattr(self, "wall_show_alarm") and not any(
                (r.get("linkage") or {}).get("wall") for r in rules):
            self.wall_show_alarm(device_id=event.get("device_id"), any_alarm=True)
        self._schedule_panel_refresh(alarms_changed=bool(rules))
        if event.get("device_id") and hasattr(self, "timeline_cache"):
            self.timeline_cache = {k: v for k, v in self.timeline_cache.items() if k[0] != event["device_id"]}
        return False

    def _schedule_panel_refresh(self, alarms_changed=False):
        """Перерисовка правой панели не чаще раза в секунду."""
        self.__dict__["_pending_alarm_refresh"] = self.__dict__.get("_pending_alarm_refresh", False) or alarms_changed
        if self.__dict__.get("_panel_refresh_source"):
            return

        def run():
            self.__dict__["_panel_refresh_source"] = 0
            self._refresh_event_panel()
            if self.__dict__.pop("_pending_alarm_refresh", False):
                self._refresh_alarm_panel()
            self._update_status_bar()
            return False

        self.__dict__["_panel_refresh_source"] = GLib.timeout_add(1000, run)

    # ------------------------------------------------------------------ действия
    def _run_linkage(self, rule, event, record_id):
        picture = None
        for action, params in alarms.linkage_plan(rule, event):
            try:
                if action == "capture":
                    picture = self._capture_for_event(event, record_id)
                elif action == "record":
                    self._record_for_event(event, params["seconds"])
                elif action == "alarm_output":
                    self.trigger_alarm_output(params["device_id"], params["output"], params["auto_close"], quiet=True)
                elif action == "ptz":
                    self._goto_preset_for_event(params)
                elif action == "wall":
                    self.wall_show_alarm(params["wall_id"], params["device_id"])
                elif action == "email":
                    self._send_alarm_email(rule, event, picture, params["to"])
                elif action == "popup" and storage.get_setting("alarm_popup", "1") == "1":
                    if self.alarm_details_window is None:
                        self.show_alarm_details(record_id)
                    if storage.get_setting("alarm_sound", "1") == "1":
                        display = self.get_display()
                        if display:
                            display.beep()
            except Exception as exc:
                self.notify(f"Действие «{action}» правила «{rule['name']}»: {exc}", error=True)

    def _slot_for_device(self, device_id):
        return next((slot for slot, st in self.streams.items() if st["base_device"]["id"] == device_id), None)

    def _capture_for_event(self, event, record_id):
        slot = self._slot_for_device(event.get("device_id"))
        if slot is None or slot >= len(self.video_slots):
            return None
        texture = self.video_slots[slot]["picture"].get_paintable()
        if texture is None or not hasattr(texture, "save_to_png"):
            return None
        folder = self.capture_folder / "alarms"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / datetime.now().strftime(f"alarm_{record_id}_%Y%m%d_%H%M%S.png")
        if texture.save_to_png(str(path)):
            alarms.update_alarm_record(record_id, picture_path=str(path))
            return str(path)
        return None

    def _record_for_event(self, event, seconds):
        device = next((d for d in self.devices if d["id"] == event.get("device_id")), None)
        credentials = self.session_credentials.get(device["id"]) if device else None
        ffmpeg = shutil.which("ffmpeg")
        if not device or not credentials or not ffmpeg:
            return
        folder = self.record_folder / "alarms"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / datetime.now().strftime(f"alarm_{device['id']}_%Y%m%d_%H%M%S.mkv")
        job = FfmpegJob(ffmpeg, self._stream_url(dict(device, stream="main"), credentials), str(output))
        GLib.timeout_add_seconds(max(5, int(seconds)), lambda: job.stop() or False)

    def _goto_preset_for_event(self, params):
        device = next((d for d in self.devices if d["id"] == params.get("device_id")), None)
        credentials = self.session_credentials.get(device["id"]) if device else None
        if device and credentials:
            path = f"/ISAPI/PTZCtrl/channels/{device['channel']}/presets/{int(params.get('preset', 1))}/goto"
            threading.Thread(target=self._isapi_quiet, args=(device, credentials, path, "PUT", None), daemon=True).start()

    def _send_alarm_email(self, rule, event, picture, recipients):
        config = storage.get_json_setting("smtp", dict(alarms.DEFAULT_SMTP))
        password = None
        try:
            from sentinel_storage import load_saved_credentials
            secret = load_saved_credentials({"host": "smtp:" + config.get("server", ""), "http_port": 0})
            password = secret.get("password") if secret else None
        except Exception:
            password = None
        subject = f"[Sentinel] {rule['name']}: {event.get('device_name', '')}"
        body = alarms.email_body(rule, event)

        def worker():
            try:
                alarms.send_email(config, password, recipients, subject, body, picture)
            except Exception as exc:
                GLib.idle_add(self.notify, f"Email не отправлен: {exc}", True)

        threading.Thread(target=worker, daemon=True).start()

    def trigger_alarm_output(self, device_id, output, auto_close=0, quiet=False):
        device = next((d for d in self.devices if d["id"] == device_id), None)
        if device is None:
            raise ValueError("Устройство тревожного выхода удалено")
        credentials = self._get_device_credentials(device)
        if not credentials:
            raise ValueError(f"Нет сохранённого пароля для {device['host']}")
        path = alarms.alarm_output_path(output)

        def worker():
            try:
                self._isapi_request(device, credentials, path, "PUT", alarms.alarm_output_xml(True))
                if not quiet:
                    GLib.idle_add(self.notify, f"Тревожный выход {output} на {device['host']} включён")
                alarms.log_operation(self.session.username, f"Тревожный выход {output} включён", device["host"])
            except Exception as exc:
                GLib.idle_add(self.notify, f"Тревожный выход {output}: {exc}", True)
                return
            if auto_close:
                time.sleep(int(auto_close))
                try:
                    self._isapi_request(device, credentials, path, "PUT", alarms.alarm_output_xml(False))
                except Exception as exc:
                    GLib.idle_add(self.notify, f"Не удалось выключить выход {output}: {exc}", True)

        threading.Thread(target=worker, daemon=True).start()

    def _isapi_quiet(self, device, credentials, path, method, body):
        try:
            self._isapi_request(device, credentials, path, method, body)
        except Exception as exc:
            GLib.idle_add(self.notify, f"{device['host']}: {exc}", True)

    # ================================================================== поиск записей NVR
    def isapi_search_recordings(self, device, credentials, start, end, max_pages=10, record_type="all"):
        """[(начало, конец, playbackURI, тип записи)] за интервал (с постраничной выдачей)."""
        url = f"http://{device['host']}:{device.get('http_port', 80)}/ISAPI/ContentMgmt/search"
        opener = authenticated_opener(url, credentials["username"], credentials.get("password", ""))

        def post(body):
            request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/xml"})
            with opener.open(request, timeout=12) as response:
                return response.read()
        items = archive.search_all(post, archive.track_id(device["channel"]), start, end, record_type, max_pages)
        return [(i["start"], i["end"], i["uri"], i["kind"]) for i in items]

    # ================================================================== резервная запись
    def _recorder_tick(self):
        if getattr(self, "_closing", False):
            return False
        ffmpeg = shutil.which("ffmpeg")
        plans = {p.device_id: p for p in recording.enabled_plans()}
        now = datetime.now().astimezone()
        root = recording.storage_root()
        for device_id, job in list(self.backup_jobs.items()):
            plan = plans.get(device_id)
            if plan is None or not recording.should_record(plan, now) or job.poll() is not None:
                job.stop()
                self.backup_jobs.pop(device_id, None)
        if not ffmpeg:
            return True
        devices = {d["id"]: d for d in self.devices}
        for device_id, plan in plans.items():
            if device_id in self.backup_jobs or not recording.should_record(plan, now):
                continue
            device = devices.get(device_id)
            credentials = self.session_credentials.get(device_id) if device else None
            if not device or not credentials:
                continue
            try:
                uri = self._stream_url(dict(device, stream=plan.stream), credentials)
                folder = recording.ensure_private_dir(recording.camera_folder(root, device_id))
                self.backup_jobs[device_id] = FfmpegJob(
                    ffmpeg, uri, str(folder), on_error=self._ffmpeg_failed,
                    command=lambda list_path, f=folder: recording.segment_command(ffmpeg, list_path, f))
            except (OSError, ValueError) as exc:
                self.notify(f"Резервная запись {device['name']}: {exc}", error=True)
        return True

    def recording_status(self, device_id):
        job = self.backup_jobs.get(device_id)
        return "Идёт запись" if job and job.poll() is None else "Остановлена"

    def _maintenance_tick(self):
        if getattr(self, "_closing", False):
            return False
        settings = recording.retention_settings()

        def worker():
            try:
                alarms.purge_older_than(settings["events_days"], settings["alarms_days"], settings["log_days"])
                recording.cleanup(recording.storage_root(), recording.enabled_plans() +
                                  [recording.load_plan(d["id"]) for d in self.devices],
                                  min_free_bytes=int(settings.get("min_free_gb", 5)) * 1024 ** 3)
            except Exception as exc:
                GLib.idle_add(self.notify, f"Обслуживание хранилища: {exc}", True)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _time_sync_tick(self):
        if getattr(self, "_closing", False):
            return False
        settings = storage.get_json_setting("time_sync", {"enabled": False, "hours": 24})
        if settings.get("enabled") and time.monotonic() - self.last_time_sync >= int(settings["hours"]) * 3600:
            self.last_time_sync = time.monotonic()
            for device in self._endpoints().values():
                credentials = self.session_credentials.get(device["id"])
                if credentials:
                    threading.Thread(target=self._sync_time_worker, args=(device, credentials), daemon=True).start()
        return True

    # ================================================================== состояние устройств
    def _status_tick(self):
        if getattr(self, "_closing", False):
            return False
        self.check_all_devices(quiet=True)
        return True

    def check_all_devices(self, devices=None, quiet=False):
        endpoints = {}
        for device in devices or self.devices:
            if device.get("protocol") == "hikvision":
                endpoints.setdefault((device["host"], int(device.get("http_port", 80))), device)
        items = [(device, self.session_credentials.get(device["id"])) for device in endpoints.values()]
        if not items:
            return
        threading.Thread(target=self._status_worker, args=(items, quiet), daemon=True).start()

    def _status_worker(self, items, quiet):
        if any(device.get("cloud_serial") for device, _c in items):
            _refresh_cloud_status()
        results = []
        for device, credentials in items:
            status, info = "offline", {}
            try:
                if credentials:
                    info = parse_device_info(self._isapi_request(device, credentials, "/ISAPI/System/deviceInfo"))
                    status = "online"
                else:
                    import socket
                    with socket.create_connection((device["host"], int(device.get("http_port", 80))), timeout=3):
                        status = "online"
            except urllib.error.HTTPError:
                status = "online"  # устройство ответило — оно в сети
            except Exception:
                status = "offline"
            results.append((device, status, info))
        GLib.idle_add(self._status_done, results, quiet)

    def _status_done(self, results, quiet):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        cloud_online = _cloud_online()
        results = [(device, "online" if status == "offline" and cloud_online.get(device.get("cloud_serial")) else status,
                    info) for device, status, info in results]
        with storage.database() as db:
            for device, status, info in results:
                db.execute("UPDATE devices SET status=?, status_checked_at=? WHERE host=? AND http_port=?",
                           (status, stamp, device["host"], int(device.get("http_port", 80))))
                if info.get("firmwareVersion"):
                    db.execute("UPDATE devices SET firmware=? WHERE host=? AND http_port=?",
                               (info["firmwareVersion"] + (" build " + info["firmwareReleasedDate"]
                                                           if info.get("firmwareReleasedDate") else ""),
                                device["host"], int(device.get("http_port", 80))))
                if info.get("serialNumber"):
                    db.execute("UPDATE devices SET serial_no=? WHERE host=? AND http_port=? AND serial_no=''",
                               (info["serialNumber"], device["host"], int(device.get("http_port", 80))))
        offline = sum(1 for _d, s, _i in results if s == "offline")
        self.refresh()
        if not quiet:
            self.notify(f"Проверено устройств: {len(results)}, не в сети: {offline}")
        return False

    def _set_device_status(self, host, status):
        with storage.database() as db:
            db.execute("UPDATE devices SET status=?, status_checked_at=? WHERE host=?",
                       (status, datetime.now().astimezone().isoformat(timespec="seconds"), host))
        for device in self.devices:
            if device["host"] == host:
                device["status"] = status
        self._fill_resource_list()
        return False

    # ================================================================== двери
    def door_command(self, device, door, command, quiet=False):
        if not self.session.can("access"):
            self.notify("Нет прав на управление дверями", error=True)
            return
        credentials = self._get_device_credentials(device)
        if not credentials:
            self._credentials_dialog("Контроллер · " + device["name"], device,
                                     lambda d, c: (self.session_credentials.__setitem__(d["id"], dict(c)),
                                                   self.door_command(d, door, command, quiet)))
            return
        body = people.door_command_xml(command)

        def worker():
            try:
                self._isapi_request(device, credentials, f"/ISAPI/AccessControl/RemoteControl/door/{door}", "PUT", body)
                alarms.log_operation(self.session.username, f"Дверь {door}: {people.DOOR_TITLES[command]}",
                                     device["host"])
                if not quiet:
                    GLib.idle_add(self.notify, f"Дверь {door}: команда «{people.DOOR_TITLES[command]}» выполнена")
                GLib.timeout_add(800, lambda: self.refresh_door_states([device]) or False)
            except Exception as exc:
                GLib.idle_add(self.notify, f"Дверь {door}: {exc}", True)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_door_states(self, devices=None):
        targets = devices or [d for d in self.devices if d.get("category") == "access"]

        def worker():
            states = {}
            for device in targets:
                credentials = self.session_credentials.get(device["id"])
                if not credentials:
                    continue
                try:
                    payload = self._isapi_request(device, credentials, "/ISAPI/AccessControl/AcsWorkStatus?format=json")
                except Exception:
                    continue
                for door, state in people.parse_door_status(payload).items():
                    states[(device["id"], door)] = state
            GLib.idle_add(self._door_states_loaded, states)

        threading.Thread(target=worker, daemon=True).start()

    def _door_states_loaded(self, states):
        self.door_states.update(states)
        for slot, entry in list(self.slot_resources.items()):
            if entry["kind"] == "door":
                self._place_resource(slot, entry)
        self._update_door_bar()
        if hasattr(self, "access_doors_holder"):
            self._render_access_doors()
        return False

    # ================================================================== применение уровней доступа
    def apply_access_to_devices(self, persons, on_done=None):
        plan = people.apply_plan(persons)
        devices = {d["id"]: d for d in self.devices}
        jobs = []
        for person, device_id, doors in plan:
            device = devices.get(device_id)
            credentials = self._get_device_credentials(device) if device else None
            jobs.append((person, device, credentials, doors, device_id))

        def worker():
            ok = failed = 0
            for person, device, credentials, doors, device_id in jobs:
                if not device or not credentials:
                    people.set_apply_status(person["id"], device_id, "failed", "нет устройства или пароля")
                    failed += 1
                    continue
                try:
                    self._isapi_request(device, credentials, "/ISAPI/AccessControl/UserInfo/SetUp?format=json", "PUT",
                                        json.dumps(people.user_info_payload(person, doors)), "application/json")
                    for card in person.get("cards", []):
                        self._isapi_request(device, credentials, "/ISAPI/AccessControl/CardInfo/SetUp?format=json",
                                            "PUT", json.dumps(people.card_payload(person, card)), "application/json")
                    people.set_apply_status(person["id"], device_id, "applied")
                    ok += 1
                except Exception as exc:
                    people.set_apply_status(person["id"], device_id, "failed", str(exc)[:200])
                    failed += 1
            alarms.log_operation(self.session.username, "Применение уровней доступа", f"{ok} ok / {failed} ошибок")
            GLib.idle_add(self.notify, f"Применено: {ok}, ошибок: {failed}", bool(failed))
            if on_done:
                GLib.idle_add(on_done)

        if not jobs:
            self.notify("Нет назначений: добавьте двери в уровни доступа и назначьте их персонам/отделам")
            return
        threading.Thread(target=worker, daemon=True).start()


def _cloud_online():
    """{серийный номер: в сети ли устройство по данным Hik-Connect}; пусто, если облака нет."""
    try:
        import sentinel_hikconnect as hikconnect
        return hikconnect.cloud_status_map()
    except Exception:
        return {}


CLOUD_STATUS_MAX_AGE = 300      # секунд: чаще состояние из Hik-Connect не запрашиваем


def _refresh_cloud_status():
    """Обновить «в сети» из Hik-Connect по сохранённым сессиям (фоновый поток; ошибки не мешают проверке)."""
    try:
        import sentinel_hikconnect as hikconnect
        now = datetime.now().astimezone()
        for account in hikconnect.load_accounts():
            if not account.get("auto_sync", 1):
                continue
            try:
                last = datetime.fromisoformat(account.get("last_sync") or "")
                if (now - last).total_seconds() < CLOUD_STATUS_MAX_AGE:
                    continue
            except ValueError:
                pass
            try:
                hikconnect.sync_account(account["id"])
            except Exception:
                continue
        hikconnect.apply_cloud_status()
    except Exception:
        pass
