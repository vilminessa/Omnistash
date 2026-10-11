"""UPnP IGD: проброс порта mesh-сервера за NAT (честный фолбэк - LAN).

Зачем: без проброса участник за роутером принимает только LAN-соединения
(для соседей по сети этого хватает, для интернета - нет). Порты пробрасываем
ОБА протокола одного номера: TCP (HTTP-передача файлов) и UDP (DHT).

Как находим шлюз:
  1) SSDP M-SEARCH на 239.255.255.250:1900 - роутер сам назовёт свой
     description.xml (стандарт, без сторонних сервисов);
  2) фолбэк - типовые адреса шлюза (192.168.0.1/192.168.1.1:5000).
Внешний IP берём у того же IGD (GetExternalIPAddress) - никаких
«узнай мой ip» наружу.

Аренда: NewLeaseDuration=0 (постоянная, где требуют - вернут свой срок);
дополнительно PortMapper периодически ОБНОВЛЯЕТ проброс: часть роутеров
молча снимает правила, а мы об этом не узнаем бы.
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DISCOVER_TIMEOUT = 2.5
SOAP_TIMEOUT = 5.0
GATEWAY_CANDIDATES = ("192.168.0.1", "192.168.1.1")
DESCRIPTION_TIMEOUT = 3.0
RENEW_INTERVAL = 1800.0     # обновление правила раз в 30 минут
USER_AGENT = "OmnistashMesh/1.0"

MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
)


class NatError(Exception):
    """IGD не найден или отказал - вызывающий честно сообщает про LAN."""


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: float = SOAP_TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        exc.close()      # тело отказа не нужно, а дескриптор закрыть надо
        raise


def _location_of(text: str) -> str | None:
    """Адрес description.xml из ответа SSDP (регистр заголовка не важен)."""
    match = re.search(r"LOCATION:\s*(\S+)", text, re.IGNORECASE)
    return match.group(1) if match else None


def _is_igd_reply(text: str) -> bool:
    """Ответ устройства действительно про шлюз (а не про ТВ в той же LAN).

    Ловушка: «upnp» искать нельзя - все SSDP-ответы несут
    schemas-upnp-org, и фильтр пропускал бы вообще всё.
    """
    return bool(re.search(r"InternetGatewayDevice", text, re.IGNORECASE))


def _discover_locations(timeout: float = DISCOVER_TIMEOUT) -> list[str]:
    """SSDP: адреса description.xml ответивших устройств.

    Окно не обрывается на первом ответе: среди откликов бывают и не-IGD
    устройства. Ответы с упоминанием IGD/UPnP ставим вперёд, остальные -
    хвостом (финальный фильтр всё равно по содержимому description.xml).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(min(0.5, timeout))
    igd_first: list[str] = []
    others: list[str] = []
    try:
        sock.sendto(MSEARCH.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "replace")
            location = _location_of(text)
            if not location:
                continue
            bucket = igd_first if _is_igd_reply(text) else others
            if location not in bucket:
                bucket.append(location)
    finally:
        sock.close()
    return igd_first + others


def _gateway_descriptions() -> list[str]:
    return [f"http://{host}:5000/rootDesc.xml" for host in GATEWAY_CANDIDATES]


