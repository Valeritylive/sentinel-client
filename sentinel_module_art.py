"""Иллюстрации для плиток разделов на «Главной» (встроенные SVG, без отдельных файлов).

Каждая картинка рисуется в поле 120×80 в общем плоском стиле: светлые контуры
на тёмном фоне плитки и акцентный цвет группы разделов.
"""
from __future__ import annotations

ACCENTS = {"Просмотр": "#e8232c", "Мониторинг": "#f5a623",
           "Доступ и безопасность": "#22b8a6", "Управление": "#3d8bfd"}

L = "#e6e8eb"   # светлые детали
M = "#9aa0a8"   # средний серый
D = "#2b2e34"   # тёмная заливка
B = "#3c4048"   # корпус

SKY = "#5b7fa6"
GRASS = "#4f8a5b"


def _scene(x, y, w, h):
    """Маленький «кадр с камеры»: небо, земля, дом."""
    g = h * 0.58
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{SKY}"/>'
            f'<rect x="{x}" y="{y + g}" width="{w}" height="{h - g}" fill="{GRASS}"/>'
            f'<rect x="{x + w * 0.55}" y="{y + h * 0.32}" width="{w * 0.28}" height="{h * 0.36}" fill="{L}" opacity="0.85"/>'
            f'<path d="M{x + w * 0.51} {y + h * 0.34} L{x + w * 0.69} {y + h * 0.16} L{x + w * 0.87} {y + h * 0.34}Z" fill="#c9503f"/>')


def _monitor(x, y, w, h):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{B}" stroke="{M}" stroke-width="1.5"/>'
            f'<rect x="{x + w / 2 - 6}" y="{y + h}" width="12" height="6" fill="{M}"/>'
            f'<rect x="{x + w / 2 - 16}" y="{y + h + 6}" width="32" height="3" rx="1.5" fill="{M}"/>')


def main_view(a):
    s = _monitor(18, 8, 84, 56)
    for col in range(2):
        for row in range(2):
            s += _scene(22 + col * 39, 12 + row * 25, 37, 23)
    s += f'<rect x="23" y="13" width="28" height="10" rx="5" fill="{D}" opacity="0.85"/><circle cx="28" cy="18" r="3" fill="{a}"/><text x="33" y="20.5" font-size="7" font-family="sans-serif" font-weight="700" fill="{L}">LIVE</text>'
    return s


def playback(a):
    s = _monitor(18, 6, 84, 52)
    s += _scene(22, 10, 76, 36)
    s += f'<rect x="22" y="49" width="76" height="5" rx="2.5" fill="{D}"/><rect x="22" y="49" width="46" height="5" rx="2.5" fill="{a}"/>'
    s += f'<rect x="66" y="46" width="3" height="11" rx="1.5" fill="{L}"/>'
    s += f'<circle cx="60" cy="28" r="11" fill="{D}" opacity="0.75"/><path d="M56 22 L66 28 L56 34Z" fill="{L}"/>'
    return s


def archive(a):
    s = ""
    for i, dx in enumerate((10, 4, -2)):
        s += f'<rect x="{22 + dx}" y="{10 + i * 8}" width="62" height="40" rx="3" fill="{B}" stroke="{M}" stroke-width="1.2"/>'
    s += _scene(24, 30, 58, 26)
    for k in range(6):
        s += f'<rect x="{25 + k * 10}" y="27" width="5" height="2" fill="{M}"/><rect x="{25 + k * 10}" y="57" width="5" height="2" fill="{M}"/>'
    s += f'<circle cx="86" cy="44" r="13" fill="{D}" stroke="{a}" stroke-width="4"/><path d="M95 53 L106 64" stroke="{a}" stroke-width="6" stroke-linecap="round"/>'
    s += f'<path d="M80 40 a7 7 0 0 1 7 -4" stroke="{L}" stroke-width="2" fill="none" stroke-linecap="round"/>'
    return s


