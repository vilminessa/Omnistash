"""База библиотеки: подключение, миграции, схема индекса.

Почему SQLite: индекс отвечает на вопросы «есть ли этот ID», «сколько
скачано в плейлисте», «покажи 50 000 строк с фильтром» - всё это требует
индексов и транзакций, чего не умеет журнал вида index.jsonl.

Режим работы:
  * WAL + busy_timeout - окно читает, пока воркер пишет (и наоборот);
  * одно соединение на поток (threading.local) - sqlite3 не любит, когда
    соединением пользуются из разных потоков;
  * схема меняется только миграциями по PRAGMA user_version, каждый
    коммит миграции атомарен вместе с номером версии.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .paths import db_path

# Статусы видео (смысл каждого - см. repo.status_label).
STATUSES = (
    "known",         # в индексе, файла ещё нет
    "queued",        # ждёт в очереди загрузки
    "downloading",   # качается прямо сейчас
    "downloaded",    # файл на диске
    "failed",        # попытка загрузки провалилась
    "missing",       # файл был, но пропал (корень доступен, файла нет)
    "unavailable",   # на площадке удалено/приватно, локальная копия остаётся
    "detached",      # папку с файлом отвязали от библиотеки
)

# Режимы синхронизации источника.
SYNC_MODES = ("full", "partial", "manual")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Первая схема: каналы, видео, файлы, плейлисты, связи, запуски, поиск."""
    conn.executescript(
        """
        -- Авторы. remote_id обязателен: «автор неизвестен» - это отсутствие
        -- строки на видео (channel_id NULL), а не строка-пустышка, иначе
        -- UNIQUE(platform, remote_id) превращается в дыру из-за NULL.
        CREATE TABLE IF NOT EXISTS channels (
            id          INTEGER PRIMARY KEY,
            platform    TEXT NOT NULL DEFAULT 'youtube',
            remote_id   TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            handle      TEXT,
            url         TEXT,
            avatar_path TEXT,
            raw_json    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (platform, remote_id)
        );

        -- Видео: тонкие колонки - под индекс, сортировку и поиск;
        -- raw_json - полный info-dict площадки, источник истины при
        -- расширении схемы (не нужно перекачивать метаданные).
        CREATE TABLE IF NOT EXISTS videos (
            id            INTEGER PRIMARY KEY,
            key           TEXT NOT NULL UNIQUE,   -- "<platform>:<remote_id>"
            platform      TEXT NOT NULL,
            remote_id     TEXT NOT NULL,
            channel_id    INTEGER REFERENCES channels(id) ON DELETE SET NULL,
            title         TEXT,
            description   TEXT,
            uploaded_at   TEXT,                   -- "YYYY-MM-DD" или NULL
            duration_s    INTEGER,
            view_count    INTEGER,
            category      TEXT,
            webpage_url   TEXT,
            thumb_url     TEXT,
            thumb_path    TEXT,
            raw_json      TEXT,
            -- откуда запись: yt-dlp | sidecar | id | title | path | local
            origin        TEXT NOT NULL DEFAULT 'yt-dlp',
            status        TEXT NOT NULL DEFAULT 'known'
                          CHECK (status IN ('known','queued','downloading',
                                            'downloaded','failed','missing',
                                            'unavailable')),
            first_seen_at TEXT NOT NULL,
            downloaded_at TEXT,
            updated_at    TEXT NOT NULL,
            -- ЛОКАЛЬНОЕ: синхронизация метаданных физически не может
            -- перезаписать эти колонки (см. repo.upsert_video).
            user_rating   INTEGER,
            user_tags     TEXT,
            watched_at    TEXT,
            notes         TEXT
        );
        CREATE INDEX IF NOT EXISTS videos_channel_idx ON videos(channel_id);
        CREATE INDEX IF NOT EXISTS videos_status_idx  ON videos(status);
        CREATE INDEX IF NOT EXISTS videos_title_idx   ON videos(title);
        CREATE INDEX IF NOT EXISTS videos_date_idx    ON videos(uploaded_at);

        -- Файлы на диске. Один путь принадлежит ровно одному видео:
        -- это и дедуп, и опора для скана (path -> запись).
        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY,
            video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL
                        CHECK (kind IN ('video','subtitle','thumbnail','sidecar')),
            path        TEXT NOT NULL UNIQUE,
            size        INTEGER,
            hash        TEXT,                     -- "sha256:<hex>"
            mtime       REAL,
            missing     INTEGER NOT NULL DEFAULT 0,
            format_json TEXT
        );
        CREATE INDEX IF NOT EXISTS files_video_idx ON files(video_id);
        CREATE INDEX IF NOT EXISTS files_hash_idx  ON files(hash);

        -- Плейлисты = источники. На YouTube всё - плейлист, поэтому
        -- загрузки канала живут здесь же (kind='uploads', remote_id UU...).
        CREATE TABLE IF NOT EXISTS playlists (
            id             INTEGER PRIMARY KEY,
            platform       TEXT NOT NULL,
            remote_id      TEXT NOT NULL,
            title          TEXT,
            description    TEXT,
            channel_id     INTEGER REFERENCES channels(id) ON DELETE SET NULL,
            kind           TEXT NOT NULL DEFAULT 'remote'
                           CHECK (kind IN ('uploads','remote','local','mix')),
            url            TEXT,
            item_count     INTEGER,
            sync_mode      TEXT NOT NULL DEFAULT 'partial'
                           CHECK (sync_mode IN ('full','partial','manual')),
            raw_json       TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            last_synced_at TEXT,
            UNIQUE (platform, remote_id)
        );
        CREATE INDEX IF NOT EXISTS playlists_channel_idx ON playlists(channel_id);

        -- Связи «видео в плейлисте» с порядком. removed_at не удаляет
        -- строку: пропавшая из плейлиста позиция должна остаться видимой
        -- в истории, а локальный файл - нетронутым.
        CREATE TABLE IF NOT EXISTS playlist_items (
            id             INTEGER PRIMARY KEY,
            playlist_id    INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            video_id       INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            position       INTEGER NOT NULL,
            added_at       TEXT NOT NULL,
            removed_at     TEXT,
            snapshot_title TEXT,
            UNIQUE (playlist_id, video_id)
        );
        CREATE INDEX IF NOT EXISTS playlist_items_pos_idx ON playlist_items(playlist_id, position);

        -- Журнал запусков: что делалось, когда и с каким результатом.
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL,            -- add | sync | scan | download
            source_id   INTEGER,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            stats_json  TEXT
        );

        -- Предложенные изменения до подтверждения (панель diff).
        CREATE TABLE IF NOT EXISTS pending_items (
            id       INTEGER PRIMARY KEY,
            run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            action   TEXT NOT NULL,               -- add | remove | move
            position INTEGER,
            UNIQUE (run_id, video_id, action)
        );
        """
    )
    # Полнотекстовый поиск на внешнем контенте: FTS держит копию значений,
    # а триггеры поддерж её в синхроне - правка videos не требует ручных
    # вставок в индекс. Пустые значения приводим к '': FTS не любит NULL.
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
            title, description, user_tags,
            content='videos', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS videos_fts_ai AFTER INSERT ON videos BEGIN
            INSERT INTO videos_fts(rowid, title, description, user_tags)
            VALUES (new.id, coalesce(new.title,''),
                    coalesce(new.description,''), coalesce(new.user_tags,''));
        END;

        CREATE TRIGGER IF NOT EXISTS videos_fts_ad AFTER DELETE ON videos BEGIN
            INSERT INTO videos_fts(videos_fts, rowid, title, description, user_tags)
            VALUES ('delete', old.id, coalesce(old.title,''),
                    coalesce(old.description,''), coalesce(old.user_tags,''));
        END;

        CREATE TRIGGER IF NOT EXISTS videos_fts_au AFTER UPDATE ON videos BEGIN
            INSERT INTO videos_fts(videos_fts, rowid, title, description, user_tags)
            VALUES ('delete', old.id, coalesce(old.title,''),
                    coalesce(old.description,''), coalesce(old.user_tags,''));
            INSERT INTO videos_fts(rowid, title, description, user_tags)
            VALUES (new.id, coalesce(new.title,''),
                    coalesce(new.description,''), coalesce(new.user_tags,''));
        END;
        """
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    """ALTER TABLE ADD COLUMN, безопасный при повторном прогоне.

    Миграция может оборваться на середине (питание, место на диске):
    повтор должен начаться с того места, а не упасть на «duplicate column».
    """
    name = declaration.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


# Новая схема videos: + 'detached' в CHECK и три локальных поля.
# SQLite не умеет менять CHECK через ALTER - только перестройка таблицы.
_VIDEOS_V2 = """
CREATE TABLE IF NOT EXISTS videos_v2 (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    platform      TEXT NOT NULL,
    remote_id     TEXT NOT NULL,
    channel_id    INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    title         TEXT,
    description   TEXT,
    uploaded_at   TEXT,
    duration_s    INTEGER,
    view_count    INTEGER,
    category      TEXT,
    webpage_url   TEXT,
    thumb_url     TEXT,
    thumb_path    TEXT,
    raw_json      TEXT,
    origin        TEXT NOT NULL DEFAULT 'yt-dlp',
    status        TEXT NOT NULL DEFAULT 'known'
                  CHECK (status IN ('known','queued','downloading',
                                    'downloaded','failed','missing',
                                    'unavailable','detached')),
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT,
    updated_at    TEXT NOT NULL,
    user_rating   INTEGER,
    user_tags     TEXT,
    watched_at    TEXT,
    notes         TEXT,
    -- отвязка: «файл был в <папка>, папку отвязали»
    detached_from TEXT,
    -- куда качать (задаёт панель выделения, а не настройки)
    target_storage_id TEXT REFERENCES storages(id),
    -- почему упал в очереди (видно в списке, а не только в журнале)
    last_error    TEXT
);
"""

# Индексы и триггеры FTS переживают только явное воссоздание: они падают
# вместе с таблицей videos.
_VIDEOS_V2_RESTORE = """
CREATE INDEX IF NOT EXISTS videos_channel_idx ON videos(channel_id);
CREATE INDEX IF NOT EXISTS videos_status_idx  ON videos(status);
CREATE INDEX IF NOT EXISTS videos_title_idx   ON videos(title);
CREATE INDEX IF NOT EXISTS videos_date_idx    ON videos(uploaded_at);

