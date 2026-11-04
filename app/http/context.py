"""Что сейчас вызывает Api: окно (pywebview) или HTTP-клиент (docs/SERVER-CLIENT.md §2, §7).

Разделение нужно для честных границ: согласие «что уходит наружу» и код
привязки принимает только человек на сервере (окно), а не удалённый клиент, у
которого есть право server_config. Поток HTTP-запроса помечается
обработчиком на время вызова метода, поэтому метод знает, откуда его зовут,
и отказ по HTTP называет причину, а не молча выполняет действие.
"""

from __future__ import annotations

import contextlib
import threading

_local = threading.local()


def is_http() -> bool:
    """Вызов пришёл по HTTP (не из окна)."""
    return bool(getattr(_local, "http", False))


@contextlib.contextmanager
def http_call():
    """Помечает текущий поток как HTTP-вызов на время диспетчера."""
    previous = getattr(_local, "http", False)
    _local.http = True
    try:
        yield
    finally:
        _local.http = previous
