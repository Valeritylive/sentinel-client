"""Объёмные (изометрические 3D) иллюстрации для плиток разделов на «Главной».

Картинки собираются из примитивов (параллелепипеды, экраны, цилиндры, сферы) с
освещением и подсветкой и встраиваются как SVG — отдельных файлов не нужно.
Поле 240×160, свет сверху слева, у каждой группы разделов свой цвет подсветки.
"""
from __future__ import annotations

import math

ACCENTS = {"Просмотр": "#ff3b4a", "Мониторинг": "#ffb020",
           "Доступ и безопасность": "#19d3b8", "Управление": "#3d9bff"}

C30 = math.cos(math.radians(30))
K = 1.1                  # масштаб сцены
OX, OY = 120.0, 40.0


def P(x, y, z=0.0):
    """Изометрическая проекция точки мира в координаты картинки."""
    return OX + (x - y) * C30 * K, OY + ((x + y) * 0.5 - z) * K


def _pts(points):
    return " ".join(f"{px:.1f},{py:.1f}" for px, py in points)


def _shade(color, k):
    """Осветлить (k > 0) или затемнить (k < 0) цвет #rrggbb."""
    color = color.lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    if k >= 0:
        r, g, b = (int(c + (255 - c) * k) for c in (r, g, b))
    else:
        r, g, b = (int(c * (1 + k)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


class Scene:
    def __init__(self, accent):
        self.accent = accent
        self.defs = []
        self.items = []
        self._ids = 0

    # ------------------------------------------------------------ служебное
    def uid(self, prefix="g"):
        self._ids += 1
        return f"{prefix}{self._ids}"

    def add(self, markup):
        self.items.append(markup)

    def lin(self, stops, x1=0, y1=0, x2=0, y2=1):
        gid = self.uid("l")
        body = "".join(f'<stop offset="{o}" stop-color="{c}"{f" stop-opacity={chr(34)}{a}{chr(34)}" if a is not None else ""}/>'
                       for o, c, a in ((s + (None,))[:3] for s in stops))
        self.defs.append(f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">{body}</linearGradient>')
        return f"url(#{gid})"

    def rad(self, stops, cx=0.5, cy=0.5, r=0.5, fx=None, fy=None):
        gid = self.uid("r")
        body = "".join(f'<stop offset="{o}" stop-color="{c}"{f" stop-opacity={chr(34)}{a}{chr(34)}" if a is not None else ""}/>'
                       for o, c, a in ((s + (None,))[:3] for s in stops))
        focus = f' fx="{fx}" fy="{fy}"' if fx is not None else ""
        self.defs.append(f'<radialGradient id="{gid}" cx="{cx}" cy="{cy}" r="{r}"{focus}>{body}</radialGradient>')
        return f"url(#{gid})"

    def glow_filter(self, blur=3.0):
        fid = self.uid("f")
        self.defs.append(f'<filter id="{fid}" x="-50%" y="-50%" width="200%" height="200%">'
                         f'<feGaussianBlur stdDeviation="{blur}"/></filter>')
        return f"url(#{fid})"

    # ------------------------------------------------------------ окружение
    def room(self, size=92):
        a = self.accent
        bg = self.rad([(0, _shade(a, -0.55)), (0.55, "#141a2a"), (1, "#0b0e16")], cx=0.5, cy=0.35, r=0.75)
        self.add(f'<rect width="240" height="160" fill="{bg}"/>')
        # пол-подиум с сеткой и неоновой кромкой
        floor = self.lin([(0, "#232b40"), (1, "#121725")], 0, 0, 1, 1)
        corners = [P(0, 0), P(size, 0), P(size, size), P(0, size)]
        edge = [P(size, 0, 0), P(size, size, 0), P(0, size, 0)]
        below = [P(size, 0, -6), P(size, size, -6), P(0, size, -6)]
        self.add(f'<polygon points="{_pts([edge[0], edge[1], below[1], below[0]])}" fill="#0d111c"/>')
        self.add(f'<polygon points="{_pts([edge[1], edge[2], below[2], below[1]])}" fill="#151b2b"/>')
        self.add(f'<polygon points="{_pts(corners)}" fill="{floor}"/>')
        lines = ""
        for k in range(1, 6):
            t = size * k / 6
            lines += f'<polyline points="{_pts([P(t, 0), P(t, size)])}"/><polyline points="{_pts([P(0, t), P(size, t)])}"/>'
        self.add(f'<g stroke="#3a4766" stroke-width="0.5" opacity="0.6" fill="none">{lines}</g>')
        glow = self.glow_filter(2.2)
        self.add(f'<polyline points="{_pts(edge)}" fill="none" stroke="{a}" stroke-width="3" opacity="0.7" filter="{glow}"/>')
        self.add(f'<polyline points="{_pts(edge)}" fill="none" stroke="{_shade(a, 0.4)}" stroke-width="1"/>')

    def spot(self, x, y, r=30, opacity=0.35):
        """Пятно света на полу под предметом."""
        cx, cy = P(x, y)
        fill = self.rad([(0, self.accent, opacity), (1, self.accent, 0)])
        self.add(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{r * 1.22 * K:.1f}" ry="{r * 0.71 * K:.1f}" fill="{fill}"/>')

    def shadow(self, x, y, w, d, opacity=0.45, grow=3):
        pts = [P(x - grow, y - grow + 2), P(x + w + grow, y - grow + 2), P(x + w + grow, y + d + grow + 2),
               P(x - grow, y + d + grow + 2)]
        blur = self.glow_filter(2.5)
        self.add(f'<polygon points="{_pts(pts)}" fill="#000" opacity="{opacity}" filter="{blur}"/>')

    # ------------------------------------------------------------ примитивы
    def box(self, x, y, z, w, d, h, color, top=None, edge=True):
        """Параллелепипед: видны верх, грань +y (слева) и грань +x (справа)."""
        tp = [P(x, y, z + h), P(x + w, y, z + h), P(x + w, y + d, z + h), P(x, y + d, z + h)]
        lf = [P(x, y + d, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x, y + d, z + h)]
        rt = [P(x + w, y, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x + w, y, z + h)]
        left = self.lin([(0, _shade(color, -0.05)), (1, _shade(color, -0.3))])
        right = self.lin([(0, _shade(color, -0.35)), (1, _shade(color, -0.55))])
        topc = top or self.lin([(0, _shade(color, 0.35)), (1, _shade(color, 0.1))], 0, 0, 1, 1)
        stroke = f' stroke="{_shade(color, 0.45)}" stroke-width="0.5" stroke-linejoin="round"' if edge else ""
        self.add(f'<polygon points="{_pts(lf)}" fill="{left}"{stroke}/>'
                 f'<polygon points="{_pts(rt)}" fill="{right}"{stroke}/>'
                 f'<polygon points="{_pts(tp)}" fill="{topc}"{stroke}/>')

    def face_y(self, x, y, z, w, h, content):
        """Содержимое на вертикальной грани y=const (лицом влево), поле w×h, ось Y вниз."""
        x0, y0 = P(x, y, z + h)
        self.add(f'<g transform="matrix({C30 * K:.4f},{0.5 * K:.4f},0,{K:.4f},{x0:.2f},{y0:.2f})">{content}</g>')

    def face_x(self, x, y, z, d, h, content):
        """Содержимое на вертикальной грани x=const (лицом вправо), поле d×h."""
        x0, y0 = P(x, y + d, z + h)
        self.add(f'<g transform="matrix({C30 * K:.4f},{-0.5 * K:.4f},0,{K:.4f},{x0:.2f},{y0:.2f})">{content}</g>')

    def face_top(self, x, y, z, content):
        """Содержимое на горизонтальной плоскости z (ось X вдоль x мира, ось Y вдоль y мира)."""
        x0, y0 = P(x, y, z)
        self.add(f'<g transform="matrix({C30 * K:.4f},{0.5 * K:.4f},{-C30 * K:.4f},{0.5 * K:.4f},{x0:.2f},{y0:.2f})">{content}</g>')

    def screen_y(self, x, y, z, w, h, content, frame="#1b1f29", depth=2.5, glow=True):
        """Монитор на грани y: корпус и светящийся экран с содержимым."""
        self.box(x, y - depth, z, w, depth, h, frame)
        if glow:
            gx, gy = P(x + w / 2, y, z + h / 2)
            halo = self.rad([(0, self.accent, 0.35), (1, self.accent, 0)])
            self.add(f'<ellipse cx="{gx:.1f}" cy="{gy:.1f}" rx="{w * 0.9:.1f}" ry="{h * 0.95:.1f}" fill="{halo}"/>')
        inner = f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" fill="#06080d"/>' \
                f'<svg x="1" y="1" width="{w - 2}" height="{h - 2}" viewBox="0 0 100 60" preserveAspectRatio="none">{content}</svg>'
        shine = self.lin([(0, "#ffffff", 0.18), (0.5, "#ffffff", 0.0)], 0, 0, 1, 1)
        inner += f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" fill="{shine}"/>'
        self.face_y(x, y, z, w, h, inner)

    def screen_x(self, x, y, z, d, h, content, frame="#1b1f29", depth=2.5):
        self.box(x - depth, y, z, depth, d, h, frame)
        inner = f'<rect x="1" y="1" width="{d - 2}" height="{h - 2}" fill="#06080d"/>' \
                f'<svg x="1" y="1" width="{d - 2}" height="{h - 2}" viewBox="0 0 100 60" preserveAspectRatio="none">{content}</svg>'
        shine = self.lin([(0, "#ffffff", 0.16), (0.5, "#ffffff", 0.0)], 0, 0, 1, 1)
        inner += f'<rect x="1" y="1" width="{d - 2}" height="{h - 2}" fill="{shine}"/>'
        self.face_x(x, y, z, d, h, inner)

    def cylinder(self, x, y, z, r, h, color, top=None):
        cx, cb = P(x, y, z)
        _cx, ct = P(x, y, z + h)
        rx, ry = r * 1.2247 * K, r * 0.7071 * K
        body = self.lin([(0, _shade(color, -0.1)), (0.35, _shade(color, 0.25)), (1, _shade(color, -0.6))], 0, 0, 1, 0)
        self.add(f'<path d="M{cx - rx:.1f} {ct:.1f} V{cb:.1f} A{rx:.1f} {ry:.1f} 0 0 0 {cx + rx:.1f} {cb:.1f} V{ct:.1f}Z" fill="{body}"/>')
        topc = top or self.lin([(0, _shade(color, 0.45)), (1, _shade(color, 0.15))], 0, 0, 1, 1)
        self.add(f'<ellipse cx="{cx:.1f}" cy="{ct:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" fill="{topc}" stroke="{_shade(color, 0.5)}" stroke-width="0.5"/>')

    def sphere(self, x, y, z, r, color):
        cx, cy = P(x, y, z)
        fill = self.rad([(0, _shade(color, 0.6)), (0.45, color), (1, _shade(color, -0.6))], 0.5, 0.5, 0.55, 0.35, 0.3)
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r * K:.1f}" fill="{fill}"/>')

    def beam(self, points, color=None, width=2.0, blur=2.0, dash=None):
        """Светящаяся линия (маршрут, кабель)."""
        color = color or self.accent
        glow = self.glow_filter(blur)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        pts = _pts(points)
        self.add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width * 2.5}" opacity="0.55" filter="{glow}" stroke-linecap="round" stroke-linejoin="round"{dash_attr}/>'
                 f'<polyline points="{pts}" fill="none" stroke="{_shade(color, 0.5)}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{dash_attr}/>')

    def chair(self, x, y):
        self.shadow(x, y, 10, 10, 0.35, 1)
        self.cylinder(x + 5, y + 5, 0, 1.2, 8, "#2a2f3a")
        self.box(x, y, 8, 10, 10, 2.5, "#2a2f3a")
        self.box(x, y + 8, 10.5, 10, 2.5, 13, "#262b35")

    def svg(self, scale=2):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{240 * scale}" height="{160 * scale}" '
                f'viewBox="0 0 240 160"><defs>{"".join(self.defs)}</defs>{"".join(self.items)}</svg>')


