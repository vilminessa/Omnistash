"""Страж SVG-иконок Omnistash/Stashroom: доступность, контраст, геометрия.

Что проверяется (без браузера и без чужих библиотек - файлы читаются как текст):
  * ассеты assets/icons/ совпадают с генерацией tools/build_icon_svgs.py
    байт-в-байт (генератор детерминирован: без случайностей и без дат);
  * самостоятельная иконка несёт доступную разметку из статьи о доступных
    SVG-иконках: role="img", <title id>, aria-labelledby по этому id,
    focusable="false" и сохранённый viewBox;
  * спрайт: <symbol> с viewBox и уникальными id, подписи решаются на месте
    <use>; монохромные символы используют только currentColor, без hex-цветов
    палитры;
  * декоративные использования в демо: aria-hidden="true" и никакого title
    рядом с текстом; .visually-hidden держит white-space: nowrap (текст не
    «размазывается» по буквам);
  * контраст: значимые слои (near/mid/accent/edge) >= 3.0 к подложке плитки
    (WCAG SC 1.4.11), декоративный far слой обязан быть ниже 3.0 и назван
    декоративным - он не носит смысл;
  * позиционирование: значимые фигуры центрованы по оси канваса (x=12 на
    24-сетке, x=8 на 16-сетке), центроиды треугольников совпадают с
    заявленными числами;
  * 16 пикселов: значимый штрих >= 1 пиксела, декоративного дальнего слоя
    нет;
  * цвета во всех файлах - только палитра человека, ничего не «подправлено»
    вручную;
  * сборка exe не трогает новые ассеты: специка их не включает, размер exe
    от этой волны не меняется.
"""

import re
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC / "tools"))

import build_icon_svgs as icons  # noqa: E402 - путь добавлен выше

ASSETS = SRC / "assets" / "icons"
SPEC = SRC / "Omnistash.spec"
VIEWBOX = re.compile(r'viewBox="0 0 (\d+) (\d+)"')
HEX = re.compile(r"#[0-9A-Fa-f]{6}")


def names() -> list[str]:
    return list(icons.GEOMETRY)


class IconSvgsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.generated = icons.render_all(names())

    def test_assets_match_generation(self):
        """Ассет либо совпадает с генерацией, либо кто-то правил вручную."""
        missing = []
        for name, text in self.generated.items():
            path = ASSETS / name
            if not path.is_file():
                missing.append(name)
                continue
            self.assertEqual(path.read_text(encoding="utf-8"), text,
                             f"{name}: ассет не совпадает с генерацией "
                             f"tools/build_icon_svgs.py")
        self.assertEqual(missing, [], f"не построено: {missing}")

    def test_standalone_icons_are_accessible(self):
        """role="img" + title по id + aria-labelledby + focusable="false"."""
        for name in names():
            for size_name, size in ((f"{name}.svg", 24), (f"{name}-16.svg", 16),
                                    (f"{name}-tile.svg", 1024)):
                text = (ASSETS / size_name).read_text(encoding="utf-8")
                self.assertIn('role="img"', text, f"{size_name}: нет role=img")
                self.assertIn('focusable="false"', text,
                              f"{size_name}: нет focusable=false (IE/Edge "
                              f"фокусировали svg)")
                title = re.search(r'<title id="([^"]+)"', text)
                self.assertIsNotNone(title, f"{size_name}: нет title с id")
                self.assertIn(f'aria-labelledby="{title.group(1)}"', text,
                              f"{size_name}: aria-labelledby не ссылается на "
                              f"title")
                self.assertEqual(VIEWBOX.search(text).groups(),
                                 (str(size), str(size)),
                                 f"{size_name}: viewBox не сохранён или "
                                 f"не совпадает с размером")
                self.assertNotIn('aria-hidden="true"', text,
                                 f"{size_name}: самостоятельная иконка не может "
                                 f"быть скрыта от AT")

    def test_sprite_symbols_keep_viewbox_and_ids(self):
        """Спрайт: <symbol> с viewBox, уникальные id, подписи на месте <use>."""
        text = (ASSETS / "sprite.svg").read_text(encoding="utf-8")
        self.assertIn('style="display:none"', text,
                      "спрайты прячутся инлайном - до загрузки стилей")
        ids = re.findall(r'<symbol id="([^"]+)" viewBox="0 0 24 24"', text)
        expected = [f"{name}" for name in names()] + [f"{name}-mono" for name in names()]
        self.assertEqual(sorted(ids), sorted(expected),
                         f"символы спрайта расходятся: {ids}")
        self.assertEqual(len(ids), len(set(ids)), "id символа не уникальны")

    def test_mono_symbols_use_current_color(self):
        """Монохром: currentColor вместо палитры, подложка плитки отсутствует."""
        text = (ASSETS / "sprite.svg").read_text(encoding="utf-8")
        blocks = re.findall(r'<symbol id="([^"]+)"(.*?)</symbol>', text,
                            re.S)
        for symbol_id, block in blocks:
            if symbol_id.endswith("-mono"):
                self.assertIn("currentColor", block,
                              f"{symbol_id}: нет currentColor - иконка не "
                              f"масштабируется от размера шрифта и не красится "
                              f"цветом родителя")
                self.assertEqual(HEX.findall(block), [],
                                 f"{symbol_id}: hex-цвета палитры в монохроме")
            else:
                self.assertNotIn("currentColor", block,
                                 f"{symbol_id}: цветная иконка не должна "
                                 f"наследовать currentColor")

    def test_demo_marks_decorative_icons_without_title(self):
        """Иконка рядом с текстом: aria-hidden и никакого title (текст первичен)."""
        text = (ASSETS / "demo.html").read_text(encoding="utf-8")
        self.assertIn(".visually-hidden", text, "демо не показывает скрытый текст")
        self.assertIn("white-space: nowrap", text,
                      "без nowrap скрытый текст читается по буквам")
        self.assertIn("height: 1em; width: 1em", text,
                      "иконка не масштабируется от размера шрифта")
        self.assertIn("fill: currentColor", text,
                      "иконка не наследует цвет родителя")
        for svg in re.findall(r"<svg[^>]*>", text):
            if 'aria-hidden="true"' in svg:
                self.assertIn('focusable="false"', svg,
                              "декоративная иконка без focusable=false")
        hidden_with_title = re.findall(
            r'<svg[^>]*aria-hidden="true"[^>]*>\s*<title', text)
        self.assertEqual(hidden_with_title, [],
                         "aria-hidden и title вместе - смысл передаётся дважды")
        signed = re.findall(
            r'<svg[^>]*role="img"[^>]*aria-labelledby="[^"]+"[^>]*>\s*<title', text)
        self.assertGreater(len(signed), 0,
                           "демо не показывает подписанную самостоятельную иконку")

    def test_meaningful_contrast_and_decorative_named(self):
        """Значимые слои >= 3.0, декоративный far - ниже 3.0 и назван."""
        problems, table = icons.verify_contrast()
        self.assertEqual(problems, [], f"контраст не выдержан: {problems}")
        self.assertTrue(all(value >= icons.MEANING_MIN for _, _, value, verdict
                            in table if verdict == "значимый"),
                        "значимый слой ниже порога SC 1.4.11")
        self.assertTrue(all(value <= icons.DECORATIVE_MAX for _, _, value, verdict
                            in table if verdict == "декоративный"),
                        "декоративный слой выше порога - он стал значимым")
        self.assertTrue(any(verdict == "декоративный" for _, _, _, verdict in table),
                        "ни один слой не назван декоративным - палитра не сверена")

    def test_geometry_is_centered_on_canvas_axis(self):
        """Центры и центроиды сверяются по числам, а не глазами."""
        problems = icons.verify_geometry()
        self.assertEqual(problems, [], f"позиционирование: {problems}")
        centroid = icons.shape_center(
            next(s for s in icons.GEOMETRY["omnistash"]["shapes"]
                 if s["role"] == "accent"))
        self.assertEqual(centroid[0], 12.0, "прорезь не центрована по x=12")
        triangle = next(s for s in icons.GEOMETRY["stashroom"]["shapes"]
                        if s["role"] == "near" and s["kind"] == "path"
                        and s["d"].endswith("Z"))
        self.assertEqual(icons.shape_center(triangle), (12.0, 13.75),
                         "центроид play-треугольника не совпал с заявленным")

    def test_16px_silhouette_reads(self):
        """На 16 пикселах значимый штрих >= 1 пиксела, декоративного слоя нет."""
        problems = icons.verify_16_readability()
        self.assertEqual(problems, [], f"16px: {problems}")
        for name in names():
            shapes = icons.GEOMETRY_16[name]["shapes"]
            self.assertNotIn("far", [s["role"] for s in shapes],
                             f"{name} 16px: декоративный слой на 16 пикселах "
                             f"не читается и обязан отсутствовать")

    def test_files_use_only_the_palette(self):
        """Ни один hex вне палитры - цвета не «подправлены» вручную."""
        allowed = set(icons.PALETTE)
        for name, text in self.generated.items():
            colors = set(HEX.findall(text))
            self.assertTrue(colors <= allowed,
                            f"{name}: цвета вне палитры {sorted(colors - allowed)}")

    def test_exe_asset_unchanged_by_this_wave(self):
        """Специка не берёт assets/icons: новый набор не меняет состав exe."""
        spec = SPEC.read_text(encoding="utf-8")
        self.assertNotIn("assets/icons", spec,
                         "специка вшивает icons в exe - состав бинаря меняется, "
                         "а сверка размера exe описана для прежней иконки")
        self.assertIn('assets", "omnistash.ico"', spec,
                      "специка потеряла прежнюю иконку exe")

    def test_tile_master_keeps_viewbox_and_scale(self):
        """Мастер 1024: viewBox сохранён, масштаб фигур = 1024/24."""
        for name in names():
            text = (ASSETS / f"{name}-tile.svg").read_text(encoding="utf-8")
            self.assertEqual(VIEWBOX.search(text).groups(), ("1024", "1024"),
                             f"{name}-tile: viewBox не сохранён")
            scale = re.search(r'transform="scale\(([\d.]+)\)"', text)
            self.assertIsNotNone(scale, f"{name}-tile: масштаб не назван")
            self.assertAlmostEqual(float(scale.group(1)), 1024 / 24, places=3,
                                   msg=f"{name}-tile: масштаб не 1024/24")


if __name__ == "__main__":
    unittest.main(verbosity=2)
