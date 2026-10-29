"""Аудио: метки файла (длительность, битрейт, частота, каналы) и обложка из тега.

Честность - главное правило модуля: метки берём только из двух источников,
которые действительно знают правду о файле:

  * ffprobe (если ffmpeg установлен - см. ffmpeg_installer и цепочку путей
    ниже): длительность, битрейт, частота дискретизации, каналы, кодек;
  * теги самого файла (ID3v2/ID3v1 у mp3, mp4-metadata у m4a, Vorbis-комментарии
    у ogg/opus/flac, RIFF INFO у wav): название, исполнитель, альбом, обложка.

Ничего не выдумываем и не «прибавляем на глаз»: без ffprobe длительность
остаётся NULL, а карточка говорит «не получена» и называет причину. Метки
файла, а не площадки: файл мог быть перекодирован руками, и битрейт у него
свой.

Новых зависимостей нет - форматы тегов читаем stdlib-парсером. Чтение
ограничено по размеру: парсер смотрит заголовок/блок тегов, а не весь файл
(FLAC на 30 МБ не должен читаться целиком ради пары строк текста).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import paths
from .util import now_iso, to_int, to_text

# Расширения, которые считаем АУДИО (строка получает media_kind='audio').
# Отдельный набор, а не расширение MEDIA_EXT: видео-список остаётся тем,
# чем был (никаких правок существующих строк скана).
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav"}

# Обложку из тега не тащим больше этого размера (и в кэш, и в data-UI окна).
COVER_MAX_BYTES = 1 * 1024 * 1024

# Сколько читаем для тегов: заголовок + блок тегов, не весь файл.
HEADER_READ_BYTES = 64 * 1024
TAG_BLOCK_MAX_BYTES = 1 * 1024 * 1024

COVER_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png",
                  "image/webp": ".webp", "image/gif": ".gif"}

# Кэш находки ffprobe: {ключ: путь|None}. Ключ - путь+mtime exe-файла,
# как в downloader.available_transcoders: переустановили ffmpeg -> проба
# снова реальная.
_PROBE_CACHE: dict[str, str | None] = {}


def is_audio(path: str | Path) -> bool:
    """Аудио-расширение файла (без чтения содержимого)."""
    return Path(str(path)).suffix.lower() in AUDIO_EXT


def media_kind_of(path: str | Path) -> str | None:
    """'audio' для аудио-контейнера, None для прочего (не гадаем)."""
    return "audio" if is_audio(path) else None


def _probe_candidates() -> list[Path]:
    """Где искать ffprobe: PATH, bin рядом с программой, профиль.

    Та же цепочка, что ищет ffmpeg в загрузчике, но без импорта downloader:
    здесь нужна только проба, а загрузчик тянет yt-dlp.
    """
    base = paths.base_dir()
    profile = paths.profile_dir()
    return [
        base / "bin" / "ffprobe.exe",
        profile / "Omnistash" / "bin" / "ffprobe.exe",
        profile / "bin" / "ffprobe.exe",
        profile / "Synfronia" / "bin" / "ffprobe.exe",
    ]


def find_ffprobe() -> str | None:
    """Путь к ffprobe (None - не установлен: метки будут «не получены»)."""
    found = shutil.which("ffprobe")
    if found:
        exe = Path(found)
    else:
        exe = next((candidate for candidate in _probe_candidates()
                    if candidate.exists()), None)
    if exe is None:
        # ffmpeg_installer знает свою папку установки (%LOCALAPPDATA%\Omnistash\bin):
        # им же и проверяем - один источник правды про установленное нами.
        try:
            from . import ffmpeg_installer
            ffmpeg = ffmpeg_installer.installed_ffmpeg()
        except Exception:  # noqa: BLE001 - кривая папка не должна валить пробу
            ffmpeg = None
        if ffmpeg:
            side = ffmpeg.parent / "ffprobe.exe"
            exe = side if side.exists() else None
    if exe is None:
        _PROBE_CACHE["none"] = None
        return None
    try:
        key = f"{exe}|{os.path.getmtime(exe)}"
    except OSError:
        return None
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    _PROBE_CACHE[key] = str(exe)
    return str(exe)


def _run(cmd: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Один запуск ffprobe (шв: тесты подменяют эту функцию без процесса)."""
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe(path: str | Path) -> dict | None:
    """Метки из ffprobe: {duration_s, bitrate_bps, sample_rate_hz, channels, codec}.

    None, когда читать нечем (нет ffprobe, файл битый, процесс не запустился).
    Никаких «примерных» значений: вызывающий пишет «не получена» с причиной.
    """
    exe = find_ffprobe()
    if not exe:
        return None
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostats",
           "-probesize", "1000000", "-analyzeduration", "0",
           "-show_entries", "format=duration,bit_rate",
           "-show_entries", "stream=codec_name,codec_type,sample_rate,channels",
           "-of", "json", str(path)]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    audio = next((s for s in streams
                  if str(s.get("codec_type") or "") == "audio"), streams[0] if streams else {})
    duration = fmt.get("duration")
    try:
        duration = float(duration) if duration not in (None, "N/A") else None
    except (TypeError, ValueError):
        duration = None
    out = {
        "duration_s": round(duration, 3) if duration is not None else None,
        "bitrate_bps": to_int(fmt.get("bit_rate")),
        "sample_rate_hz": to_int(audio.get("sample_rate")),
        "channels": to_int(audio.get("channels")),
        "codec": to_text(audio.get("codec_name")),
    }
    if not any(value is not None for value in out.values()):
        return None
    return out