# ---------------------------------------------------------------- содержимое экранов (поле 100×60)
def cam(kind=0):
    """Кадр с камеры: улица, стоянка, двор, ночь."""
    skies = (("#7fb6ff", "#cfe6ff"), ("#5a8fd6", "#b6d4ff"), ("#ffb36b", "#ffe1b0"), ("#0f1b3d", "#2c3e78"))
    top, low = skies[kind % 4]
    g = f'<defs><linearGradient id="sk{kind}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{low}"/></linearGradient></defs>'
    g += f'<rect width="100" height="60" fill="url(#sk{kind})"/>'
    if kind % 4 == 0:
        g += '<rect x="0" y="34" width="100" height="26" fill="#6f7a86"/><path d="M38 60 L48 34 H54 L70 60Z" fill="#9aa4ae"/>'
        g += '<rect x="6" y="12" width="20" height="24" fill="#d9dee6"/><rect x="70" y="6" width="24" height="30" fill="#c3cad6"/>'
        g += "".join(f'<rect x="{9 + c * 6}" y="{15 + r * 6}" width="3" height="3" fill="#6b88b8"/>' for c in range(3) for r in range(3))
        g += '<rect x="58" y="44" width="16" height="7" rx="2" fill="#e04848"/><rect x="20" y="48" width="14" height="6" rx="2" fill="#f2f2f2"/>'
    elif kind % 4 == 1:
        g += '<rect x="0" y="30" width="100" height="30" fill="#5f6b78"/>'
        for i, col in enumerate(("#e9edf2", "#3b6fd1", "#e04848", "#2e2e2e", "#d8b44a")):
            g += f'<rect x="{4 + i * 20}" y="{38 + (i % 2) * 8}" width="15" height="8" rx="2" fill="{col}"/>'
        g += '<path d="M0 30 L100 30" stroke="#fff" stroke-width="1"/><rect x="10" y="10" width="80" height="20" fill="#9fb2c7"/>'
    elif kind % 4 == 2:
        g += '<rect x="0" y="36" width="100" height="24" fill="#4f8a5b"/><circle cx="80" cy="14" r="7" fill="#fff3c4"/>'
        g += '<rect x="14" y="16" width="34" height="22" fill="#e8dcc8"/><path d="M10 18 L31 4 L52 18Z" fill="#b8503f"/>'
        g += '<rect x="26" y="26" width="8" height="12" fill="#6b4a33"/><circle cx="66" cy="34" r="7" fill="#2f6b3a"/><rect x="65" y="34" width="2" height="8" fill="#5a3b25"/>'
    else:
        g += '<rect x="0" y="34" width="100" height="26" fill="#1a2238"/>'
        for i in range(6):
            g += f'<rect x="{4 + i * 16}" y="{14 + (i % 3) * 5}" width="12" height="{22 - (i % 3) * 5}" fill="#273457"/>'
            g += f'<rect x="{7 + i * 16}" y="{18 + (i % 3) * 5}" width="2.5" height="2.5" fill="#ffd36b"/>'
        g += '<circle cx="30" cy="48" r="3" fill="#ffe9a8"/><circle cx="70" cy="48" r="3" fill="#ffe9a8"/>'
    return g


