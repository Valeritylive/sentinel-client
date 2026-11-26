"""Панель инструментов окна видео (появляется при наведении) и PTZ на изображении.

Кнопки: снимок, клип из живого потока, звук, двусторонняя связь, цифровой
зум, основной/дополнительный поток, PTZ, закрыть окно. В режиме PTZ в углу
окна появляется джойстик, щелчок по картинке центрирует камеру, рамка
приближает выбранную область (ISAPI position3D), стрелки и +/− на
клавиатуре управляют выбранной камерой.
"""
from __future__ import annotations

import threading
import urllib.request

from gi.repository import Gdk, GLib, Gst, Gtk

import sentinel_talk as talk
import sentinel_tiletools as tt
from sentinel_protocols import authenticated_opener

TOOL_BUTTONS = (
    # ключ, значок, подсказка
    ("capture", "camera-photo-symbolic", "Снимок кадра"),
    ("clip", "media-record-symbolic", "Клип: начать/остановить запись живого потока"),
    ("audio", "audio-volume-muted-symbolic", "Звук камеры"),
    ("talk", "audio-input-microphone-symbolic", "Двусторонняя связь (микрофон ПК → динамик устройства)"),
    ("zoom", "zoom-in-symbolic", "Цифровой зум: ×2 по центру / сброс (колесо и рамка — тоже зум)"),
    ("stream", None, "Переключить основной / дополнительный поток"),
    ("ptz", "input-gaming-symbolic", "PTZ: джойстик, щелчок по картинке — центрировать, рамка — приблизить, стрелки и +/−"),
    ("close", "window-close-symbolic", "Закрыть камеру в этом окне"),
)