# --------------------------------------------------------------------------- #
#  Теговые парсеры (stdlib): ID3v2/ID3v1, mp4 ilst, Vorbis-комментарии, RIFF
# --------------------------------------------------------------------------- #

_ID3_TEXT = {"TIT2": "title", "TPE1": "artist", "TALB": "album",
             "TCOM": "composer", "TCON": "genre", "TRCK": "track",
             "TT2": "title", "TP1": "artist", "TAL": "album",
             "TP2": "artist", "COM": "composer", "TCO": "genre", "TRC": "track"}
_ID3_PICTURE = {"APIC", "PIC"}


def _sync_safe(data: bytes) -> int:
    """ID3-размер: 7 значимых бит на байт (sync-safe integers)."""
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def _unsync(data: bytes) -> bytes:
    """Снять «unsynchronisation» ID3v2: 0xFF 0x00 -> 0xFF."""
    return data.replace(b"\xff\x00", b"\xff")


def _decode_text(payload: bytes) -> str | None:
    """Текст ID3-кадра: 0=latin1, 1=utf16 (BOM или LE), 2=utf16be, 3=utf8."""
    if not payload:
        return None
    encoding = payload[0]
    body = payload[1:]
    if encoding == 0:
        text = body.decode("latin-1", "replace")
    elif encoding == 1:
        if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = body.decode("utf-16", "replace")
        else:
            text = body.decode("utf-16-le", "replace")
    elif encoding == 2:
        text = body.decode("utf-16-be", "replace")
    else:
        text = body.decode("utf-8", "replace")
    text = text.replace("\x00", "").strip()
    return text or None


def _picture_payload(payload: bytes) -> tuple[bytes, str] | None:
    """APIC/PIC: кодировка, mime (latin1, до \x00), описание, картинка."""
    if len(payload) < 2:
        return None
    encoding = payload[0]
    rest = payload[1:]
    mime, _, rest = rest.partition(b"\x00")
    # Описание кадра нам не нужен (в индексе нет колонки «описание обложки»),
    # но его обязательно пропустить: оно стоит перед картинкой.
    if encoding in (1, 2):          # utf-16: описание заканчивается двумя \x00
        _desc, sep, rest = rest.partition(b"\x00\x00")
        if not sep:
            return None
    else:
        _desc, sep, rest = rest.partition(b"\x00")
        if not sep:
            return None
    mime = to_text(mime.decode("latin-1", "replace")) or "image/jpeg"
    if not rest:
        return None
    return rest, mime


