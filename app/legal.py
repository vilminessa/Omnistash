"""Юридические согласия человека (LEG-1/LEG-3): датированный факт.

Сознательно отдельный файл, а НЕ settings: схема настроек - это UI-форма,
а факт «человек явно нажал Принять» (когда именно) - запись для
юридической рамки (ACCEPTABLE_USE.md): она не должна вылезать в форму
настроек или путаться с прочими ключами.

Флаги:
  aup_accepted_at    - дата/время принятия правил (строка, пусто = нет);
  first_send_accepted - предупреждение «передаёте файл другому лицу»
                        принято (одноразово; mesh и wormhole).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .paths import legal_path

_DEFAULTS = {"aup_accepted_at": "", "first_send_accepted": False}
_KEYS = tuple(_DEFAULTS)


def _read() -> dict:
    try:
        raw = legal_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        return dict(_DEFAULTS)
    return {key: data.get(key, _DEFAULTS[key]) for key in _KEYS}


def _write(data: dict) -> None:
    path = legal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def state() -> dict:
    """Текущие факты согласий."""
    return _read()


def accept(*, aup: bool = False, first_send: bool = False) -> dict:
    """Зафиксировать согласия (вызывается только по явному действию
    человека из UI); возвращает свежее состояние.

    Дата AUP пишется ОДИН раз: повторные «Принятия» не откатывают
    исходный факт (юридически важен первый момент).
    """
    data = _read()
    if aup and not data["aup_accepted_at"]:
        data["aup_accepted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if first_send:
        data["first_send_accepted"] = True
    _write(data)
    return data
