"""Канал link - быстрый обмен журналом и состоянием двух ПК для тестов.

Зачем: живым тестам нужны данные с двух машин, а агенты (и люди) работают
каждый в своей консоли на своём ПК. Ручной обход файлами теряет порядок
и живость, поэтому два экземпляра link держат постоянный канал:

    ПК А:  python tools/link.py serve --id pcA --token T --peer <ip_B>
    ПК Б:  python tools/link.py serve --id pcB --token T --peer <ip_A>

    python tools/link.py log "очередь пуста"        # строка в журнал
    python tools/link.py state set queue '[]'       # своё состояние
    python tools/link.py state get                  # состояние обоих ПК
    python tools/link.py tail --follow              # живой поток журнала
    python tools/link.py status                     # пир, seq, задержки

Почему своё, а не поверх app/mesh (продуктовый канал уже есть):
  * mesh односторонний «запрос->ответ» под передачу файлов с TLS и
    манифестами; под непрерывный двусторонний поток событий пришлось бы
    строить поверх него свой фрейминг, а тесты не должны зависеть от
    состояния продуктового стека (DHT, профиль, DPAPI);
  * link - инструмент: только stdlib (запускается там, где вообще нет
    установленных зависимостей), без профиля и настроек; протокол -
    читаемый глазами NDJSON, журнал - обычный JSONL.

Как устроено:
  * TCP на фикс. порту, NDJSON: одна строка - одно сообщение;
  * симметрия: оба ПК слушают и набирают друг друга; из пары соединений
    остаётся то, которое набрал узел с меньшим id (обе стороны решают
    одинаково - соединение одно, детерминированно);
  * авторизация - challenge-response по общему токену: сам токен в
    канал не уходит, наблюдатель видит только sha256(tok|nonce);
  * журнал `journal.jsonl` - источник правды: свои записи идут с
    монотонным seq (счётчик восстанавливается из файла и переживает
    перезапуск), чужие лежат тем же файлом, поэтому `tail` читается и
    без демона;
  * дыры и перезапуски закрываются протоколом req/replay: получатель
    говорит «у меня до N», отправитель отдаёт недостающее пачками
    (REPLAY_BATCH за раз, следующая пачка - по новому req); seq ниже
    ожидаемого = дубль, выше = пропуск (запрос на догон);
  * состояние - карта ключей (rev у себя, ts на приёме, LWW): полный
    снимок уходит при каждом соединении и заменяет карту пира (снимок
    авторитетен - так переживается потеря state.json на пире), дальше -
    дельтами с ростом rev;
  * CLI ходит в демон по локальному порту управления (127.0.0.1,
    порт mesh+1000) - наружу управление не торчит.

Адрес пары: --peer ip[:port] (Tailscale 100.x, LAN 192.168.x - что есть);
опциональный --announce шлёт UDP-приветствие по широковещанию, когда IP
неизвестны или меняются по DHCP (LAN; широковещание через Tailscale не
идёт). Windows при первом запуске спросит разрешение для порта - в
сообщении об отказе готовая команда правила брандмауэра.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROTOCOL = 1
DEFAULT_PORT = 18571            # mesh TCP
CTRL_PORT_OFFSET = 1000         # локальный порт управления = mesh + 1000
ANNOUNCE_PORT = 18572           # UDP-приветствие (LAN)
ANNOUNCE_INTERVAL = 5.0
DIAL_INTERVAL = 3.0             # пауза между наборами адреса пары
HANDSHAKE_TIMEOUT = 10.0
PING_INTERVAL = 15.0
DEAD_TIMEOUT = 45.0
ACK_INTERVAL = 2.0
REQ_COOLDOWN = 5.0              # защита от цикла «дыра -> req -> пусто»
REPLAY_BATCH = 1000             # записей в одном replay (пачки по req)
FOLLOW_HISTORY = 30             # хвост для слика с файлом клиента (--follow)
MAX_OUT_QUEUE = 50_000          # медленный пир: сбрасываем соединение
SUB_QUEUE = 2000                # буфер подписчика (tail --follow)
CTRL_TIMEOUT = 10.0
DEFAULT_DIR = "link_data"
ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class LinkError(Exception):
    """Рукопожатие/конфигурация не прошли: токен, свой id, битый формат."""


class ControlError(Exception):
    """Нет связи с локальным демоном или демон ответил отказом."""


def _proof(token: str, nonce: str) -> str:
    """Ответ на вызов: sha256(токен|nonce) - токена в канале нет."""
    return hashlib.sha256(f"{token}|{nonce}".encode("utf-8")).hexdigest()


def _split_addr(addr: str, default_port: int) -> tuple[str, int]:
    """«ip», «ip:порт» -> (хост, порт); ValueError на явный мусор.

    IPv6 без скобок считаем хостом целиком (порт для него задаётся
    отдельным параметром) - в тестовом инструменте это не нужно.
    """
    addr = (addr or "").strip()
    if not addr:
        raise ValueError("пустой адрес пары")
    if addr.count(":") == 1:
        host, _, port = addr.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"битый адрес пары: {addr}")
        return host, int(port)
    return addr, default_port


def _lan_ip() -> str:
    """Локальный адрес для подсказки «ваш адрес для пары».

    UDP-connect не шлёт пакетов - просто выбирается маршрут, и по нему
    видно адрес исходящего интерфейса (Tailscale/LAN/Ethernet - что выше
    в маршрутизации).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))     # TEST-NET: пакеты не уходят
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# ------------------------------------------------------------------ #
#  Журнал: JSONL, источник правды
# ------------------------------------------------------------------ #

