"""Деление окон просмотра и выбор типа потока (чистая логика, без GTK).

Ячейка — кортеж (col, row, colspan, rowspan) на сетке cols x rows.
"""
from dataclasses import dataclass

STREAM_MAIN, STREAM_SUB, STREAM_THIRD, STREAM_AUTO = "main", "sub", "third", "auto"
STREAM_MODES = (STREAM_MAIN, STREAM_SUB, STREAM_THIRD, STREAM_AUTO)
STREAM_SUFFIX = {STREAM_MAIN: 1, STREAM_SUB: 2, STREAM_THIRD: 3}


@dataclass(frozen=True)
class Division:
    key: str
    title: str
    cols: int
    rows: int
    cells: tuple
    wide: bool = False

    @property
    def count(self):
        return len(self.cells)


def _uniform(cols, rows):
    return tuple((c, r, 1, 1) for r in range(rows) for c in range(cols))


def _big_plus_singles(cols, rows, big):
    """Одна большая ячейка big x big в левом верхнем углу, остальное — единичные."""
    cells = [(0, 0, big, big)]
    for r in range(rows):
        for c in range(cols):
            if c < big and r < big:
                continue
            cells.append((c, r, 1, 1))
    return tuple(cells)


def _ring_with_center(size, center):
    """Кольцо единичных ячеек вокруг центральной center x center."""
    lo = (size - center) // 2
    hi = lo + center
    cells = []
    for r in range(size):
        for c in range(size):
            if lo <= c < hi and lo <= r < hi:
                continue
            cells.append((c, r, 1, 1))
    cells.insert(len(cells) // 2 + 0, (lo, lo, center, center))
    return tuple(cells)


def _mk(prefix, count, cols, rows, cells, wide=False):
    return Division(f"{prefix}{count}", str(count), cols, rows, tuple(cells), wide)


def _build():
    std = [
        _mk("s", 1, 1, 1, _uniform(1, 1)),
        _mk("s", 4, 2, 2, _uniform(2, 2)),
        _mk("s", 6, 3, 3, _big_plus_singles(3, 3, 2)),
        _mk("s", 8, 4, 4, _big_plus_singles(4, 4, 3)),
        _mk("s", 9, 3, 3, _uniform(3, 3)),
        _mk("s", 13, 4, 4, _ring_with_center(4, 2)),
        _mk("s", 16, 4, 4, _uniform(4, 4)),
        _mk("s", 25, 5, 5, _uniform(5, 5)),
        _mk("s", 36, 6, 6, _uniform(6, 6)),
        _mk("s", 64, 8, 8, _uniform(8, 8)),
    ]
    wide_seven = [(0, 0, 3, 2)] + [(3, 0, 1, 1), (3, 1, 1, 1)] + [(c, 2, 1, 1) for c in range(4)]
    wide = [
        _mk("w", 1, 1, 1, _uniform(1, 1), True),
        _mk("w", 4, 2, 2, _uniform(2, 2), True),
        _mk("w", 6, 3, 2, _uniform(3, 2), True),
        _mk("w", 7, 4, 3, wide_seven, True),
        _mk("w", 9, 3, 3, _uniform(3, 3), True),
        _mk("w", 12, 4, 3, _uniform(4, 3), True),
        _mk("w", 16, 4, 4, _uniform(4, 4), True),
    ]
    return std, wide


STANDARD_DIVISIONS, WIDE_DIVISIONS = _build()
DIVISIONS = {d.key: d for d in STANDARD_DIVISIONS + WIDE_DIVISIONS}


def get_division(key, default="s4"):
    return DIVISIONS.get(key) or DIVISIONS[default]


def division_for_count(count, wide=False):
    pool = WIDE_DIVISIONS if wide else STANDARD_DIVISIONS
    for d in pool:
        if d.count >= count:
            return d
    return pool[-1]


def validate_cells(cols, rows, cells):
    """Ячейки должны полностью покрывать сетку без наложений и выхода за границы."""
    if cols < 1 or rows < 1 or not cells:
        return False
    seen = set()
    for cell in cells:
        if len(cell) != 4:
            return False
        c, r, cs, rs = cell
        if cs < 1 or rs < 1 or c < 0 or r < 0 or c + cs > cols or r + rs > rows:
            return False
        for y in range(r, r + rs):
            for x in range(c, c + cs):
                if (x, y) in seen:
                    return False
                seen.add((x, y))
    return len(seen) == cols * rows


def resolve_stream(mode, cell_count=1, maximized=False, available=None):
    """Выбрать номер потока (1 основной, 2 дополнительный, 3 третий).

    Для режима auto: развёрнутое окно или одно окно — основной,
    2–9 окон — дополнительный, больше — третий (если есть).
    Если выбранный поток недоступен, берётся ближайший доступный.
    """
    avail = tuple(sorted(available)) if available else (1, 2, 3)
    if mode == STREAM_AUTO or mode not in STREAM_SUFFIX:
        if maximized or cell_count <= 1:
            want = 1
        elif cell_count <= 9:
            want = 2
        else:
            want = 3
    else:
        want = STREAM_SUFFIX[mode]
    if want in avail:
        return want
    return min(avail, key=lambda n: (abs(n - want), n))