def grid(n, kinds=(0, 1, 2, 3)):
    """Сетка n×n кадров."""
    cell_w, cell_h = 100 / n, 60 / n
    out = ""
    for r in range(n):
        for c in range(n):
            k = kinds[(r * n + c) % len(kinds)]
            out += f'<svg x="{c * cell_w + 0.6}" y="{r * cell_h + 0.6}" width="{cell_w - 1.2}" height="{cell_h - 1.2}" viewBox="0 0 100 60" preserveAspectRatio="none">{cam(k)}</svg>'
    return out


def world_map(color):
    out = '<rect width="100" height="60" fill="#071a33"/>'
    out += "".join(f'<path d="M0 {y} H100" stroke="#12345e" stroke-width="0.5"/>' for y in range(6, 60, 8))
    out += "".join(f'<path d="M{x} 0 V60" stroke="#12345e" stroke-width="0.5"/>' for x in range(6, 100, 10))
    out += ('<path d="M10 18 q8 -8 18 -4 q6 6 2 12 q-8 6 -14 2 q-8 -4 -6 -10Z M40 14 q14 -6 26 0 q10 6 4 14 q-10 4 -18 0 '
            'q-12 -4 -12 -14Z M70 34 q8 -2 12 4 q2 8 -6 10 q-8 0 -6 -14Z M30 36 q8 0 8 8 q-2 8 -8 6 q-4 -6 0 -14Z" fill="#1f6fb0"/>')
    for x, y in ((22, 18), (52, 20), (78, 40), (34, 42)):
        out += f'<circle cx="{x}" cy="{y}" r="2" fill="{color}"/><circle cx="{x}" cy="{y}" r="5" fill="none" stroke="{color}" stroke-width="0.7"/>'
    out += f'<path d="M22 18 Q38 4 52 20 T78 40" stroke="{color}" stroke-width="0.8" fill="none" stroke-dasharray="2 1.5"/>'
    return out


def dashboard(color):
    out = '<rect width="100" height="60" fill="#0c1424"/>'
    out += f'<rect x="3" y="3" width="44" height="26" rx="2" fill="#13203a"/><rect x="51" y="3" width="46" height="26" rx="2" fill="#13203a"/>'
    out += f'<rect x="3" y="32" width="94" height="25" rx="2" fill="#13203a"/>'
    out += f'<path d="M12 24 a13 13 0 0 1 26 0" stroke="#2c3e63" stroke-width="5" fill="none"/><path d="M12 24 a13 13 0 0 1 20 -11" stroke="{color}" stroke-width="5" fill="none"/>'
    for i, h in enumerate((8, 14, 10, 19, 12, 22)):
        out += f'<rect x="{56 + i * 7}" y="{26 - h}" width="5" height="{h}" fill="{color if i in (3, 5) else "#4da3ff"}"/>'
    out += f'<polyline points="6,52 18,44 30,48 44,38 58,42 72,34 86,40 94,36" fill="none" stroke="{color}" stroke-width="1.6"/>'
    out += '<polyline points="6,54 20,50 34,52 48,46 62,49 76,44 94,46" fill="none" stroke="#4da3ff" stroke-width="1.2"/>'
    return out


