"""Виды на сервере Sentinel: приватные (только владельцу) и публичные (всем клиентам).

Каталоги устройств на сервере и в клиентах нумеруются независимо, поэтому
камеры и двери в общем виде хранятся по адресу устройства и номеру канала,
а при открытии сопоставляются с локальным каталогом клиента. Без GTK: модуль
используют и сервер, и клиент.
"""
from __future__ import annotations

import json
from datetime import datetime

import sentinel_storage as storage
import sentinel_views as views

MAX_SLOTS = 64
PUBLIC_PERMISSION = "system"   # создавать и менять публичные виды


class ViewAccessError(PermissionError):
    pass


def device_key(device):
    return {"host": str(device.get("host", "")), "port": int(device.get("sdk_port") or 8000),
            "channel": int(device.get("channel") or 1)}


def _match(key, device):
    return (str(device.get("host", "")) == key.get("host")
            and int(device.get("channel") or 1) == int(key.get("channel") or 1)
            and int(device.get("sdk_port") or 8000) == int(key.get("port") or 8000))


def portable_mapping(mapping, devices):
    """Назначение окон вида -> переносимое (камеры и двери по адресу и каналу).

    Карты и неизвестные камеры отбрасываются: они есть только в этом клиенте.
    """
    by_id = {d["id"]: d for d in devices}
    result = []
    for item in views.normalize_mapping(mapping):
        if item["kind"] in (views.KIND_CAMERA, views.KIND_DOOR):
            device = by_id.get(item["device_id"])
            if device is None:
                continue
            entry = {"slot": item["slot"], "kind": item["kind"], "device": device_key(device)}
            if item["kind"] == views.KIND_DOOR:
                entry["door"] = item["door"]
            result.append(entry)
        elif item["kind"] == views.KIND_WEB:
            result.append(dict(item))
    return result


def local_mapping(portable, devices):
    """Переносимое назначение -> (назначение с локальными id, сколько камер не найдено)."""
    result, missing = [], 0
    for item in portable or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or views.KIND_CAMERA
        if kind in (views.KIND_CAMERA, views.KIND_DOOR):
            key = item.get("device") or {}
            device = next((d for d in devices if _match(key, d)), None)
            if device is None:
                missing += 1
                continue
            entry = {"slot": item.get("slot"), "kind": kind, "device_id": device["id"]}
            if kind == views.KIND_DOOR:
                entry["door"] = item.get("door") or 1
            result.append(entry)
        elif kind == views.KIND_WEB:
            result.append(item)
    return views.normalize_mapping(result), missing


