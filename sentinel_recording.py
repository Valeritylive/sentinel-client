"""Резервная запись камер на этот компьютер («Системное хранилище») — без GTK.

Как в HikCentral Lite: регистратор остаётся основным хранилищем, а клиент по
недельному расписанию дублирует выбранные камеры в локальную папку. FFmpeg
пишет 10-минутные сегменты без перекодирования; старые сегменты удаляются по
сроку хранения, а при нехватке места — самые старые первыми (перезапись).

Имена файлов: <root>/<device_id>/<YYYYMMDD>/<YYYYMMDD-HHMMSS>.mkv
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import sentinel_storage as storage
from sentinel_people import full_week, normalize_week, schedule_allows

SEGMENT_SECONDS = 600
FILE_PATTERN = "%Y%m%d-%H%M%S"
DEFAULT_ROOT = Path.home() / "Videos" / "Sentinel" / "storage"
DEFAULT_RETENTION = {"recording_days": 30, "events_days": 90, "alarms_days": 180, "log_days": 365,
                     "min_free_gb": 5}


def storage_root():
    return Path(storage.get_setting("storage_root", str(DEFAULT_ROOT))).expanduser()


def retention_settings():
    return storage.get_json_setting("data_retention", dict(DEFAULT_RETENTION))


@dataclass
class Plan:
    device_id: int
    enabled: bool
    stream: str
    schedule: list
    retention_days: int
    overwrite: bool


def load_plan(device_id):
    with storage.database() as db:
        row = db.execute("SELECT * FROM recording_plans WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return Plan(device_id, False, "sub", full_week(), retention_settings()["recording_days"], True)
    try:
        schedule = normalize_week(json.loads(row["schedule"])) if row["schedule"] else full_week()
    except (ValueError, json.JSONDecodeError):
        schedule = full_week()
    return Plan(device_id, bool(row["enabled"]), row["stream"], schedule, int(row["retention_days"]),
                bool(row["overwrite"]))


def save_plan(plan: Plan):
    if plan.stream not in ("main", "sub", "third"):
        raise ValueError("Неизвестный поток")
    if not 1 <= int(plan.retention_days) <= 3650:
        raise ValueError("Срок хранения: от 1 до 3650 дней")
    with storage.database() as db:
        db.execute("""INSERT INTO recording_plans(device_id,enabled,stream,schedule,retention_days,overwrite)
            VALUES(?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET enabled=excluded.enabled,
            stream=excluded.stream, schedule=excluded.schedule, retention_days=excluded.retention_days,
            overwrite=excluded.overwrite""",
                   (plan.device_id, int(plan.enabled), plan.stream, json.dumps(normalize_week(plan.schedule)),
                    int(plan.retention_days), int(plan.overwrite)))


def copy_plan(source: Plan, device_ids):
    for device_id in device_ids:
        if device_id != source.device_id:
            save_plan(Plan(device_id, source.enabled, source.stream, source.schedule,
                           source.retention_days, source.overwrite))


def enabled_plans():
    with storage.database() as db:
        ids = [r[0] for r in db.execute("SELECT device_id FROM recording_plans WHERE enabled=1")]
    return [load_plan(i) for i in ids]


def should_record(plan: Plan, moment):
    return plan.enabled and schedule_allows(plan.schedule, moment)


# ---------------------------------------------------------------- ffmpeg
def camera_folder(root, device_id):
    return Path(root) / str(int(device_id))


def segment_command(ffmpeg, list_path, folder):
    """argv для сегментированной записи (strftime-шаблон имени)."""
    pattern = str(Path(folder) / "%Y%m%d" / f"{FILE_PATTERN}.mkv")
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,rtsp,tcp,udp,rtp",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_atclocktime", "1",
            "-reset_timestamps", "1", "-strftime", "1", "-strftime_mkdir", "1",
            "-segment_format", "matroska", pattern]


# ---------------------------------------------------------------- индекс файлов
def parse_segment_name(path):
    try:
        return datetime.strptime(Path(path).stem, FILE_PATTERN).astimezone()
    except ValueError:
        return None


def list_segments(root, device_id, start=None, end=None):
    """[(начало, конец, путь)] локальных сегментов камеры, отсортированных по времени.

    Конец сегмента — начало следующего (если ближе SEGMENT_SECONDS) или +SEGMENT_SECONDS,
    для последнего файла — время его изменения.
    """
    folder = camera_folder(root, device_id)
    if not folder.is_dir():
        return []
    files = []
    for path in folder.glob("*/*.mkv"):
        began = parse_segment_name(path)
        if began:
            files.append((began, path))
    files.sort()
    result = []
    for index, (began, path) in enumerate(files):
        if index + 1 < len(files):
            finish = min(files[index + 1][0], began + timedelta(seconds=SEGMENT_SECONDS))
        else:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                modified = began
            finish = max(began + timedelta(seconds=1), min(modified, began + timedelta(seconds=SEGMENT_SECONDS)))
        if start and finish <= start:
            continue
        if end and began >= end:
            continue
        result.append((began, finish, path))
    return result


def segment_for(root, device_id, moment):
    for began, finish, path in list_segments(root, device_id, moment - timedelta(seconds=SEGMENT_SECONDS),
                                             moment + timedelta(seconds=1)):
        if began <= moment < finish:
            return path, (moment - began).total_seconds()
    return None, 0.0


# ---------------------------------------------------------------- очистка
def cleanup(root, plans, now=None, min_free_bytes=None, disk_usage=shutil.disk_usage):
    """Удалить сегменты старше срока хранения камеры; при нехватке места — самые старые
    у камер с разрешённой перезаписью. Возвращает число удалённых файлов."""
    now = now or datetime.now().astimezone()
    root = Path(root)
    removed = 0
    by_device = {p.device_id: p for p in plans}
    all_files = []
    if not root.is_dir():
        return 0
    for device_dir in root.iterdir():
        if not device_dir.is_dir() or not device_dir.name.isdigit():
            continue
        plan = by_device.get(int(device_dir.name))
        days = plan.retention_days if plan else retention_settings()["recording_days"]
        limit = now - timedelta(days=days)
        for path in device_dir.glob("*/*.mkv"):
            began = parse_segment_name(path)
            if began is None:
                continue
            if began < limit:
                _unlink(path)
                removed += 1
            else:
                all_files.append((began, path, plan.overwrite if plan else True))
        for day_dir in device_dir.iterdir():
            if day_dir.is_dir() and not any(day_dir.iterdir()):
                day_dir.rmdir()
    if min_free_bytes:
        all_files.sort()
        for began, path, overwrite in all_files:
            try:
                free = disk_usage(root).free
            except OSError:
                break
            if free >= min_free_bytes:
                break
            if overwrite and began < now - timedelta(seconds=SEGMENT_SECONDS):
                _unlink(path)
                removed += 1
    return removed


def _unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def usage(root):
    """{'video_bytes', 'total', 'free', 'days': {device_id: int}} для страницы «Системное хранилище»."""
    root = Path(root)
    video = 0
    days = {}
    if root.is_dir():
        for device_dir in root.iterdir():
            if device_dir.is_dir() and device_dir.name.isdigit():
                count = 0
                for day_dir in device_dir.iterdir():
                    if day_dir.is_dir():
                        count += 1
                        for path in day_dir.glob("*.mkv"):
                            try:
                                video += path.stat().st_size
                            except OSError:
                                pass
                days[int(device_dir.name)] = count
    probe = root if root.exists() else root.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        disk = shutil.disk_usage(probe)
        total, free = disk.total, disk.free
    except OSError:
        total = free = 0
    return {"video_bytes": video, "total": total, "free": free, "days": days}


def estimate_days_left(video_bytes, recorded_days, free_bytes):
    if not recorded_days or not video_bytes:
        return None
    per_day = video_bytes / recorded_days
    return int((free_bytes + video_bytes) / per_day) if per_day else None


def human_bytes(value):
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit in ("Б", "КБ") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