class IGD:
    """Один найденный IGD: SOAP-вызовы WANIPConnection."""

    def __init__(self, description_url: str, control_url: str,
                 service_type: str):
        self.description_url = description_url
        self.control_url = control_url
        self.service_type = service_type

    @classmethod
    def from_description(cls, description_url: str) -> "IGD | None":
        """Разобрать description.xml: service WANIPConnection -> controlURL."""
        try:
            xml = _http(description_url, timeout=DESCRIPTION_TIMEOUT)
        except (OSError, ValueError):
            return None
        text = xml.decode("utf-8", "replace")
        if "WANIPConnection" not in text and "WANPPPConnection" not in text:
            return None      # это не маршрутизатор с пробросом
        # serviceType ... controlURL рядом внутри <service>
        for service in re.findall(r"<service>(.*?)</service>", text,
                                  re.DOTALL):
            kind = re.search(r"<serviceType>\s*([^<]+)\s*</serviceType>",
                             service)
            url = re.search(r"<controlURL>\s*([^<]+)\s*</controlURL>", service)
            if not kind or not url:
                continue
            if ("WANIPConnection" in kind.group(1)
                    or "WANPPPConnection" in kind.group(1)):
                base = description_url.rsplit("/", 1)[0]
                control = url.group(1).strip()
                if control.startswith("/"):
                    # абсолютный путь: схема + хост + порт (split("/", 3)[0]
                    # дал бы лишь "http:" - кошка об этом молчит)
                    origin = "/".join(description_url.split("/", 3)[:3])
                    full = f"{origin}{control}"
                else:
                    full = f"{base}/{control}"
                return cls(description_url, full, kind.group(1).strip())
        return None

    @classmethod
    def discover(cls, timeout: float = DISCOVER_TIMEOUT) -> "IGD | None":
        candidates = _discover_locations(timeout) + _gateway_descriptions()
        for candidate in candidates:
            if not candidate:
                continue
            igd = cls.from_description(candidate)
            if igd is not None:
                return igd
        return None

    def _soap(self, action: str, args: dict, timeout: float = SOAP_TIMEOUT) -> str:
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/encoding/urn:upnp-org:service:upnp-1.0">'
            f'<s:Body><u:{action} xmlns:u="{self.service_type}">'
            + "".join(
                f"<{key}>{escape(str(value))}</{key}>" for key, value in args.items())
            + f"</u:{action}></s:Body></s:Envelope>"
        )
        data = body.encode("utf-8")
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self.service_type}#{action}"',
        }
        answer = _http(self.control_url, data=data, headers=headers,
                       timeout=timeout)
        return answer.decode("utf-8", "replace")

    def get_external_ip(self) -> str | None:
        try:
            xml = self._soap("GetExternalIPAddress", {})
        except (OSError, ValueError):
            return None
        match = re.search(r"<NewExternalIPAddress>\s*([^<]+)\s*"
                          r"</NewExternalIPAddress>", xml)
        if not match:
            return None
        ip = match.group(1).strip()
        return ip if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) else None

    def add_port(self, port: int, protocol: str = "TCP") -> bool:
        """Проброс порта; False - роутер отказал (или недоступен)."""
        try:
            self._soap("AddPortMapping", {
                "NewRemoteHost": "",
                "NewExternalPort": str(int(port)),
                "NewProtocol": protocol.upper(),
                "NewInternalPort": str(int(port)),
                "NewInternalClient": local_ip(),
                "NewEnabled": "1",
                "NewPortMappingDescription": "Omnistash mesh",
                "NewLeaseDuration": "0",
            })
            return True
        except (OSError, ValueError):
            return False

    def remove_port(self, port: int, protocol: str = "TCP") -> bool:
        try:
            self._soap("RemovePortMapping", {
                "NewRemoteHost": "",
                "NewExternalPort": str(int(port)),
                "NewProtocol": protocol.upper(),
            })
            return True
        except (OSError, ValueError):
            return False


def local_ip() -> str:
    """Локальный адрес машины (без отправленных пакетов)."""
    from . import dht
    return dht.local_ip()


