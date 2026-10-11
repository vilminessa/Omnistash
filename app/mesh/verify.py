"""Приём mesh-копии: уровни, сверка со СВОЕЙ строкой, провенанс.

Решение пользователя (зафиксировано): для приёма достаточно ОДНОГО узла;
консенсус и дата синхронизации узла - информация для человека, а не
барьер. Барьеры только два:
  * хеш в локальном стоп-листе (feedback) - «несовпадение» прошлого;
  * хеш не совпал с ЭТАЛОНОМ в моей базе (файл когда-то был скачан
    с площадки - это сильнее любого узла).

Структурная сверка (ffprobe) идёт ВСЕГДА: длительность чужого файла
против duration_s из СВОЕЙ строки (метаданные у нас свои, от площадки),
видео-поток на месте. Без ffmpeg проверка пропускается честной пометкой
в провенансе, а не молча.
"""

from __future__ import annotations

import json
import subprocess
import time

from . import manifest as manifest_mod

DURATION_SLACK = 2.0      # секунд допуска: свои меты округлены площадкой
FFPROBE_TIMEOUT = 30


def _ffprobe_exe() -> str | None:
    """Путь к ffprobe: рядом с найденным ffmpeg (ни один не найден -> None)."""
    from .. import downloader                 # лениво: тянет yt_dlp
    ffmpeg = downloader.find_ffmpeg()
    if not ffmpeg:
        return None
    from pathlib import Path
    probe = Path(ffmpeg).with_name("ffprobe.exe")
    return str(probe) if probe.is_file() else None


def probe(path, expected_duration=None) -> dict:
    """Структурная проверка файла. Возвращает:
    checked - вообще ли удалось проверить (ffprobe есть),
    ok - False только при явном несоответствии (None = не проверялось),
    duration - что показал ffprobe, errors - что не так.
    """
    exe = _ffprobe_exe()
    if not exe:
        return {"checked": False, "ok": None, "duration": None,
                "errors": [], "skipped": "ffprobe не найден"}
    try:
        done = subprocess.run(
            [exe, "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"checked": True, "ok": False, "duration": None,
                "errors": [f"ffprobe не запустился: {exc}"]}
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[:300]
        return {"checked": True, "ok": False, "duration": None,
                "errors": [f"файл не читается как медиа: {tail or 'без вывода'}"]}
    try:
        info = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return {"checked": True, "ok": False, "duration": None,
                "errors": ["нечитаемый ответ ffprobe"]}

    errors: list[str] = []
    streams = info.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        errors.append("в файле нет видео-потока")
    duration = None
    raw = (info.get("format") or {}).get("duration")
    try:
        duration = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        duration = None
    if expected_duration and duration is not None:
        if abs(duration - float(expected_duration)) > DURATION_SLACK:
            errors.append(
                f"длительность {duration:.1f}s не совпадает с нашей строкой "
                f"({expected_duration}s)")
    if expected_duration and duration is None:
        errors.append("ffprobe не сообщил длительность - сверить нечем")
    return {"checked": True, "ok": not errors, "duration": duration,
            "errors": errors}


def review(received_digest: str, own_digest: str | None = None,
           manifests: list | None = None,
           banned=frozenset()) -> dict:
    """Решение о приёме + сводка для человека.

    Возвращает: ok (принимаем), reason (почему нет), level (уровень
    приёма для провенанса/UI), consensus (сколько подписанных манифестов
    дают принятый хеш), claims (все предлаганные хеши -> кол-во),
    peer_synced_at (даты синка узлов с принятым хешом, новые первыми),
    divergent (есть ли альтернативные хеши).
    """
    signed = [m for m in (manifests or []) if manifest_mod.verify(m) is None]
    claims = manifest_mod.claims(signed)
    consensus = len(claims.get(received_digest, []))
    peer_dates = sorted(claims.get(received_digest, []), reverse=True)
    divergent = {h: len(v) for h, v in claims.items()
                 if h != received_digest}
    base = {"received": received_digest, "consensus": consensus,
            "claims": {h: len(v) for h, v in claims.items()},
            "peer_synced_at": peer_dates, "divergent": divergent,
            "ok": True, "reason": None, "level": ""}

    if received_digest in banned:
        base.update(ok=False, reason="хеш в стоп-листе («несовпадение»)",
                    level="отклонено")
        return base
    if own_digest:
        if received_digest != own_digest:
            base.update(
                ok=False,
                reason="не совпадает с эталоном из моей базы",
                level="отклонено по эталону")
            return base
        base["level"] = "эталон моей базы"
        return base
    if consensus >= 2:
        base["level"] = f"подтверждён {consensus} узлами"
    else:
        base["level"] = "одиночный узел (принимаем по правилу пользователя)"
    return base


def provenance(decision: dict, peer_pubkey: str, peer_synced_at: str = "",
               structure: dict | None = None) -> dict:
    """Запись для files.format_json: откуда копия и чем проверена.

    Ничего не выдумывает: только результат review() и probe() - чтобы
    через месяц было видно, на каком основании файл считается принятым.
    """
    structure = structure or {}
    return {
        "source": "mesh",
        "peer": str(peer_pubkey or ""),
        "peer_synced_at": str(peer_synced_at or ""),
        "level": decision.get("level") or "",
        "consensus": int(decision.get("consensus") or 0),
        "claims": decision.get("claims") or {},
        "structure_checked": bool(structure.get("checked")),
        "structure_errors": list(structure.get("errors") or []),
        "received_duration": structure.get("duration"),
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
