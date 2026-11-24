"""Логика раздела «Управление устройствами» (без GTK)."""
import csv
import ipaddress

MAX_SEGMENT = 256


def physical_devices(devices, groups=None):
    """Свернуть строки-каналы в физические устройства (регистратор/камера).

    Возвращает список словарей: key, name, host, protocol, sdk_port, http_port,
    rtsp_port, channels, ids, group_id, group_name, serial_no, device_model.
    """
    names = {g["id"]: g["name"] for g in (groups or [])}
    result = {}
    for d in devices:
        key = (d["protocol"], d["host"], d["sdk_port"], d["http_port"], d["rtsp_port"])
        row = result.get(key)
        if row is None:
            row = result[key] = {
                "key": key, "protocol": d["protocol"], "host": d["host"], "sdk_port": d["sdk_port"],
                "http_port": d["http_port"], "rtsp_port": d["rtsp_port"], "ids": [], "channels": 0,
                "name": d.get("host_name") or d["host"], "group_id": d.get("group_id"),
                "serial_no": d.get("serial_no", ""), "device_model": d.get("device_model", ""),
            }
        row["ids"].append(d["id"])
        row["channels"] += 1
        if d.get("host_name") and row["name"] == row["host"]:
            row["name"] = d["host_name"]
        for field in ("serial_no", "device_model"):
            if not row[field] and d.get(field):
                row[field] = d[field]
    rows = list(result.values())
    for row in rows:
        row["group_name"] = names.get(row["group_id"], "")
    return sorted(rows, key=lambda r: (r["host"], r["sdk_port"]))


def filter_rows(rows, text):
    needle = (text or "").strip().lower()
    if not needle:
        return list(rows)
    fields = ("name", "host", "group_name", "serial_no", "device_model", "protocol")
    return [r for r in rows if any(needle in str(r.get(f, "")).lower() for f in fields)]


CSV_HEADER = ("Название", "Адрес", "Протокол", "SDK-порт", "HTTP-порт", "RTSP-порт",
              "Каналов", "Группа", "Модель", "Серийный номер")


def _safe_cell(value):
    text = str(value)
    # защита от формул при открытии CSV в таблицах
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_csv(rows, handle):
    writer = csv.writer(handle)
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow([_safe_cell(v) for v in (
            r["name"], r["host"], "Hikvision" if r["protocol"] == "hikvision" else "RTSP",
            r["sdk_port"], r["http_port"], r["rtsp_port"], r["channels"],
            r.get("group_name", ""), r.get("device_model", ""), r.get("serial_no", ""))])


def expand_ip_segment(start, end, limit=MAX_SEGMENT):
    """Адреса от start до end включительно. ValueError при ошибке или превышении limit."""
    try:
        a, b = ipaddress.ip_address(start.strip()), ipaddress.ip_address(end.strip())
    except ValueError as exc:
        raise ValueError("Некорректный IP-адрес") from exc
    if a.version != b.version:
        raise ValueError("Адреса разных версий")
    if int(b) < int(a):
        raise ValueError("Конечный адрес меньше начального")
    count = int(b) - int(a) + 1
    if count > limit:
        raise ValueError(f"Слишком большой диапазон: {count} адресов (максимум {limit})")
    return [str(ipaddress.ip_address(int(a) + i)) for i in range(count)]


def channel_title(base_name, channel, names=None, single=False):
    """Имя канала: с регистратора, иначе «<устройство> CH n»."""
    if names and names.get(channel):
        return names[channel]
    return base_name if single else f"{base_name} CH {channel}"


def group_tree(groups, rows):
    """[(группа | None, [строки устройств])]: сначала группы по имени, затем «без группы»."""
    by_group = {}
    for r in rows:
        by_group.setdefault(r.get("group_id"), []).append(r)
    tree = [(g, by_group.get(g["id"], [])) for g in sorted(groups, key=lambda g: g["name"].lower())]
    loose = [r for gid, items in by_group.items() if gid not in {g["id"] for g in groups} for r in items]
    if loose:
        tree.append((None, loose))
    return tree
