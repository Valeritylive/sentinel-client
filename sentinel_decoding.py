"""Выбор декодера видео: процессор или видеокарта.

GStreamer (playbin) сам выбирает декодер по рангу элемента. Здесь ранги аппаратных декодеров поднимаются
(«Видеокарта») или обнуляются («Процессор»); «Автоматически» возвращает ранги системы. Ранги меняются
в реестре процесса, поэтому новый режим действует для потоков, открытых после сохранения."""
from __future__ import annotations

import sentinel_storage as storage

SETTING = "video_decoding"
DEFAULTS = {"mode": "auto", "backend": "any"}
MODES = (("auto", "Автоматически (как решит GStreamer)"), ("gpu", "Видеокарта"), ("cpu", "Процессор"))
BACKENDS = (("any", "Любая доступная"), ("va", "VA-API — Intel / AMD"), ("vaapi", "VA-API (старый gstreamer-vaapi)"),
            ("nvcodec", "NVIDIA NVDEC"), ("v4l2", "V4L2 (ARM, встроенный декодер)"))
BACKEND_TITLES = dict(BACKENDS)
# порядок предпочтения при «Любая доступная»
PREFERENCE = ("nvcodec", "va", "vaapi", "v4l2")
SOFTWARE_HINT = ("avdec_", "openh264", "libde265", "dav1d", "vp8dec", "vp9dec", "jpegdec", "theoradec")

RANK_PRIMARY = 256
RANK_NONE = 0
_original = {}          # имя фабрики -> ранг до наших изменений


def backend_of(name, klass=""):
    """К какому интерфейсу видеокарты относится декодер (None — программный)."""
    if name.startswith("vaapi"):
        return "vaapi"
    if name.startswith("va") and name.endswith("dec"):
        return "va"
    if name.startswith("nv") and ("dec" in name):
        return "nvcodec"
    if name.startswith("v4l2") and ("dec" in name):
        return "v4l2"
    if "Hardware" in klass:
        return "other"
    return None


def _registry():
    from gi.repository import Gst
    return Gst.Registry.get()


def video_decoders(registry=None):
    """[(имя, класс, ранг)] всех видеодекодеров в реестре GStreamer."""
    from gi.repository import Gst
    registry = registry or _registry()
    result = []
    for feature in registry.get_feature_list(Gst.ElementFactory):
        klass = feature.get_metadata("klass") or ""
        if "Decoder" in klass and "Video" in klass:
            result.append((feature.get_name(), klass, _original.get(feature.get_name(), feature.get_rank())))
    return result


def hardware_decoders(decoders):
    """{интерфейс: [имена декодеров]} из списка video_decoders()."""
    found = {}
    for name, klass, _rank in decoders:
        backend = backend_of(name, klass)
        if backend:
            found.setdefault(backend, []).append(name)
    return {k: sorted(v) for k, v in found.items()}


def load_settings():
    settings = dict(DEFAULTS)
    settings.update(storage.get_json_setting(SETTING, {}) or {})
    if settings["mode"] not in dict(MODES):
        settings["mode"] = "auto"
    if settings["backend"] not in BACKEND_TITLES:
        settings["backend"] = "any"
    return settings


def save_settings(mode, backend="any"):
    settings = {"mode": mode if mode in dict(MODES) else "auto",
                "backend": backend if backend in BACKEND_TITLES else "any"}
    storage.set_json_setting(SETTING, settings)
    return settings


def plan(settings, hardware):
    """Какие ранги выставить: {имя: ранг}. Чистая функция — проверяется тестами."""
    mode, backend = settings["mode"], settings["backend"]
    every = [name for names in hardware.values() for name in names]
    if mode == "cpu":
        return {name: RANK_NONE for name in every}
    if mode != "gpu":
        return {}
    if backend == "any":
        chosen = next((b for b in PREFERENCE if hardware.get(b)), None) or next(iter(hardware), None)
    else:
        chosen = backend if hardware.get(backend) else None
    if chosen is None:
        return {}
    ranks = {name: RANK_PRIMARY + 100 for name in hardware[chosen]}
    # остальные интерфейсы видеокарты — ниже программных, чтобы не мешали выбранному
    ranks.update({name: RANK_NONE for b, names in hardware.items() if b != chosen for name in names})
    return ranks