def _id3v2(data: bytes) -> dict | None:
    """ID3v2.2/2.3/2.4 в начале файла: текстовые кадры + APIC."""
    if len(data) < 10 or data[:3] != b"ID3":
        return None
    version = data[3]
    if version not in (2, 3, 4):
        return None
    flags = data[5]
    size = _sync_safe(data[6:10])
    frames = data[10:10 + size]
    if version == 3 and (flags & 0x40):      # расширенный заголовок
        if len(frames) < 6:
            return None
        frames = frames[6 + frames[4]:]      # v2.3: свой размер в байтах
    elif version == 4 and (flags & 0x08):
        ext_size = _sync_safe(frames[:4])
        frames = frames[4 + ext_size:]
    if version in (3, 4) and (flags & 0x80):
        frames = _unsync(frames)
    elif version == 2 and (flags & 0x80):
        frames = _unsync(frames)

    out: dict = {}
    offset = 0
    limit = len(frames)
    while offset + (3 if version == 2 else 10) <= limit:
        if version == 2:
            key = frames[offset:offset + 3].decode("latin-1", "replace")
            frame_size = int.from_bytes(frames[offset + 3:offset + 6], "big")
            body_start = offset + 6
        else:
            key = frames[offset:offset + 4].decode("latin-1", "replace")
            if not key:                      #padding после последнего кадра
                break
            frame_size = (_sync_safe(frames[offset + 4:offset + 8])
                          if version == 4 else
                          int.from_bytes(frames[offset + 4:offset + 8], "big"))
            body_start = offset + 10
        if frame_size <= 0:
            break
        body = frames[body_start:body_start + frame_size]
        offset = body_start + frame_size
        if key in _ID3_PICTURE:
            picture = _picture_payload(body)
            if picture and len(picture[0]) <= COVER_MAX_BYTES:
                out["cover"], out["cover_mime"] = picture
        elif key in _ID3_TEXT:
            value = _decode_text(body)
            if value:
                out[_ID3_TEXT[key]] = value
    return out or None


def _id3v1_tail(handle) -> dict | None:
    """ID3v1 в хвосте файла (128 байт): title/artist/album по 30 символов."""
    try:
        handle.seek(-128, os.SEEK_END)
        tail = handle.read(128)
    except (OSError, ValueError):
        return None
    if len(tail) < 128 or tail[:3] != b"TAG":
        return None
    def clean(chunk: bytes) -> str | None:
        return to_text(chunk.decode("latin-1", "replace").strip("\x00 "))
    out = {}
    title, artist, album = clean(tail[3:33]), clean(tail[33:63]), clean(tail[63:93])
    if title:
        out["title"] = title
    if artist:
        out["artist"] = artist
    if album:
        out["album"] = album
    return out or None


_MP4_TEXT = {b"\xa9nam": "title", b"\xa9ART": "artist", b"\xa9alb": "album",
             b"aART": "artist", b"\xa9day": "date", b"\xa9gen": "genre"}
_MP4_COVER_FLAGS = {0x01: "image/jpeg", 0x02: "image/png", 0x14: "image/jpeg",
                    0x18: "image/png"}


def _mp4_atoms(data: bytes, start: int, end: int, depth: int = 0) -> dict | None:
    """Обход контейнера mp4 (moov -> udta -> meta -> ilst) без чтения всего файла.

    Атом, объявленный больше, чем прочитано, - обрыв: честно None, а не
    половинка тега, которую парсер выдал бы за целую.
    """
    if depth > 5:
        return None
    out: dict = {}
    offset = start
    guard = 0
    while offset + 8 <= end and guard < 2000:
        guard += 1
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        body_start = offset + 8
        if size == 1:                       # 64-bit размер
            if offset + 16 > end:
                return None
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            body_start = offset + 16
        elif size == 0:                     # до конца файла
            size = end - offset
        if size < 8 or body_start + (size - 8) > end:
            return None
        body_end = body_start + (size - 8)
        if kind in (b"moov", b"udta", b"ilst"):
            nested = _mp4_atoms(data, body_start, body_end, depth + 1)
            if nested:
                out.update(nested)
        elif kind == b"meta":
            # meta содержит 4 байта версии/флагов перед дочерними атомами
            nested = _mp4_atoms(data, body_start + 4, body_end, depth + 1)
            if nested:
                out.update(nested)
        elif kind in _MP4_TEXT or kind == b"covr":
            payload = _mp4_data(data, body_start, body_end)
            if payload is None:
                continue
            blob, mime = payload
            if kind == b"covr":
                if blob and len(blob) <= COVER_MAX_BYTES:
                    out["cover"], out["cover_mime"] = blob, mime or "image/jpeg"
            else:
                text = to_text(blob.decode("utf-8", "replace"))
                if text:
                    out[_MP4_TEXT[kind]] = text
        offset = body_end
    return out or None


