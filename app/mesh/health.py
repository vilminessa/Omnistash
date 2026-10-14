"""Health-check устройств (1.4): тихий TLS-probe 5 мин ± джиттер 20%.

Проба = быстрый handshake + аттестация (mesh_client.probe, pin=False,
timeout малой): никаких id/запросов наружу. Джиттер размазывает пробы
(N устройств ровно раз в 5 мин = пачки; ±20% - нет). Статус «dead»
ИНФОРМАЦИОНЕН (как решили в §8): барьер только по манифесту или отзыву
доверия - «друг молчит» ≠ «друг отозван».

Смена статуса живёт в БД (repo.peer_health_set), в журнал попадает
ТОЛЬКО смена, а не каждый проб.
"""

from __future__ import annotations

import random
import threading
import time

DEFAULT_INTERVAL = 300.0      # 5 мин
DEFAULT_JITTER = 0.2          # ±20%


class HealthChecker:
    """Фоновый обход устройств: probe_fn(addr, expect) -> None|исключение.

    devices_fn() -> [(pubkey, addr)] - какие устройства проверяем
    (обычно truststore с известным адресом, без отозванных).
    on_change(pubkey, status, detail, was) - смена статуса (лог/БД).
    """

    def __init__(self, probe_fn, devices_fn, on_change, log=None,
                 interval: float = DEFAULT_INTERVAL,
                 jitter: float = DEFAULT_JITTER):
        self._probe = probe_fn
        self._devices = devices_fn
        self._on_change = on_change
        self._log = log or (lambda message: None)
        self._interval = float(interval)
        self._jitter = float(jitter)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="omnistash-health",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def _sleep(self) -> bool:
        """Пауза с джиттером; True - пора проверять."""
        delay = self._interval * (1.0 + random.uniform(-self._jitter,
                                                       self._jitter))
        return not self._stop.wait(delay)

    def check_now(self) -> dict:
        """Один проход по устройствам (используется и фоном, и тестами).

        Возвращает {pubkey: "alive"|"dead"} для отчёта.
        """
        result: dict = {}
        for pubkey, addr in (self._devices() or ()):
            if not addr:
                continue
            try:
                self._probe(addr, expect=pubkey)
                status, detail = "alive", ""
            except Exception as exc:  # noqa: BLE001 - мёртв/не отвечает
                status, detail = "dead", str(exc)[:120]
            result[pubkey] = status
            try:
                was = self._on_change(pubkey, status, detail)
            except Exception as exc:  # noqa: BLE001 - БД не должна валить
                self._log(f"health: статус не записался: {exc}")
                continue
            if was != status:
                self._log(f"health: устройство {pubkey[:16]}… "
                          f"{'ожило' if status == 'alive' else 'замолчало'}"
                          + (f" ({detail})" if detail else ""))
        return result

    def _loop(self) -> None:
        while self._sleep():
            try:
                self.check_now()
            except Exception as exc:  # noqa: BLE001 - цикл не должен умирать
                self._log(f"health: проход упал: {exc}")
