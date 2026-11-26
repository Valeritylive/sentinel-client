"""Поиск записей по типам и пакет доказательств для экспорта (без GTK).

Типы записей Hikvision приходят в searchMatchItem как
``<metadataDescriptor>recordType.meta.std-cgi.com/MOTION</metadataDescriptor>``.
Пакет экспорта — папка (и ZIP) с видео MP4, страницей-плеером index.html,
manifest.json с описанием и SHA256SUMS для проверки целостности.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

RECORD_TYPES = {"all": "Все типы", "continuous": "Непрерывная", "motion": "Движение",
                "alarm": "Тревога", "smart": "Интеллектуальное событие"}
RECORD_COLORS_HEX = {"continuous": "#2985e0", "motion": "#edc733", "alarm": "#e03333", "smart": "#9e66e6",
                     "local": "#40bf73"}
# Тип записи -> дескриптор ISAPI для фильтра поиска
SEARCH_DESCRIPTORS = {"continuous": "//recordType.meta.std-cgi.com/CMR",
                      "motion": "//recordType.meta.std-cgi.com/MOTION",
                      "alarm": "//recordType.meta.std-cgi.com/ALARM",
                      "smart": "//recordType.meta.std-cgi.com/EDR"}

_CONTINUOUS = {"CMR", "TIMING", "MANUAL", "CONTINUOUS", "ALLEVENT", "ALL"}
_MOTION = {"MOTION", "VMD", "MOTIONDETECTION"}
_ALARM = {"ALARM", "ALARMANDMOTION", "ALARMORMOTION", "ALARMINPUT", "IO"}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def record_kind(descriptor):
    """Дескриптор типа записи -> continuous / motion / alarm / smart."""
    if not descriptor:
        return "continuous"
    name = re.split(r"[/.]", descriptor.strip().rstrip("/"))[-1].upper().replace("_", "").replace("-", "")
    if name in _CONTINUOUS:
        return "continuous"
    if name in _MOTION:
        return "motion"
    if name in _ALARM:
        return "alarm"
    return "smart"


def track_id(channel, stream=1):
    return int(channel) * 100 + int(stream)


def search_query(track, start, end, position=0, record_type="all", max_results=50, search_id=None):
    """XML для POST /ISAPI/ContentMgmt/search (время — ISO-строки или datetime)."""
    def iso(value):
        return value.astimezone().isoformat(timespec="seconds") if isinstance(value, datetime) else str(value)
    metadata = ""
    descriptor = SEARCH_DESCRIPTORS.get(record_type)
    if descriptor:
        metadata = f"<metadataList><metadataDescriptor>{descriptor}</metadataDescriptor></metadataList>"
    return (f"<CMSearchDescription><searchID>{search_id or uuid.uuid4()}</searchID>"
            f"<trackList><trackID>{int(track)}</trackID></trackList>"
            f"<timeSpanList><timeSpan><startTime>{iso(start)}</startTime><endTime>{iso(end)}</endTime>"
            f"</timeSpan></timeSpanList><maxResults>{int(max_results)}</maxResults>"
            f"<searchResultPosition>{int(position)}</searchResultPosition>{metadata}"
            "</CMSearchDescription>").encode()


def _parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


def parse_search_response(payload):
    """(записи [{start, end, uri, kind}], есть_ещё, найдено_в_ответе)."""
    root = ET.fromstring(payload)
    status = ""
    items = []
    count = 0
    for node in root.iter():
        tag = _local(node.tag)
        if tag == "responseStatusStrg":
            status = (node.text or "").strip().upper()
        if tag != "searchMatchItem":
            continue
        count += 1
        fields = {}
        descriptor = ""
        for child in node.iter():
            name = _local(child.tag)
            if name == "metadataDescriptor":
                descriptor = (child.text or "").strip()
            elif child.text and name not in fields:
                fields[name] = child.text.strip()
        try:
            start, end = _parse_time(fields["startTime"]), _parse_time(fields["endTime"])
        except (KeyError, ValueError):
            continue
        if end <= start:
            continue
        items.append({"start": start, "end": end, "uri": fields.get("playbackURI", ""),
                      "kind": record_kind(descriptor)})
    return items, status == "MORE", count


def search_all(post, track, start, end, record_type="all", max_pages=10, page_size=50):
    """Постраничный поиск; post(xml_bytes) -> bytes ответа."""
    search_id = str(uuid.uuid4())
    result, position = [], 0
    for _page in range(max_pages):
        items, more, count = parse_search_response(
            post(search_query(track, start, end, position, record_type, page_size, search_id)))
        result.extend(items)
        position += count
        if not more or not count:
            break
    return result


def group_by_camera(results):
    """[(устройство, [записи по времени])] в порядке имён камер; пустые группы отбрасываются."""
    groups = {}
    for device, items in results:
        if items:
            groups.setdefault(device["id"], (device, []))[1].extend(items)
    ordered = sorted(groups.values(), key=lambda pair: (pair[0].get("name") or "").lower())
    return [(device, sorted(items, key=lambda i: i["start"])) for device, items in ordered]


def scope_devices(devices, selected, scope):
    """Камеры для поиска: выбранный канал, все каналы регистратора или вся область."""
    if selected is None:
        return []
    cameras = [d for d in devices if (d.get("category") or "video") == "video"]
    if scope == "device":
        return [d for d in cameras if d["host"] == selected["host"]
                and int(d.get("sdk_port") or 8000) == int(selected.get("sdk_port") or 8000)]
    if scope == "area":
        group = selected.get("group_id")
        area = selected.get("area") or ""
        if group:
            return [d for d in cameras if d.get("group_id") == group]
        if area:
            return [d for d in cameras if (d.get("area") or "") == area]
        return cameras
    return [selected]


# ---------------------------------------------------------------- пакет доказательств
def safe_name(text, limit=40):
    cleaned = re.sub(r"[^\w.-]+", "_", text or "", flags=re.UNICODE).strip("._")
    return (cleaned or "camera")[:limit]


def clip_filename(index, device, start):
    return f"{index:02d}_{safe_name(device.get('name'))}_{start:%Y%m%d_%H%M%S}.mp4"


def ffmpeg_export_command(ffmpeg, list_path, output, duration):
    """argv: RTSP-архив -> MP4 без перекодирования (URL с паролем — в list-файле, не в argv)."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file,rtsp,tcp,udp,rtp",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-t", f"{max(1, int(duration))}", "-c", "copy", "-map", "0:v:0", "-map", "0:a?",
            "-movflags", "+faststart", "-f", "mp4", str(output)]


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def package_folder(root, device, start, now=None):
    now = now or datetime.now()
    return Path(root) / f"{now:%Y%m%d_%H%M%S}_{safe_name(device.get('name'))}_{start:%Y%m%d_%H%M}"