def _mp4_data(data: bytes, start: int, end: int) -> tuple[bytes, str | None] | None:
    """Атом «data» внутри текстового/обложечного элемента ilst."""
    offset = start
    while offset + 16 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if kind != b"data":
            offset += (size if size >= 8 else 8)
            continue
        flags = int.from_bytes(data[offset + 8:offset + 12], "big") & 0xFF
        body = data[offset + 16:min(offset + size, end)]
        return body, _MP4_COVER_FLAGS.get(flags)
    return None


def _read_mp4_tags(path: str | Path) -> dict | None:
    """mp4/m4a/mov: moov бывает в начале (faststart) или в конце (старые m4a)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(min(size, HEADER_READ_BYTES))
            found = _mp4_atoms(head, 0, len(head))
            if found:
                return found
            if size > HEADER_READ_BYTES:
                handle.seek(max(0, size - HEADER_READ_BYTES))
                tail = handle.read(HEADER_READ_BYTES)
                return _mp4_atoms(tail, 0, len(tail))
    except OSError:
        return None
    return None


_VORBIS_KEYS = {"TITLE": "title", "ARTIST": "artist", "ALBUM": "album",
                "COMPOSER": "composer", "GENRE": "genre", "TRACKNUMBER": "track",
                "DATE": "date", "YEAR": "date"}


def _vorbis_comments(block: bytes) -> dict | None:
    """Vorbis/Opus-комментарии (и FLAC METADATA_BLOCK_VORBIS) -> тексты + обложка."""
    if block.startswith(b"\x03Vorbis"):
        block = block[7:]
    elif block.startswith(b"OpusTags"):
        block = block[8:]
    elif block.startswith(b"Vorbis"):
        block = block[6:]
    out: dict = {}
    offset = 0

    def le32(chunk: bytes) -> int:
        return int.from_bytes(chunk[:4], "little")

    if len(block) < 4:
        return None
    vendor_len = le32(block[offset:offset + 4])
    offset += 4 + vendor_len
    if offset + 4 > len(block):
        return None
    count = le32(block[offset:offset + 4])
    offset += 4
    if count > 200:
        return None
    for _ in range(count):
        if offset + 4 > len(block):
            break
        length = le32(block[offset:offset + 4])
        offset += 4
        value = block[offset:offset + length]
        offset += length
        if length > 16 and value.startswith(b"METADATA_BLOCK_PICTURE"):
            cover = _flac_picture(value[len(b"METADATA_BLOCK_PICTURE"):])
            if cover:
                out["cover"], out["cover_mime"] = cover
            continue
        text = value.decode("utf-8", "replace")
        key, _, item = text.partition("=")
        item = to_text(item)
        if item and key.upper() in _VORBIS_KEYS:
            out[_VORBIS_KEYS[key.upper()]] = item
    return out or None


def _flac_picture(payload: bytes) -> tuple[bytes, str] | None:
    """METADATA_BLOCK_PICTURE: mime, описание, размеры, сами байты картинки."""
    offset = 4                       # тип изображения
    def le32(chunk: bytes) -> int:
        return int.from_bytes(chunk[:4], "little")
    if len(payload) < offset + 4:
        return None
    mime_len = le32(payload[offset:offset + 4])
    offset += 4
    mime = payload[offset:offset + mime_len].decode("latin-1", "replace")
    offset += mime_len
    if len(payload) < offset + 4:
        return None
    desc_len = le32(payload[offset:offset + 4])
    offset += 4 + desc_len
    # ширина, высота, глубина, палитра
    if len(payload) < offset + 16:
        return None
    offset += 12 + le32(payload[offset + 12:offset + 16])
    if len(payload) < offset + 4:
        return None
    data_len = le32(payload[offset:offset + 4])
    offset += 4
    blob = payload[offset:offset + data_len]
    if not blob or len(blob) > COVER_MAX_BYTES:
        return None
    return blob, (to_text(mime) or "image/jpeg")


def _read_flac_tags(path: str | Path) -> dict | None:
    """FLAC: «fLaC» + блоки метаданных; Vorbis-блок (тип 4) несёт тексты и обложку."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(TAG_BLOCK_MAX_BYTES)
    except OSError:
        return None
    if not head.startswith(b"fLaC"):
        return None
    offset = 4
    while offset + 4 <= len(head):
        last = head[offset] & 0x80
        kind = head[offset] & 0x7F
        length = int.from_bytes(head[offset + 1:offset + 4], "big")
        offset += 4
        block = head[offset:offset + length]
        offset += length
        if kind == 4 and block:
            found = _vorbis_comments(block)
            if found:
                return found
        if last:
            return None
    return None


