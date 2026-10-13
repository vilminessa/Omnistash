"""LAN-обнаружение по mDNS/Zeroconf (B4): участники видят друг друга.

Дополнение к работающему multicast-приветствию на фикс. порту (B1.5):
mDNS (RFC 6762) - стандартный механизм с поддержкой в системах из
коробки. Сервис объявляется как _omnistash._tcp.local., в TXT - версия
протокола и ник.

Библиотека zeroconf **опциональна и не поставляется** ( LGPL - см.
THIRD_PARTY_NOTICES): не установлена - модуль честно пишет об этом в
журнал и обнаружение работает старыми способами (hello/DHT/ручные
адреса). Поставить: pip install zeroconf.
"""

from __future__ import annotations

import socket
import threading

SERVICE_TYPE = "_omnistash._tcp.local."
RESOLVE_TIMEOUT = 3000        # мс на резолв адреса при Added

try:
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange
    from zeroconf import Zeroconf
    AVAILABLE = True
    IMPORT_ERROR = ""
except ImportError as exc:                # нулл-объект: без пакета не падаем
    Zeroconf = ServiceInfo = ServiceBrowser = None   # type: ignore[assignment]
    ServiceStateChange = None             # type: ignore[assignment]
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

# Один Zeroconf на процесс (рекомендованный библиотекой паттерн): один
# сокет, поиск видит свои же объявления через общий реестр - не полагаемся
# на петлю мультикаста между двумя экземплярами на одной машине.
_zc_lock = threading.Lock()
_zc = None
_zc_users = 0


def available() -> bool:
    """Библиотека установлена (иначе все старты честно откажут)."""
    return AVAILABLE


def _acquire():
    """Общий Zeroconf; None - нет библиотеки/отказ системы."""
    global _zc, _zc_users
    if not AVAILABLE:
        return None
    with _zc_lock:
        if _zc is None:
            try:
                _zc = Zeroconf()
            except Exception:  # noqa: BLE001 - сеть/права не позволили
                return None
        _zc_users += 1
        return _zc


def _release(zc) -> None:
    """Отпустить общий Zeroconf; закрывается на последнем пользователе."""
    global _zc, _zc_users
    old = None
    with _zc_lock:
        _zc_users = max(0, _zc_users - 1)
        if _zc_users == 0 and _zc is not None:
            old, _zc = _zc, None
    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass


def lan_ip() -> str:
    """LAN-адрес машины: UDP-connect выбирает маршрут, пакетов нет."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))     # TEST-NET: пакеты не уходят
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def own_name() -> str:
    """Короткое имя машины для имени сервиса (без домена, ASCII-путь)."""
    return (socket.gethostname().split(".")[0] or "omnistash")[:32]


def service_info(port: int, nick: str = "") -> "ServiceInfo":
    """Описание своего сервиса: адрес, порт, TXT {v, nick}."""
    properties = {"v": b"1"}
    if nick:
        properties["nick"] = str(nick).encode("utf-8")
    return ServiceInfo(
        SERVICE_TYPE,
        f"{own_name()}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(lan_ip())],
        port=int(port),
        properties=properties,
        server=f"{own_name()}.local.")


class Announcer:
    """Объявление своего участника в mDNS."""

    def __init__(self, port: int, nick: str = "", log=None):
        self._port = int(port)
        self._nick = str(nick or "")
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._zc = None
        self._info = None

    @property
    def running(self) -> bool:
        return self._zc is not None

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> bool:
        """Поднять объявление; False - нет библиотеки или отказ системы."""
        if not AVAILABLE:
            self._log(f"mDNS: zeroconf не установлена ({IMPORT_ERROR}) - "
                      "обнаружение только старыми способами (hello/DHT/"
                      "ручные адреса); включить: pip install zeroconf")
            return False
        with self._lock:
            if self._zc is not None:
                return True
            zc = _acquire()
            if zc is None:
                self._log("mDNS: объявление не поднялось (нет библиотеки "
                          "или сеть недоступна)")
                return False
            try:
                info = service_info(self._port, self._nick)
                zc.register_service(info)
            except Exception as exc:  # noqa: BLE001 - 5353 занят/нет прав
                _release(zc)
                self._log(f"mDNS: объявление не поднялось: {exc}")
                return False
            self._zc, self._info = zc, info
        self._log(f"mDNS: сервис объявлен ({info.server}, порт "
                  f"{self._port})")
        return True

    def set_nick(self, nick: str) -> None:
        """Смена ника: перерегистрация сервиса (без перезапуска окна)."""
        nick = str(nick or "")
        with self._lock:
            if self._zc is None or nick == self._nick:
                self._nick = nick
                return
            zc, info = self._zc, self._info
            try:
                zc.unregister_service(info)
            except Exception:  # noqa: BLE001 - снятие не должно валить
                pass
            try:
                info = service_info(self._port, nick)
                zc.register_service(info)
            except Exception as exc:  # noqa: BLE001
                self._log(f"mDNS: перерегистрация не удалась: {exc}")
                return
            self._info, self._nick = info, nick

    def stop(self) -> None:
        with self._lock:
            zc, info = self._zc, self._info
            self._zc, self._info = None, None
        if zc is None:
            return
        try:
            zc.unregister_service(info)
        except Exception:  # noqa: BLE001
            pass
        _release(zc)


class Browser:
    """Поиск участников: on_found(addr, port, nick) - свой поток zeroconf.

    Свой сервис тоже придёт в события - фильтрует вызывающая сторона
    (знает свой порт и имя): здесь честная доставка всего найденного.
    """

    def __init__(self, on_found, log=None):
        self._on_found = on_found
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._zc = None
        self._browser = None

    @property
    def running(self) -> bool:
        return self._zc is not None

    def start(self) -> bool:
        if not AVAILABLE:
            self._log(f"mDNS: поиск не поднят - zeroconf не установлена "
                      f"({IMPORT_ERROR})")
            return False
        with self._lock:
            if self._zc is not None:
                return True
            zc = _acquire()
            if zc is None:
                self._log("mDNS: поиск не поднялся (нет библиотеки "
                          "или сеть недоступна)")
                return False
            try:
                browser = ServiceBrowser(zc, SERVICE_TYPE,
                                         handlers=[self._on_event])
            except Exception as exc:  # noqa: BLE001
                _release(zc)
                self._log(f"mDNS: поиск не поднялся: {exc}")
                return False
            self._zc, self._browser = zc, browser
        self._log("mDNS: поиск участников запущен")
        return True

    def _on_event(self, zeroconf, service_type, name, state_change) -> None:
        """Обработчик ServiceBrowser: имена параметров - по API zeroconf
        (он вызывает хендлер с именованными аргументами)."""
        try:
            if state_change != ServiceStateChange.Added:
                return
            info = zeroconf.get_service_info(service_type, name,
                                              timeout=RESOLVE_TIMEOUT)
            if info is None or not info.port:
                return
            nick = ""
            raw = (info.properties or {}).get(b"nick")
            if raw:
                nick = bytes(raw).decode("utf-8", "replace")
            for addr in info.parsed_addresses():
                if ":" in addr:
                    continue                 # IPv6 пока не поддерживаем
                self._on_found(addr, int(info.port), nick)
        except Exception as exc:  # noqa: BLE001 - кривой пакет сети
            self._log(f"mDNS: событие не разобрано: {exc}")

    def stop(self) -> None:
        with self._lock:
            zc = self._zc
            self._zc, self._browser = None, None
        if zc is None:
            return
        _release(zc)