def timeline(color):
    out = cam(2)
    out += '<rect x="0" y="48" width="100" height="12" fill="#0b0f18" opacity="0.85"/>'
    out += f'<rect x="6" y="52" width="88" height="3" rx="1.5" fill="#33405a"/><rect x="6" y="52" width="58" height="3" rx="1.5" fill="{color}"/>'
    out += f'<circle cx="64" cy="53.5" r="3" fill="#fff"/><path d="M44 20 L60 30 L44 40Z" fill="#fff" opacity="0.9"/>'
    return out


# ---------------------------------------------------------------- сцены
def main_view(s):
    s.room()
    s.spot(46, 50, 42)
    s.shadow(8, 36, 74, 24)
    s.box(8, 36, 0, 74, 24, 14, "#d9dde5")
    s.box(8, 36, 14, 74, 24, 1.2, "#eef1f6")
    for i, (x, kinds) in enumerate(((10, (0, 1, 2, 3)), (34, (1, 3, 0, 2)), (58, (2, 0, 3, 1)))):
        s.box(x + 9, 40, 15.2, 4, 3, 4, "#2a2f3a")
        s.screen_y(x, 43, 19, 22, 15, grid(2, kinds))
    rx, ry = P(11.5, 43, 33)
    s.add(f'<rect x="{rx - 1:.1f}" y="{ry - 2.5:.1f}" width="16" height="6" rx="3" fill="#0b0e16" opacity="0.85"/>'
          f'<circle cx="{rx + 2.5:.1f}" cy="{ry + 0.5:.1f}" r="1.7" fill="{s.accent}"/>'
          f'<text x="{rx + 5.2:.1f}" y="{ry + 2.3:.1f}" font-size="5" font-family="sans-serif" font-weight="700" fill="#fff">LIVE</text>')
    s.chair(24, 68)
    s.chair(54, 68)


def playback(s):
    s.room()
    s.spot(40, 40, 42)
    s.box(36, 30, 0, 8, 8, 4, "#2a2f3a")
    s.box(38.5, 32.5, 4, 3, 3, 10, "#2a2f3a")
    s.screen_y(12, 34, 12, 56, 32, timeline(s.accent))
    # объёмная шкала времени на полу
    for i, (w, col) in enumerate(((16, "#3d9bff"), (10, s.accent), (22, "#3d9bff"), (8, "#ffb020"), (12, s.accent))):
        x = 8 + sum(v for v, _ in ((16, 0), (10, 0), (22, 0), (8, 0), (12, 0))[:i]) + i * 1.5
        s.box(x, 62, 0, w, 7, 2.5, col)
    s.box(52, 58, 0, 2, 14, 9, "#ffffff")
    cx, cy = P(80, 70, 14)
    s.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="13" fill="{s.accent}"/><circle cx="{cx:.1f}" cy="{cy:.1f}" r="13" fill="none" stroke="#fff" stroke-opacity="0.5"/>'
          f'<path d="M{cx - 4:.1f} {cy - 6:.1f} L{cx + 7:.1f} {cy:.1f} L{cx - 4:.1f} {cy + 6:.1f}Z" fill="#fff"/>')


def archive(s):
    s.room()
    s.spot(30, 30, 34)
    s.shadow(14, 16, 26, 22)
    s.box(14, 16, 0, 26, 22, 46, "#2b3242")
    for k in range(5):
        z = 6 + k * 7.5
        s.face_y(16, 38, z, 22, 6, f'<rect width="22" height="6" rx="1" fill="#141925"/>'
                 + "".join(f'<rect x="{2 + j * 4.5}" y="1.5" width="3.5" height="3" rx="0.6" fill="#2d3547"/>' for j in range(3))
                 + f'<circle cx="17" cy="3" r="1" fill="{"#38e07b" if k % 3 else s.accent}"/><circle cx="20" cy="3" r="1" fill="#3d9bff"/>')
    # кассеты-клипы
    for i in range(3):
        s.screen_y(46 + i * 4, 52 - i * 6, 0 + i * 9, 26, 16, cam(i), depth=1.5, glow=False)
    # лупа
    cx, cy = P(78, 68, 22)
    glass = s.rad([(0, "#bfe3ff", 0.55), (0.7, "#5aa9ff", 0.25), (1, "#1d5fa8", 0.5)], 0.4, 0.35, 0.6)
    s.add(f'<path d="M{cx + 9:.1f} {cy + 9:.1f} L{cx + 22:.1f} {cy + 24:.1f}" stroke="#20242e" stroke-width="7" stroke-linecap="round"/>'
          f'<path d="M{cx + 9:.1f} {cy + 9:.1f} L{cx + 22:.1f} {cy + 24:.1f}" stroke="{s.accent}" stroke-width="4" stroke-linecap="round"/>'
          f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="15" fill="{glass}" stroke="#e6e9ef" stroke-width="3.5"/>'
          f'<path d="M{cx - 9:.1f} {cy - 4:.1f} a10 10 0 0 1 8 -7" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" opacity="0.8"/>')


def wall(s):
    s.room()
    s.spot(46, 20, 50, 0.45)
    # стена экранов
    s.box(2, 2, 0, 88, 6, 44, "#1e2433")
    contents = (world_map(s.accent), cam(0), dashboard(s.accent), cam(3), world_map("#19d3b8"), cam(1),
                cam(2), dashboard("#3d9bff"), cam(3), world_map(s.accent), cam(0), cam(1))
    for r in range(3):
        for c in range(4):
            s.screen_y(5 + c * 21, 8, 29 - r * 12.5, 20.4, 12, contents[r * 4 + c], depth=0.6, glow=False)
    gx, gy = P(46, 8, 26)
    halo = s.rad([(0, "#4da3ff", 0.35), (1, "#4da3ff", 0)])
    s.add(f'<ellipse cx="{gx:.1f}" cy="{gy:.1f}" rx="90" ry="50" fill="{halo}"/>')
    # пульт операторов
    s.shadow(16, 52, 60, 16)
    s.box(16, 52, 0, 60, 16, 12, "#dde1e9")
    for i, x in enumerate((18, 37, 56)):
        s.screen_y(x, 55, 13, 17, 11, grid(2, (i, i + 1, i + 2, i + 3)), depth=1.5, glow=False)
    s.chair(26, 74)
    s.chair(50, 74)