class TileBarMixin:
    """Подмешивается к окну просмотра (SentinelWindow и окна мониторов)."""

    # ------------------------------------------------------------ построение
    def _build_tile_toolbar(self, index, pane):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        bar.add_css_class("tile-toolbar")
        bar.set_halign(Gtk.Align.END)
        bar.set_valign(Gtk.Align.START)
        bar.set_margin_top(4)
        bar.set_margin_end(4)
        buttons = {}
        for key, icon, tip in TOOL_BUTTONS:
            item = Gtk.Button(label=tt.STREAM_SHORT["main"]) if icon is None else Gtk.Button.new_from_icon_name(icon)
            item.set_has_frame(False)
            item.add_css_class("tile-tool")
            item.set_tooltip_text(tip)
            item.set_focus_on_click(False)
            item.connect("clicked", lambda _b, k=key, i=index: self._tile_tool(i, k))
            bar.append(item)
            buttons[key] = item
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        revealer.set_transition_duration(120)
        revealer.set_halign(Gtk.Align.END)
        revealer.set_valign(Gtk.Align.START)
        revealer.set_child(bar)
        revealer.set_reveal_child(False)
        pane.add_overlay(revealer)

        pad = self._build_tile_ptz_pad(index)
        pane.add_overlay(pad)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a, i=index: self._tile_hover(i, True))
        motion.connect("leave", lambda *_a, i=index: self._tile_hover(i, False))
        pane.add_controller(motion)
        return {"toolbar": revealer, "tool_buttons": buttons, "ptz_pad": pad}

    def _build_tile_ptz_pad(self, index):
        grid = Gtk.Grid(column_spacing=1, row_spacing=1)
        grid.add_css_class("tile-ptz-pad")
        grid.set_halign(Gtk.Align.END)
        grid.set_valign(Gtk.Align.END)
        grid.set_margin_end(6)
        grid.set_margin_bottom(28)
        for x, y, text, direction in ((0, 0, "↖", "up_left"), (1, 0, "↑", "up"), (2, 0, "↗", "up_right"),
                                      (0, 1, "←", "left"), (2, 1, "→", "right"),
                                      (0, 2, "↙", "down_left"), (1, 2, "↓", "down"), (2, 2, "↘", "down_right"),
                                      (0, 3, "−", "zoom_out"), (2, 3, "+", "zoom_in")):
            item = Gtk.Button(label=text)
            item.add_css_class("tile-tool")
            item.set_focus_on_click(False)
            gesture = Gtk.GestureClick()
            gesture.connect("pressed", lambda *_a, d=direction, i=index: self._tile_ptz_press(i, d))
            gesture.connect("released", lambda *_a, i=index: self._tile_ptz_press(i, "stop"))
            item.add_controller(gesture)
            grid.attach(item, x, y, 1, 1)
        hint = Gtk.Label(label="PTZ")
        hint.add_css_class("tile-ptz-label")
        grid.attach(hint, 1, 1, 1, 1)
        grid.set_visible(False)
        return grid

    def _install_ptz_keys(self):
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._tile_ptz_key_pressed)
        keys.connect("key-released", self._tile_ptz_key_released)
        self.add_controller(keys)

    # ------------------------------------------------------------ состояние
    def _tile_ptz_slots(self):
        if "tile_ptz" not in self.__dict__:
            self.tile_ptz = set()
        return self.tile_ptz

    def _talk_sessions(self):
        if "talk_sessions" not in self.__dict__:
            self.talk_sessions = {}
        return self.talk_sessions

    def _tile_hover(self, index, inside):
        if index >= len(self.video_slots):
            return
        slot = self.video_slots[index]
        revealer = slot.get("toolbar")
        if revealer is None:
            return
        state = self.streams.get(index)
        show = bool(inside and state)
        if show:
            self._sync_tile_toolbar(index)
        revealer.set_reveal_child(show or index in self._talk_sessions())

    def _sync_tile_toolbar(self, index):
        """Обновить значки (звук, поток, запись, PTZ, разговор) по состоянию окна."""
        if index >= len(self.video_slots):
            return
        slot = self.video_slots[index]
        buttons = slot.get("tool_buttons") or {}
        state = self.streams.get(index) or {}
        live = bool(state) and not state.get("playback")
        if "audio" in buttons:
            buttons["audio"].set_icon_name(
                "audio-volume-high-symbolic" if state.get("audio_enabled") else "audio-volume-muted-symbolic")
        if "stream" in buttons:
            current = (state.get("device") or {}).get("stream", "main")
            buttons["stream"].set_label(tt.STREAM_SHORT.get(current, "ОСН"))
            buttons["stream"].set_sensitive(live)
        recording = state.get("recording")
        active = {
            "clip": bool(recording and recording.poll() is None),
            "talk": index in self._talk_sessions(),
            "ptz": index in self._tile_ptz_slots(),
            "zoom": state.get("digital_zoom", 1.0) > 1.001,
        }
        for key, on in active.items():
            if key in buttons:
                (buttons[key].add_css_class if on else buttons[key].remove_css_class)("tile-tool-on")
        session = self._tile_session()
        allowed = {"clip": True, "talk": True, "ptz": session is None or session.can("ptz")}
        for key in ("clip", "talk", "ptz"):
            if key in buttons:
                buttons[key].set_sensitive(live and allowed[key])
        if not allowed["ptz"]:
            self._tile_ptz_slots().discard(index)
        pad = slot.get("ptz_pad")
        if pad is not None:
            pad.set_visible(live and index in self._tile_ptz_slots())

    def _tile_session(self):
        session = self.__dict__.get("session")
        if session is None:
            session = getattr(getattr(self, "parent_window", None), "session", None)
        return session

    # ------------------------------------------------------------ действия
    def _tile_tool(self, index, key):
        self._select_slot(index)
        state = self.streams.get(index)
        if not state:
            self.notify("В этом окне нет камеры")
            return
        if key == "capture":
            self.capture_selected()
        elif key == "clip":
            self.toggle_recording()
        elif key == "audio":
            self.toggle_audio()
        elif key == "talk":
            self.toggle_talk(index)
        elif key == "zoom":
            if state.get("digital_zoom", 1.0) > 1.001:
                self._reset_digital_zoom(index)
            else:
                state["digital_zoom"] = 2.0
                state["digital_x"] = state["digital_y"] = 0.5
        elif key == "stream":
            current = state["device"].get("stream", "main")
            self.set_selected_stream(tt.next_stream(current))
        elif key == "ptz":
            self.toggle_tile_ptz(index)
        elif key == "close":
            self.stop_talk(index)
            self._tile_ptz_slots().discard(index)
            self._stop_stream(index)
        self._sync_tile_toolbar(index)

    def toggle_tile_ptz(self, index):
        session = self._tile_session()
        if session is not None and not session.can("ptz"):
            self.notify("Нет прав на управление PTZ", error=True)
            return
        slots = self._tile_ptz_slots()
        if index in slots:
            slots.discard(index)
            self._tile_ptz_press(index, "stop")
        else:
            slots.add(index)
            self.notify("PTZ: щелчок по картинке — центрировать, рамка слева направо — приблизить, "
                        "справа налево — отдалить; стрелки и +/− на клавиатуре")
        self._sync_tile_toolbar(index)

    def _tile_ptz_press(self, index, direction):
        if index not in self.streams:
            return
        self._select_slot(index)
        self._ptz_press(direction)

    # ------------------------------------------------------------ визуальный PTZ
    def _tile_drag_end(self, index, start_x, start_y, dx, dy, width, height):
        """Окончание жеста на окне: PTZ (position3D) в режиме PTZ, иначе цифровой зум рамкой."""
        state = self.streams.get(index)
        if not state:
            return
        if index not in self._tile_ptz_slots() or state.get("playback"):
            self._digital_zoom_region(index, start_x, start_y, dx, dy, width, height)
            return
        picture = self.video_slots[index]["picture"]
        paintable = picture.get_paintable()
        video_w = paintable.get_intrinsic_width() if paintable is not None else 0
        video_h = paintable.get_intrinsic_height() if paintable is not None else 0
        rect = tt.video_rect(width, height, video_w, video_h)
        # Текстура уже обрезана цифровым зумом, поэтому пересчитываем в полный кадр.
        zoom = (state.get("digital_zoom", 1.0), state.get("digital_x", 0.5), state.get("digital_y", 0.5))
        start = tt.pane_to_frame(start_x, start_y, rect, *zoom)
        if start is None:
            return
        if tt.is_click(dx, dy):
            end = None
        else:
            end = tt.pane_to_frame(start_x + dx, start_y + dy, rect, *zoom)
            if end is None:
                return
        self.send_position3d(index, start, end)

    def send_position3d(self, index, start, end=None):
        state = self.streams.get(index)
        if not state:
            return
        device = state["device"]
        if device.get("protocol", "hikvision") != "hikvision" or state.get("cloud") is not None:
            self.notify("Визуальный PTZ доступен для камер Hikvision при прямом подключении")
            return
        creds = state["credentials"]
        url = (f"http://{device['host']}:{device.get('http_port', 80)}"
               f"/ISAPI/PTZCtrl/channels/{device['channel']}/position3D")
        body = tt.position3d_xml(start, end)

        def request():
            try:
                opener = authenticated_opener(url, creds.get("username", ""), creds.get("password", ""))
                req = urllib.request.Request(url, data=body, method="PUT",
                                             headers={"Content-Type": "application/xml"})
                with opener.open(req, timeout=4) as response:
                    response.read(512)
            except Exception as exc:
                GLib.idle_add(self.notify, f"PTZ (3D-позиционирование): {exc}")
        threading.Thread(target=request, daemon=True).start()

    # ------------------------------------------------------------ клавиатура
    def _keyboard_ptz_slot(self):
        index = getattr(self, "selected_slot", None)
        if index is None or index not in self._tile_ptz_slots() or index not in self.streams:
            return None
        focus = self.get_focus() if hasattr(self, "get_focus") else None
        if focus is not None and isinstance(focus, Gtk.Editable):
            return None
        return index

    def _tile_ptz_key_pressed(self, _controller, keyval, _keycode, _state):
        index = self._keyboard_ptz_slot()
        if index is None:
            return False
        direction = tt.ptz_direction_for_key(Gdk.keyval_name(keyval))
        if direction is None:
            return False
        if getattr(self, "_ptz_key_down", None) != direction:  # автоповтор клавиши
            self._ptz_key_down = direction
            self._ptz_press(direction)
        return True

    def _tile_ptz_key_released(self, _controller, keyval, _keycode, _state):
        if getattr(self, "_ptz_key_down", None) is None:
            return False
        if tt.ptz_direction_for_key(Gdk.keyval_name(keyval)) is None:
            return False
        self._ptz_key_down = None
        self._ptz_press("stop")
        return True

    # ------------------------------------------------------------ двусторонняя связь
    def toggle_talk(self, index):
        if index in self._talk_sessions():
            self.stop_talk(index)
            self.notify("Разговор завершён")
        else:
            self.start_talk(index)
        self._sync_tile_toolbar(index)

    def start_talk(self, index):
        state = self.streams.get(index)
        if not state:
            return
        if state.get("cloud") is not None or state["device"].get("protocol", "hikvision") != "hikvision":
            self.notify("Двусторонняя связь доступна для Hikvision при прямом подключении")
            return
        for other in list(self._talk_sessions()):  # устройство держит один канал разговора
            self.stop_talk(other)

        def on_state(text, error=False, i=index):
            def update():
                if error:
                    self._talk_sessions().pop(i, None)
                    self._sync_tile_toolbar(i)
                self.notify(text, error=error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(update)

        session = talk.TalkSession(state["base_device"], state["credentials"], Gst, on_state)
        self._talk_sessions()[index] = session
        session.start()
        self.notify("Разговор: подключение к устройству…")

    def stop_talk(self, index=None):
        sessions = self._talk_sessions()
        for key in ([index] if index is not None else list(sessions)):
            session = sessions.pop(key, None)
            if session is not None:
                session.stop()
