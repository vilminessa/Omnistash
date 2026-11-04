"""HTTP-слой клиента (docs/SERVER-CLIENT.md): права, токены, сервер, аудит.

Сервер - тот же процесс Omnistash, что окно: он остаётся единственным
владельцем базы, хранилищ и очереди (docs/SERVER-CLIENT.md §2). Сюда входит
то, что решает «кто может спросить что»: скоупы (scopes), ключи доступа
(tokens), журнал обращений (audit) и слушатель (server).

Байтовые маршруты (/thumb, /media, /subs, /hls) - http.media и http.hls;
привязка по короткому коду и режимы экспозиции - S6.
"""

from __future__ import annotations

from . import bind, pair
from .audit import Audit
from .hls import HlsBuilder, HlsError, invalidate as hls_invalidate
from .media import Viewers, etag_for, parse_range, srt_to_vtt
from .pair import Pairing, PRESET_LABELS, SCOPE_PRESETS, preset_scopes
from .scopes import (INTERNAL, LOCAL, NATIVE, SCOPES, SCOPE_OF, WINDOW_ONLY,
                     WINDOW_REASON, exposed, is_internal, is_local,
                     is_native, is_window_only, normalize_scopes, scope_for,
                     window_reason)
from .server import ServeError, ServeServer, bind_address, byte_route, route_scope
from .tls import TlsError, context_from, fingerprint, material, server_context
from .tokens import MAX_TOKENS, Token, TokenStore, TokenStoreError

__all__ = ["Audit", "ServeError", "ServeServer", "bind_address", "byte_route",
           "route_scope", "Token", "TokenStore", "TokenStoreError", "MAX_TOKENS",
           "SCOPES", "SCOPE_OF", "NATIVE", "LOCAL", "INTERNAL", "scope_for",
           "exposed", "is_native", "is_local", "is_internal", "normalize_scopes",
           "WINDOW_ONLY", "WINDOW_REASON", "is_window_only", "window_reason",
           "bind", "pair", "Pairing", "SCOPE_PRESETS", "PRESET_LABELS",
           "preset_scopes",
           "TlsError", "context_from", "fingerprint", "material", "server_context",
           "Viewers", "etag_for", "parse_range", "srt_to_vtt",
           "HlsBuilder", "HlsError", "hls_invalidate"]
