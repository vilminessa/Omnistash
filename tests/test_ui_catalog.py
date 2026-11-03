"""Страж: карточки каталога и плитки (DESIGN-UI шаг 3).

Поведение в браузере офлайн не проверить полностью, поэтому держим
наличие фактов разметки, атрибутов и обработчиков: их удаление или
«упрощение» станет красным тестом, а не ночным баг-репортом.

Что проверяет тест:
  * сегмент Библиотека/Каталог - role="radiogroup" с role="radio" и
    aria-checked (состояние читается AT, не только классом .active);
    стрелки перемещают и выбирают цель (APG radiogroup);
  * placeholder поиска каталога без литерала {площадка} (замена №20):
    JS подставляет имя площадки до показа, нейтральный текст - до него;
  * полоса каталога - progressbar: valuenow/valuemax при известном итоге,
    без valuenow при недетерминированной + valuetext со счётчиком карточек;
  * карточка каталога фокусируема (tabindex="0"): Enter - страница видео,
    Space - добавление; вложенные кнопки не перехватываются; отказ
    отвечает одной и той же подписью на клик и клавиатуру;
  * CSS: состояния карточки - граница --ok-line/--info-line/--err-line
    (текст не гасится, opacity только на отказной обложке, замена №18);
    hover карточки/плитки - --line-strong + --shadow-1; play-кнопка
    плитки всегда видима 44×44 (замена №4); --ctl-h и --tile-min объявлены
    и читаются контролами каталога; active-сегмент - тон + underline.
"""

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui_src"
CSS = UI / "app.css"
JS = UI / "app.js"
HTML = UI / "index.html"
REPO = UI.parent / "app" / "repo.py"


