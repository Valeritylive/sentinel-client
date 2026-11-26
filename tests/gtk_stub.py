"""Минимальная заглушка PyGObject: позволяет собрать окно без GTK/GStreamer (дымовой тест)."""
import sys
import types


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Any()


class Any(metaclass=_Meta):
    def __init__(self, *a, **k):
        self.__dict__["_handlers"] = {}
        self.__dict__["_state"] = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Any()

    def __call__(self, *a, **k):
        return Any()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __index__(self):
        return 0

    def __len__(self):
        return 0

    def __or__(self, o): return Any()
    __ror__ = __and__ = __rand__ = __or__
    def __lt__(self, o): return False
    def __gt__(self, o): return False
    def __le__(self, o): return True
    def __ge__(self, o): return True
    def __add__(self, o): return 0
    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __add__
    def __truediv__(self, o): return 0.0
    __rtruediv__ = __floordiv__ = __truediv__


class Widget(Any):
    """Запоминает детей, обработчики сигналов и состояние переключателей."""

    def __init__(self, *a, **k):
        super().__init__()
        self._state["children"] = []
        self._state["active"] = False
        self._state["label"] = k.get("label", "")
        self._state["css"] = set()

    def connect(self, signal, handler, *args):
        self._handlers.setdefault(signal, []).append((handler, args))
        return 1

    def emit(self, signal):
        for handler, args in list(self._handlers.get(signal, [])):
            handler(self, *args)

    def append(self, child): self._state["children"].append(child)
    def prepend(self, child): self._state["children"].insert(0, child)
    def remove(self, child):
        if child in self._state["children"]:
            self._state["children"].remove(child)
    def set_child(self, child): self._state["children"] = [child] if child is not None else []
    def get_label(self): return self._state["label"]
    def set_label(self, text): self._state["label"] = text
    def set_text(self, text): self._state["label"] = text
    def get_text(self): return self._state["label"]
    def add_css_class(self, name): self._state["css"].add(name)
    def remove_css_class(self, name): self._state["css"].discard(name)
    def has_css_class(self, name): return name in self._state["css"]
    def get_parent(self): return None
    def get_first_child(self):
        kids = self._state["children"]
        return kids[0] if kids else None
    def get_next_sibling(self): return None
    def get_last_child(self):
        kids = self._state["children"]
        return kids[-1] if kids else None
    def get_prev_sibling(self): return None

    def set_active(self, value):
        value = bool(value)
        if self._state["active"] != value:
            self._state["active"] = value
            self.emit("toggled")

    def get_active(self): return self._state["active"]

    def set_visible(self, value): self._state["visible_flag"] = bool(value)
    def get_visible(self): return self._state.get("visible_flag", True)


class Stack(Widget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._state["pages"] = {}
        self._state["visible"] = None

    def add_named(self, child, name): self._state["pages"][name] = child
    def set_visible_child_name(self, name):
        assert name in self._state["pages"], f"нет страницы {name}"
        self._state["visible"] = name
    def get_visible_child_name(self): return self._state["visible"]


class ApplicationWindow(Widget):
    pass


def install():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")

    class NS:
        def __init__(self, special):
            self.__dict__["_special"] = special

        def __getattr__(self, name):
            if name in self._special:
                return self._special[name]
            if name[:1].isupper():
                return type(name, (Widget,), {})
            return Any()

    Gtk = NS({"Stack": Stack, "ApplicationWindow": ApplicationWindow})
    for n in ("Box", "Grid", "Button", "ToggleButton", "Label", "HeaderBar", "FlowBox", "ScrolledWindow"):
        Gtk.__dict__[n] = type(n, (Widget,), {})
    other = NS({})
    repo.Gtk, repo.Gdk, repo.GLib, repo.GObject, repo.Gst = Gtk, other, other, other, other
    repo.Graphene, repo.Pango = other, other
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo
    return Gtk
