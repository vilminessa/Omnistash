"""UPnP-проброс (Mesh-4): фейковый роутер вместо настоящего.

Настоящий роутер в тестах недопустим (проброс - это изменения чужого
устройства), поэтому поднимаем HTTP-сервер, отдающий description.xml и
принимающий SOAP-вызовы - ровно то, что делает IGD. Проверяем:

  * разбор controlURL: абсолютный путь (ловушка: origin = "http:" +
    путь даёт мусор) и относительный;
  * вызовы GetExternalIPAddress / AddPortMapping (TCP и UDP) /
    RemovePortMapping;
  * PortMapper: успех -> внешний адрес для объявлений; отказ роутера ->
    честное «только LAN», а не падение;
  * SSDP-хелперы (LOCATION, фильтр ответов) без сокетов.
"""

import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from app.mesh import nat

DESCRIPTION_ABS = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <serviceList>
   <service>
    <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
    <serviceSubType>X</serviceSubType>
    <controlURL>/ctl</controlURL>
    <eventSubURL>/evt</eventSubURL>
   </service>
  </serviceList>
 </device>
</root>"""

DESCRIPTION_REL = DESCRIPTION_ABS.replace("<controlURL>/ctl</controlURL>",
                                          "<controlURL>ctl</controlURL>")

NOT_A_GATEWAY = """<?xml version="1.0"?>
<root><device><deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
</device></root>"""


class FakeRouter(BaseHTTPRequestHandler):
    """Один сервер на тест: description.xml + SOAP с записью вызовов."""

    # Разделяемое состояние на классе (сервер один на весь тест-кейс).
    external_ip = "203.0.113.5"
    fail_soap = False
    adds: list = []
    removes: list = []
    actions: list = []

    def log_message(self, *args):    # тишина в тестовом выводе
        pass

    def _send_xml(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):                # noqa: N802 - имя из стандарта
        if self.path == "/rootDesc.xml":
            self._send_xml(DESCRIPTION_ABS)
        elif self.path == "/desc/root.xml":
            self._send_xml(DESCRIPTION_REL)
        elif self.path == "/other.xml":
            self._send_xml(NOT_A_GATEWAY)
        else:
            self._send_xml("<error/>", status=404)

    def do_POST(self):               # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        action = (self.headers.get("SOAPAction") or "").split("#")[-1].rstrip('"')
        FakeRouter.actions.append(action)
        if FakeRouter.fail_soap:
            self._send_xml("<error/>", status=500)
            return
        if action == "GetExternalIPAddress":
            self._send_xml(
                "<u:GetExternalIPAddressResponse "
                'xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">'
                f"<NewExternalIPAddress>{FakeRouter.external_ip}"
                "</NewExternalIPAddress></u:GetExternalIPAddressResponse>")
            return
        if action == "AddPortMapping":
            FakeRouter.adds.append({
                "protocol": (re.search(r"<NewProtocol>([^<]+)", body) or [None, ""])[1],
                "port": (re.search(r"<NewExternalPort>([^<]+)", body) or [None, ""])[1],
                "client": (re.search(r"<NewInternalClient>([^<]+)", body) or [None, ""])[1],
            })
            self._send_xml("<ok/>")
            return
        if action == "RemovePortMapping":
            FakeRouter.removes.append(
                (re.search(r"<NewProtocol>([^<]+)", body) or [None, ""])[1])
            self._send_xml("<ok/>")
            return
        self._send_xml("<error/>", status=501)


class RouterCase(unittest.TestCase):
    """Фейковый роутер поднимается один раз на тест-кейс."""

    @classmethod
    def setUpClass(cls):
        FakeRouter.adds, FakeRouter.removes, FakeRouter.actions = [], [], []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        FakeRouter.adds, FakeRouter.removes, FakeRouter.actions = [], [], []
        FakeRouter.fail_soap = False
        FakeRouter.external_ip = "203.0.113.5"


class TestSsdpHelpers(unittest.TestCase):
    def test_location_header_any_case(self):
        reply = "HTTP/1.1 200 OK\r\nlocation: http://192.168.1.1:5000/d.xml\r\n\r\n"
        self.assertEqual(nat._location_of(reply),
                         "http://192.168.1.1:5000/d.xml")
        self.assertIsNone(nat._location_of("HTTP/1.1 200 OK\r\n\r\n"))

    def test_igd_reply_detection(self):
        self.assertTrue(nat._is_igd_reply(
            "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1"))
        self.assertFalse(nat._is_igd_reply(
            "ST: urn:schemas-upnp-org:device:MediaRenderer:1"),
            "любой SSDP-ответ несёт schemas-upnp-org - это не признак IGD")
        self.assertFalse(nat._is_igd_reply(
            "LOCATION: http://192.168.1.1:5000/d.xml\r\n"))


class TestDescriptionParsing(RouterCase):
    def test_absolute_control_url_keeps_origin(self):
        igd = nat.IGD.from_description(f"{self.base}/rootDesc.xml")
        self.assertIsNotNone(igd, "IGD-описание должно распознаться")
        self.assertEqual(igd.control_url, f"{self.base}/ctl",
                         "абсолютный controlURL: origin = схема+хост+порт")

    def test_relative_control_url_resolves_from_directory(self):
        igd = nat.IGD.from_description(f"{self.base}/desc/root.xml")
        self.assertIsNotNone(igd)
        self.assertEqual(igd.control_url, f"{self.base}/desc/ctl",
                         "относительный путь - рядом с description.xml")

    def test_foreign_device_is_not_a_gateway(self):
        self.assertIsNone(nat.IGD.from_description(f"{self.base}/other.xml"))
        self.assertIsNone(
            nat.IGD.from_description("http://127.0.0.1:9/nope.xml"),
            "недоступный хост - просто не IGD, без исключений наружу")


class TestSoapCalls(RouterCase):
    def _igd(self) -> nat.IGD:
        igd = nat.IGD.from_description(f"{self.base}/rootDesc.xml")
        self.assertIsNotNone(igd)
        return igd

    def test_external_ip_from_router(self):
        self.assertEqual(self._igd().get_external_ip(), "203.0.113.5")
        self.assertIn("GetExternalIPAddress", FakeRouter.actions)

    def test_add_both_protocols_and_remove(self):
        igd = self._igd()
        self.assertTrue(igd.add_port(38574, "TCP"))
        self.assertTrue(igd.add_port(38574, "UDP"))
        self.assertEqual(len(FakeRouter.adds), 2)
        self.assertEqual({a["protocol"] for a in FakeRouter.adds},
                         {"TCP", "UDP"}, "нужны оба транспорта одного порта")
        self.assertTrue(all(a["port"] == "38574" for a in FakeRouter.adds))
        self.assertTrue(all(re.match(r"^\d+\.\d+\.\d+\.\d+$", a["client"])
                            for a in FakeRouter.adds),
                        "internal client - свой IP, а не пусто")
        self.assertTrue(igd.remove_port(38574, "TCP"))
        self.assertTrue(igd.remove_port(38574, "UDP"))
        self.assertEqual(set(FakeRouter.removes), {"TCP", "UDP"})

    def test_router_refusal_is_false_not_exception(self):
        FakeRouter.fail_soap = True
        igd = self._igd()
        self.assertFalse(igd.add_port(38574, "TCP"),
                         "отказ роутера - это False, а не падение")
        self.assertIsNone(igd.get_external_ip())


class TestPortMapper(RouterCase):
    def _mapper(self, renew: float = 999.0, expect_ok: bool = True) -> nat.PortMapper:
        logs: list = []
        with mock.patch.object(nat, "_discover_locations",
                               return_value=[]), \
             mock.patch.object(nat, "_gateway_descriptions",
                               return_value=[f"{self.base}/rootDesc.xml"]):
            mapper = nat.PortMapper(38574, log=logs.append, renew=renew)
            mapper.start()
            state = mapper.wait(15)
            if expect_ok:
                self.assertTrue(state["enabled"],
                                f"проброс должен пройти: {state}")
        self.logs = logs
        return mapper

    def test_success_maps_ports_and_announces_external(self):
        mapper = self._mapper()
        self.assertEqual(mapper.external_ip, "203.0.113.5")
        self.assertEqual(mapper.announce_addr(), "203.0.113.5:38574",
                         "внешний адрес уходит в DHT-заявления")
        self.assertEqual(len(FakeRouter.adds), 2)
        mapper.stop()
        mapper.join(10)
        self.assertFalse(mapper.state()["enabled"], "после остановки проброса нет")
        self.assertEqual(set(FakeRouter.removes), {"TCP", "UDP"},
                         "остановка обязана снять оба правила у роутера")
        self.assertTrue(any("проброс" in line for line in self.logs))

    def test_router_refusal_falls_back_to_lan(self):
        FakeRouter.fail_soap = True
        mapper = self._mapper(expect_ok=False)
        state = mapper.state()
        self.assertFalse(state["enabled"])
        self.assertIn("отказал", state["reason"])
        self.assertIn("LAN", state["reason"])
        self.assertTrue(mapper.announce_addr().endswith(":38574"),
                        "без проброса объявляем локальный адрес")
        mapper.stop()
        mapper.join(5)

    def test_no_router_at_all_falls_back_to_lan(self):
        with mock.patch.object(nat, "_discover_locations", return_value=[]), \
             mock.patch.object(nat, "_gateway_descriptions",
                               return_value=["http://127.0.0.1:9/desc.xml"]):
            logs: list = []
            mapper = nat.PortMapper(38574, log=logs.append)
            mapper.start()
            state = mapper.wait(15)
        self.assertFalse(state["enabled"])
        self.assertIn("LAN", state["reason"])
        self.assertTrue(any("не найден" in line for line in logs),
                        "причина обязана попасть в журнал")
        mapper.stop()

    def test_renewal_keeps_mapping_alive(self):
        mapper = self._mapper(renew=0.1)
        deadline = threading.Event()
        for _ in range(100):
            if len(FakeRouter.adds) >= 4:      # 2 старта + 2 продления
                break
            deadline.wait(0.05)
        self.assertGreaterEqual(len(FakeRouter.adds), 4,
                                "правило должно продлеваться, иначе роутер "
                                "молча снимет его через аренду")
        mapper.stop()
        mapper.join(10)

    def test_failed_setup_retries_without_log_spam(self):
        """Неудача больше не приговор до перезапуска: попытки повторяются,
        но одна и та же причина пишется в журнал один раз."""
        logs = []
        with mock.patch.object(nat.IGD, "discover",
                               return_value=None) as discover:
            mapper = nat.PortMapper(38574, log=logs.append, retry=0.05)
            mapper.start()
            deadline = time.time() + 3.0
            while time.time() < deadline and discover.call_count < 3:
                time.sleep(0.02)
            self.assertGreaterEqual(discover.call_count, 3,
                                    "попытка проброса должна повторяться")
            mapper.stop()
            mapper.join(5)
        failures = [line for line in logs if "повтор" in line]
        self.assertEqual(len(failures), 1,
                         f"одна причина - одна строка, получили: {logs}")
        self.assertIn("LAN", failures[0])
        self.assertIn("UPnP", failures[0])


class TestLocalIp(unittest.TestCase):
    def test_local_ip_is_ipv4(self):
        self.assertRegex(nat.local_ip(), r"^\d{1,3}(\.\d{1,3}){3}$")


if __name__ == "__main__":
    unittest.main()
