"""B1.7: лимиты переноса - конверсии единиц и темп (RateLimiter)."""

import time
import unittest

from app.mesh import pacing


class TestConversions(unittest.TestCase):
    def test_mb_to_bytes(self):
        self.assertEqual(pacing.mb_to_bytes(10), 10 * 1024 * 1024,
                         "10 МиБ по умолчанию")
        self.assertEqual(pacing.mb_to_bytes(0), 0, "0 = без лимита")
        self.assertEqual(pacing.mb_to_bytes(None), 0)
        self.assertEqual(pacing.mb_to_bytes(-5), 0)

    def test_mbit_to_bps(self):
        self.assertEqual(pacing.mbit_to_bps(10), 1_250_000,
                         "10 Мбит/с = 1.25 МБ/с (десятичный мегабит)")
        self.assertEqual(pacing.mbit_to_bps(0), 0, "0 = без ограничения")
        self.assertEqual(pacing.mbit_to_bps(None), 0)


class TestRateLimiter(unittest.TestCase):
    def test_free_when_disabled(self):
        limiter = pacing.RateLimiter(0)
        started = time.monotonic()
        limiter.consume(50 * 1024 * 1024)
        self.assertLess(time.monotonic() - started, 0.2,
                        "лимит выключен - не должно спать")

    def test_paces_absolute_schedule(self):
        limiter = pacing.RateLimiter(20_000)   # 20 КБ/с
        started = time.monotonic()
        limiter.consume(10_000)                # ~0.5с
        limiter.consume(10_000)                # ещё ~0.5с (абсолютное
        elapsed = time.monotonic() - started   #  расписание, без дрейфа)
        self.assertGreaterEqual(elapsed, 0.85, "темп обязан сдерживаться")
        self.assertLess(elapsed, 3.0, "и не должен виснуть")

    def test_zero_bytes_is_noop(self):
        limiter = pacing.RateLimiter(1000)
        started = time.monotonic()
        limiter.consume(0)
        self.assertLess(time.monotonic() - started, 0.1)


if __name__ == "__main__":
    unittest.main()