def local_files(s):
    s.room()
    s.spot(44, 46, 40)
    s.shadow(16, 34, 54, 34)
    folder = "#f0b43c"
    s.box(16, 34, 0, 54, 34, 20, folder)
    s.box(16, 34, 20, 22, 8, 3, _shade(folder, 0.1))
    # фотографии, торчащие из папки
    for i, k in enumerate((2, 0, 1)):
        s.screen_y(22 + i * 14, 44 + i * 2, 18 + i * 3, 20, 15, cam(k), frame="#f3f5f8", depth=0.8, glow=False)
    s.box(16, 58, 0, 54, 10, 14, _shade(folder, 0.05))
    # стрелка «скачать»
    ax, ay = P(78, 68, 34)
    s.add(f'<circle cx="{ax:.1f}" cy="{ay + 3:.1f}" r="15" fill="{_shade(s.accent, -0.4)}"/>'
          f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="15" fill="{s.accent}"/>'
          f'<path d="M{ax:.1f} {ay - 8:.1f} V{ay + 5:.1f} M{ax - 6:.1f} {ay:.1f} L{ax:.1f} {ay + 6:.1f} L{ax + 6:.1f} {ay:.1f}" '
          f'stroke="#fff" stroke-width="3.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')


def board(s):
    s.room()
    s.spot(40, 30, 46)
    s.box(30, 18, 0, 16, 8, 3, "#2a2f3a")
    s.box(36, 20, 3, 4, 3, 13, "#2a2f3a")
    s.screen_y(8, 26, 14, 62, 34, dashboard(s.accent))
    # объёмная диаграмма рядом
    for i, h in enumerate((10, 18, 13, 26)):
        s.box(58 + i * 8, 64, 0, 6, 6, h, (s.accent, "#3d9bff", "#19d3b8", s.accent)[i])
    s.beam([P(58, 76, 14), P(66, 76, 22), P(74, 76, 18), P(82, 76, 30)], "#ffffff", 1.2, 1.5)