def wall(a):
    s = f'<rect x="10" y="6" width="100" height="58" rx="3" fill="{D}" stroke="{M}" stroke-width="1.2"/>'
    for col in range(3):
        for row in range(2):
            s += _scene(14 + col * 31.5, 10 + row * 26.5, 29.5, 24.5)
    s += f'<rect x="14" y="10" width="29.5" height="24.5" fill="none" stroke="{a}" stroke-width="2"/>'
    s += f'<rect x="56" y="64" width="8" height="8" fill="{M}"/><rect x="40" y="72" width="40" height="3" rx="1.5" fill="{M}"/>'
    return s


def local_files(a):
    s = f'<path d="M14 18 h30 l6 7 h56 v44 a3 3 0 0 1 -3 3 h-86 a3 3 0 0 1 -3 -3Z" fill="{B}" stroke="{M}" stroke-width="1.2"/>'
    s += (f'<g transform="rotate(-8 46 27)"><rect x="26" y="12" width="40" height="30" fill="{L}"/>'
          + _scene(29, 15, 34, 22) + '</g>')
    s += f'<path d="M14 32 h92 v37 a3 3 0 0 1 -3 3 h-86 a3 3 0 0 1 -3 -3Z" fill="{D}" stroke="{M}" stroke-width="1.2"/>'
    s += f'<circle cx="80" cy="50" r="13" fill="{a}"/><path d="M80 42 v14 M74 51 l6 6 l6 -6" stroke="{L}" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    return s


def board(a):
    s = f'<rect x="8" y="8" width="104" height="64" rx="4" fill="{D}" stroke="{M}" stroke-width="1.2"/>'
    s += f'<rect x="14" y="14" width="44" height="52" rx="3" fill="{B}"/><rect x="62" y="14" width="44" height="24" rx="3" fill="{B}"/><rect x="62" y="42" width="44" height="24" rx="3" fill="{B}"/>'
    s += f'<path d="M22 50 a14 14 0 0 1 28 0" stroke="{M}" stroke-width="5" fill="none"/>'
    s += f'<path d="M22 50 a14 14 0 0 1 21 -12" stroke="{a}" stroke-width="5" fill="none"/><path d="M36 50 L44 40" stroke="{L}" stroke-width="2.5" stroke-linecap="round"/><circle cx="36" cy="50" r="3" fill="{L}"/>'
    s += f'<rect x="22" y="56" width="28" height="3" rx="1.5" fill="{M}"/><rect x="22" y="20" width="20" height="3" rx="1.5" fill="{M}"/>'
    for i, h in enumerate((8, 14, 10, 17)):
        s += f'<rect x="{68 + i * 9}" y="{34 - h}" width="6" height="{h}" fill="{a if i == 3 else L}"/>'
    s += f'<path d="M66 60 L76 52 L84 56 L94 47 L102 50" stroke="{a}" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    return s


def events(a):
    s = f'<rect x="58" y="12" width="52" height="56" rx="4" fill="{D}" stroke="{M}" stroke-width="1.2"/>'
    for i in range(4):
        y = 20 + i * 12
        s += f'<circle cx="66" cy="{y + 3}" r="3" fill="{a if i == 0 else M}"/><rect x="73" y="{y + 1}" width="{30 - i * 3}" height="4" rx="2" fill="{L if i == 0 else M}"/>'
    s += f'<path d="M34 16 a16 16 0 0 1 16 16 v14 l6 8 h-44 l6 -8 v-14 a16 16 0 0 1 16 -16Z" fill="{L}"/>'
    s += f'<circle cx="34" cy="60" r="5" fill="{L}"/><rect x="31" y="10" width="6" height="7" rx="3" fill="{L}"/>'
    s += f'<circle cx="48" cy="20" r="8" fill="{a}" stroke="{D}" stroke-width="2"/><text x="48" y="24" font-size="11" font-family="sans-serif" font-weight="700" text-anchor="middle" fill="#fff">!</text>'
    return s


