"""Математика временной шкалы нижней панели «Живой просмотр / Воспроизведение» (без GTK).

Шкала показывает окно времени длиной span секунд, центр которого задаётся
пользователем (прокрутка/перетаскивание). Масштабы как в клиенте Hikvision:
24 ч, 12 ч, 6 ч, 2 ч, 1 ч, 30 мин, 10 мин.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

ZOOM_LEVELS = (24 * 3600, 12 * 3600, 6 * 3600, 2 * 3600, 3600, 1800, 600)
ZOOM_TITLES = {24 * 3600: "24ч", 12 * 3600: "12ч", 6 * 3600: "6ч", 2 * 3600: "2ч",
               3600: "1ч", 1800: "30мин", 600: "10мин"}
# Шаг подписей и мелких делений для каждого масштаба (секунды)
_TICKS = {24 * 3600: (7200, 1800), 12 * 3600: (3600, 900), 6 * 3600: (1800, 300),
          2 * 3600: (600, 120), 3600: (300, 60), 1800: (300, 60), 600: (60, 10)}

# Типы отрезков записи и их цвета (как на шкале HikCentral: обычная запись — синяя,
# событие — красная/оранжевая)
SEGMENT_COLORS = {"continuous": (0.16, 0.52, 0.88), "event": (0.91, 0.33, 0.2),
                  "person": (0.95, 0.62, 0.15), "vehicle": (0.95, 0.62, 0.15),
                  "local": (0.25, 0.75, 0.45)}
FILTERS = ("all", "event", "person", "vehicle")


@dataclass
class Segment:
    start: datetime
    end: datetime
    kind: str = "continuous"
    payload: object = None

    def contains(self, moment):
        return self.start <= moment < self.end


def zoom_in(span):
    smaller = [z for z in ZOOM_LEVELS if z < span]
    return smaller[0] if smaller else ZOOM_LEVELS[-1]


def zoom_out(span):
    larger = [z for z in ZOOM_LEVELS if z > span]
    return larger[-1] if larger else ZOOM_LEVELS[0]


class TimelineModel:
    def __init__(self, center=None, span=24 * 3600):
        self.center = (center or datetime.now().astimezone()).replace(microsecond=0)
        self.span = span if span in ZOOM_LEVELS else 24 * 3600
        self.cursor = self.center          # текущая позиция воспроизведения/«сейчас»
        self.segments: list[Segment] = []
        self.filter = "all"

    @property
    def start(self):
        return self.center - timedelta(seconds=self.span / 2)

    @property
    def end(self):
        return self.center + timedelta(seconds=self.span / 2)

    def x_for(self, moment, width):
        if width <= 0:
            return 0.0
        return (moment - self.start).total_seconds() / self.span * width

    def time_at(self, x, width):
        if width <= 0:
            return self.center
        fraction = min(1.0, max(0.0, x / width))
        return (self.start + timedelta(seconds=fraction * self.span)).replace(microsecond=0)

    def pan_pixels(self, dx, width):
        """Сдвинуть видимое окно на dx пикселей (перетаскивание шкалы)."""
        if width > 0:
            self.center -= timedelta(seconds=dx / width * self.span)

    def zoom(self, direction, anchor=None):
        """direction > 0 — крупнее (меньше span). anchor — время под курсором остаётся на месте."""
        old_span = self.span
        self.span = zoom_in(old_span) if direction > 0 else zoom_out(old_span)
        if anchor is not None and self.span != old_span:
            ratio = self.span / old_span
            self.center = anchor + (self.center - anchor) * ratio
            self.center = self.center.replace(microsecond=0)
        return self.span

    def ticks(self):
        """[(moment, major: bool)] — деления в видимом окне."""
        major, minor = _TICKS[self.span]
        start = self.start
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (start - midnight).total_seconds()
        first = midnight + timedelta(seconds=(int(offset // minor) + 1) * minor)
        result = []
        moment = first
        while moment <= self.end:
            seconds = (moment - midnight).total_seconds()
            result.append((moment, int(seconds) % major == 0))
            moment += timedelta(seconds=minor)
        return result

    def label(self, moment):
        return moment.strftime("%H:%M" if self.span >= 1800 else "%H:%M:%S")

    def visible_segments(self):
        """Отрезки, попадающие в окно и проходящие фильтр, обрезанные по краям окна."""
        result = []
        for seg in self.segments:
            if self.filter != "all" and seg.kind != self.filter:
                continue
            if seg.end <= self.start or seg.start >= self.end:
                continue
            result.append(Segment(max(seg.start, self.start), min(seg.end, self.end), seg.kind, seg.payload))
        return result

    def segment_at(self, moment):
        candidates = [s for s in self.segments if s.contains(moment)]
        if not candidates:
            return None
        # событийный отрезок важнее непрерывного
        candidates.sort(key=lambda s: 0 if s.kind != "continuous" else 1)
        return candidates[0]

    def next_segment_after(self, moment):
        later = sorted((s for s in self.segments if s.start > moment), key=lambda s: s.start)
        return later[0] if later else None


def merge_segments(segments, gap=2):
    """Склеить соседние отрезки одного типа с зазором <= gap секунд (у NVR файлы по ~1 ч)."""
    ordered = sorted(segments, key=lambda s: (s.kind, s.start))
    merged = []
    for seg in ordered:
        if merged and merged[-1].kind == seg.kind and (seg.start - merged[-1].end).total_seconds() <= gap:
            if seg.end > merged[-1].end:
                merged[-1] = Segment(merged[-1].start, seg.end, seg.kind, merged[-1].payload)
            continue
        merged.append(Segment(seg.start, seg.end, seg.kind, seg.payload))
    return sorted(merged, key=lambda s: s.start)


def day_bounds(day):
    """Начало и конец локальных суток для date/datetime."""
    if isinstance(day, datetime):
        base = day.astimezone()
    else:
        base = datetime(day.year, day.month, day.day).astimezone()
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def format_position(moment):
    return moment.strftime("%Y/%m/%d %H:%M:%S")
