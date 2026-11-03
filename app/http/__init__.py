"""HTTP-слой клиента (docs/SERVER-CLIENT.md): права, токены, сервер, аудит.

Сервер - тот же процесс Omnistash, что окно: он остаётся единственным
владельцем базы, хранилищ и очереди (docs/SERVER-CLIENT.md §2). Сюда входит
то, что решает «кто может спросить что»: скоупы (scopes), ключи доступа
(tokens), журнал обращений (audit) и слушатель (server).

Байтовые маршруты (/thumb, /media, /subs, /hls) - S2 и S3; привязка по
короткому коду и режимы экспозиции - S6.
"""

from __future__ import annotations

from .audit import Audit
from .media import Watchers, etag_for, parse_range, srt_to_vtt
from .scopes import (INTERNAL, LOCAL, NATIVE, SCOPES, SCOPE_OF, exposed,
                     is_internal, is_local, is_native, normalize_scopes,
                     scope_for)
from .server import ServeError, ServeServer, bind_address, byte_route, route_scope
from .tls import TlsError, context_from, fingerprint, material, server_context
from .tokens import MAX_TOKENS, Token, TokenStore, TokenStoreError

__all__ = ["Audit", "ServeError", "ServeServer", "bind_address", "byte_route",
           "route_scope", "Token", "TokenStore", "TokenStoreError", "MAX_TOKENS",
           "SCOPES", "SCOPE_OF", "NATIVE", "LOCAL", "INTERNAL", "scope_for",
           "exposed", "is_native", "is_local", "is_internal", "normalize_scopes",
           "TlsError", "context_from", "fingerprint", "material", "server_context",
           "Watchers", "etag_for", "parse_range", "srt_to_vtt"]
