"""Живое видео устройств облака P2P через облачный ретранслятор (VTM), без прямого доступа к регистратору.

Регистраторы из облака P2P обычно стоят в чужой сети: облако сообщает только их локальный
адрес, и RTSP с этого компьютера недоступен. Мобильное приложение смотрит видео через
облачный ретранслятор VTM. Его протокол (вход, токены VTDU, кадрирование VTM) реализован в
открытых библиотеках pyezvizapi и hikcloudstream (Apache-2.0). Они живут в отдельном
виртуальном окружении (``<данные Sentinel>/cloud-runtime``), чтобы не трогать системный Python
и пакет Sentinel.

Модуль работает в двух ролях:

* в Sentinel (только stdlib): поиск окружения, его установка, запуск помощника и разбор его ошибок;
* помощник (``python sentinel_cloudstream.py`` из окружения): получает задание JSON на stdin,
  открывает поток VTM, разбирает RTP (H.264 и H.265) и через FFmpeg отдаёт MPEG-TS в stdout.
  GStreamer читает этот поток через ``fd://``.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

RUNTIME_ENV = "SENTINEL_CLOUD_PYTHON"
RUNTIME_PACKAGES = ("hikcloudstream==0.1.4", "pyezvizapi==1.0.5.0")
MIN_PYTHON = (3, 12)       # hikcloudstream и pyezvizapi требуют Python 3.12+
# Пакеты (AppImage, Flatpak) кладут библиотеки рядом с модулем, чтобы облако работало без установки.
BUNDLED_LIBS = Path(__file__).resolve().parent / "cloud-libs"
ERROR_PREFIX = "SENTINEL_CLOUD_ERROR "
STREAM_MAIN, STREAM_SUB = 1, 2

ERROR_TITLES = {
    "runtime": "модуль облачного видео не установлен",
    "session": "сессия облака P2P истекла: откройте «Облако P2P» и войдите снова",
    "not_found": "облако не нашло этот канал (камера не в сети или канал не подключён)",
    "negotiation": "облачный ретранслятор отказал в потоке",
    "encrypted": "видео на регистраторе зашифровано: нужен код проверки устройства",
    "bad_code": "код проверки устройства не подошёл",
    "no_video": "облако не прислало видео в понятном формате",
    "ffmpeg": "для облачного видео нужен FFmpeg",
    "failed": "ошибка облачного потока",
}


# ==================================================================== сторона Sentinel
def runtime_dir() -> Path:
    import sentinel_storage as storage
    return Path(storage.DATA) / "cloud-runtime"


def _python_version(python) -> tuple[int, int] | None:
    try:
        result = subprocess.run([python, "-I", "-c", "import sys; print(*sys.version_info[:2])"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        major, minor = result.stdout.split()
        return int(major), int(minor)
    except ValueError:
        return None


def find_python() -> str | None:
    """Python 3.12+ для окружения облачного видео: текущий или один из установленных в системе."""
    if sys.version_info[:2] >= MIN_PYTHON and not os.environ.get("APPIMAGE"):
        return sys.executable
    seen = set()
    for name in ("python3", "python3.14", "python3.13", "python3.12"):
        path = shutil.which(name)
        if not path or path in seen:
            continue
        seen.add(path)
        version = _python_version(path)
        if version and version >= MIN_PYTHON:
            return path
    return None


def runtime_python() -> str | None:
    """Python окружения с библиотеками облачного потока, если оно установлено."""
    configured = os.environ.get(RUNTIME_ENV)
    if configured:
        return configured if Path(configured).exists() else None
    candidate = runtime_dir() / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    if BUNDLED_LIBS.is_dir() and sys.version_info[:2] >= MIN_PYTHON:
        return sys.executable
    return None


def install_runtime(log=None) -> str:
    """Создать окружение и поставить библиотеки (нужен интернет). Возвращает путь к python."""
    target = runtime_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    python = target / "bin" / "python"
    if python.exists() and (_python_version(str(python)) or (0, 0)) < MIN_PYTHON:
        shutil.rmtree(target, ignore_errors=True)     # окружение от старого Python: библиотеки туда не встанут
    steps = []
    if not python.exists():
        base = find_python()
        if not base:
            raise RuntimeError("для облачного видео нужен Python 3.12 или новее (пакеты python3.12 и python3.12-venv); "
                               "путь к нему можно указать в SENTINEL_CLOUD_PYTHON")
        steps.append([base, "-I", "-m", "venv", str(target)])
    steps.append([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "-q", *RUNTIME_PACKAGES])
    for command in steps:
        if log:
            log(" ".join(command[:5]) + " …")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        except OSError as exc:
            raise RuntimeError(str(exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "ensurepip" in detail or "No module named pip" in detail:
                detail = "не хватает модуля venv/pip: установите пакет python3-venv (или python3.12-venv)"
            raise RuntimeError(detail[-600:] or "установка не удалась")
    return str(python)


def stream_kind(device) -> int:
    """Основной поток — 1, дополнительный и третий — 2 (у облака их два)."""
    return STREAM_MAIN if (device.get("stream") or "main") == "main" else STREAM_SUB


def build_request(device, session_state, validate_code="") -> dict:
    """Задание помощнику: какой канал открыть и с какой сессией облака P2P."""
    if not device.get("cloud_serial"):
        raise ValueError("Устройство не из облака P2P")
    if not session_state or not session_state.get("session_id"):
        raise CloudStreamError("session", "нет сессии облака P2P")
    return {
        "api_url": session_state.get("api_url") or "https://api.hik-connect.com",
        "session_id": session_state["session_id"],
        "feature_code": session_state.get("feature_code") or "",
        "serial": device["cloud_serial"],
        "channel": int(device.get("channel") or 1),
        "stream": stream_kind(device),
        "ffmpeg": shutil.which("ffmpeg") or "ffmpeg",
        "validate_code": (validate_code or "").strip(),
    }


class CloudStreamError(RuntimeError):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        title = ERROR_TITLES.get(code, ERROR_TITLES["failed"])
        super().__init__(f"{title}" + (f" ({detail})" if detail and code in ("failed", "negotiation", "no_video")
                                       else ""))


def parse_error(stderr_text) -> CloudStreamError | None:
    """Последняя ошибка, о которой сообщил помощник."""
    for line in reversed((stderr_text or "").splitlines()):
        if line.startswith(ERROR_PREFIX):
            try:
                data = json.loads(line[len(ERROR_PREFIX):])
            except json.JSONDecodeError:
                continue
            return CloudStreamError(data.get("code", "failed"), data.get("detail", ""))
    return None


class CloudStreamProcess:
    """Запущенный помощник. ``uri`` — адрес для GStreamer playbin (fd://N)."""

    def __init__(self, request, python=None, script=None):
        python = python or runtime_python()
        if not python:
            raise CloudStreamError("runtime")
        script = script or str(Path(__file__).resolve())
        self.stderr_lines: list[str] = []
        self.process = subprocess.Popen(
            [python, "-I", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.process.stdin.write(json.dumps(request).encode())
        self.process.stdin.close()
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()

    @property
    def uri(self) -> str:
        return f"fd://{self.process.stdout.fileno()}"

    def _read_stderr(self):
        for raw in self.process.stderr:
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())
            del self.stderr_lines[:-50]

    def error(self) -> CloudStreamError | None:
        self._reader.join(timeout=0.5)
        return parse_error("\n".join(self.stderr_lines))

    def stop(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.process.stdout:
            self.process.stdout.close()


# ==================================================================== разбор RTP (без зависимостей)
H264, H265, MPEG_PS = "h264", "hevc", "mpeg"
NO_VIDEO_TIMEOUT = 25      # секунд без видео после начала потока — сообщить и выйти
_SC = b"\x00\x00\x00\x01"


def rtp_payload(packet: bytes) -> tuple[int, bytes]:
    """(payload type, полезная нагрузка) RTP-пакета; пустая нагрузка — пакет битый."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return -1, b""
    header = 12 + 4 * (packet[0] & 0x0F)
    if packet[0] & 0x10:
        if len(packet) < header + 4:
            return -1, b""
        header += 4 + 4 * int.from_bytes(packet[header + 2:header + 4], "big")
    end = len(packet)
    if packet[0] & 0x20 and end > header:
        end -= packet[-1]
    return packet[1] & 0x7F, packet[header:end] if end > header else b""


def guess_codec(payload: bytes) -> str | None:
    """H.265 или H.264 по заголовку NAL. None — пока не понятно."""
    if len(payload) < 2:
        return None
    hevc_type = (payload[0] >> 1) & 0x3F
    if payload[0] & 0x81 == 0 and payload[1] & 0x07 == 1 and hevc_type in (1, 19, 20, 32, 33, 34, 48, 49):
        return H265
    if payload[0] & 0x80 == 0 and payload[0] & 0x1F in (1, 5, 7, 8, 24, 28):
        return H264
    return None


HIK_ENCRYPTION_PROFILE = 0x4000     # RTP-расширение Hikvision у зашифрованных NAL
HIK_ENCRYPTED_PREFIX = 4096         # шифруются только первые 4096 байт тела NAL (AES-128, блоками по 16)
H264_PROFILES = {44, 66, 77, 83, 86, 88, 100, 110, 118, 122, 128, 134, 135, 138, 139, 144, 244}


def rtp_extension_profile(packet: bytes) -> int | None:
    if len(packet) < 12 or not packet[0] & 0x10:
        return None
    header = 12 + 4 * (packet[0] & 0x0F)
    return int.from_bytes(packet[header:header + 2], "big") if len(packet) >= header + 4 else None


def plausible_parameter_set(codec, nal: bytes) -> bool | None:
    """Проверка расшифровки по VPS (H.265) или SPS (H.264). None — это не набор параметров."""
    if codec == H265 and len(nal) >= 6 and (nal[0] >> 1) & 0x3F == 32:
        return nal[4:6] == b"\xff\xff" and nal[2] & 0x0C == 0x0C
    if codec == H264 and len(nal) >= 4 and nal[0] & 0x1F == 7:
        return nal[1] in H264_PROFILES
    return None


def decrypt_nal(codec, nal: bytes, block_decrypt) -> bytes:
    """Расшифровать тело NAL (заголовок открыт): первые 4096 байт целыми блоками по 16."""
    header = 2 if codec == H265 else 1
    body = nal[header:header + HIK_ENCRYPTED_PREFIX]
    size = len(body) - len(body) % 16
    if size <= 0:
        return nal
    return nal[:header] + block_decrypt(body[:size]) + nal[header + size:]


class RtpDepacketizer:
    """RTP (RFC 6184 / RFC 7798) → целые NAL. Пакеты с другим payload type (метаданные Hikvision) пропускаются.

    ``encrypted`` — устройство шифрует видео (расширение RTP 0x4000 на видеопакетах).
    """

    def __init__(self):
        self.codec = None
        self.video_pt = None
        self.encrypted = False
        self._fu = None

    def feed(self, packet: bytes) -> bytes:
        """Annex B без расшифровки (MPEG-PS — как есть)."""
        if self.codec == MPEG_PS:
            pt, payload = rtp_payload(packet)
            return payload if pt == self.video_pt else b""
        nals = self.feed_nals(packet)
        if self.codec == MPEG_PS:
            return rtp_payload(packet)[1]
        return b"".join(_SC + nal for nal in nals)

    def feed_nals(self, packet: bytes) -> list[bytes]:
        pt, payload = rtp_payload(packet)
        if not payload:
            return []
        if self.video_pt is None:
            codec = MPEG_PS if payload.startswith(b"\x00\x00\x01\xba") else guess_codec(payload)
            if codec is None:
                return []
            self.codec, self.video_pt = codec, pt
        if pt != self.video_pt or self.codec == MPEG_PS:   # Hikvision часто кладёт в RTP поток MPEG-PS
            return []
        if rtp_extension_profile(packet) == HIK_ENCRYPTION_PROFILE:
            self.encrypted = True
        return self._h265(payload) if self.codec == H265 else self._h264(payload)

    def _fragment(self, start, end, header, data):
        if start:
            self._fu = bytearray(header)
        if self._fu is None:
            return []                       # середина NAL без начала — пропускаем
        self._fu += data
        if not end:
            return []
        nal, self._fu = bytes(self._fu), None
        return [nal]

    def _h265(self, p: bytes) -> list[bytes]:
        if len(p) < 3:
            return []
        nal_type = (p[0] >> 1) & 0x3F
        if nal_type == 49:                          # FU
            fu = p[2]
            header = bytes([(p[0] & 0x81) | ((fu & 0x3F) << 1), p[1]])
            return self._fragment(fu & 0x80, fu & 0x40, header, p[3:])
        if nal_type == 48:                          # AP
            return _aggregated(p, 2)
        return [p]

    def _h264(self, p: bytes) -> list[bytes]:
        nal_type = p[0] & 0x1F
        if nal_type == 28:                          # FU-A
            if len(p) < 2:
                return []
            header = bytes([(p[0] & 0xE0) | (p[1] & 0x1F)])
            return self._fragment(p[1] & 0x80, p[1] & 0x40, header, p[2:])
        if nal_type == 24:                          # STAP-A
            return _aggregated(p, 1)
        return [p]


def _aggregated(p: bytes, offset: int) -> list[bytes]:
    out, i = [], offset
    while i + 2 <= len(p):
        size = int.from_bytes(p[i:i + 2], "big")
        if size:
            out.append(p[i + 2:i + 2 + size])
        i += 2 + size
    return out


# ==================================================================== помощник (окружение cloud-runtime)
def _fail(code, detail=""):
    sys.stderr.write(ERROR_PREFIX + json.dumps({"code": code, "detail": str(detail)[:300]}) + "\n")
    sys.stderr.flush()
    return 2


class _HelperFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _aes_ecb(key: bytes):
    """Расшифровка блоков AES-128 (CBC с нулевым IV на каждый блок = ECB)."""
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.decrypt


def _info(text):
    sys.stderr.write(f"sentinel-cloud: {text}\n")
    sys.stderr.flush()


def _all_vtm_pages(adapter):
    """Все страницы ресурсов VTM: сервер отдаёт максимум 30 за раз, а hikcloudstream читает одну."""
    merged = {"resourceInfos": [], "VTM": {}}
    offset = 0
    for _page in range(100):
        page = adapter._api_get_pagelist(page_filter="VTM", limit=50, offset=offset)
        items = page.get("resourceInfos") or []
        merged["resourceInfos"].extend(items)
        merged["VTM"].update(page.get("VTM") or {})
        for key, value in page.items():
            merged.setdefault(key, value)
        if not items or not (page.get("page") or {}).get("hasNext"):
            break
        offset += len(items)
    return merged


def _ffmpeg(path, codec):
    return subprocess.Popen(
        [path, "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+nobuffer", "-flags", "low_delay",
         "-use_wallclock_as_timestamps", "1", "-f", codec, "-i", "pipe:0", "-c", "copy",
         "-f", "mpegts", "-muxdelay", "0", "pipe:1"],
        stdin=subprocess.PIPE, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL)


def helper_main() -> int:      # pragma: no cover - нужен реальный облачный поток
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        request = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        return _fail("failed", exc)
    if BUNDLED_LIBS.is_dir() and str(BUNDLED_LIBS) not in sys.path:
        sys.path.append(str(BUNDLED_LIBS))      # после site-packages: своё окружение важнее встроенного
    try:
        import hikcloudstream.stream.session as hc_session
        from hikcloudstream import Camera, ClientConfig, HikConnectClient
        from pyezvizapi.stream import StreamTransport, VtmChannel, detect_transport
    except ImportError as exc:
        return _fail("runtime", exc)
    if not shutil.which(request.get("ffmpeg") or "ffmpeg"):
        return _fail("ffmpeg")
    hc_session.get_vtm_page_list = _all_vtm_pages

    client = HikConnectClient(ClientConfig(api_base_url=request["api_url"]))
    client.session_id = request["session_id"]
    if request.get("feature_code"):
        client._feature_code = request["feature_code"]
    camera = Camera(index=0, name="", device_serial=request["serial"], channel_no=int(request["channel"]),
                    device_name="")
    try:
        live = hc_session.open_live_stream(client, camera, stream_type=int(request.get("stream") or 1))
        info = live.start()
    except Exception as exc:
        text = str(exc)
        if "401" in text or "session" in text.lower() and "expired" in text.lower():
            return _fail("session", text)
        if "could not find VTM resource" in text:
            return _fail("not_found", text)
        return _fail("negotiation", text)
    if getattr(info, "result", 0) not in (0, None):
        live.close()
        return _fail("negotiation", f"code {info.result}")

    depacketizer = RtpDepacketizer()
    ffmpeg = None
    pending = bytearray()
    seen, samples, got_video = 0, [], threading.Event()
    keys = {"active": None, "candidates": None}
    code = (request.get("validate_code") or "").strip()
    _info(f"поток открыт: result={getattr(info, 'result', None)} stream={request.get('stream')}")

    def watchdog():
        # iter_packets может молча ждать данных бесконечно: сообщаем и выходим сами.
        if not got_video.wait(NO_VIDEO_TIMEOUT):
            _fail("no_video", f"пакетов {seen}, начало: {' '.join(samples) or 'нет данных'}")
            os._exit(2)

    def candidates():
        # Ключ — код проверки устройства; облако может выдать по нему отдельный ключ шифрования.
        if keys["candidates"] is None:
            found = []
            try:
                found.append(client.query_device_encrypt_key(request["serial"], code))
            except Exception as exc:
                _info(f"облако не выдало ключ: {exc}")
            found.append(code)
            keys["candidates"] = [_aes_ecb((k or "").encode()[:16].ljust(16, b"\0")) for k in found if k]
        return keys["candidates"]

    def video_nals(packet_body):
        """Целые NAL (расшифрованные) или None, если пакет ещё ничего не дал."""
        nals = depacketizer.feed_nals(packet_body)
        if not depacketizer.encrypted:
            return nals
        if not code:
            raise _HelperFailure("encrypted")
        result = []
        for nal in nals:
            if keys["active"] is None:
                if plausible_parameter_set(depacketizer.codec, nal) is None:
                    continue                    # до набора параметров ключ не проверить
                keys["active"] = next((k for k in candidates()
                                       if plausible_parameter_set(depacketizer.codec,
                                                                  decrypt_nal(depacketizer.codec, nal, k))), None)
                if keys["active"] is None:
                    raise _HelperFailure("bad_code")
                _info("видео расшифровано")
            result.append(decrypt_nal(depacketizer.codec, nal, keys["active"]))
        return result

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for packet in live._client.iter_packets():
            if packet.body and len(samples) < 3:
                samples.append(f"ch{packet.channel}:{packet.body[:16].hex()}")
            seen += 1
            if packet.channel == VtmChannel.ENCRYPTED_STREAM:
                return _fail("encrypted")
            if packet.channel != VtmChannel.STREAM or not packet.body:
                continue
            transport = detect_transport(packet.body)
            if transport in (StreamTransport.MPEG_PS, StreamTransport.MPEG_TS) and depacketizer.codec is None:
                chunk, codec = packet.body, "mpeg" if transport == StreamTransport.MPEG_PS else "mpegts"
            elif depacketizer.codec == MPEG_PS:
                chunk, codec = depacketizer.feed(packet.body), MPEG_PS
            else:
                nals = video_nals(packet.body)
                codec = depacketizer.codec
                chunk = b"".join(_SC + nal for nal in nals)
            if not chunk or codec is None:
                continue
            if ffmpeg is None:
                _info(f"видео: {codec}" + (" (зашифровано)" if depacketizer.encrypted else ""))
                got_video.set()
                ffmpeg = _ffmpeg(request.get("ffmpeg") or "ffmpeg", codec)
            pending += chunk
            if len(pending) >= 4096 or packet.body[1] & 0x80:
                ffmpeg.stdin.write(pending)
                ffmpeg.stdin.flush()
                pending.clear()
        if ffmpeg is None:
            return _fail("no_video")
        return _fail("failed", "облако закрыло поток")
    except _HelperFailure as exc:
        return _fail(exc.code)
    except (BrokenPipeError, OSError):
        return 0
    except Exception as exc:
        return _fail("failed", exc)
    finally:
        live.close()
        if ffmpeg is not None:
            try:
                ffmpeg.stdin.close()
            except OSError:
                pass
            ffmpeg.terminate()


if __name__ == "__main__":     # pragma: no cover
    if "--install" in sys.argv:
        print(install_runtime(print))
        sys.exit(0)
    sys.exit(helper_main())
