"""Онлайн-карты: источники тайлов, проекция Web Mercator, дисковый кэш, загрузка и поиск адреса.

Модуль без GTK — его можно проверять тестами. Виджет карты — в sentinel_mapview.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sentinel_storage as storage

TILE_SIZE = 256
MIN_ZOOM = 1
MAX_LAT = 85.05112878
SETTINGS_KEY = "online_maps"
MAP_IMAGE, MAP_ONLINE = "image", "online"

# Встроенные источники. Подпись (attribution) обязательна по условиям использования тайлов.
BUILTIN_PROVIDERS = (
    {"id": "osm", "name": "Схема (OpenStreetMap)", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
     "subdomains": "", "max_zoom": 19, "attribution": "© участники OpenStreetMap"},
    {"id": "topo", "name": "Рельеф (OpenTopoMap)", "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
     "subdomains": "abc", "max_zoom": 17, "attribution": "© участники OpenStreetMap, SRTM · стиль © OpenTopoMap"},
    {"id": "satellite", "name": "Спутник (Esri World Imagery)",
     "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
     "subdomains": "", "max_zoom": 19, "attribution": "Снимки © Esri и поставщики данных"},
    {"id": "light", "name": "Светлая (CARTO)", "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
     "subdomains": "abcd", "max_zoom": 20, "attribution": "© участники OpenStreetMap · © CARTO"},
    {"id": "dark", "name": "Тёмная (CARTO)", "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
     "subdomains": "abcd", "max_zoom": 20, "attribution": "© участники OpenStreetMap · © CARTO"},
)

DEFAULT_SETTINGS = {
    "default_provider": "osm",
    "custom_providers": [],          # [{id, name, url, subdomains, max_zoom, attribution}]
    "cache_days": 30,                # тайл старше — перезапрашивается (если есть сеть)
    "cache_mb": 500,                 # предел размера кэша на диске
    "offline": False,                # только кэш, без обращений в интернет
    "geocoder_url": "https://nominatim.openstreetmap.org/search",
    "default_lat": 55.7558,
    "default_lon": 37.6173,
    "default_zoom": 12,
}


def user_agent():
    try:
        version = (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except OSError:
        version = "dev"
    return f"SentinelPRO/{version} (security management client)"


# ------------------------------------------------------------------ настройки
def load_settings():
    saved = storage.get_json_setting(SETTINGS_KEY, {}) or {}
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    settings["custom_providers"] = [normalize_provider(p) for p in settings.get("custom_providers") or []
                                    if isinstance(p, dict) and p.get("url")]
    settings["cache_days"] = max(1, int(settings["cache_days"] or 30))
    settings["cache_mb"] = max(20, int(settings["cache_mb"] or 500))
    settings["default_zoom"] = clamp_zoom(settings["default_zoom"])
    return settings


def save_settings(settings):
    clean = {k: settings[k] for k in DEFAULT_SETTINGS if k in settings}
    clean["custom_providers"] = [normalize_provider(p) for p in clean.get("custom_providers", [])]
    storage.set_json_setting(SETTINGS_KEY, clean)
    return load_settings()


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "custom"


def normalize_provider(provider):
    url = str(provider.get("url", "")).strip()
    name = str(provider.get("name", "")).strip() or "Свой источник"
    pid = str(provider.get("id", "")).strip() or "custom-" + _slug(name)
    try:
        max_zoom = int(provider.get("max_zoom") or 19)
    except (TypeError, ValueError):
        max_zoom = 19
    return {"id": pid, "name": name, "url": url, "subdomains": str(provider.get("subdomains", "") or "").strip(),
            "max_zoom": max(MIN_ZOOM, min(22, max_zoom)), "attribution": str(provider.get("attribution", "")).strip()}


def validate_template(url):
    """Ошибка шаблона адреса тайла или пустая строка."""
    url = (url or "").strip()
    if not url:
        return "Укажите адрес тайлов"
    if not all(f"{{{key}}}" in url for key in "zxy") and not url.startswith("/"):
        return "В адресе нужны {z}, {x} и {y}"
    if "{s}" in url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if url.startswith("/") or parsed.scheme in ("http", "https", "file"):
        return ""
    return "Адрес должен начинаться с http://, https://, file:// или /"


def providers(settings=None):
    settings = settings or load_settings()
    items = [dict(p) for p in BUILTIN_PROVIDERS]
    ids = {p["id"] for p in items}
    for custom in settings.get("custom_providers", []):
        custom = dict(custom)
        while custom["id"] in ids:
            custom["id"] += "-1"
        ids.add(custom["id"])
        items.append(custom)
    return items


def provider_by_id(pid, settings=None):
    settings = settings or load_settings()
    items = providers(settings)
    wanted = pid or settings.get("default_provider") or "osm"
    return next((p for p in items if p["id"] == wanted), None) or \
        next((p for p in items if p["id"] == settings.get("default_provider")), items[0])


def tile_url(provider, z, x, y):
    url = provider["url"]
    subdomains = provider.get("subdomains") or ""
    s = subdomains[(x + y) % len(subdomains)] if subdomains else ""
    url = (url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{s}", s)
           .replace("{-y}", str((1 << z) - 1 - y)))
    if url.startswith("/"):
        return "file://" + url
    return url


# ------------------------------------------------------------------ проекция
def clamp_zoom(zoom, max_zoom=22):
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        zoom = 12
    return max(MIN_ZOOM, min(max_zoom, zoom))


def clamp_lat(lat):
    return max(-MAX_LAT, min(MAX_LAT, float(lat)))


def wrap_lon(lon):
    return (float(lon) + 180.0) % 360.0 - 180.0


def lonlat_to_world(lon, lat, zoom):
    """Мировые пиксели (0..256·2^z) для долготы/широты при масштабе zoom (может быть дробным)."""
    scale = TILE_SIZE * (2 ** zoom)
    lat = math.radians(clamp_lat(lat))
    x = (float(lon) + 180.0) / 360.0 * scale
    y = (1.0 - math.log(math.tan(lat) + 1.0 / math.cos(lat)) / math.pi) / 2.0 * scale
    return x, y


def world_to_lonlat(x, y, zoom):
    scale = TILE_SIZE * (2 ** zoom)
    lon = x / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return wrap_lon(lon), clamp_lat(lat)


def visible_tiles(center_lon, center_lat, zoom, width, height, max_tile_zoom=22):
    """Тайлы (z, x, y, screen_x, screen_y, size) для окна width×height с центром в точке.

    Дальше max_tile_zoom тайлы не запрашиваются, а растягиваются (size > 256)."""
    z = max(0, min(int(math.floor(zoom + 1e-9)), int(max_tile_zoom)))
    size = TILE_SIZE * (2 ** (zoom - z))
    cx, cy = lonlat_to_world(center_lon, center_lat, zoom)
    left, top = cx - width / 2.0, cy - height / 2.0
    count = 1 << z
    first_x, first_y = int(math.floor(left / size)), int(math.floor(top / size))
    last_x, last_y = int(math.floor((left + width) / size)), int(math.floor((top + height) / size))
    tiles = []
    for ty in range(max(0, first_y), min(count - 1, last_y) + 1):
        for tx in range(first_x, last_x + 1):
            tiles.append((z, tx % count, ty, tx * size - left, ty * size - top, size))
    # Сначала центр — он важнее для оператора.
    tiles.sort(key=lambda t: (t[3] + t[5] / 2 - width / 2) ** 2 + (t[4] + t[5] / 2 - height / 2) ** 2)
    return tiles


def fit_bounds(points, width, height, max_zoom=18, padding=48):
    """Центр и масштаб, при которых все точки (lon, lat) видны в окне."""
    points = [(float(lon), float(lat)) for lon, lat in points]
    if not points:
        return None
    if len(points) == 1:
        return points[0][0], points[0][1], min(max_zoom, 17)
    width, height = max(64, width - 2 * padding), max(64, height - 2 * padding)
    xs, ys = zip(*(lonlat_to_world(lon, lat, 0) for lon, lat in points))
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    zoom = max_zoom
    for candidate in range(int(max_zoom), MIN_ZOOM - 1, -1):
        factor = 2 ** candidate
        if span_x * factor <= width and span_y * factor <= height:
            zoom = candidate
            break
    else:
        zoom = MIN_ZOOM
    lon, lat = world_to_lonlat((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, 0)
    return lon, lat, zoom


def parse_coordinates(text):
    """«55.75, 37.62» / «55.75 37.62» → (lat, lon) или None."""
    numbers = re.findall(r"[-+]?\d+(?:[.,]\d+)?", text or "")
    if len(numbers) != 2:
        return None
    lat, lon = (float(n.replace(",", ".")) for n in numbers)
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


# ------------------------------------------------------------------ кэш и загрузка
def cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sentinel" / "tiles"


def cache_path(provider, z, x, y):
    return cache_root() / _slug(provider["id"]) / str(z) / str(x) / f"{y}.tile"


def cache_size_bytes(root=None):
    root = Path(root or cache_root())
    total = 0
    for path in root.rglob("*.tile") if root.exists() else ():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def clear_cache(root=None):
    root = Path(root or cache_root())
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def trim_cache(limit_mb, root=None):
    """Удалить самые старые тайлы, пока кэш больше предела. Возвращает число удалённых."""
    root = Path(root or cache_root())
    if not root.exists():
        return 0
    files = []
    total = 0
    for path in root.rglob("*.tile"):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_atime, stat.st_size, path))
        total += stat.st_size
    limit = int(limit_mb) * 1024 * 1024
    removed = 0
    for _atime, size, path in sorted(files):
        if total <= limit:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def _read_url(url, timeout=15):
    if url.startswith("file://"):
        return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class TileFetcher:
    """Фоновая загрузка тайлов: диск → сеть. Готовые байты отдаются через callback(key, data)."""

    def __init__(self, workers=4, reader=_read_url):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tiles")
        self._pending = set()
        self._failed = {}
        self._lock = threading.Lock()
        self._reader = reader
        self._fetched = 0

    def request(self, provider, z, x, y, callback, settings):
        key = (provider["id"], z, x, y)
        with self._lock:
            if key in self._pending or time.monotonic() < self._failed.get(key, 0):
                return False
            self._pending.add(key)
        self._pool.submit(self._load, key, dict(provider), z, x, y, callback, dict(settings))
        return True

    def _load(self, key, provider, z, x, y, callback, settings):
        data = None
        try:
            path = cache_path(provider, z, x, y)
            local = tile_url(provider, z, x, y).startswith("file://")
            stale = True
            if path.is_file():
                data = path.read_bytes()
                stale = time.time() - path.stat().st_mtime > settings["cache_days"] * 86400
            if not local and not settings.get("offline") and (data is None or stale):
                try:
                    fresh = self._reader(tile_url(provider, z, x, y))
                    if fresh:
                        data = fresh
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_suffix(".part")
                        tmp.write_bytes(fresh)
                        tmp.replace(path)
                        self._fetched += 1
                        if self._fetched % 200 == 0:
                            trim_cache(settings["cache_mb"])
                except Exception:
                    if data is None:
                        raise
            elif local and data is None:
                data = self._reader(tile_url(provider, z, x, y))
        except Exception:
            data = None
        with self._lock:
            self._pending.discard(key)
            if data is None:
                self._failed[key] = time.monotonic() + 30
        callback(key, data)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def geocode(query, settings=None, limit=5, reader=None):
    """Поиск адреса: [(подпись, lat, lon)]. Координаты в запросе разбираются без сети."""
    coords = parse_coordinates(query)
    if coords:
        return [(f"{coords[0]:.6f}, {coords[1]:.6f}", coords[0], coords[1])]
    settings = settings or load_settings()
    base = (settings.get("geocoder_url") or "").strip()
    if not base or settings.get("offline"):
        return []
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": limit, "accept-language": "ru"})
    raw = (reader or _read_url)(url)
    results = []
    for item in json.loads(raw.decode("utf-8")):
        try:
            results.append((item.get("display_name", query), float(item["lat"]), float(item["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return results