def events(s):
    s.room()
    s.spot(36, 40, 38)
    # маяк тревоги
    s.cylinder(30, 40, 0, 12, 6, "#2a2f3a")
    glow = s.glow_filter(5)
    cx, cy = P(30, 40, 22)
    s.add(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="26" ry="20" fill="{s.accent}" opacity="0.55" filter="{glow}"/>')
    s.cylinder(30, 40, 6, 9, 20, s.accent)
    s.sphere(30, 40, 29, 10.5, _shade(s.accent, 0.1))
    for r in (24, 32):
        s.add(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{r}" ry="{r * 0.6}" fill="none" stroke="{s.accent}" stroke-width="1" opacity="0.5"/>')
    # карточки событий
    for i in range(3):
        col = (s.accent, "#3d9bff", "#19d3b8")[i]
        s.screen_y(52 + i * 6, 70 - i * 10, 10 + i * 10, 30, 12,
                   f'<rect width="100" height="60" fill="#121a2b"/><rect x="0" y="0" width="6" height="60" fill="{col}"/>'
                   f'<svg x="10" y="8" width="34" height="44" viewBox="0 0 100 60" preserveAspectRatio="none">{cam(i)}</svg>'
                   f'<rect x="50" y="12" width="40" height="8" rx="3" fill="#e6e9ef"/><rect x="50" y="28" width="30" height="7" rx="3" fill="#6f7a92"/>'
                   f'<rect x="50" y="42" width="22" height="7" rx="3" fill="{col}"/>', depth=1.2, glow=False)


def emap(s):
    s.room()
    s.spot(46, 46, 50, 0.3)
    # карта-плита с рекой и кварталами
    s.box(6, 6, 0, 80, 80, 3, "#1b2a3c")
    s.face_top(6, 6, 3, '<rect width="80" height="80" fill="#2b5f45"/>'
               '<path d="M0 52 C20 44 30 62 48 54 S70 30 80 34 V46 C70 44 56 64 44 66 S18 58 0 62Z" fill="#2f7fd0"/>'
               '<path d="M0 24 H80 M30 0 V80 M62 0 V80" stroke="#c9ced8" stroke-width="3"/>')
    for x, y, h, c in ((10, 10, 10, "#cfd6e2"), (36, 10, 16, "#aeb9cb"), (66, 8, 12, "#d8dde6"), (10, 30, 8, "#c2cad8"),
                       (36, 30, 22, "#9fb0c8"), (68, 28, 9, "#cfd6e2"), (66, 62, 7, "#d9dee6")):
        s.box(x, y, 3, 12, 10, h, c)
    # метки
    for x, y, col in ((50, 46, s.accent), (22, 70, "#3d9bff"), (76, 46, "#19d3b8")):
        px, py = P(x, y, 3)
        top_y = py - 28
        s.add(f'<ellipse cx="{px:.1f}" cy="{py:.1f}" rx="6" ry="3" fill="#000" opacity="0.4"/>'
              f'<path d="M{px:.1f} {py:.1f} L{px - 7:.1f} {top_y + 9:.1f} A9 9 0 1 1 {px + 7:.1f} {top_y + 9:.1f}Z" fill="{col}"/>'
              f'<circle cx="{px:.1f}" cy="{top_y + 3:.1f}" r="3.6" fill="#fff"/>')


def summary(s):
    s.room()
    s.spot(40, 40, 46)
    s.box(8, 8, 0, 70, 70, 1.5, "#1d2638")
    colors = ("#3d9bff", "#19d3b8", "#ffb020", s.accent)
    for row in range(2):
        for col in range(4):
            h = (14, 22, 18, 34, 10, 26, 20, 40)[row * 4 + col]
            s.box(14 + col * 14, 20 + row * 22, 1.5, 9, 9, h, colors[col])
    # объёмная круговая диаграмма
    cx, cy = P(84, 70, 8)
    rx, ry = 22, 12
    s.add(f'<path d="M{cx - rx} {cy} v6 a{rx} {ry} 0 0 0 {2 * rx} 0 v-6Z" fill="{_shade(s.accent, -0.45)}"/>')
    s.add(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="#3d9bff"/>')
    s.add(f'<path d="M{cx} {cy} L{cx} {cy - ry} A{rx} {ry} 0 0 1 {cx + rx} {cy}Z" fill="{s.accent}"/>')
    s.add(f'<path d="M{cx} {cy} L{cx + rx} {cy} A{rx} {ry} 0 0 1 {cx + 4} {cy + ry - 0.3}Z" fill="#19d3b8"/>')


def access(s):
    s.room()
    s.spot(40, 40, 40)
    wallc = "#2f3647"
    s.box(4, 20, 0, 70, 6, 46, wallc)
    # дверь
    s.box(14, 26, 0, 24, 1.5, 40, "#8a5a36")
    s.face_y(14, 27.5, 0, 26, 40, '<rect x="3" y="4" width="20" height="16" rx="1" fill="#7a4e2e"/><rect x="3" y="24" width="20" height="18" rx="1" fill="#7a4e2e"/>'
             '<rect x="19" y="22" width="5" height="2" rx="1" fill="#e6e9ef"/>')
    # считыватель
    s.box(46, 26, 18, 10, 2, 16, "#1b1f29")
    s.face_y(46, 28, 18, 10, 16, f'<rect width="10" height="16" rx="1.5" fill="#141925"/><circle cx="5" cy="6" r="3" fill="none" stroke="{s.accent}" stroke-width="1"/>'
             f'<circle cx="5" cy="6" r="1" fill="{s.accent}"/><rect x="2" y="12" width="6" height="1.6" rx="0.8" fill="#38e07b"/>')
    glow = s.glow_filter(3)
    gx, gy = P(51, 28, 28)
    s.add(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="8" fill="{s.accent}" opacity="0.5" filter="{glow}"/>')
    # турникет
    s.shadow(52, 52, 10, 26)
    s.box(52, 52, 0, 10, 26, 18, "#c9ced8")
    s.box(62, 60, 12, 18, 2, 2, "#e6e9ef")
    # карта доступа
    cx, cy = P(80, 66, 34)
    s.add(f'<g transform="translate({cx:.1f} {cy:.1f}) rotate(-20)">'
          f'<rect x="-15" y="-10" width="30" height="20" rx="3" fill="#000" opacity="0.3" transform="translate(2 3)"/>'
          f'<rect x="-15" y="-10" width="30" height="20" rx="3" fill="#f5f7fa"/><rect x="-15" y="-10" width="30" height="6" rx="3" fill="{s.accent}"/>'
          f'<rect x="-11" y="-1" width="7" height="6" rx="1" fill="#d8b44a"/><rect x="-1" y="0" width="12" height="2" fill="#9aa4b2"/><rect x="-1" y="4" width="8" height="2" fill="#9aa4b2"/></g>')


def parking(s):
    s.room()
    s.spot(50, 50, 48, 0.25)
    s.face_top(4, 4, 0.1, '<rect width="84" height="84" fill="#2b3140"/>'
               + "".join(f'<rect x="{6 + i * 20}" y="4" width="1.6" height="30" fill="#e8ecf2"/>' for i in range(5))
               + '<rect x="0" y="40" width="84" height="22" fill="#232835"/>'
               + "".join(f'<rect x="{4 + i * 12}" y="50" width="7" height="1.6" fill="#ffd34a"/>' for i in range(7)))
    # машины на местах
    def car(x, y, color, rot=False):
        w, d = (14, 26) if not rot else (26, 14)
        s.shadow(x, y, w, d, 0.4, 1)
        s.box(x, y, 1.5, w, d, 6, color)
        if not rot:
            s.box(x + 1.5, y + 6, 7.5, w - 3, d - 13, 5, _shade(color, 0.15), top=_shade(color, 0.3))
            s.face_y(x + 1.5, y + d - 7, 7.5, w - 3, 5, f'<rect width="{w - 3}" height="5" fill="#1d2a3c"/>')
        else:
            s.box(x + 6, y + 1.5, 7.5, w - 13, d - 3, 5, _shade(color, 0.15), top=_shade(color, 0.3))
            s.face_x(x + w - 7, y + 1.5, 7.5, d - 3, 5, f'<rect width="{d - 3}" height="5" fill="#1d2a3c"/>')
    car(9, 6, "#e04848")
    car(29, 6, "#3d7bd9")
    car(69, 6, "#e9edf2")
    car(36, 46, "#ffb020", rot=True)
    # шлагбаум
    s.box(74, 66, 0, 6, 6, 14, "#d9dde5")
    s.add(f'<polygon points="{_pts([P(77, 68, 13), P(77, 68, 16), P(77, 30, 16), P(77, 30, 13)])}" fill="#fff"/>')
    for k in range(5):
        y0 = 64 - k * 8
        s.add(f'<polygon points="{_pts([P(77, y0, 13), P(77, y0, 16), P(77, y0 - 4, 16), P(77, y0 - 4, 13)])}" fill="{s.accent}"/>')
    # знак P
    s.cylinder(86, 36, 0, 1, 26, "#c9ced8")
    px, py = P(86, 36, 33)
    s.add(f'<rect x="{px - 7:.1f}" y="{py - 7:.1f}" width="14" height="14" rx="2.5" fill="#2f6fe0" stroke="#fff" stroke-width="1"/>'
          f'<text x="{px:.1f}" y="{py + 4.5:.1f}" font-size="12" font-weight="800" font-family="sans-serif" text-anchor="middle" fill="#fff">P</text>')


def patrol(s):
    s.room()
    route = [(10, 78), (14, 44), (40, 30), (62, 50), (80, 20)]
    s.beam([P(x, y) for x, y in route], s.accent, 1.8, 2.2, "5 3")
    for i, (x, y) in enumerate(route[1:]):
        s.spot(x, y, 10, 0.5)
        s.cylinder(x, y, 0, 4, 10 + i * 2, "#2c3448", top=s.accent)
        cx, cy = P(x, y, 10 + i * 2)
        s.add(f'<path d="M{cx - 3:.1f} {cy:.1f} l2 2 l4 -4" stroke="#fff" stroke-width="1.6" fill="none" stroke-linecap="round"/>')
    # щит охраны
    cx, cy = P(36, 66, 40)
    s.add(f'<path d="M{cx:.1f} {cy - 24:.1f} l20 7 v15 c0 14 -10 22 -20 27 c-10 -5 -20 -13 -20 -27 v-15Z" fill="{_shade(s.accent, -0.45)}" transform="translate(3 3)"/>'
          f'<path d="M{cx:.1f} {cy - 24:.1f} l20 7 v15 c0 14 -10 22 -20 27 c-10 -5 -20 -13 -20 -27 v-15Z" fill="{s.accent}"/>'
          f'<path d="M{cx:.1f} {cy - 24:.1f} l20 7 v15 c0 14 -10 22 -20 27Z" fill="#000" opacity="0.18"/>'
          f'<path d="M{cx - 8:.1f} {cy:.1f} l6 6 l11 -12" stroke="#fff" stroke-width="3.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    s.sphere(80, 20, 22, 5, "#3d9bff")


def evidence(s):
    s.room()
    s.spot(36, 46, 40)
    for i in range(4):
        s.box(12 + i, 36 - i, i * 3, 40, 30, 3, ("#f5f7fa", "#e6e9ef", "#f0e2c4", "#f5f7fa")[i])
    s.face_top(15, 33, 12, '<rect x="4" y="4" width="22" height="16" fill="#2b5f8e"/>'
               + "".join(f'<rect x="4" y="{24 + k * 4}" width="{30 - k * 5}" height="2" fill="#9aa4b2"/>' for k in range(3)))
    # голограмма отпечатка над проектором
    s.box(56, 60, 0, 22, 14, 3, "#1b1f29")
    s.cylinder(67, 67, 3, 3, 1.5, s.accent)
    fx, fy = P(67, 67, 30)
    cone = s.lin([(0, s.accent, 0.0), (1, s.accent, 0.35)])
    bx, by = P(67, 67, 4.5)
    s.add(f'<path d="M{bx:.1f} {by:.1f} L{fx - 22:.1f} {fy + 6:.1f} L{fx + 22:.1f} {fy + 6:.1f}Z" fill="{cone}"/>')
    rings = "".join(f'<path d="M{fx - r:.1f} {fy + 4:.1f} v-{r * 0.3:.1f} a{r:.1f} {r * 1.15:.1f} 0 0 1 {2 * r:.1f} 0 v{r * 0.5:.1f}" '
                    f'stroke="{_shade(s.accent, 0.4)}" stroke-width="2" fill="none" stroke-linecap="round"/>' for r in (4, 8, 12, 16, 20))
    glow = s.glow_filter(2.5)
    s.add(f'<g filter="{glow}" opacity="0.9">{rings}</g>{rings}')
    # замок
    lx, ly = P(30, 74, 26)
    s.add(f'<path d="M{lx - 6:.1f} {ly - 4:.1f} v-6 a6 6 0 0 1 12 0 v6" stroke="#c9ced8" stroke-width="3" fill="none"/>'
          f'<rect x="{lx - 10:.1f}" y="{ly - 5:.1f}" width="20" height="16" rx="3" fill="#ffb020"/><circle cx="{lx:.1f}" cy="{ly + 2:.1f}" r="2.2" fill="#5a3b10"/>')


def devices(s):
    s.room()
    s.spot(30, 50, 34)
    # регистратор
    s.shadow(8, 40, 46, 30)
    s.box(8, 40, 0, 46, 30, 10, "#2b3242")
    s.face_y(8, 70, 0, 46, 10, f'<rect width="46" height="10" fill="#1a1f2b"/>'
             + "".join(f'<rect x="{14 + k * 7}" y="2" width="5.5" height="6" rx="0.8" fill="#2d3547" stroke="#3e4a62" stroke-width="0.4"/>' for k in range(4))
             + f'<circle cx="4" cy="5" r="1.3" fill="#38e07b"/><circle cx="8" cy="5" r="1.3" fill="{s.accent}"/>')
    s.box(10, 42, 10, 42, 26, 0.6, "#3b4458")
    # кабели к камерам
    s.beam([P(54, 52, 5), P(70, 52, 1), P(74, 30, 1), P(74, 26, 30)], s.accent, 1, 1.8)
    s.beam([P(54, 60, 4), P(80, 62, 1), P(80, 74, 1)], "#3d9bff", 1, 1.8)
    # купольная камера на стойке
    s.cylinder(74, 26, 0, 2, 32, "#c9ced8")
    s.cylinder(74, 26, 32, 8, 3, "#e6e9ef")
    s.sphere(74, 26, 30, 7, "#1d2433")
    s.sphere(76, 28, 29, 2.4, s.accent)
    # уличная камера
    s.box(80, 70, 0, 3, 3, 16, "#c9ced8")
    s.box(77, 62, 16, 9, 22, 8, "#eef1f6")
    s.face_y(77, 84, 16, 9, 8, f'<rect width="9" height="8" fill="#d9dde5"/><circle cx="4.5" cy="4" r="3.2" fill="#151a24"/><circle cx="4.5" cy="4" r="1.4" fill="{s.accent}"/>')
    s.box(76, 60, 24, 11, 26, 1.2, "#d9dde5")


def _gear(s, x, y, z, r, color, teeth=10, thick=5):
    cx, cy = P(x, y, z)
    for layer, col in ((thick, _shade(color, -0.45)), (0, color)):
        path = ""
        for k in range(teeth * 2):
            ang = math.pi * k / teeth
            rr = r if k % 2 == 0 else r * 0.8
            a1, a2 = ang - math.pi / teeth / 2, ang + math.pi / teeth / 2
            for a in (a1, a2):
                px, py = cx + rr * math.cos(a), cy + layer + rr * math.sin(a) * 0.62
                path += f"{'M' if not path else 'L'}{px:.1f} {py:.1f} "
        fill = col if layer else s.lin([(0, _shade(color, 0.4)), (1, _shade(color, -0.1))], 0, 0, 1, 1)
        s.add(f'<path d="{path}Z" fill="{fill}"/>')
    s.add(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{r * 0.36:.1f}" ry="{r * 0.22:.1f}" fill="#0e121c"/>')


def event_config(s):
    s.room()
    s.spot(34, 40, 36)
    # колокол
    cx, cy = P(32, 40, 30)
    bell = s.lin([(0, "#ffe08a"), (0.5, "#ffb020"), (1, "#a4620a")], 0, 0, 1, 0)
    s.add(f'<path d="M{cx:.1f} {cy - 20:.1f} c-14 0 -18 12 -18 22 v8 l-6 8 h48 l-6 -8 v-8 c0 -10 -4 -22 -18 -22Z" fill="{bell}"/>'
          f'<ellipse cx="{cx:.1f}" cy="{cy + 18:.1f}" rx="24" ry="4" fill="#a4620a"/><circle cx="{cx:.1f}" cy="{cy + 23:.1f}" r="5" fill="#ffd36b"/>'
          f'<rect x="{cx - 3:.1f}" y="{cy - 25:.1f}" width="6" height="7" rx="3" fill="#ffb020"/>')
    for d in (30, 38):
        s.add(f'<path d="M{cx - d:.1f} {cy - 6:.1f} a{d} {d} 0 0 1 {d * 0.4:.1f} -{d * 0.5:.1f} M{cx + d:.1f} {cy - 6:.1f} a{d} {d} 0 0 0 -{d * 0.4:.1f} -{d * 0.5:.1f}" '
              f'stroke="{s.accent}" stroke-width="2.4" fill="none" stroke-linecap="round" opacity="0.8"/>')
    _gear(s, 68, 62, 6, 22, s.accent)
    _gear(s, 42, 80, 4, 12, "#3d9bff", teeth=8, thick=4)


def storage(s):
    s.room()
    s.spot(30, 36, 40)
    # календарь-расписание
    s.box(8, 26, 0, 46, 4, 40, "#e9edf2")
    s.box(8, 26, 40, 46, 4, 7, s.accent)
    cells = ""
    for r in range(5):
        for c in range(7):
            on = (c + r * 2) % 5 not in (0, 3)
            cells += f'<rect x="{2 + c * 6.2}" y="{3 + r * 7.2}" width="5" height="5.6" rx="0.8" fill="{s.accent if on else "#c3cad6"}" opacity="{1 if on else 0.7}"/>'
    s.face_y(8, 30, 0, 46, 40, cells)
    for x in (16, 44):
        s.cylinder(x, 28, 45, 1.5, 5, "#5a6478")
    # стопка дисков
    for i in range(4):
        s.shadow(58, 48 - 0, 26, 30) if i == 0 else None
        s.box(58, 48, i * 9, 26, 30, 8, "#2b3242")
        s.face_y(58, 78, i * 9, 26, 8, f'<rect width="26" height="8" fill="#1a1f2b"/><rect x="3" y="3" width="14" height="2" rx="1" fill="#3e4a62"/>'
                 f'<circle cx="21" cy="4" r="1.3" fill="{"#38e07b" if i != 2 else s.accent}"/>')
    rx, ry = P(64, 78, 42)
    s.add(f'<circle cx="{rx + 12:.1f}" cy="{ry - 14:.1f}" r="8" fill="{s.accent}"/><circle cx="{rx + 12:.1f}" cy="{ry - 14:.1f}" r="3.5" fill="#fff"/>')


def accounts(s):
    s.room()
    s.spot(40, 46, 44)
    for x, y, col, scale in ((58, 30, "#3d9bff", 0.85), (34, 52, s.accent, 1.0)):
        s.cylinder(x, y, 0, 10 * scale, 2, "#1b2132")
        bx, by = P(x, y, 2)
        body = s.lin([(0, _shade(col, 0.35)), (1, _shade(col, -0.45))], 0, 0, 1, 0)
        w, h = 17 * scale, 26 * scale
        s.add(f'<path d="M{bx - w:.1f} {by:.1f} v-{h * 0.45:.1f} a{w:.1f} {h * 0.55:.1f} 0 0 1 {2 * w:.1f} 0 v{h * 0.45:.1f} a{w:.1f} {w * 0.35:.1f} 0 0 1 -{2 * w:.1f} 0Z" fill="{body}"/>')
        s.sphere(x, y, 2 + h + 9 * scale, 10 * scale, "#f2c9a8")
    # бейдж
    cx, cy = P(78, 70, 22)
    s.add(f'<g transform="translate({cx:.1f} {cy:.1f}) rotate(-12)"><rect x="-14" y="-18" width="28" height="36" rx="3" fill="#000" opacity="0.3" transform="translate(2 3)"/>'
          f'<rect x="-14" y="-18" width="28" height="36" rx="3" fill="#f5f7fa"/><rect x="-14" y="-18" width="28" height="8" rx="3" fill="#19d3b8"/>'
          f'<circle cx="0" cy="0" r="6" fill="#9fb0c8"/><rect x="-9" y="9" width="18" height="2.5" rx="1.2" fill="#6f7a92"/></g>')


def settings(s):
    s.room()
    s.spot(40, 40, 46)
    _gear(s, 30, 38, 8, 26, "#c9ced8", teeth=12, thick=7)
    _gear(s, 58, 70, 5, 16, s.accent, teeth=9, thick=5)
    # пульт с ползунками
    s.box(56, 10, 0, 30, 34, 4, "#1d2433")
    for i, pos in enumerate((0.3, 0.7, 0.5)):
        y = 16 + i * 10
        s.box(60, y, 4, 22, 2, 0.8, "#3b4458")
        col = (s.accent, "#3d9bff", "#19d3b8")[i]
        s.box(60 + 22 * pos - 2, y - 2, 4, 4, 6, 4, col)


ART = {f.__name__: f for f in (main_view, playback, archive, wall, local_files, board, events, emap, summary,
                               access, parking, patrol, evidence, devices, event_config, storage, accounts,
                               settings)}


def svg(key, section, scale=2):
    """SVG-разметка иллюстрации раздела key; None, если картинки нет."""
    draw = ART.get(key)
    if draw is None:
        return None
    scene = Scene(ACCENTS.get(section, "#ff3b4a"))
    draw(scene)
    return scene.svg(scale)