def emap(a):
    s = f'<path d="M12 20 L40 12 L80 20 L108 12 V62 L80 70 L40 62 L12 70Z" fill="{B}" stroke="{M}" stroke-width="1.2" stroke-linejoin="round"/>'
    s += f'<path d="M40 12 V62 M80 20 V70" stroke="{M}" stroke-width="1.2"/>'
    s += f'<path d="M12 46 C30 40 44 52 60 44 S90 34 108 40" stroke="{SKY}" stroke-width="5" fill="none"/>'
    s += f'<path d="M20 26 h14 v10 h-14Z M88 50 h12 v8 h-12Z" fill="{GRASS}"/>'
    s += f'<path d="M30 60 L52 30 L72 36 L92 26" stroke="{L}" stroke-width="2" stroke-dasharray="4 3" fill="none"/>'
    s += f'<path d="M64 8 a12 12 0 0 1 12 12 c0 10 -12 22 -12 22 s-12 -12 -12 -22 a12 12 0 0 1 12 -12Z" fill="{a}"/><circle cx="64" cy="20" r="4.5" fill="{L}"/>'
    return s


def summary(a):
    s = f'<path d="M14 66 H106 M14 66 V10" stroke="{M}" stroke-width="2"/>'
    for i, h in enumerate((18, 30, 24, 42)):
        s += f'<rect x="{22 + i * 13}" y="{66 - h}" width="9" height="{h}" rx="1.5" fill="{a if i == 3 else L}"/>'
    s += f'<circle cx="88" cy="34" r="16" fill="{B}" stroke="{D}" stroke-width="1"/>'
    s += f'<path d="M88 34 V18 A16 16 0 0 1 103.2 39Z" fill="{a}"/><path d="M88 34 L103.2 39 A16 16 0 0 1 80 47.9Z" fill="{L}"/>'
    return s


def access(a):
    s = f'<rect x="20" y="8" width="44" height="64" rx="2" fill="{B}" stroke="{M}" stroke-width="1.2"/>'
    s += f'<rect x="25" y="13" width="34" height="59" fill="{D}"/><rect x="29" y="18" width="26" height="22" rx="1" fill="{B}"/><rect x="29" y="44" width="26" height="22" rx="1" fill="{B}"/>'
    s += f'<circle cx="52" cy="44" r="2.5" fill="{L}"/><rect x="51" y="44" width="6" height="2.5" rx="1" fill="{L}"/>'
    s += f'<rect x="72" y="22" width="22" height="34" rx="4" fill="{B}" stroke="{M}" stroke-width="1.2"/><circle cx="83" cy="34" r="6" fill="none" stroke="{a}" stroke-width="2"/><circle cx="83" cy="34" r="2" fill="{a}"/><rect x="78" y="46" width="10" height="3" rx="1.5" fill="{GRASS}"/>'
    s += f'<g transform="rotate(-18 98 56)"><rect x="84" y="46" width="28" height="18" rx="3" fill="{L}"/><rect x="88" y="50" width="8" height="6" rx="1" fill="{a}"/><rect x="88" y="59" width="18" height="2" fill="{M}"/></g>'
    return s


def parking(a):
    s = f'<rect x="8" y="64" width="104" height="4" rx="2" fill="{M}"/>'
    s += f'<rect x="10" y="26" width="7" height="38" fill="{B}" stroke="{M}" stroke-width="1"/>'
    s += f'<g transform="rotate(-24 14 30)"><rect x="14" y="27" width="56" height="6" rx="3" fill="{L}"/>'
    for k in range(4):
        s += f'<rect x="{22 + k * 12}" y="27" width="6" height="6" fill="{a}"/>'
    s += '</g>'
    s += f'<path d="M48 54 l6 -12 a6 6 0 0 1 5 -3 h28 a6 6 0 0 1 5 3 l7 12 h3 a4 4 0 0 1 4 4 v6 h-62 v-6 a4 4 0 0 1 4 -4Z" fill="{SKY}"/>'
    s += f'<path d="M57 52 l4 -9 h12 v9Z M77 52 v-9 h9 l4 9Z" fill="{D}" opacity="0.7"/>'
    s += f'<circle cx="60" cy="64" r="6" fill="{D}" stroke="{M}" stroke-width="2"/><circle cx="94" cy="64" r="6" fill="{D}" stroke="{M}" stroke-width="2"/>'
    s += f'<rect x="96" y="8" width="16" height="16" rx="3" fill="{a}"/><text x="104" y="21" font-size="13" font-family="sans-serif" font-weight="700" text-anchor="middle" fill="#fff">P</text>'
    return s