CREATE TRIGGER IF NOT EXISTS videos_fts_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, title, description, user_tags)
    VALUES (new.id, coalesce(new.title,''),
            coalesce(new.description,''), coalesce(new.user_tags,''));
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description, user_tags)
    VALUES ('delete', old.id, coalesce(old.title,''),
            coalesce(old.description,''), coalesce(old.user_tags,''));
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description, user_tags)
    VALUES ('delete', old.id, coalesce(old.title,''),
            coalesce(old.description,''), coalesce(old.user_tags,''));
    INSERT INTO videos_fts(rowid, title, description, user_tags)
    VALUES (new.id, coalesce(new.title,''),
            coalesce(new.description,''), coalesce(new.user_tags,''));
END;
"""

# Колонки v1: их и переносим, новые (detached_from и пр.) берём из _VIDEOS_V2.
_VIDEOS_V1_COLUMNS = (
    "id", "key", "platform", "remote_id", "channel_id", "title", "description",
    "uploaded_at", "duration_s", "view_count", "category", "webpage_url",
    "thumb_url", "thumb_path", "raw_json", "origin", "status", "first_seen_at",
    "downloaded_at", "updated_at", "user_rating", "user_tags", "watched_at",
    "notes",
)


def _rebuild_videos(conn: sqlite3.Connection) -> None:
    """Перестроить videos: расширить CHECK статусов и добавить колонки.

    Внимание на FOREIGN KEYS: DROP TABLE родителя при включённых ключах
    делает неявный DELETE FROM - files и playlist_items ушли бы по CASCADE
    в мусор. Поэтому: сначала выходим из любой неявной транзакции (иначе
    PRAGMA молча не применяется!), потом выключаем ключи на всю операцию.
    """
    # Обрыв на середине: videos уже упала, копия осталась - просто доделать.
    if not _table_exists(conn, "videos") and _table_exists(conn, "videos_v2"):
        conn.execute("ALTER TABLE videos_v2 RENAME TO videos")
        conn.executescript(_VIDEOS_V2_RESTORE)
        return

    # commit() безопасен в autocommit (no-op) и вытаскивает из legacy-режима,
    # где драйвер сам открывает транзакцию перед INSERT/DDL.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS videos_v2")
        conn.executescript(_VIDEOS_V2)
        marks = ", ".join(_VIDEOS_V1_COLUMNS)
        conn.execute(f"INSERT INTO videos_v2 ({marks}) SELECT {marks} FROM videos")
        conn.execute("DROP TABLE videos")
        conn.execute("ALTER TABLE videos_v2 RENAME TO videos")
        conn.executescript(_VIDEOS_V2_RESTORE)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Хранилища, привязка файлов к ним, статус «откреплено».

    Файлы получают storage_id + rel_path (канон пути): переезд корня
    превращается в один UPDATE по storages, а не в переписывание тысяч
    абсолютных путей.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS storages (
            id             TEXT PRIMARY KEY,
            path           TEXT NOT NULL,
            label          TEXT NOT NULL,
            kind           TEXT NOT NULL DEFAULT 'local',
            status         TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','detached')),
            enabled        INTEGER NOT NULL DEFAULT 1,
            read_only      INTEGER NOT NULL DEFAULT 0,
            recursive      INTEGER NOT NULL DEFAULT 1,
            min_free_bytes INTEGER NOT NULL DEFAULT 2147483648,
            root_key       TEXT,
            available      INTEGER NOT NULL DEFAULT 0,
            missing_since  TEXT,
            last_seen_at   TEXT,
            free_bytes     INTEGER,
            total_bytes    INTEGER,
            detached_at    TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        """
    )

    _add_column(conn, "files", "storage_id TEXT REFERENCES storages(id)")
    _add_column(conn, "files", "rel_path TEXT")
    _add_column(conn, "channels", "storage_id TEXT")
    _add_column(conn, "playlists", "storage_id TEXT")

    # CHECK(status) допускает 'detached'? Если нет - перестройка.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()
    if not (row and row["sql"] and "'detached'" in row["sql"]):
        _rebuild_videos(conn)
    else:
        for declaration in ("detached_from TEXT",
                            "target_storage_id TEXT REFERENCES storages(id)",
                            "last_error TEXT"):
            _add_column(conn, "videos", declaration)

    # Путь -> хранилище задаётся в bootstrap (там, где известны настройки),
    # здесь создаём только индекс для будущих сверок.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS files_storage_idx ON files(storage_id)")