def _ogg_packets(data: bytes) -> list[bytes]:
    """Страницы Ogg -> пакеты первых страниц (OggS + таблица сегментов)."""
    packets: list[bytes] = []
    offset = 0
    current = bytearray()
    pages = 0
    while offset + 27 <= len(data) and pages < 4:
        if data[offset:offset + 4] != b"OggS":
            break
        segments = data[offset + 27:offset + 27 + (len(data) - offset - 27)]
        seg_count = data[offset + 26]
        segments = segments[:seg_count]
        body_start = offset + 27 + seg_count
        consumed = 0
        for segment in segments:
            chunk = data[body_start + consumed:body_start + consumed + segment]
            current.extend(chunk)
            consumed += segment
            if segment < 255:           # конец пакета
                packets.append(bytes(current))
                current = bytearray()
        offset = body_start + consumed
        pages += 1
    if current:
        packets.append(bytes(current))
    return packets


def _read_ogg_tags(path: str | Path) -> dict | None:
    """Ogg (Vorbis/Theora-аудио) и Opus: комментарийный пакет - второй в потоке."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(TAG_BLOCK_MAX_BYTES)
    except OSError:
        return None
    if not head.startswith(b"OggS"):
        return None
    for packet in _ogg_packets(head):
        if packet.startswith(b"\x03Vorbis") or packet.startswith(b"OpusTags"):
            return _vorbis_comments(packet)
    return None


def _read_wav_tags(path: str | Path) -> dict | None:
    """WAV: ID3v2 в начале (класть умеют некоторые конвертеры) + RIFF LIST INFO."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEADER_READ_BYTES)
    except OSError:
        return None
    found = _id3v2(head)
    if found:
        return found
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    out: dict = {}
    offset = 12
    keys = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
            b"ICRD": "date", b"IGNR": "genre"}
    while offset + 8 <= len(head):
        kind = head[offset:offset + 4]
        length = int.from_bytes(head[offset + 4:offset + 8], "little")
        body = head[offset + 8:offset + 8 + length]
        offset += 8 + length + (length % 2)
        if kind == b"LIST" and body[:4] == b"INFO":
            inner = 4
            while inner + 8 <= len(body):
                sub = body[inner:inner + 4]
                sub_len = int.from_bytes(body[inner + 4:inner + 8], "little")
                text = to_text(body[inner + 8:inner + 8 + sub_len].decode(
                    "latin-1", "replace").strip("\x00"))
                if text and sub in keys:
                    out[keys[sub]] = text
                inner += 8 + sub_len + (sub_len % 2)
    return out or None