def patrol(a):
    s = f'<path d="M14 60 C30 60 26 30 46 30 S62 58 80 52 S96 22 106 18" stroke="{M}" stroke-width="2.5" stroke-dasharray="5 4" fill="none"/>'
    for x, y in ((14, 60), (46, 30), (80, 52)):
        s += f'<circle cx="{x}" cy="{y}" r="6" fill="{D}" stroke="{a}" stroke-width="2.5"/><path d="M{x - 2.5} {y} l2 2 l3.5 -4" stroke="{L}" stroke-width="1.8" fill="none"/>'
    s += f'<path d="M100 6 l12 4 v10 c0 8 -6 13 -12 16 c-6 -3 -12 -8 -12 -16 v-10Z" fill="{a}"/>'
    s += f'<path d="M95 20 l4 4 l7 -8" stroke="#fff" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    return s


def evidence(a):
    s = f'<rect x="20" y="10" width="58" height="62" rx="3" fill="{L}"/><path d="M62 10 h16 v16Z" fill="{M}"/>'
    for i in range(4):
        s += f'<rect x="28" y="{20 + i * 8}" width="{34 - (i % 2) * 10}" height="3" rx="1.5" fill="{M}"/>'
    s += _scene(28, 52, 30, 14)
    s += f'<circle cx="84" cy="52" r="18" fill="{D}" stroke="{a}" stroke-width="3"/>'
    for r in (4, 8, 12):
        s += f'<path d="M{84 - r} 56 a{r} {r} 0 0 1 {2 * r} 0" stroke="{a}" stroke-width="2" fill="none" stroke-linecap="round"/>'
    s += f'<path d="M84 54 v6" stroke="{a}" stroke-width="2" stroke-linecap="round"/>'
    return s


def devices(a):
    s = f'<rect x="8" y="46" width="70" height="20" rx="3" fill="{B}" stroke="{M}" stroke-width="1.2"/>'
    s += f'<circle cx="16" cy="56" r="2.5" fill="{GRASS}"/><circle cx="24" cy="56" r="2.5" fill="{a}"/>'
    for k in range(4):
        s += f'<rect x="{38 + k * 9}" y="52" width="6" height="8" rx="1" fill="{D}"/>'
    s += f'<path d="M78 56 C92 56 92 40 96 34" stroke="{M}" stroke-width="2" fill="none"/>'
    s += f'<rect x="78" y="8" width="36" height="6" rx="3" fill="{L}"/>'
    s += f'<path d="M82 14 h28 v6 a14 14 0 0 1 -28 0Z" fill="{L}"/><circle cx="96" cy="21" r="7" fill="{D}"/><circle cx="96" cy="21" r="3.5" fill="{a}"/><circle cx="94.5" cy="19.5" r="1.2" fill="#fff"/>'
    s += f'<path d="M30 40 a14 14 0 0 1 20 -18 M24 42 a22 22 0 0 1 32 -28" stroke="{a}" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.8"/>'
    return s


def _gear(cx, cy, r, color, hole):
    s = ""
    for k in range(8):
        s += f'<rect x="{cx - r * 0.22}" y="{cy - r * 1.28}" width="{r * 0.44}" height="{r * 0.5}" rx="1.5" fill="{color}" transform="rotate({k * 45} {cx} {cy})"/>'
    s += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/><circle cx="{cx}" cy="{cy}" r="{r * 0.42}" fill="{hole}"/>'
    return s