# Порядок важен: номер версии = индекс + 1.
MIGRATIONS = (_migrate_v1, _migrate_v2)


class Database:
    """Соединения на поток + миграции на старте."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._registry_lock = threading.Lock()
        self._migrate_lock = threading.Lock()
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        """Соединение текущего потока (создаётся лениво, свои настройки).

        isolation_level=None (autocommit): каждая запись сразу на диске.
        Это принципиально - в неявной транзакции запись воркера висела бы
        незакоммиченной и держала RESERVED-лок на файле, блокируя запись из
        другого потока (симптом: «database is locked» спустя busy_timeout).

        Многооперационная атомарность - через transaction(), а не через
        неявную транзакцию драйвера.

        check_same_thread=False: соединения всё равно используются строго
        в своём потоке (threading.local), но close() в конце работы должен
        уметь закрыть и те, что открыл фоновый воркер.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0,
                                   isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            with self._registry_lock:
                self._all.append(conn)
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """Явная транзакция: COMMIT в конце, ROLLBACK при исключении.

        Нужна фазе C добавления плейлиста: стадии отдаются в UI по ходу,
        но падение или «Отмена» на любой из них обязаны откатить всё.
        """
        conn = self.conn
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # закрытый/битый коннект и так уже мёртв
                pass
            raise
        else:
            conn.execute("COMMIT")

    def migrate(self) -> None:
        """Применить недостающие миграции.

        Атомарность между схемой и user_version не гарантируется
        (executescript коммитит сам), поэтому все объекты в миграциях
        создаются с IF NOT EXISTS: обрыв на середине лечится повторным
        прогоном той же миграции, а не откатом.
        """
        with self._migrate_lock:
            conn = self.conn
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for version, step in enumerate(MIGRATIONS, start=1):
                if version > current:
                    step(conn)
                    conn.execute(f"PRAGMA user_version = {version}")

    def close(self) -> None:
        """Закрыть все соединения, включая те, что открыл фоновый воркер.

        Иначе файл базы остаётся занятым (на Windows это молча ломает
        удаление профиля и «зависшие» бэкапы).
        """
        with self._registry_lock:
            conns, self._all = list(self._all), []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - закрытие не должно падать
                pass
        self._local.conn = None