def apply(settings=None, registry=None):
    """Применить настройку к реестру GStreamer. Возвращает (выбранный интерфейс или None, текст состояния)."""
    settings = settings or load_settings()
    try:
        registry = registry or _registry()
        decoders = video_decoders(registry)
    except Exception as exc:        # GStreamer недоступен (тесты, сервер без GStreamer)
        return None, f"GStreamer недоступен: {exc}"
    hardware = hardware_decoders(decoders)
    for name, rank in list(_original.items()):      # сначала вернуть ранги системы
        feature = registry.lookup_feature(name)
        if feature is not None:
            feature.set_rank(rank)
    _original.clear()
    ranks = plan(settings, hardware)
    for name, rank in ranks.items():
        feature = registry.lookup_feature(name)
        if feature is None:
            continue
        _original.setdefault(name, feature.get_rank())
        feature.set_rank(rank)
    return _summary(settings, hardware, ranks)


def _summary(settings, hardware, ranks):
    mode = settings["mode"]
    if mode == "cpu":
        return None, "Декодирование на процессоре: аппаратные декодеры отключены."
    if mode == "auto":
        return None, ("Автоматический выбор GStreamer. Найдены аппаратные декодеры: "
                      + describe_hardware(hardware) if hardware else
                      "Автоматический выбор GStreamer. Аппаратные декодеры не найдены — работает процессор.")
    raised = [name for name, rank in ranks.items() if rank > RANK_PRIMARY]
    if not raised:
        wanted = BACKEND_TITLES[settings["backend"]]
        return None, (f"Видеокарта ({wanted}) недоступна: подходящих декодеров GStreamer нет, работает процессор. "
                      + install_hint(settings["backend"]))
    backend = backend_of(raised[0]) or "other"
    return backend, f"Декодирование на видеокарте: {BACKEND_TITLES.get(backend, backend)} ({', '.join(sorted(raised))})."


def describe_hardware(hardware):
    if not hardware:
        return "нет"
    return "; ".join(f"{BACKEND_TITLES.get(b, b)}: {', '.join(n)}" for b, n in sorted(hardware.items()))


def install_hint(backend):
    return {"va": "Для Intel/AMD установите gstreamer1.0-plugins-bad и драйвер VA-API (intel-media-va-driver "
                  "или mesa-va-drivers).",
            "vaapi": "Установите gstreamer1.0-vaapi и драйвер VA-API.",
            "nvcodec": "Для NVIDIA нужны проприетарный драйвер и gstreamer1.0-plugins-bad (nvcodec).",
            "v4l2": "Нужен драйвер встроенного декодера и gstreamer1.0-plugins-good."}.get(
        backend, "Для Intel/AMD: gstreamer1.0-plugins-bad и драйвер VA-API; для NVIDIA: проприетарный драйвер "
                 "и gstreamer1.0-plugins-bad.")


def pipeline_decoder(pipeline):
    """Имя видеодекодера, который выбрал GStreamer в работающем конвейере ('' — пока не выбран)."""
    try:
        iterator = pipeline.iterate_recurse()
    except Exception:
        return ""
    while True:
        try:
            ok, element = iterator.next()
        except Exception:
            return ""
        if ok != 1 or element is None:          # Gst.IteratorResult.OK == 1
            return ""
        factory = element.get_factory()
        if factory is None:
            continue
        klass = factory.get_metadata("klass") or ""
        if "Decoder" in klass and "Video" in klass:
            return factory.get_name()


def decoder_title(name):
    if not name:
        return "декодер ещё не выбран"
    backend = backend_of(name)
    return f"{name} — " + ("видеокарта, " + BACKEND_TITLES.get(backend, "аппаратный") if backend else "процессор")