def event_config(a):
    s = f'<path d="M44 12 a18 18 0 0 1 18 18 v16 l7 9 h-50 l7 -9 v-16 a18 18 0 0 1 18 -18Z" fill="{L}"/>'
    s += f'<circle cx="44" cy="62" r="5.5" fill="{L}"/><rect x="40.5" y="6" width="7" height="8" rx="3.5" fill="{L}"/>'
    s += f'<path d="M14 24 a30 30 0 0 1 8 -14 M74 24 a30 30 0 0 0 -8 -14" stroke="{a}" stroke-width="3" fill="none" stroke-linecap="round"/>'
    s += _gear(88, 52, 12, a, "#1f2126")
    return s


def storage(a):
    s = f'<rect x="12" y="14" width="64" height="56" rx="4" fill="{L}"/><rect x="12" y="14" width="64" height="14" rx="4" fill="{a}"/><rect x="12" y="24" width="64" height="4" fill="{a}"/>'
    s += f'<rect x="24" y="8" width="5" height="12" rx="2.5" fill="{M}"/><rect x="59" y="8" width="5" height="12" rx="2.5" fill="{M}"/>'
    for row in range(3):
        for col in range(5):
            on = (row, col) in ((0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3), (2, 2))
            s += f'<rect x="{18 + col * 11}" y="{34 + row * 11}" width="8" height="7" rx="1" fill="{a if on else M}" opacity="{1 if on else 0.5}"/>'
    s += f'<rect x="80" y="30" width="32" height="42" rx="4" fill="{B}" stroke="{M}" stroke-width="1.2"/>'
    s += f'<circle cx="96" cy="47" r="10" fill="{D}" stroke="{M}" stroke-width="1.2"/><circle cx="96" cy="47" r="2.5" fill="{M}"/><path d="M96 47 L104 58" stroke="{L}" stroke-width="2"/>'
    s += f'<circle cx="88" cy="65" r="2.5" fill="{a}"/>'
    return s


def accounts(a):
    s = f'<circle cx="78" cy="26" r="12" fill="{M}"/><path d="M56 66 a22 20 0 0 1 44 0Z" fill="{M}"/>'
    s += f'<circle cx="48" cy="28" r="14" fill="{L}"/><path d="M22 70 a26 24 0 0 1 52 0Z" fill="{L}"/>'
    s += f'<path d="M92 50 l10 3 v8 c0 6 -5 9 -10 11 c-5 -2 -10 -5 -10 -11 v-8Z" fill="{a}" stroke="#1f2126" stroke-width="2"/>'
    s += f'<path d="M88 61 l3 3 l5 -6" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>'
    return s


def settings(a):
    s = _gear(40, 40, 18, L, "#1f2126")
    s += f'<circle cx="40" cy="40" r="4" fill="{a}"/>'
    for i, pos in enumerate((0.3, 0.7, 0.45)):
        y = 22 + i * 18
        s += f'<rect x="72" y="{y - 1.5}" width="38" height="3" rx="1.5" fill="{M}"/><circle cx="{72 + 38 * pos}" cy="{y}" r="5" fill="{a if i == 1 else L}" stroke="#1f2126" stroke-width="1.5"/>'
    return s


ART = {f.__name__: f for f in (main_view, playback, archive, wall, local_files, board, events, emap, summary,
                               access, parking, patrol, evidence, devices, event_config, storage, accounts,
                               settings)}


def svg(key, section, scale=2):
    """SVG-разметка иллюстрации раздела key; None, если картинки нет."""
    draw = ART.get(key)
    if draw is None:
        return None
    accent = ACCENTS.get(section, "#e8232c")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{120 * scale}" height="{80 * scale}" '
            f'viewBox="0 0 120 80">{draw(accent)}</svg>')
