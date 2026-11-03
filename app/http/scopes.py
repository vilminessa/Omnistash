"""Права токена доступа (docs/SERVER-CLIENT.md §4).

Скоуп решает СЕРВЕР: клиент не может «попросить» то, что ему не выдано.
Список целей здесь - единственный источник правды по HTTP: тесты сверяют его
с фасадом `Api` (app/gui.py), с таблицей скоупов в документе и со списком
клиента (ui_src/app.js), поэтому цель не может исчезнуть из одного места и
остаться в другом (то же, что зеркало настроек держит для карточки).

Разделение прав - не «тонкий клиент»: клиент с полными скоупами делает всё,
что окно, кроме нативных диалогов; клиент с `read` + `watch` только смотрит.
"""

from __future__ import annotations

# Скоупы токена. `watch` - право только на байтовые маршруты (/media, /subs,
# /hls): методов Api он не требует, поэтому в SCOPE_OF его нет.
SCOPES = ("read", "watch", "queue", "mark", "sources", "mesh", "accounts",
          "server_config", "desktop")

# Цели, уходящие по HTTP: имя метода -> скоуп, который сервер требует.
SCOPE_OF: dict[str, str] = {
    # read: чтение библиотеки и diagnostics, ничего не меняет
    "adult_tab": "read", "collections_list": "read", "data_api_meta": "read",
    "data_api_status": "read", "duplicates": "read", "get_initial": "read",
    "get_thumb": "read", "get_thumbs": "read", "get_video": "read",
    "language_list": "read", "legal_state": "read", "list_videos": "read",
    "migrate_preview": "read", "platforms_list": "read", "poll": "read",
    "repack_preview": "read", "site_catalog": "read",
    "site_catalog_cancel": "read", "site_catalog_channels": "read",
    "site_catalog_clear": "read", "site_catalog_more": "read",
    "site_catalog_status": "read", "storage_preview_detach": "read",
    "storages_list": "read", "tags_popular": "read", "theme_list": "read",
    "ui_texts": "read",

    # queue: добавление, очередь, закачка; включает подтверждение 18+
    "add_close": "queue", "add_confirm": "queue", "add_start": "queue",
    "bulk_download": "queue", "enqueue": "queue", "legal_accept": "queue",
    "queue_retry": "queue", "queue_start": "queue", "queue_stop": "queue",
    "repair_broken": "queue", "sync_queue_new": "queue",

    # mark: пометки строк - поля, рейтинг, локальные теги
    "bulk_rating": "mark", "bulk_tags": "mark", "save_fields": "mark",

    # sources: хранилища, коллекции, скан, синк, перенос, целостность
    "bulk_collection": "sources", "bulk_remove_rows": "sources",
    "collection_create": "sources", "collection_delete": "sources",
    "collection_edit": "sources", "collection_rename": "sources",
    "dedupe_resolve": "sources", "migrate_start": "sources",
    "migrate_stop": "sources", "rebind_file": "sources",
    "refresh_meta_start": "sources", "refresh_meta_stop": "sources",
    "repack_start": "sources", "repack_stop": "sources", "start_scan": "sources",
    "stop_scan": "sources", "storage_add": "sources", "storage_check": "sources",
    "storage_detach": "sources", "storage_enable": "sources",
    "storage_forget": "sources", "storage_restore": "sources",
    "storage_set_default": "sources", "storage_set_path": "sources",
    "sync_start": "sources", "sync_stop": "sources", "verify_start": "sources",
    "verify_stop": "sources",

    # mesh: сеть участников, доверие, wormhole
    "catalog_forget": "mesh", "catalog_list": "mesh", "catalog_refresh": "mesh",
    "catalog_restore": "mesh", "catalog_restore_friend": "mesh",
    "device_doctor": "mesh", "mesh_fetch_stop": "mesh",
    "mesh_fetch_video": "mesh", "mesh_reject": "mesh",
    "mesh_request_video": "mesh", "nick_probe": "mesh", "nick_search": "mesh",
    "peer_profile": "mesh", "trust_op": "mesh", "trust_sas_begin": "mesh",
    "trust_sas_confirm": "mesh", "wormhole_receive": "mesh",
    "wormhole_send": "mesh", "wormhole_status": "mesh",

    # accounts: аккаунты и OAuth без нативного окна (вход - только на сервере)
    "account_forget": "accounts", "account_import": "accounts",
    "oauth_forget": "accounts", "oauth_status": "accounts",

    # server_config: настройки программы и установка инструментов
    "adult_tab_hide": "server_config", "adult_tab_show": "server_config",
    "ffmpeg_start": "server_config", "ffmpeg_stop": "server_config",
    "rotate_log_file": "server_config", "save_setting": "server_config",
    "wormhole_setup": "server_config",
}

# Нативные цели окна (диалог папки, проводник, системный браузер, окно
# площадки, вход OAuth, закрытие приложения): сервер не может открыть диалог
# на чужой машине, поэтому по HTTP не выполняются вовсе.
NATIVE = frozenset({
    "account_capture_now", "account_login_start", "account_login_stop",
    "account_visible", "browser_add_from_address", "browser_address",
    "browser_close", "browser_open", "browser_open_system_browser", "close",
    "oauth_start", "open_file", "open_folder", "open_log", "open_path",
    "open_url", "pick_folder",
})

# Предпочтения клиента (тема, шрифт, язык): применяются локально и серверу не
# передаются - иначе клиент менял бы вид окна сервера.
LOCAL = frozenset({"theme_set", "font_set", "language_set"})

# Внутренний метод моста окна (нужен для нативных диалогов) - не экспортируется.
INTERNAL = frozenset({"bind_window"})


def scope_for(name: str) -> str | None:
    """Скоуп, который сервер требует для цели (None - цели нет по HTTP)."""
    return SCOPE_OF.get(name)


def exposed() -> frozenset:
    """Цели, доступные по HTTP (все, кроме нативных, локальных и внутренних)."""
    return frozenset(SCOPE_OF)


def is_native(name: str) -> bool:
    return name in NATIVE


def is_local(name: str) -> bool:
    return name in LOCAL


def is_internal(name: str) -> bool:
    return name in INTERNAL


def normalize_scopes(scopes) -> tuple:
    """Проверить список скоупов токена: чужое имя - отказ, а не «тихо отбросить».

    Молча убрать право человек не увидит: токен с мусорным скоупом выглядел бы
    как полный доступ, которого нет.
    """
    out: list[str] = []
    for scope in (scopes or ()):
        if scope not in SCOPES:
            raise ValueError(f"неизвестный скоуп: {scope}")
        if scope not in out:
            out.append(scope)
    return tuple(out)
