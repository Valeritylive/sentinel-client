"""Логика панели инструментов окна видео: визуальный PTZ, клавиши PTZ, геометрия.

Модуль без GTK, чтобы его можно было проверять unit-тестами.
"""
from __future__ import annotations

# Hikvision ISAPI position3D: координаты 0…255, начало — левый НИЖНИЙ угол кадра.
POSITION3D_MAX = 255

# Клавиши (имена Gdk без префикса KEY_) -> направление PTZ.
PTZ_KEYS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "KP_Up": "up", "KP_Down": "down", "KP_Left": "left", "KP_Right": "right",
    "KP_8": "up", "KP_2": "down", "KP_4": "left", "KP_6": "right",
    "KP_7": "up_left", "KP_9": "up_right", "KP_1": "down_left", "KP_3": "down_right",
    "KP_Home": "up_left", "KP_Page_Up": "up_right", "KP_End": "down_left", "KP_Page_Down": "down_right",
    "plus": "zoom_in", "equal": "zoom_in", "KP_Add": "zoom_in", "Page_Up": "zoom_in",
    "minus": "zoom_out", "KP_Subtract": "zoom_out", "Page_Down": "zoom_out",
}

STREAM_CYCLE = ("main", "sub")
STREAM_LABELS = {"main": "Основной", "sub": "Дополнительный", "third": "Третий"}
STREAM_SHORT = {"main": "ОСН", "sub": "ДОП", "third": "3-й"}


def next_stream(current):
    """Следующий поток для кнопки переключения (основной <-> дополнительный)."""
    current = current or "main"
    if current not in STREAM_CYCLE:
        return "main"
    return STREAM_CYCLE[(STREAM_CYCLE.index(current) + 1) % len(STREAM_CYCLE)]


def video_rect(pane_width, pane_height, video_width, video_height):
    """Прямоугольник кадра (x, y, w, h) внутри окна при вписывании с полями."""
    if pane_width <= 0 or pane_height <= 0:
        return 0.0, 0.0, 0.0, 0.0
    if video_width <= 0 or video_height <= 0:
        return 0.0, 0.0, float(pane_width), float(pane_height)
    scale = min(pane_width / video_width, pane_height / video_height)
    width, height = video_width * scale, video_height * scale
    return (pane_width - width) / 2, (pane_height - height) / 2, width, height


def pane_to_frame(x, y, rect, zoom=1.0, center_x=0.5, center_y=0.5):
    """Точка окна -> доля полного кадра камеры (0…1, начало слева сверху).

    Учитывает поля вписывания и цифровой зум (показана часть кадра вокруг центра).
    Возвращает None, если точка вне изображения.
    """
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return None
    fx, fy = (x - left) / width, (y - top) / height
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    zoom = max(1.0, zoom or 1.0)
    if zoom > 1.001:
        visible = 1.0 / zoom
        start_x = min(max(center_x - visible / 2, 0.0), 1.0 - visible)
        start_y = min(max(center_y - visible / 2, 0.0), 1.0 - visible)
        fx, fy = start_x + fx * visible, start_y + fy * visible
    return fx, fy


def _scaled(fx, fy):
    x = int(round(min(max(fx, 0.0), 1.0) * POSITION3D_MAX))
    y = int(round((1.0 - min(max(fy, 0.0), 1.0)) * POSITION3D_MAX))
    return x, y


def position3d_xml(start, end=None):
    """XML для PUT /ISAPI/PTZCtrl/channels/<n>/position3D.

    ``start``/``end`` — доли кадра (0…1, начало слева сверху). Одна точка —
    камера центрируется на ней; прямоугольник слева направо — приблизить
    область, справа налево — отдалить (так ведут себя камеры Hikvision).
    """
    end = end or start
    sx, sy = _scaled(*start)
    ex, ey = _scaled(*end)
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><position3D>"
            f"<StartPoint><positionX>{sx}</positionX><positionY>{sy}</positionY></StartPoint>"
            f"<EndPoint><positionX>{ex}</positionX><positionY>{ey}</positionY></EndPoint>"
            "</position3D>").encode()


def is_click(dx, dy, threshold=6):
    return abs(dx) < threshold and abs(dy) < threshold


def ptz_direction_for_key(key_name):
    return PTZ_KEYS.get(key_name)