def read_tags(path: str | Path) -> dict | None:
    """Текстовые метки и обложка из тега файла (None - тегов нет/формат не прочитан).

    Читает ТОЛЬКО то, что нужно для тегов: заголовок и блок метаданных,
    не содержимое дорожки. Схема прочитана - возвращаем «tags», чтобы
    карточка могла сказать, откуда взялось значение.
    """
    ext = Path(str(path)).suffix.lower()
    readers = {
        ".mp3": _read_id3, ".m4a": _read_mp4_tags, ".aac": _read_mp4_tags,
        ".wav": _read_wav_tags, ".ogg": _read_ogg_tags, ".opus": _read_ogg_tags,
        ".flac": _read_flac_tags,
    }
    reader = readers.get(ext)
    if reader is None:
        return None
    found = reader(path) or {}
    if not found:
        return None
    found.setdefault("tags", _scheme_name(ext, found))
    return found


def _read_id3(path: str | Path) -> dict | None:
    """mp3: ID3v2 в начале, ID3v1 в хвосте (v1 - запасной путь для старых файлов)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(TAG_BLOCK_MAX_BYTES)
            found = _id3v2(head)
            if found:
                found["tags"] = "id3v2"
                return found
            tail = _id3v1_tail(handle)
    except OSError:
        return None
    if tail:
        tail["tags"] = "id3v1"
        return tail
    return None


def _scheme_name(ext: str, found: dict) -> str:
    """Как называть схему тегов в карточке (честно: чем прочитано)."""
    if ext == ".mp3":
        return str(found.get("tags") or "id3")
    if ext in (".m4a", ".aac"):
        return "mp4-metadata"
    if ext in (".ogg", ".opus"):
        return "vorbis-comments"
    if ext == ".flac":
        return "flac/vorbis"
    return "wav/RIFF"


def cover_dir() -> Path:
    """Кэш обложек в профиле: извлечённая из тега картинка лежит у нас.

    Не рядом с файлом пользователя: библиотека человека не должна
    обрастать файлами, которых он не просил. Обложка становится строкой
    files(kind='thumbnail') - плитка и карточка показывают её тем же
    путём, что и локальную обложку видео.
    """
    folder = paths.profile_dir() / "audio-covers"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_cover(path: str | Path, blob: bytes, mime: str) -> str | None:
    """Обложку из тега -> файл кэша; возвращает путь (None - не записали)."""
    if not blob or len(blob) > COVER_MAX_BYTES:
        return None
    ext = COVER_MIME_EXT.get(str(mime or "").lower(), ".jpg")
    digest = hashlib.sha1(str(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    target = cover_dir() / f"{digest}{ext}"
    try:
        target.write_bytes(blob)
    except OSError:
        return None
    return str(target)


def audio_meta(path: str | Path, *, want_probe: bool = True,
               extract_cover: bool = True) -> dict:
    """Метки аудио для files.format_json: то, что прочитано, и честные NULL.

    Ключи:
      media/format - 'audio' и расширение файла;
      duration_s/bitrate_bps/sample_rate_hz/channels/codec - из ffprobe
      (NULL, если probe=None - «не получена», а не выдумана);
      probe/probe_note - чем получены метки и почему не получены;
      title/artist/album/date/genre/track + tags_scheme - из тега;
      cover_path - путь извлечённой обложки (кэш), cover_mime - её mime.
    """
    path = str(path)
    meta: dict = {"media": "audio", "format": Path(path).suffix.lower()}
    probe_data = probe(path) if want_probe else None
    if probe_data is None:
        meta["probe"] = None
        meta["probe_note"] = ("ffprobe не установлен: длительность и битрейт "
                              "не получены" if not find_ffprobe()
                              else "ffprobe не прочитал файл")
    else:
        meta.update(probe_data)
        meta["probe"] = "ffprobe"

    tags = read_tags(path) or {}
    for key in ("title", "artist", "album", "date", "genre", "composer", "track"):
        value = tags.get(key)
        if value:
            meta[key] = value
    if tags.get("tags"):
        meta["tags_scheme"] = tags["tags"]
    cover = tags.get("cover")
    if cover and extract_cover:
        target = write_cover(path, cover, tags.get("cover_mime") or "image/jpeg")
        if target:
            meta["cover_path"] = target
            meta["cover_mime"] = tags.get("cover_mime") or "image/jpeg"
    meta["obtained_at"] = now_iso()
    return meta


def dumps(meta: dict) -> str:
    """format_json строки files (одна запись на файл, компактная)."""
    try:
        return json.dumps(meta, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""