class PortMapper:
    """Живой проброс: поднимает, периодически обновляет, снимает при выключении.

    Ведёт себя тихо: любая ошибка - это «работаем по LAN», а не падение
    (причина пишется в итоговом состоянии для журнала).
    """

    def __init__(self, port: int, log=None, renew: float = RENEW_INTERVAL):
        self.port = int(port)
        self.protocols = ("TCP", "UDP")
        self._log = log or (lambda message: None)
        self._renew = float(renew)
        self.igd: IGD | None = None
        self.external_ip: str | None = None
        self.enabled = False
        self.reason = "не пробовал"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> dict:
        """Поднять проброс в ФОНЕ (не блокирует вызывающего).

        SSDP-поиск и SOAP к роутеру занимают секунды - на них нельзя
        вешать ни открытие окна, ни сохранение настроек. Итог готовности
        ждём через wait() там, где он нужен (адрес объявлений).
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.state()
            self._stop.clear()
            thread = threading.Thread(target=self._run, daemon=True,
                                      name="omnistash-upnp")
            self._thread = thread
            thread.start()
        return self.state()

    def wait(self, timeout: float) -> dict:
        """Дождаться ПЕРВОГО итога проброса (снимок состояния).

        Фон после успеха живёт вечно (продление) - ждать его смерть здесь
        нельзя: важно лишь, чтобы роутер уже ответил «да/нет».
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            thread = self._thread
            if self.state()["reason"] != "не пробовал" or thread is None \
                    or not thread.is_alive():
                break
            time.sleep(0.02)
        return self.state()

    def join(self, timeout: float) -> None:
        """Дождаться полного завершения фона (для остановки и тестов)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def stop(self) -> None:
        """Попросить фон остановиться; снятие проброса делает сам фон."""
        self._stop.set()

    def state(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "port": self.port,
                    "external_ip": self.external_ip, "reason": self.reason}

    def _run(self) -> None:
        # 1. Найти шлюз.
        try:
            igd = IGD.discover()
        except Exception:  # noqa: BLE001 - сеть шлюза не должна валить
            igd = None
        if igd is None:
            with self._lock:
                self.enabled = False
                self.reason = "роутер с UPnP не найден - только LAN"
            self._log("UPnP: шлюз не найден, проброса нет (только LAN)")
            return
        if self._stop.is_set():
            return          # выключили, пока искали шлюз - проброса не было
        with self._lock:
            self.igd = igd
        # 2. Пробросить оба протокола.
        ok = 0
        for protocol in self.protocols:
            if igd.add_port(self.port, protocol):
                ok += 1
        external = igd.get_external_ip()
        with self._lock:
            self.external_ip = external
            self.enabled = ok == len(self.protocols)
            if not self.enabled:
                self.reason = (f"роутер отказал в пробросе "
                               f"({ok}/{len(self.protocols)}) - только LAN")
            elif not external:
                self.enabled = False
                self.reason = "внешний адрес роутер не сообщил - только LAN"
            else:
                self.reason = f"проброс {self.port} на {external}"
        if self.enabled:
            self._log(f"UPnP: проброс {self.port}/tcp+udp -> "
                      f"{external}:{self.port}")
        else:
            self._log(f"UPnP: {self.reason}")
            return
        # 3. Продление/проверка: роутер мог снять правило молча; при
        # выключении - снимаем проброс сами (иначе роутер помнит наш порт).
        while True:
            if self._stop.wait(self._renew):
                removed = 0
                for protocol in self.protocols:
                    if igd.remove_port(self.port, protocol):
                        removed += 1
                with self._lock:
                    self.enabled = False
                    self.reason = "проброс снят"
                self._log(f"UPnP: проброс {self.port} снят "
                          f"({removed}/{len(self.protocols)})")
                return
            renewed = 0
            for protocol in self.protocols:
                if igd.add_port(self.port, protocol):
                    renewed += 1
            if renewed != len(self.protocols):
                with self._lock:
                    self.enabled = False
                    self.external_ip = igd.get_external_ip()
                    self.reason = "проброс перестал обновляться - только LAN"
                self._log("UPnP: проброс потерялся - работаем по LAN")
                return

    def announce_addr(self) -> str:
        """Адрес для DHT-заявлений: внешний (если проброс жив) или локальный."""
        from . import dht
        with self._lock:
            if self.enabled and self.external_ip:
                return f"{self.external_ip}:{self.port}"
        return f"{dht.local_ip()}:{self.port}"