def build_manifest(clips, exported_by, case="", note="", now=None):
    """clips: [{file, camera, host, channel, start, end, kind, sha256, size}]."""
    now = now or datetime.now().astimezone()
    return {"format": "sentinel-evidence/1", "created": now.isoformat(timespec="seconds"),
            "exported_by": exported_by, "case": case, "note": note,
            "clips": [{**c, "start": c["start"].isoformat(timespec="seconds"),
                       "end": c["end"].isoformat(timespec="seconds")} for c in clips]}


def player_html(manifest):
    """Самодостаточная страница-плеер: список клипов, видео, сведения и контрольные суммы."""
    rows = []
    for index, clip in enumerate(manifest["clips"]):
        color = RECORD_COLORS_HEX.get(clip.get("kind"), "#2985e0")
        rows.append(
            f'<li><button data-i="{index}"><span class="dot" style="background:{color}"></span>'
            f'{html.escape(clip["camera"])}<small>{html.escape(clip["start"][:19].replace("T", " "))} – '
            f'{html.escape(clip["end"][11:19])}</small></button></li>')
    data = json.dumps(manifest, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Экспорт видео Sentinel</title>
<style>
:root {{ color-scheme: light dark; --bg:#f6f7f9; --fg:#1b1d22; --muted:#626873; --card:#fff; --line:#dde1e6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16171a; --fg:#e8e9ec; --muted:#9aa0aa; --card:#1f2024; --line:#2e3036; }} }}
body {{ margin:0; font:15px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
main {{ display:grid; grid-template-columns:minmax(220px, 300px) 1fr; gap:16px; padding:16px; max-width:1400px; margin:auto; }}
@media (max-width:760px) {{ main {{ grid-template-columns:1fr; }} }}
h1 {{ font-size:18px; margin:16px 16px 0; }}
ul {{ list-style:none; margin:0; padding:0; }}
li button {{ width:100%; text-align:left; background:var(--card); color:inherit; border:1px solid var(--line);
  border-radius:6px; padding:8px 10px; margin-bottom:6px; cursor:pointer; font:inherit; }}
li button.on {{ outline:2px solid #2985e0; }}
small {{ display:block; color:var(--muted); }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }}
video {{ width:100%; max-height:70vh; background:#000; border-radius:6px; }}
.meta {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin-top:10px; }}
code {{ word-break:break-all; font-size:12px; color:var(--muted); }}
</style></head><body>
<h1>Экспорт видео · {html.escape(manifest["created"][:19].replace("T", " "))}</h1>
<main><section><ul>{"".join(rows)}</ul>
<div class="meta"><b>Экспортировал:</b> {html.escape(manifest["exported_by"] or "—")}<br>
<b>Дело:</b> {html.escape(manifest["case"] or "—")}<br>{html.escape(manifest["note"] or "")}</div></section>
<section><video id="v" controls playsinline></video><div class="meta" id="info"></div></section></main>
<script>
const M = {data};
const v = document.getElementById("v"), info = document.getElementById("info");
const esc = s => String(s).replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]);
function show(i) {{
  const c = M.clips[i];
  document.querySelectorAll("li button").forEach((b, k) => b.classList.toggle("on", k === i));
  v.src = c.file;
  info.innerHTML = "<b>" + esc(c.camera) + "</b> · " + esc(c.host) + " · канал " + esc(c.channel) +
    "<br>" + esc(c.start.replace("T", " ")) + " — " + esc(c.end.replace("T", " ")) +
    "<br>SHA-256: <code>" + esc(c.sha256) + "</code>" +
    "<br><small>Если видео не воспроизводится в браузере (например, H.265), откройте файл " + esc(c.file) +
    " в VLC или mpv.</small>";
}}
document.querySelectorAll("li button").forEach(b => b.addEventListener("click", () => show(+b.dataset.i)));
if (M.clips.length) show(0);
</script></body></html>
"""


README_TEXT = """Экспорт видео Sentinel

index.html     — открыть в браузере: список клипов, плеер и сведения
*.mp4          — видео без перекодирования (H.264/H.265); если браузер не
                 показывает H.265, откройте файл в VLC или mpv
manifest.json  — камеры, время, кто и когда экспортировал
SHA256SUMS     — контрольные суммы; проверка: sha256sum -c SHA256SUMS
"""


def finalize_package(folder, clips, exported_by, case="", note="", now=None, make_zip=True):
    """Посчитать суммы, записать manifest.json, SHA256SUMS, index.html, README; при make_zip — ZIP рядом.

    clips: [{file (имя в папке), camera, host, channel, start, end, kind}]; отсутствующие файлы
    пропускаются. Возвращает (manifest, путь ZIP или None).
    """
    folder = Path(folder)
    done = []
    for clip in clips:
        path = folder / clip["file"]
        if not path.is_file() or path.stat().st_size == 0:
            continue
        done.append({**clip, "sha256": sha256_file(path), "size": path.stat().st_size})
    manifest = build_manifest(done, exported_by, case, note, now)
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sums = "".join(f"{c['sha256']}  {c['file']}\n" for c in done)
    (folder / "SHA256SUMS").write_text(sums, encoding="utf-8")
    (folder / "index.html").write_text(player_html(manifest), encoding="utf-8")
    (folder / "README.txt").write_text(README_TEXT, encoding="utf-8")
    archive = None
    if make_zip and done:
        archive = folder.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            for path in sorted(folder.iterdir()):
                bundle.write(path, f"{folder.name}/{path.name}")
        os.chmod(archive, 0o600)
    return manifest, archive