def clean_layout(payload):
    """Проверить раскладку из запроса: {division, layout, grid_columns, grid_rows, cells}."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается раскладка вида")
    layout = int(payload.get("layout") or 4)
    if not 1 <= layout <= MAX_SLOTS:
        raise ValueError("Недопустимое число окон")
    cells = payload.get("cells") or []
    if not isinstance(cells, list) or len(cells) > MAX_SLOTS:
        raise ValueError("Недопустимое назначение окон")
    clean = []
    for item in cells:
        if not isinstance(item, dict):
            continue
        slot = int(item.get("slot", -1))
        if not 0 <= slot < MAX_SLOTS:
            continue
        kind = item.get("kind") or views.KIND_CAMERA
        if kind in (views.KIND_CAMERA, views.KIND_DOOR):
            key = item.get("device") or {}
            entry = {"slot": slot, "kind": kind,
                     "device": {"host": str(key.get("host", ""))[:255], "port": int(key.get("port") or 8000),
                                "channel": int(key.get("channel") or 1)}}
            if kind == views.KIND_DOOR:
                entry["door"] = max(1, int(item.get("door") or 1))
            clean.append(entry)
        elif kind == views.KIND_WEB and item.get("url"):
            clean.append({"slot": slot, "kind": kind, "url": str(item["url"])[:2000],
                          "name": str(item.get("name") or item["url"])[:200]})
    return {"division": str(payload.get("division") or "")[:32], "layout": layout,
            "grid_columns": max(0, min(16, int(payload.get("grid_columns") or 0))),
            "grid_rows": max(0, min(16, int(payload.get("grid_rows") or 0))), "cells": clean}


def tab_layout(tab, devices):
    """Раскладка для отправки на сервер из вкладки вида клиента."""
    return {"division": tab.division or "", "layout": tab.layout, "grid_columns": tab.grid_columns,
            "grid_rows": tab.grid_rows, "cells": portable_mapping(tab.mapping, devices)}


def view_values(item, devices):
    """(значения ViewTab, сколько камер не найдено) из вида сервера."""
    layout = item.get("layout") or {}
    mapping, missing = local_mapping(layout.get("cells"), devices)
    return {"division": layout.get("division") or "", "layout": int(layout.get("layout") or 4),
            "grid_columns": int(layout.get("grid_columns") or 0), "grid_rows": int(layout.get("grid_rows") or 0),
            "mapping": mapping}, missing


# ---------------------------------------------------------------- хранение (на сервере)
def _row(row):
    item = dict(row)
    try:
        item["layout"] = json.loads(item["layout"])
    except (TypeError, json.JSONDecodeError):
        item["layout"] = {}
    item["public"] = bool(item["public"])
    return item


def can_edit(session, item):
    if item["public"]:
        return session.can(PUBLIC_PERMISSION) or item["owner"] == session.username
    return item["owner"] == session.username


def list_views(session):
    """Публичные виды и приватные виды этого пользователя."""
    with storage.database() as db:
        rows = db.execute("""SELECT * FROM shared_views WHERE public=1 OR owner=?
            ORDER BY public DESC, name COLLATE NOCASE""", (session.username,)).fetchall()
    result = []
    for row in rows:
        item = _row(row)
        item["editable"] = can_edit(session, item)
        result.append(item)
    return result


def get_view(view_id):
    with storage.database() as db:
        row = db.execute("SELECT * FROM shared_views WHERE id=?", (int(view_id),)).fetchone()
    return _row(row) if row else None


def create_view(session, name, public, layout):
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("Введите название вида")
    if public and not session.can(PUBLIC_PERMISSION):
        raise ViewAccessError("Публичные виды может создавать только пользователь с правом «Системная конфигурация»")
    layout = clean_layout(layout)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with storage.database() as db:
        return db.execute("""INSERT INTO shared_views(name, owner, public, layout, updated_at, updated_by)
            VALUES(?,?,?,?,?,?)""", (name, session.username, int(bool(public)),
                                     json.dumps(layout, ensure_ascii=False), now, session.username)).lastrowid


def update_view(session, view_id, name=None, public=None, layout=None):
    item = get_view(view_id)
    if item is None:
        raise KeyError("Вид не найден")
    if not can_edit(session, item):
        raise ViewAccessError("Нет прав на изменение этого вида")
    if public and not item["public"] and not session.can(PUBLIC_PERMISSION):
        raise ViewAccessError("Сделать вид публичным может только пользователь с правом «Системная конфигурация»")
    values = {"name": (name or "").strip()[:120] or item["name"],
              "public": int(bool(item["public"] if public is None else public)),
              "layout": json.dumps(clean_layout(layout) if layout is not None else item["layout"],
                                   ensure_ascii=False)}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with storage.database() as db:
        db.execute("UPDATE shared_views SET name=?, public=?, layout=?, updated_at=?, updated_by=? WHERE id=?",
                   (values["name"], values["public"], values["layout"], now, session.username, int(view_id)))
    return get_view(view_id)


def delete_view(session, view_id):
    item = get_view(view_id)
    if item is None:
        raise KeyError("Вид не найден")
    if not can_edit(session, item):
        raise ViewAccessError("Нет прав на удаление этого вида")
    with storage.database() as db:
        db.execute("DELETE FROM shared_views WHERE id=?", (int(view_id),))
    return item