class Journal:
    """JSONL-журнал событий: переживает перезапуск и работает без демона.

    Один файл на обе стороны: свои записи (origin == свой id) хранятся
    для догонов чужой стороны и восстановления seq, чужие - чтобы
    `tail` читался офлайн. Писатель открывает файл на допись и закрывает
    (дескриптор не держим - читатель на Windows не упирается в шаринг).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self, origin: str | None = None, from_seq: int | None = None,
             first: int | None = None) -> list[dict]:
        """Записи по порядку; first - обрезать после фильтров (для пачек)."""
        with self._lock:
            out: list[dict] = []
            try:
                handle = open(self.path, "r", encoding="utf-8")
            except OSError:
                return []
            with handle as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue            # битая строка не должна валить чтение
                    if not isinstance(entry, dict):
                        continue
                    if origin is not None and entry.get("o") != origin:
                        continue
                    if from_seq is not None:
                        seq = entry.get("seq")
                        if not isinstance(seq, int) or seq < from_seq:
                            continue
                    out.append(entry)
            if first is not None:
                out = out[:first]
            return out

    def tail(self, count: int) -> list[dict]:
        """Последние count записей (0 и меньше - всё)."""
        entries = self.read()
        return entries if count <= 0 else entries[-count:]

    def append(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)


# ------------------------------------------------------------------ #
#  Соединение
# ------------------------------------------------------------------ #

class Conn:
    """Соединение с пиром: очередь исходящих + состояние здоровья."""

    __slots__ = ("sock", "peer_id", "addr", "dialed_by_me", "alive", "out",
                 "out_lock", "out_ready", "last_rx", "linked_at", "rtt_ms",
                 "peer_ack", "last_ping", "last_ack_sent", "last_ack_time",
                 "link_keys")

    def __init__(self, sock: socket.socket, peer_id: str, addr: str,
                 dialed_by_me: bool):
        self.sock = sock
        self.peer_id = peer_id
        self.addr = addr                # «ip:порт» - как пира находят
        self.dialed_by_me = dialed_by_me
        self.alive = True
        self.out: list[str] = []
        self.out_lock = threading.Lock()
        self.out_ready = threading.Event()
        self.last_rx = time.time()
        self.linked_at = time.time()
        self.rtt_ms: float | None = None
        self.peer_ack = -1              # до какого МОЕГО seq дошёл пир
        self.last_ping = 0.0
        self.last_ack_sent = -1
        self.last_ack_time = 0.0
        self.link_keys: set[str] = set()

    def enqueue(self, obj: dict) -> bool:
        """В очередь на отправку; False - соединение закрыто или переполнено."""
        if not self.alive:
            return False
        with self.out_lock:
            if not self.alive:
                return False
            if len(self.out) >= MAX_OUT_QUEUE:
                self.alive = False         # медленный пир: обслуживание сбросит
                return False
            self.out.append(json.dumps(obj, ensure_ascii=False) + "\n")
        self.out_ready.set()
        return True

    def close(self) -> None:
        self.alive = False
        with self.out_lock:
            self.out.clear()
        self.out_ready.set()
        try:                               # shutdown будит заблокированный
            self.sock.shutdown(socket.SHUT_RDWR)   # readline читателя
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def out_pump(self, stop: threading.Event) -> None:
        """Писатель: выгребает очередь, будится событием; OSError = разрыв.

        Очередь под замком, поэтому два производителя (живой log и
        replay) не перемешивают байты одного сообщения; порядок внутри
        соединения = порядок постановки.
        """
        while not stop.is_set() and self.alive:
            self.out_ready.wait(0.5)
            self.out_ready.clear()
            with self.out_lock:
                batch, self.out = self.out, []
            for line in batch:
                try:
                    self.sock.sendall(line.encode("utf-8"))
                except OSError:
                    self.close()
                    return


# ------------------------------------------------------------------ #
#  Демон
# ------------------------------------------------------------------ #

class Link:
    """Демон канала: слушает, набирает пару, ведёт журнал и состояние."""

    def __init__(self, data_dir: str | Path = DEFAULT_DIR,
                 peer_id: str | None = None, token: str = "",
                 mesh_port: int = DEFAULT_PORT, ctrl_port: int | None = None,
                 peers: Sequence[str] = (), announce: bool = False,
                 dial_interval: float = DIAL_INTERVAL,
                 handshake_timeout: float = HANDSHAKE_TIMEOUT,
                 ping_interval: float = PING_INTERVAL,
                 dead_timeout: float = DEAD_TIMEOUT):
        self.dir = Path(data_dir)
        self.token = token or ""
        self.mesh_port = int(mesh_port)
        self.ctrl_port = (DEFAULT_PORT + CTRL_PORT_OFFSET
                          if ctrl_port is None else int(ctrl_port))
        self.announce = bool(announce)
        self.dial_interval = float(dial_interval)
        self.handshake_timeout = float(handshake_timeout)
        self.ping_interval = float(ping_interval)
        self.dead_timeout = float(dead_timeout)

        self._id_arg = peer_id            # --id: иначе meta.json, иначе хост
        self.peer_id = ""                # заполняется в start()
        self.journal = Journal(self.dir / "journal.jsonl")
        # RLock: запись состояния внутри критической секции может
        # заметить ошибку и положить строку в кольцо заметок
        self.lock = threading.RLock()
        self.next_seq = 1                # свой счётчик (восстанавливается)
        self.peer_next: dict[str, int] = {}   # origin -> ожидаемый seq
        self.state: dict[str, dict[str, dict]] = {}   # origin -> key -> запись
        self.conns: dict[str, Conn] = {}
        self.peers: dict[str, dict] = {}  # «ip:порт» -> {via}
        self.linked_addrs: set[str] = set()
        self.dialing: set[str] = set()
        self.dial_fail: dict[str, float] = {}
        self.subs: list[queue.Queue] = []
        self.req_cooldown: dict[str, float] = {}
        self.notes: deque[tuple[float, str]] = deque(maxlen=50)
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

        self._mesh_sock: socket.socket | None = None
        self._ctrl_sock: socket.socket | None = None

        for addr in peers:
            self.add_peer(addr)

    # ---------------- жизненный цикл ---------------- #

    def start(self) -> None:
        """Каталог, счётчики из журнала, сокеты, потоки. LinkError - занят порт."""
        if not self.peer_id:
            self._resolve_id()
        entries = self.journal.read()
        own_max = 0
        peer_max: dict[str, int] = {}
        for entry in entries:
            origin, seq = entry.get("o"), entry.get("seq")
            if not isinstance(seq, int):
                continue
            if origin == self.peer_id:
                own_max = max(own_max, seq)
            elif isinstance(origin, str):
                peer_max[origin] = max(peer_max.get(origin, 0), seq)
        with self.lock:
            self.next_seq = own_max + 1
            for origin, seq in peer_max.items():
                self.peer_next[origin] = seq + 1
        self.state = _read_state(self.dir)

        self._mesh_sock = self._listen("0.0.0.0", self.mesh_port, 16)
        self.mesh_port = int(self._mesh_sock.getsockname()[1])
        self._ctrl_sock = self._listen("127.0.0.1", self.ctrl_port, 8)
        self.ctrl_port = int(self._ctrl_sock.getsockname()[1])

        self._spawn(self._mesh_accept_loop, "mesh-accept")
        self._spawn(self._ctrl_accept_loop, "ctrl-accept")
        self._spawn(self._dial_loop, "dial")
        self._spawn(self._maintenance_loop, "maintenance")
        if self.announce:
            self._spawn(self._announce_loop, "announce")

    @staticmethod
    def _listen(host: str, port: int, backlog: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(backlog)
            sock.settimeout(1.0)          # accept просыпается раз в секунду
        except OSError as exc:
            sock.close()
            raise LinkError(
                f"не удалось слушать {host}:{port} - {exc} "
                "(порт занят другим экземпляром link?)") from exc
        return sock

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=f"link-{name}",
                                  daemon=True)
        self.threads.append(thread)
        thread.start()

    def stop_link(self) -> None:
        """Остановить всё; повторный вызов безопасен."""
        already = self.stop.is_set()
        self.stop.set()
        for sock in (self._mesh_sock, self._ctrl_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        with self.lock:
            conns = list(self.conns.values())
            self.conns.clear()
            subs = list(self.subs)
            self.subs.clear()
        for conn in conns:
            conn.close()
        for sub in subs:                   # будим хвосты tail --follow
            try:
                sub.put_nowait(None)
            except queue.Full:
                pass
        if not already:
            for thread in self.threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=2.0)

    # ---------------- идентичность и каталог ---------------- #

    def _resolve_id(self) -> None:
        """--id > сохранённый в meta.json > хостнейм; каталог под себя."""
        meta_path = self.dir / "meta.json"
        stored = None
        try:
            stored = json.loads(meta_path.read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError):
            stored = None
        if stored and self._id_arg and stored != self._id_arg:
            raise LinkError(
                f"в каталоге {self.dir} уже живёт id «{stored}», а вы "
                f"просите «{self._id_arg}» - укажите другой --dir")
        peer_id = stored or self._id_arg or socket.gethostname()
        peer_id = ID_RE.sub("-", str(peer_id)).strip("-.")[:64] or "pc"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.peer_id = peer_id
        try:
            meta_path.write_text(
                json.dumps({"id": peer_id}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass                            # каталог только для чтения

    # ---------------- адреса пары ---------------- #

    def add_peer(self, addr: str, port: int | None = None) -> str:
        """Запомнить адрес пары; возвращает ключ «ip:порт»."""
        if port is None:
            host, number = _split_addr(addr, DEFAULT_PORT)
        else:
            host, number = addr.strip(), int(port)
        key = f"{host}:{number}"
        with self.lock:
            self.peers.setdefault(key, {"via": "cli"})
        return key

    # ---------------- рукопожатие ---------------- #

    def _handshake(self, sock: socket.socket, dialed_by_me: bool) -> dict:
        """Три шага: id/порт/nonce/счётчик + proof токена в обе стороны.

        Возвращает приветствие пира {"me", "port", "next"}; LinkError -
        любой отказ (токен, чужой формат, свой id с другой стороны).
        """
        sock.settimeout(self.handshake_timeout)
        nonce = os.urandom(16).hex()
        hello: dict[str, Any] = {
            "t": "hello", "v": PROTOCOL, "me": self.peer_id,
            "port": self.mesh_port, "nonce": nonce, "next": self.next_seq,
        }

        def send(obj: dict) -> None:
            sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n")
                         .encode("utf-8"))

        def recv() -> dict:
            data = sock.makefile("rb").readline()
            if not data:
                raise LinkError("пир закрыл соединение на рукопожатии")
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError as exc:
                raise LinkError("пир прислал не JSON") from exc
            if not isinstance(msg, dict):
                raise LinkError("пир прислал не объект")
            return msg

        if dialed_by_me:
            send(hello)
            other = recv()
            if other.get("t") != "hello":
                raise LinkError(f"ожидалось hello, пришло {other.get('t')!r}")
            if not hmac.compare_digest(str(other.get("proof") or ""),
                                       _proof(self.token, nonce)):
                raise LinkError("токен не совпал: пир не подтвердил, что "
                                "знает наш токен")
            send({"t": "auth",
                  "proof": _proof(self.token, str(other.get("nonce") or ""))})
            verdict = recv()
            if verdict.get("t") != "ok":
                raise LinkError(str(verdict.get("why") or "пир отказал"))
        else:
            other = recv()
            if other.get("t") != "hello":
                raise LinkError(f"ожидалось hello, пришло {other.get('t')!r}")
            send({**hello, "proof": _proof(self.token,
                                           str(other.get("nonce") or ""))})
            auth = recv()
            if auth.get("t") != "auth" or not hmac.compare_digest(
                    str(auth.get("proof") or ""), _proof(self.token, nonce)):
                raise LinkError("токен не совпал: наборщик не знал наш токен")
            send({"t": "ok"})

        if other.get("v") != PROTOCOL:
            raise LinkError(f"протокол {other.get('v')!r} != {PROTOCOL}")
        me = str(other.get("me") or "")
        if not me:
            raise LinkError("в hello нет id пира")
        if me == self.peer_id:
            raise LinkError(
                f"пир объявил наш же id «{me}» - проверьте --id и --dir "
                "(два узла с одним именем не синхронизируются)")
        sock.settimeout(None)
        return {"me": me, "port": other.get("port"), "next": other.get("next")}

    def _link_ready(self, sock: socket.socket, hello: dict,
                    dialed_by_me: bool, addr: str) -> Conn | None:
        """Поставить соединение в работу; None - проиграли дедупликации.

        Оба ПК набирают друг друга - из пары соединений остаётся то,
        которое набрал узел с меньшим id: обе стороны сравнивают одни и
        те же два id и закрывают одно и то же (лишнего нет ни у кого).
        """
        peer = hello["me"]
        source_ip = sock.getpeername()[0]
        listen_addr = f"{source_ip}:{hello.get('port')}"
        conn = Conn(sock, peer, addr, dialed_by_me)
        conn.link_keys = {addr, listen_addr}

        drop_new = False
        replaced = False
        with self.lock:
            if self.stop.is_set():
                conn.close()                # останавливаемся - не берём новых
                return None
            old = self.conns.get(peer)
            if old is not None and old.alive:
                replaced = True             # пара набралась с двух сторон
                mine = self.peer_id if dialed_by_me else peer
                theirs = self.peer_id if old.dialed_by_me else peer
                if mine > theirs:
                    drop_new = True
                else:
                    self.conns[peer] = conn
                    old.close()
            else:
                self.conns[peer] = conn
            if not drop_new:
                advertised = hello.get("next")
                current = self.peer_next.get(peer, 1)
                # счётчик пира меньше нашего - его журнал стёрся, он
                # начинает заново: выравниваемся по нему, иначе дыра
                # никогда не закроется
                if isinstance(advertised, int) and advertised < current:
                    self.peer_next[peer] = advertised
                self.linked_addrs |= conn.link_keys
                self.req_cooldown.pop(peer, None)
                snapshot = {k: dict(v) for k, v in
                            self.state.get(self.peer_id, {}).items()}
                want = self.peer_next.get(peer, 1)
        if drop_new:
            conn.close()
            return None

        if not replaced:                    # замену при дедупликации не
            self._note(                     # печатаем: пир уже был подключён
                f"пир {peer} подключён ({conn.addr})", print_it=True)
        conn.enqueue({"t": "state", "origin": self.peer_id,
                      "entries": snapshot, "snap": True})
        conn.enqueue({"t": "req", "from": want})
        return conn

    def _drop_conn(self, conn: Conn) -> None:
        dropped = False
        with self.lock:
            if self.conns.get(conn.peer_id) is conn:
                del self.conns[conn.peer_id]
                dropped = True
            self.linked_addrs -= conn.link_keys
        if dropped:
            self._note(f"пир {conn.peer_id} отключился", print_it=True)

    # ---------------- петли приёма/набора ---------------- #

    def _mesh_accept_loop(self) -> None:
        assert self._mesh_sock is not None
        while not self.stop.is_set():
            try:
                sock, src = self._mesh_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(lambda s=sock, a=src: self._serve_incoming(s, a),
                        "incoming")

    def _serve_incoming(self, sock: socket.socket, src) -> None:
        try:
            hello = self._handshake(sock, dialed_by_me=False)
            sock.settimeout(None)
            conn = self._link_ready(sock, hello, dialed_by_me=False,
                                    addr=f"{src[0]}:{hello.get('port')}")
            if conn is not None:
                self._run_conn(conn)
        except (OSError, LinkError) as exc:
            self._note(f"входящее соединение с {src[0]}: {exc}",
                       print_it=True)
            try:
                sock.close()
            except OSError:
                pass

    def _dial_loop(self) -> None:
        # первый набор - сразу при старте (пара могла быть запущена
        # раньше нас), дальше - с паузой и отметками неудач
        while not self.stop.is_set():
            now = time.time()
            with self.lock:
                targets = [addr for addr in self.peers
                           if addr not in self.linked_addrs
                           and addr not in self.dialing
                           and now - self.dial_fail.get(addr, 0.0)
                           >= self.dial_interval]
                for addr in targets:
                    self.dialing.add(addr)
            for addr in targets:
                self._spawn(lambda a=addr: self._dial(a), "dialer")
            if self.stop.wait(self.dial_interval):
                break

    def _dial(self, addr: str) -> None:
        sock = None
        try:
            host, port = _split_addr(addr, DEFAULT_PORT)
            sock = socket.create_connection((host, port),
                                            timeout=self.handshake_timeout)
            hello = self._handshake(sock, dialed_by_me=True)
            sock.settimeout(None)
            conn = self._link_ready(sock, hello, dialed_by_me=True, addr=addr)
            if conn is not None:
                self._run_conn(conn)
        except (OSError, LinkError) as exc:
            with self.lock:
                self.dial_fail[addr] = time.time()
            self._note(f"набор {addr}: {exc}")
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        finally:
            with self.lock:
                self.dialing.discard(addr)

    def _run_conn(self, conn: Conn) -> None:
        """Писатель + читатель соединения; выход = закрытие и снятие учёта."""
        threading.Thread(target=conn.out_pump, args=(self.stop,),
                         daemon=True).start()
        reader = None
        try:
            reader = conn.sock.makefile("rb")
            while not self.stop.is_set() and conn.alive:
                line = reader.readline()
                if not line:
                    break                       # пир закрыл
                conn.last_rx = time.time()
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue                    # мусор не должен ронять пир
                if isinstance(msg, dict):
                    self._on_message(conn, msg)
        except OSError:
            pass
        finally:
            if reader is not None:
                try:
                    reader.close()
                except OSError:
                    pass
            conn.close()
            self._drop_conn(conn)

    def _maintenance_loop(self) -> None:
        """Раз в секунду: пинги, мёртвые пиры, подтверждения (ack)."""
        while not self.stop.wait(1.0):
            now = time.time()
            with self.lock:
                conns = list(self.conns.values())
            for conn in conns:
                if not conn.alive or now - conn.last_rx > self.dead_timeout:
                    if conn.alive:
                        self._note(f"пир {conn.peer_id} замолчал "
                                   f"{round(now - conn.last_rx)} с - разрываю",
                                   print_it=True)
                    conn.close()
                    self._drop_conn(conn)
                    continue
                if now - conn.last_ping >= self.ping_interval:
                    conn.last_ping = now
                    conn.enqueue({"t": "ping", "t0": now})
                # подтверждение: «я получил твои записи до N» - пир по нему
                # считает свою задержку в status
                ack = self.peer_next.get(conn.peer_id, 1) - 1
                if (ack != conn.last_ack_sent
                        and now - conn.last_ack_time >= ACK_INTERVAL):
                    if conn.enqueue({"t": "ack", "seq": ack}):
                        conn.last_ack_sent = ack
                        conn.last_ack_time = now

    def _on_message(self, conn: Conn, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "log":
            self._apply_entry(conn, msg)
        elif kind == "replay":
            self._apply_replay(conn, msg)
        elif kind == "req":
            self._serve_req(conn, msg)
        elif kind == "ack":
            seq = msg.get("seq")
            if isinstance(seq, int):
                conn.peer_ack = seq
        elif kind == "state":
            self._apply_state(conn, msg)
        elif kind == "ping":
            conn.enqueue({"t": "pong", "t0": msg.get("t0")})
        elif kind == "pong":
            t0 = msg.get("t0")
            if isinstance(t0, (int, float)):
                conn.rtt_ms = (time.time() - float(t0)) * 1000.0
        elif kind == "bye":
            self._note(f"пир {conn.peer_id} отказал: "
                       f"{msg.get('why') or 'без причины'}", print_it=True)

    # ---------------- журнал: приём, догон, отправка ---------------- #

    def add_log(self, text: str, level: str = "info") -> dict:
        """Своя запись: seq++, допись в журнал, раздача подключённым."""
        with self.lock:
            entry = {"o": self.peer_id, "seq": self.next_seq,
                     "ts": time.time(), "lvl": str(level or "info"),
                     "txt": str(text)}
            self.next_seq += 1
            self.journal.append(entry)
            conns = list(self.conns.values())
        for conn in conns:
            conn.enqueue({"t": "log", **entry})
        self._notify(entry)
        return entry

    def _apply_entry(self, conn: Conn, msg: dict) -> None:
        """Чужая запись: seq строго по порядку, иначе дубль или догон.

        Origin берётся из соединения, а не из сообщения: подпись записи
        не нужна, канал уже прошёл рукопожатие с токеном.
        """
        origin = conn.peer_id
        seq = msg.get("seq")
        if not isinstance(seq, int):
            return
        with self.lock:
            expect = self.peer_next.get(origin, 1)
            if seq < expect:
                return                              # дубль
            if seq > expect:
                self._request_gap_locked(origin, expect)
                return                              # пропуск - на догон
            entry = {"o": origin, "seq": seq,
                     "ts": msg.get("ts") if isinstance(msg.get("ts"),
                                                       (int, float))
                     else time.time(),
                     "lvl": str(msg.get("lvl") or "info"),
                     "txt": str(msg.get("txt") or "")}
            try:
                self.journal.append(entry)
            except OSError as exc:
                self._note(f"журнал не дописался: {exc}", print_it=True)
                return                              # не двигаем seq: догоним
            self.peer_next[origin] = expect + 1
        self._notify(entry)

    def _request_gap_locked(self, origin: str, expect: int) -> None:
        """Попросить недостающее; cooldown гасит цикл «дыра -> req -> пусто»."""
        now = time.time()
        if now < self.req_cooldown.get(origin, 0.0):
            return
        conn = self.conns.get(origin)
        if conn is None:
            return
        self.req_cooldown[origin] = now + REQ_COOLDOWN
        conn.enqueue({"t": "req", "from": expect})

    def _serve_req(self, conn: Conn, msg: dict) -> None:
        """Отдать СВОИ записи с seq >= from одной пачкой."""
        start = msg.get("from")
        if not isinstance(start, int) or start < 1:
            return
        batch = self.journal.read(origin=self.peer_id, from_seq=start,
                                  first=REPLAY_BATCH)
        conn.enqueue({"t": "replay", "entries": batch})

    def _apply_replay(self, conn: Conn, msg: dict) -> None:
        entries = msg.get("entries")
        if not isinstance(entries, list):
            return
        origin = conn.peer_id
        for item in entries:
            if isinstance(item, dict):
                self._apply_entry(conn, item)
        if not entries:
            # пир не смог отдать (журнал короче запроса) - пауза, чтобы
            # не крутить бесконечный цикл req/replay
            with self.lock:
                self.req_cooldown[origin] = time.time() + REQ_COOLDOWN
            return
        if len(entries) >= REPLAY_BATCH:
            # пачка полная - есть что догонять: следующая порция по новому
            # req (прямой, без cooldown: это продолжение, а не дыра); если
            # на самом деле больше нет, пир ответит пусто и cooldown
            # остановит цикл
            with self.lock:
                expect = self.peer_next.get(origin, 1)
            conn.enqueue({"t": "req", "from": expect})

    # ---------------- состояние ---------------- #

    def add_state(self, key: str, value: Any) -> dict:
        """Своя запись состояния: rev++, снимок на диск, дельта парам."""
        if not isinstance(key, str) or not key:
            raise ValueError("ключ состояния - непустая строка")
        with self.lock:
            mine = self.state.setdefault(self.peer_id, {})
            prev = mine.get(key)
            rev = (prev.get("rev", 0) if isinstance(prev, dict) else 0) + 1
            entry = {"rev": rev, "ts": time.time(), "value": value}
            mine[key] = entry
            self._save_state_locked()
            payload = {key: dict(entry)}
            conns = list(self.conns.values())
        for conn in conns:
            conn.enqueue({"t": "state", "origin": self.peer_id,
                          "entries": payload})
        return entry

    def _apply_state(self, conn: Conn, msg: dict) -> None:
        origin = conn.peer_id
        entries = msg.get("entries")
        if not isinstance(entries, dict):
            return
        with self.lock:
            current = self.state.setdefault(origin, {})
            if msg.get("snap") is True:
                # полный снимок авторитетен: так переживается потеря
                # state.json на пире (rev начинается заново)
                current = {k: dict(v) for k, v in entries.items()
                           if isinstance(k, str) and isinstance(v, dict)}
                self.state[origin] = current
                dirty = True
            else:
                dirty = False
                for key, entry in entries.items():
                    if not isinstance(key, str) or not isinstance(entry, dict):
                        continue
                    rev, ts = entry.get("rev"), entry.get("ts")
                    if not isinstance(rev, int):
                        continue
                    old = current.get(key)
                    if isinstance(old, dict):
                        old_rev = old.get("rev")
                        if isinstance(old_rev, int) and (
                                rev < old_rev
                                or (rev == old_rev
                                    and isinstance(ts, (int, float))
                                    and isinstance(old.get("ts"), (int, float))
                                    and ts <= old["ts"])):
                            continue                # не новее - пропускаем
                    current[key] = {"rev": rev,
                                    "ts": ts if isinstance(ts, (int, float))
                                    else time.time(),
                                    "value": entry.get("value")}
                    dirty = True
            if dirty:
                self._save_state_locked()

    def _save_state_locked(self) -> None:
        """Атомарная запись state.json (вызывается под self.lock)."""
        path = self.dir / "state.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self._note(f"состояние не сохранилось: {exc}", print_it=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def state_merged(self) -> dict:
        """Сведённое состояние: по последнему ts на ключ (LWW)."""
        return merge_state(self.state)

    # ---------------- подписчики (tail --follow) ---------------- #

    def _notify(self, entry: dict) -> None:
        with self.lock:
            subs = list(self.subs)
        for sub in subs:
            try:
                sub.put_nowait(entry)
            except queue.Full:
                try:
                    sub.get_nowait()               # медленный хвост: старое
                except queue.Empty:
                    pass
                try:
                    sub.put_nowait(entry)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=SUB_QUEUE)
        with self.lock:
            self.subs.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self.lock:
            if sub in self.subs:
                self.subs.remove(sub)

    # ---------------- служебное: заметки и статус ---------------- #

    def _note(self, text: str, print_it: bool = False) -> None:
        """Служебная строка: в кольцо заметок (status), иногда в консоль.

        Повторяющиеся ноты (набор каждые 3 секунды) не плодятся - одна
        и та же строка обновляет время, а не занимает кольцо.
        """
        now = time.time()
        with self.lock:
            if self.notes and self.notes[-1][1] == text and not print_it:
                self.notes[-1] = (now, text)
            else:
                self.notes.append((now, text))
        if print_it:
            print(f"link: {text}", flush=True)

    def status(self) -> dict:
        now = time.time()
        with self.lock:
            peers = []
            for conn in self.conns.values():
                with conn.out_lock:
                    out_queue = len(conn.out)
                ack = conn.peer_ack
                peers.append({
                    "id": conn.peer_id,
                    "addr": conn.addr,
                    "dialed_by": "me" if conn.dialed_by_me else "peer",
                    "linked_s": round(now - conn.linked_at, 1),
                    "rtt_ms": (round(conn.rtt_ms, 1)
                               if conn.rtt_ms is not None else None),
                    "they_have_ours_up_to": ack,
                    "send_lag": max(0, self.next_seq - 1 - max(ack, -1)),
                    "we_have_theirs_up_to":
                        self.peer_next.get(conn.peer_id, 1) - 1,
                    "out_queue": out_queue,
                })
            return {
                "ok": True,
                "id": self.peer_id,
                "mesh_port": self.mesh_port,
                "ctrl_port": self.ctrl_port,
                "dir": str(self.dir),
                "announce": self.announce,
                "next_seq": self.next_seq,
                "peers_configured": sorted(self.peers),
                "peers": peers,
                "notes": [{"age_s": round(now - ts, 1), "text": text}
                          for ts, text in list(self.notes)[-10:]],
            }

    # ---------------- локальный порт управления ---------------- #

    def _ctrl_accept_loop(self) -> None:
        assert self._ctrl_sock is not None
        while not self.stop.is_set():
            try:
                sock, _src = self._ctrl_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(lambda s=sock: self._serve_ctrl(s), "ctrl")

    def _serve_ctrl(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(CTRL_TIMEOUT)
            reader = sock.makefile("rb")
            line = reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                self._ctrl_send(sock, {"ok": False, "error": "не JSON"})
                return
            if not isinstance(msg, dict):
                self._ctrl_send(sock, {"ok": False, "error": "не объект"})
                return
            if msg.get("cmd") == "tail" and msg.get("follow") is True:
                self._ctrl_follow(sock, reader, msg)
                return
            for answer in self._ctrl_answers(msg):
                self._ctrl_send(sock, answer)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _ctrl_send(sock: socket.socket, obj: dict) -> None:
        sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n")
                     .encode("utf-8"))

    def _ctrl_answers(self, msg: dict) -> list[dict]:
        cmd = msg.get("cmd")
        try:
            if cmd == "log":
                text = str(msg.get("text") or "")
                entry = self.add_log(text, str(msg.get("level") or "info"))
                return [{"ok": True, "seq": entry["seq"], "ts": entry["ts"]}]
            if cmd == "state_set":
                key = str(msg.get("key") or "")
                entry = self.add_state(key, msg.get("value"))
                return [{"ok": True, "key": key, "rev": entry["rev"]}]
            if cmd == "state_get":
                key = msg.get("key")
                verbose = msg.get("verbose") is True
                if verbose:
                    return [{"ok": True, "origins": self.state,
                             "state": self.state_merged()}]
                if isinstance(key, str) and key:
                    return [{"ok": True,
                             "value": self.state_merged().get(key)}]
                return [{"ok": True, "state": self.state_merged()}]
            if cmd == "status":
                return [self.status()]
            if cmd == "tail":
                last = msg.get("last")
                entries = self.journal.tail(int(last)
                                            if isinstance(last, int) else 20)
                return [{"e": e} for e in entries] + \
                    [{"ok": True, "done": True}]
            if cmd == "ping":
                return [{"ok": True}]
            return [{"ok": False, "error": f"неизвестная команда {cmd!r}"}]
        except (ValueError, TypeError) as exc:
            return [{"ok": False, "error": str(exc)}]
        except Exception as exc:  # noqa: BLE001 - демон не должен умирать
            return [{"ok": False, "error": f"{type(exc).__name__}: {exc}"}]

    def _ctrl_follow(self, sock: socket.socket, reader, msg: dict) -> None:
        """Хвост: подписка раньше чтения файла, потом живой поток.

        Порядок «подписаться раньше чтения файла» + сливка по seq на
        стороне читателя исключают и дыру (запись между чтением файла и
        подпиской), и дубль (запись попала и в файл, и в поток).
        """
        sub = self.subscribe()
        try:
            for entry in self.journal.tail(FOLLOW_HISTORY):
                self._ctrl_send(sock, {"e": entry})
            self._ctrl_send(sock, {"ok": True, "done": True})
            sock.settimeout(None)
            while not self.stop.is_set():
                try:
                    entry = sub.get(timeout=10.0)
                except queue.Empty:
                    self._ctrl_send(sock, {"t": "tick"})   # живость для CLI
                    continue
                if entry is None:
                    break
                self._ctrl_send(sock, {"e": entry})
        except OSError:
            pass
        finally:
            self.unsubscribe(sub)

    # ---------------- LAN-приветствие ---------------- #

    def _announce_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", ANNOUNCE_PORT))
        except OSError as exc:
            sock.close()
            self._note(f"LAN-приветствие не поднялось: {exc}", print_it=True)
            return
        sock.settimeout(1.0)
        payload = json.dumps({"t": "hi", "me": self.peer_id,
                              "port": self.mesh_port},
                             ensure_ascii=False).encode("utf-8")
        last_sent = 0.0
        while not self.stop.is_set():
            if time.time() - last_sent >= ANNOUNCE_INTERVAL:
                last_sent = time.time()
                try:
                    sock.sendto(payload, ("255.255.255.255", ANNOUNCE_PORT))
                except OSError:
                    pass
            try:
                data, src = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                hello = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(hello, dict) or hello.get("t") != "hi":
                continue
            me = str(hello.get("me") or "")
            port = hello.get("port")
            if me == self.peer_id or not isinstance(port, int):
                continue                        # это мы сами или мусор
            with self.lock:
                if f"{src[0]}:{port}" in self.conns:
                    continue
            self.add_peer(src[0], port)
        sock.close()


# ------------------------------------------------------------------ #
#  Офлайн-чтение (работает без демона)
# ------------------------------------------------------------------ #

def read_journal_tail(dir_path: str | Path, count: int = 20) -> list[dict]:
    return Journal(Path(dir_path) / "journal.jsonl").tail(count)


def _read_state(dir_path: Path) -> dict:
    try:
        data = json.loads((Path(dir_path) / "state.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_state(dir_path: str | Path) -> dict:
    return _read_state(Path(dir_path))


def merge_state(state: dict) -> dict:
    """Сведение по всем узлам: побеждает запись с последним ts (LWW)."""
    merged: dict[str, tuple[float, Any]] = {}
    for origin, keys in state.items():
        if not isinstance(keys, dict):
            continue
        for key, entry in keys.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            ts = entry.get("ts") if isinstance(entry.get("ts"),
                                               (int, float)) else 0.0
            if key not in merged or ts >= merged[key][0]:
                merged[key] = (ts, entry.get("value"))
    return {key: value for key, (_ts, value) in merged.items()}


# ------------------------------------------------------------------ #
#  CLI
# ------------------------------------------------------------------ #

def _ctrl_call(ctrl_port: int, payload: dict,
               timeout: float = CTRL_TIMEOUT) -> dict:
    """Одна команда локальному демону: строка запроса, строка ответа."""
    try:
        with socket.create_connection(("127.0.0.1", ctrl_port),
                                      timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n")
                         .encode("utf-8"))
            reader = sock.makefile("rb")
            while True:
                line = reader.readline()
                if not line:
                    raise ControlError("демон оборвал соединение")
                try:
                    answer = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(answer, dict) and "ok" in answer:
                    if not answer.get("ok"):
                        raise ControlError(
                            str(answer.get("error") or "отказ демона"))
                    return answer
    except OSError as exc:
        raise ControlError(
            f"нет связи с демоном на 127.0.0.1:{ctrl_port} ({exc})") from exc


def _ctrl_port_of(args) -> int:
    if args.ctrl:
        return int(args.ctrl)
    return int(args.port) + CTRL_PORT_OFFSET


def _fmt_entry(entry: dict, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(entry, ensure_ascii=False)
    ts = entry.get("ts")
    stamp = (time.strftime("%H:%M:%S", time.localtime(float(ts)))
             if isinstance(ts, (int, float)) else "--:--:--")
    return (f"[{stamp}] {entry.get('o', '?')} "
            f"{entry.get('lvl', '')} {entry.get('txt', '')}")


def _cmd_serve(args) -> int:
    link = Link(data_dir=args.dir, peer_id=args.id, token=args.token,
                mesh_port=args.port, ctrl_port=_ctrl_port_of(args),
                peers=args.peer or (), announce=args.announce)
    try:
        link.start()
    except LinkError as exc:
        print(f"link: {exc}", file=sys.stderr)
        return 2

    print(f"link: демон {link.peer_id} - mesh 0.0.0.0:{link.mesh_port}, "
          f"управление 127.0.0.1:{link.ctrl_port}, каталог {link.dir}",
          flush=True)
    print(f"link: адрес этой машины для пары: "
          f"--peer {_lan_ip()}:{link.mesh_port}", flush=True)
    if link.peers:
        print("link: адреса пары: " + ", ".join(sorted(link.peers)),
              flush=True)
    elif not link.announce:
        print("link: ВНИМАНИЕ: адрес пары не задан - укажите --peer "
              "<ip_второго_пка> или включите --announce", flush=True)
    if link.announce:
        print(f"link: LAN-приветствия включены (UDP {ANNOUNCE_PORT})",
              flush=True)
    if not args.token:
        print("link: без --tokенa открыт любому, кто знает порт - "
              "только для закрытой сети теста", flush=True)
    print("link: управление: python tools/link.py log|state|tail|status "
          f"--ctrl 127.0.0.1:{link.ctrl_port}", flush=True)
    print("link: Windows при первом запуске спросит разрешение; если паре "
          "не видно друг друга, готовое правило:", flush=True)
    print(f"link:   netsh advfirewall firewall add rule "
          f"name=\"omnistash-link\" dir=in action=allow protocol=TCP "
          f"localport={link.mesh_port}", flush=True)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nlink: останавливаюсь…", flush=True)
    finally:
        link.stop_link()
    return 0


def _cmd_log(args) -> int:
    answer = _ctrl_call(_ctrl_port_of(args),
                        {"cmd": "log", "text": " ".join(args.text),
                         "level": args.level})
    print(json.dumps(answer, ensure_ascii=False))
    return 0


def _cmd_state(args) -> int:
    ctrl = _ctrl_port_of(args)
    if args.op == "set":
        raw = args.value
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw                  # просто строка - тоже состояние
        answer = _ctrl_call(ctrl, {"cmd": "state_set", "key": args.key,
                                   "value": value})
        print(json.dumps(answer, ensure_ascii=False))
        return 0

    # get: сначала живой демон, без него - последний снимок на диске
    try:
        answer = _ctrl_call(ctrl, {"cmd": "state_get", "key": args.key,
                                   "verbose": args.verbose})
    except ControlError as exc:
        state = read_state(args.dir)
        print(f"link: демон недоступен ({exc}) - читаю {args.dir}/state.json",
              file=sys.stderr)
        if args.verbose:
            print(json.dumps({"ok": True, "origins": state,
                              "state": merge_state(state)},
                             ensure_ascii=False))
        elif isinstance(args.key, str) and args.key:
            print(json.dumps(merge_state(state).get(args.key),
                             ensure_ascii=False))
        else:
            print(json.dumps(merge_state(state), ensure_ascii=False))
        return 0
    if args.verbose:
        print(json.dumps({"ok": True, "origins": answer.get("origins"),
                          "state": answer.get("state")},
                         ensure_ascii=False))
    elif isinstance(args.key, str) and args.key:
        print(json.dumps(answer.get("value"), ensure_ascii=False))
    else:
        print(json.dumps(answer.get("state"), ensure_ascii=False))
    return 0


def _cmd_tail(args) -> int:
    entries = read_journal_tail(args.dir, args.last)
    if not args.follow:
        for entry in entries:
            print(_fmt_entry(entry, args.json))
        return 0

    # --follow: подписка раньше чтения файла, слика по seq убирает и
    # дыру (запись между чтением и подпиской), и дубль (в обоих источниках)
    control = _ctrl_port_of(args)
    seen: dict[str, int] = {}
    for entry in entries:
        origin, seq = entry.get("o"), entry.get("seq")
        if isinstance(seq, int) and isinstance(origin, str):
            seen[origin] = max(seen.get(origin, -1), seq)
        print(_fmt_entry(entry, args.json), flush=True)

    try:
        with socket.create_connection(("127.0.0.1", control),
                                      timeout=CTRL_TIMEOUT) as sock:
            sock.settimeout(None)
            sock.sendall((json.dumps({"cmd": "tail", "last": 0,
                                      "follow": True},
                                     ensure_ascii=False) + "\n")
                         .encode("utf-8"))
            reader = sock.makefile("rb")
            while True:
                line = reader.readline()
                if not line:
                    print("link: демон оборвал поток", file=sys.stderr)
                    return 1
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("t") == "tick":
                    continue
                entry = msg.get("e")
                if not isinstance(entry, dict):
                    continue
                origin, seq = entry.get("o"), entry.get("seq")
                if isinstance(seq, int) and isinstance(origin, str):
                    if seq <= seen.get(origin, -1):
                        continue            # уже напечатано из файла
                    seen[origin] = seq
                print(_fmt_entry(entry, args.json), flush=True)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"link: --follow требует запущенного демона "
              f"(127.0.0.1:{control}): {exc}", file=sys.stderr)
        return 2


def _cmd_status(args) -> int:
    answer = _ctrl_call(_ctrl_port_of(args), {"cmd": "status"})
    print(json.dumps(answer, ensure_ascii=False, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link",
        description="Канал двух ПК: обмен журналом и состоянием для тестов "
                    "(демон на каждом ПК + CLI для агентов).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"mesh-порт демона (по умолчанию {DEFAULT_PORT})")
        sp.add_argument("--ctrl", type=int, default=None,
                        help="порт управления 127.0.0.1 (по умолчанию "
                             "--port+1000)")
        sp.add_argument("--dir", default=DEFAULT_DIR,
                        help=f"каталог журнала и состояния (по умолчанию "
                             f"{DEFAULT_DIR})")

    serve = sub.add_parser("serve", help="запустить демон канала")
    common(serve)
    serve.add_argument("--id", default=None,
                       help="имя этого ПК (иначе из meta.json, иначе "
                            "хостнейм)")
    serve.add_argument("--token", default="",
                       help="общий токен авторизации (рекомендуется)")
    serve.add_argument("--peer", action="append", metavar="IP[:ПОРТ]",
                       help="адрес пары (можно несколько)")
    serve.add_argument("--announce", action="store_true",
                       help="LAN-приветствие по широковещанию "
                            "(UDP %d)" % ANNOUNCE_PORT)

    log = sub.add_parser("log", help="строка в журнал (видна паре)")
    common(log)
    log.add_argument("text", nargs="+", help="текст записи")
    log.add_argument("--level", default="info",
                     help="метка уровня (info/warn/error - свободная)")

    state = sub.add_parser("state", help="состояние: set / get")
    state_sub = state.add_subparsers(dest="op", required=True)
    state_set = state_sub.add_parser("set", help="записать ключ")
    common(state_set)
    state_set.add_argument("key", help="ключ состояния")
    state_set.add_argument("value", help="JSON или просто строка")
    state_get = state_sub.add_parser("get", help="снять ключи (свои+чужие)")
    common(state_get)
    state_get.add_argument("key", nargs="?", default=None,
                           help="один ключ (иначе все)")
    state_get.add_argument("--verbose", action="store_true",
                           help="показать по узлам отдельно")

    tail = sub.add_parser("tail", help="читать журнал (офлайн или --follow)")
    common(tail)
    tail.add_argument("--last", type=int, default=20,
                      help="сколько последних записей (0 - всё)")
    tail.add_argument("--follow", action="store_true",
                      help="живой поток от демона")
    tail.add_argument("--json", action="store_true",
                      help="NDJSON вместо человекочитаемых строк")

    status = sub.add_parser("status", help="пир, seq, задержки, заметки")
    common(status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"serve": _cmd_serve, "log": _cmd_log, "state": _cmd_state,
                "tail": _cmd_tail, "status": _cmd_status}
    try:
        return handlers[args.cmd](args)
    except ControlError as exc:
        print(f"link: {exc} - запустите: python tools/link.py serve "
              f"(порт управления = --port+{CTRL_PORT_OFFSET} или --ctrl)",
              file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"link: {exc}", file=sys.stderr)
        return 2
    except LinkError as exc:
        print(f"link: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