def _clean(text: str) -> str:
    """Файл без комментариев: комментарии объясняют ловушки и не код."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


class TestCatalogUi(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    # ------------------------------ разметка ------------------------------

    def test_view_segment_is_radiogroup(self):
        self.assertRegex(self.html,
                         r'id="pf-view"[^>]*role="radiogroup"',
                         "сегмент режима снова просто кнопки")
        self.assertRegex(self.html, r'id="pf-view"[^>]*aria-label="')
        self.assertIn('role="radio"', self.html)
        self.assertRegex(self.html, r'role="radio"[^>]*aria-checked="true"')
        self.assertRegex(self.html, r'role="radio"[^>]*aria-checked="false"')

    def test_segment_checked_state_follows_active(self):
        # aria-checked ставится и при сбросе площадки, и при выборе сегмента.
        self.assertGreaterEqual(self.js.count('setAttribute("aria-checked"'), 3,
                                "aria-checked сегмента потеряно - AT не читает режим")
        self.assertRegex(self.js,
                         r'x\.setAttribute\("aria-checked", on \? "true" : "false"\)')
        self.assertRegex(self.js, r'b\.setAttribute\("aria-checked", "true"\)')
        # Стрелки выбирают цель (радио выбирается при перемещении).
        seg = re.search(r'pfView\.addEventListener\("keydown"[^{]*\{(.+?)\n      \}\);',
                        self.js, re.DOTALL)
        self.assertTrue(seg, "клавиатура сегмента удалена")
        body = seg.group(1)
        for fact in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp",
                     "next.focus()", "next.click()", "ev.preventDefault()"):
            self.assertIn(fact, body, f"клавиатура сегмента потеряла {fact}")

    def test_search_placeholder_neutral(self):
        # Литерал {площадка} не показывается: имя подставляет applyPlatform.
        self.assertNotIn("{площадка}", self.html,
                         "placeholder снова содержит литерал {площадка}")
        self.assertRegex(self.html,
                         r'id="cat-search"[^>]*placeholder="Поиск по площадке')
        # Имя площадки всё же доходит до поля (взрослая - нейтральная подпись).
        self.assertIn('$("cat-search").placeholder = platState.adult', self.js)
        self.assertIn('t("ph.site", "Поиск на площадке…")', self.js)

    def test_catalog_progress_is_progressbar(self):
        self.assertRegex(self.html,
                         r'id="cat-progress-track"[^>]*role="progressbar"',
                         "полоса каталога снова нефункциональный div")
        self.assertRegex(self.html,
                         r'id="cat-progress-track"[^>]*aria-label="')
        self.assertRegex(self.html, r'id="cat-progress-track"[^>]*aria-valuemin="0"')
        bar = re.search(r"function catBar\([^)]*\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(bar, "catBar не найден")
        body = bar.group(1)
        self.assertIn('setAttribute("aria-valuemax"', body)
        self.assertIn('setAttribute("aria-valuenow"', body)
        self.assertIn('setAttribute("aria-valuetext"', body)
        # Недетерминированная: valuenow снимается, ход несёт valuetext.
        self.assertIn('removeAttribute("aria-valuenow"', body)
        self.assertIn("итог неизвестен", body)

    # ------------------------------ карточка ------------------------------

    def test_card_focusable_and_keyboard(self):
        render = re.search(r"function catRender\(\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(render, "catRender не найден")
        self.assertIn('tabindex="0"', render.group(1),
                      "карточка каталога снова нефокусируема")
        handler = re.search(r'\$\("cat-grid"\)\.addEventListener\("keydown"'
                            r"[^{]*\{(.+?)\n    \}\);", self.js, re.DOTALL)
        self.assertTrue(handler, "клавиатура карточек удалена")
        body = handler.group(1)
        for fact in ('"Enter"', '" "', "ev.preventDefault()",
                     'closest("button, input, select, a")',
                     'closest(".cat-card")', 'call("open_url", item.url)',
                     "catAddItem(item)"):
            self.assertIn(fact, body, f"клавиатура карточек потеряла {fact}")

    def test_unavailable_answer_single_source(self):
        # Одна подпись отказа на клик и клавиатуру (текст в одном месте).
        self.assertRegex(self.js, r"function catUnavailable\(")
        click = re.search(r'\$\("cat-grid"\)\.addEventListener\("click"'
                          r"[^{]*\{(.+?)\n    \}\);", self.js, re.DOTALL)
        self.assertTrue(click, "клик по карточкам удалён")
        self.assertIn("catUnavailable(item)", click.group(1))
        keydown = re.search(r'\$\("cat-grid"\)\.addEventListener\("keydown"'
                            r"[^{]*\{(.+?)\n    \}\);", self.js, re.DOTALL)
        self.assertTrue(keydown)
        self.assertIn("catUnavailable(item)", keydown.group(1))

    # ------------------------------ CSS ------------------------------

    def test_tokens_declared_and_read(self):
        self.assertRegex(self.css, r"--ctl-h:\s*32px")
        self.assertRegex(self.css, r"--tile-min:\s*240px")
        self.assertRegex(self.css,
                         r"#cat-mode, #cat-sort, #cat-search[^{]*\{"
                         r"[^}]*height: var\(--ctl-h\)")
        self.assertRegex(self.css,
                         r"\.seg-btn\s*\{[^}]*height: var\(--ctl-h\)")
        grid = re.search(r"\.card-grid\s*\{([^}]*)\}", self.css)
        self.assertTrue(grid, "нет правила .card-grid")
        self.assertIn("var(--tile-min)", grid.group(1))
        self.assertIn("gap: var(--s-4)", grid.group(1),
                      "gap карточек снова вне шкалы отступов")

    def test_card_states_are_border_not_opacity(self):
        # Замена №18: состояние несёт граница + подпись; гасится только
        # отказная обложка, текст остаётся читаемым.
        self.assertRegex(self.css, r"\.cat-card\.added[^{}]*\{[^}]*var\(--ok-line\)")
        self.assertRegex(self.css, r"\.cat-card\.adding[^{}]*\{[^}]*var\(--info-line\)")
        self.assertRegex(self.css, r"\.cat-card\.failed[^{}]*\{[^}]*var\(--err-line\)")
        self.assertRegex(self.css,
                         r"\.cat-card\.failed \.cat-thumb img[^{}]*\{[^}]*opacity")
        self.assertNotRegex(self.css,
                            r"\.cat-card\.(added|adding|failed)\s*\{[^}]*opacity",
                            "карточка снова гасится целиком (замена №18)")

    def test_card_and_tile_hover_are_line_shadow(self):
        self.assertRegex(self.css,
                         r"\.cat-card:hover[^{}]*\{[^}]*var\(--line-strong\)")
        self.assertRegex(self.css,
                         r"\.cat-card:hover[^{}]*\{[^}]*var\(--hover\)")
        self.assertRegex(self.css,
                         r"\.tile:hover[^{}]*\{[^}]*var\(--line-strong\)")
        self.assertRegex(self.css,
                         r"\.tile:hover[^{}]*\{[^}]*var\(--shadow-1\)")
        card = re.search(r"(?m)^\.cat-card\s*\{([^}]*)\}", self.css)
        self.assertTrue(card, "нет правила .cat-card")
        self.assertIn("box-shadow: var(--shadow-1)", card.group(1),
                      "карточка без --shadow-1 (§3.3)")

    def test_play_button_always_visible(self):
        # Замена №4: значок не прячется в hover - клавиатура и touch без него.
        play = re.search(r"\.tile-play\s*\{([^}]*)\}", self.css)
        self.assertTrue(play, "нет правила .tile-play")
        self.assertNotIn("opacity", play.group(1),
                         "tile-play снова прячется через opacity")
        self.assertRegex(play.group(1), r"width:\s*44px")
        self.assertRegex(play.group(1), r"height:\s*44px")
        self.assertNotRegex(self.css, r"\.tile:hover \.tile-play[^{}]*\{[^}]*opacity",
                            "hover-раскрытие значка вернулось")
        self.assertRegex(self.css,
                         r"\.tile-play:hover[^{}]*\{[^}]*background: var\(--accent\)")

    def test_active_segment_has_two_indicators(self):
        # Активный сегмент различим тоном и underline (NN/g: не только цвет).
        self.assertRegex(self.css,
                         r"\.seg-btn\.active[^{}]*\{[^}]*background: var\(--surface-2\)")

    # ------------------------------ чипсы тегов ------------------------------

    def test_tag_chips_read_the_backend_shape(self):
        # Подпись чипса строится из t.name и t.n: с другими ключами кнопка
        # читает «(undefined)» — превью показывает то, чего в окне нет.
        self.assertGreaterEqual(self.js.count("esc(t.name)"), 2,
                                "подпись чипса не читает t.name")
        self.assertGreaterEqual(self.js.count("t.n"), 2,
                                "счётчик чипса не читает t.n")
        self.assertNotRegex(self.js, r"esc\(t\.tag\)", "чипс читает t.tag")

    def test_preview_mock_tags_match_repo_columns(self):
        # Мок обязан врать теми же ключами, что отдаёт repo.tags_popular.
        mock = re.search(r"tags_popular: function \(\) \{([\s\S]*?)\n      \},", self.js)
        self.assertTrue(mock, "мок tags_popular удалён")
        push = re.search(r"tags\.push\(\{([^}]*)\}\)", mock.group(1))
        self.assertTrue(push, "мок не строит список тегов")
        keys = set(re.findall(r"([a-z_]+):", push.group(1)))
        self.assertTrue({"slug", "name", "n"} <= keys,
                        f"мок тегов без ключей UI: {sorted(keys)}")
        self.assertNotIn("tag", keys, "мок отдаёт tag вместо name")
        self.assertNotIn("count", keys, "мок отдаёт count вместо n")

        sql = re.search(r"def tags_popular[\s\S]*?SELECT([\s\S]*?)FROM",
                        REPO.read_text(encoding="utf-8"))
        self.assertTrue(sql, "не найден SELECT tags_popular")
        cols = set()
        for a, b in re.findall(r"t\.([a-z_]+)|AS\s+([a-z_]+)", sql.group(1)):
            cols.add(a or b)
        self.assertTrue(keys <= cols,
                        f"мок врёт ключами, которых нет у бэкенда: {sorted(keys - cols)}")

        self.assertRegex(self.css,
                         r"\.seg-btn\.active[^{}]*\{[^}]*inset 0 -2px[^}]*var\(--accent\)")

    def test_public_texts_stay_neutral(self):
        # Публичные тексты шага 3 не называют взрослую площадку (как
        # test_public_texts: внутренние id ниже по регистру разрешены).
        for needle in ("Поиск по площадке…", "Поиск на площадке…",
                      "Прогресс загрузки карточек каталога", "итог неизвестен"):
            self.assertIn(needle, self.html + self.js)
        blob = self.html + self.js
        self.assertFalse(re.search(r"PornHub|PORNHUB|Pornhub", blob),
                         "имя взрослой площадки в публичных текстах UI")
        self.assertFalse(re.search(r"pornhub\.com|pornhub\.org|pornhub\.net", blob),
                         "домены взрослой площадки в публичных текстах UI")


if __name__ == "__main__":
    unittest.main()
