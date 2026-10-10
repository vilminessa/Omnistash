/* Omnistash - логика главного окна.
 *
 * Работа с Python идёт через js_api (мост pywebview) и опрос poll(since):
 * окно ничего не считает само, оно только рисует снимок состояния. Таблица
 * библиотеки виртуализирована - страницы по 120 строк подтягиваются по мере
 * прокрутки, поэтому 100 000 записей листаются так же, как 100.
 */
(function () {
  "use strict";

  var ROW_H = 34;          // высота строки (должна совпадать с --row-h)
  // Плитка: ширина подсказка, а фактические колонки считает ширина окна.
  // row - высота ряда плиток (плитка + название + канал + воздух): виртуализация
  // считает по ней, поэтому точность важна - плитка выше ряда легла бы на следующий.
  var TILE = {
    small:  { w: 160, row: 168, gap: 8 },
    medium: { w: 240, row: 218, gap: 10 },
    large:  { w: 320, row: 262, gap: 12 }
  };
  var THUMB_CACHE_MAX = 400;   // сколько data-URI обложек держим в памяти
  var THUMB_BATCH = 100;       // id за один вызов get_thumbs (лимит бэкенда)
  var PAGE = 120;          // сколько строк тянем за один запрос
  var POLL_MS = 500;       // период опроса
  // Порядок статусов в фильтре - как движется видео: от «увидели» к «скачали».
  var STATUS_ORDER = ["known", "queued", "downloading", "downloaded",
                      "failed", "missing", "unavailable"];
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    tab: "library",
    scope: { type: "pool" },
    status: "",
    ratingMin: "",
    query: "",
    pages: {},            // номер страницы -> строки
    total: 0,
    since: 0,
    scan: null,
    settings: {},
    schema: [],
    rev: -1,
    logLines: [],
    busy: false,
    selected: null,        // Set(id): мультивыбор в таблице
    storages: [],          // хранилища из poll (вместо старых корней)
    selStorage: "",        // явный выбор в панели выделения
    wizardDismissed: false, // «настроить позже» на первом запуске
    dedupeResolved: null,   // Set путей: разобранные «возможные переезды»
    ffmpeg: null,           // установка ffmpeg из poll (найден/качается/ошибка)
    ffmpegNoteDismissed: false, // «Скрыть» у заметки в очереди (до конца сессии)
    viewMode: "list",       // «list»/«grid» - из настройки view_mode
    tileSize: "medium",     // «small»/«medium»/«large» - из tile_size
    account: null,          // состояние аккаунта Google из poll (без секретов)
    botNoteDismissed: false // «Скрыть» у подсказки про аккаунт
  };
  state.selected = new Set();
  state.dedupeResolved = new Set();

  /* ------------------------------------------------------------------ *
   *  Мост к Python
   * ------------------------------------------------------------------ */

  var mock = null;

  function bridge() {
    if (window.pywebview && window.pywebview.api) {
      // Настоящий мост появился после превью (медленный старт) - переключаемся
      // и прячем предупреждение, чтобы окно не осталось на выдуманных данных.
      if (mock) {
        mock = null;
        var badge = $("preview-badge");
        if (badge) badge.hidden = true;
      }
      return window.pywebview.api;
    }
    return mock;
  }

  function call(name, arg) {
    var api = bridge();
    if (!api || typeof api[name] !== "function") return Promise.resolve(null);
    try {
      // Важно: аргумент не передаём вовсе, если его нет. pywebview превращает
      // undefined в None и Python получает лишний позиционный аргумент -
      // метод вида get_initial() на это отвечает TypeError.
      var out = (arg === undefined) ? api[name]() : api[name](arg);
      return out && typeof out.then === "function" ? out : Promise.resolve(out);
    } catch (err) {
      return Promise.reject(err);
    }
  }

  /* ------------------------------------------------------------------ *
   *  Утилиты
   * ------------------------------------------------------------------ */

  function humanSize(bytes) {
    var n = Number(bytes) || 0;
    var units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? n + " " + units[0] : n.toFixed(1) + " " + units[i];
  }

  function humanDuration(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return "—";
    sec = Number(sec);
    if (sec < 0) return "—";
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    var pad = function (v) { return (v < 10 ? "0" : "") + v; };
    return h ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }

  /* Русская плюрализация: 1 запись / 2 записи / 5 записей. */
  function plural(n, forms) {
    n = Number(n) || 0;
    var mod100 = n % 100, mod10 = n % 10;
    if (mod100 >= 11 && mod100 <= 14) return forms[2];
    if (mod10 === 1) return forms[0];
    if (mod10 >= 2 && mod10 <= 4) return forms[1];
    return forms[2];
  }

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function shortDate(iso) {
    return iso ? String(iso).slice(0, 10) : "—";
  }

  function shortTime(iso) {
    if (!iso) return "—";
    var text = String(iso).replace("T", " ").slice(0, 16);
    return text;
  }

  function toast(text, isError) {
    var el = $("toast");
    el.textContent = text;
    el.className = "toast" + (isError ? " err" : "");
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.hidden = true; }, 3800);
  }

  function debounce(fn, ms) {
    var timer = 0;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  /* ------------------------------------------------------------------ *
   *  Опрос состояния
   * ------------------------------------------------------------------ */

  var ticking = false;

  function tick() {
    if (ticking) return;
    ticking = true;
    call("poll", state.since).then(function (snap) {
      ticking = false;
      if (!snap) return;
      state.since = snap.log_cursor;
      applySnapshot(snap);
    }).catch(function () { ticking = false; });
  }

  function applySnapshot(snap) {
    state.busy = !!snap.busy;
    $("status").textContent = snap.status || "Готов.";
    // Рисуем только то, что реально изменилось: перестройка innerHTML на
    // каждом тике (2 раза в секунду) ломала бы открытые селекты и клики.
    if (changed("stats", snap.stats)) renderStats(snap.stats);
    if (changed("storages", snap.storages)) renderStorages(snap.storages);
    if (changed("scan", snap.scan)) renderScan(snap.scan);
    if (changed("tree", snap.tree)) renderTree(snap.tree);
    if (changed("sources", snap.sources)) renderSources(snap.sources);
    if (changed("runs", snap.runs)) renderRuns(snap.runs);
    if (changed("queue", snap.queue)) renderQueue(snap.queue);
    if (changed("dl", snap.dl)) {
      renderDl(snap.dl);
      updateBotNote();
    }
    if (changed("sync", snap.sync)) renderSync(snap.sync);
    if (changed("add", snap.add_flow)) renderAddFlow(snap.add_flow);
    if (changed("migrate", snap.migrate)) {
      state.migrate = snap.migrate;
      if (!$("migrate-overlay").hidden) renderMigrateBody();
    }
    if (changed("repack", snap.repack)) {
      state.repack = snap.repack;
      if (!$("repack-overlay").hidden) renderRepackBody();
    }
    if (changed("schedule", snap.schedule)) renderSchedule(snap.schedule);
    if (changed("verify", snap.verify)) renderVerify(snap.verify);
    if (changed("ffmpeg", snap.ffmpeg)) {
      state.ffmpeg = snap.ffmpeg;
      renderFfmpegState();
    }
    if (changed("account", snap.account)) {
      state.account = snap.account;
      renderAccountState();
      updateBotNote();
    }
    appendLogs(snap.logs);

    if (snap.settings_rev !== state.rev) {
      state.rev = snap.settings_rev;
      state.settings = snap.settings || state.settings;
      document.body.dataset.theme = state.settings.theme === "light" ? "light" : "dark";
      // Вид/размер плитки живут в настройках - при смене перерисовываем
      // без сброса: страницы загружены, выделение и скролл не трогаем.
      var viewChanged = syncViewFromSettings();
      applyViewState();
      renderSettings();
      if (viewChanged) paintGrid();
    }
    $("stop-scan-btn").hidden = !(snap.scan && snap.scan.running);
    $("rescan-btn").disabled = !!(snap.scan && snap.scan.running);
  }

  var signatures = {};

  function changed(key, data) {
    var sig = JSON.stringify(data);
    if (signatures[key] === sig) return false;
    signatures[key] = sig;
    return true;
  }

  function renderStats(stats) {
    if (!stats) return;
    $("head-stats").innerHTML =
      "<span>Всего <b>" + stats.total.toLocaleString("ru-RU") + "</b></span>" +
      "<span>Скачано <b>" + stats.downloaded.toLocaleString("ru-RU") + "</b></span>" +
      "<span>В очереди <b>" + stats.queued.toLocaleString("ru-RU") + "</b></span>" +
      "<span>Плейлистов <b>" + stats.playlists + "</b></span>" +
      "<span>Каналов <b>" + stats.channels + "</b></span>" +
      "<span>Объём <b>" + humanSize(stats.bytes) + "</b></span>";
    var badge = $("queue-badge");
    badge.textContent = stats.queued;
    badge.hidden = !stats.queued;
    renderStatusFilter(stats.by_status);
  }

  function renderStatusFilter(byStatus) {
    var select = $("status-filter");
    var current = select.value;
    // Всегда все статусы, а не только встретившиеся: иначе «файл пропал»
    // не выбрать, пока библиотека чистая, а фильтр «скачано» с нулём
    // исчезнет ровно в тот момент, когда он нужен.
    var html = ['<option value="">все статусы</option>'];
    STATUS_ORDER.forEach(function (key) {
      var count = (byStatus && byStatus[key]) || 0;
      html.push('<option value="' + esc(key) + '">' +
        esc(statusLabel(key)) + " (" + count + ")</option>");
    });
    select.innerHTML = html.join("");
    select.value = current;
    if (select.selectedIndex < 0) select.value = "";
  }

  function statusLabel(status) {
    var known = {
      known: "в индексе", queued: "в очереди", downloading: "качается",
      downloaded: "скачано", failed: "ошибка", missing: "файл пропал",
      unavailable: "удалено на площадке"
    };
    return known[status] || status;
  }

  function renderScan(scan) {
    state.scan = scan || null;
    var bar = $("scan-bar");
    if (!scan) { bar.hidden = true; renderDedupeNote(null); return; }
    if (scan.running) {
      // Новый прогон - новая сверка: прошлые разборы больше неактуальны.
      state.dedupeResolved.clear();
      bar.hidden = false;
      renderDedupeNote(null);
      var percent = scan.total ? Math.round(scan.done * 100 / scan.total) : 0;
      $("scan-fill").style.width = percent + "%";
      $("scan-text").textContent =
        "Переиндексация " + scan.done + " / " + scan.total +
        (scan.path ? " · " + scan.path : "");
      return;
    }
    if (scan.summary) {
      bar.hidden = false;
      $("scan-fill").style.width = "100%";
      $("scan-text").textContent = scan.summary;
    } else {
      bar.hidden = true;
    }
    renderDedupeNote(scan);
  }

  function renderDedupeNote(scan) {
    // После скана стоит напомнить о том, что требует ручного решения:
    // ни копии, ни «возможный переезд» не разрешаются сами.
    var note = $("dedupe-note");
    if (!scan || scan.running) { note.hidden = true; return; }
    var parts = [];
    // Разобранные строки больше не напоминают о себе: вычтем их из счётчика.
    var moves = Math.max(0, (scan.move_count || 0) - state.dedupeResolved.size);
    if (moves) parts.push("похоже на переезд: " + moves);
    if (scan.dup_count) parts.push("копий найдено: " + scan.dup_count);
    if (!parts.length) { note.hidden = true; return; }
    note.hidden = false;
    $("dedupe-note-text").textContent =
      "Сверка нашла: " + parts.join(" · ") +
      ". Разберите - в индексе должно остаться по одной копии.";
  }

  /* ------------------------------------------------------------------ *
   *  Проверка целостности (сверка файлов с хешем)
   * ------------------------------------------------------------------ */

  function renderVerify(v) {
    var bar = $("verify-bar");
    var note = $("verify-note");
    if (!v) { bar.hidden = true; note.hidden = true; return; }

    var running = !!v.running;
    $("verify-btn").textContent = running ? "Остановить проверку"
                                          : "Целостность";
    if (running) {
      bar.hidden = false;
      var pct = v.total ? Math.round(v.done * 100 / v.total) : 0;
      $("verify-fill").style.width = pct + "%";
      $("verify-text").textContent = "Проверка " + v.done + " / " + v.total +
        (v.current ? " · " + v.current : "");
      note.hidden = true;
      return;
    }
    if (!v.summary) { bar.hidden = true; note.hidden = true; return; }
    bar.hidden = true;
    note.hidden = false;

    var html = esc(v.summary);
    if (v.broken && v.broken.length) {
      html += '<div class="migrate-list verify-list">' +
        v.broken.slice(0, 20).map(function (row) {
          return '<div class="migrate-item warn" title="' + esc(row.path) +
            '">' + esc(row.title || row.path) + " - хеш не совпал</div>";
        }).join("") + "</div>";
    }
    $("verify-note-text").innerHTML = html;
    var repair = $("verify-repair-btn");
    repair.hidden = !(v.broken_total > 0);
    repair.textContent = "Перекачать битые (" + v.broken_total + ")";
  }

  function runVerify() {
    if (state.verify && state.verify.running) { call("verify_stop"); return; }
    call("verify_start", repackRequest()).then(function (res) {
      if (!res) return;
      if (res.error) toast(res.error, true);
    });
  }

  function repairBroken() {
    call("repair_broken").then(function (res) {
      if (!res) return;
      if (res.error) { toast(res.error, true); return; }
      toast("В очередь поставлено: " + res.queued);
      switchTab("queue");
    });
  }

  /* ------------------------------------------------------------------ *
   *  Пометки группы (теги / рейтинг / просмотрено)
   * ------------------------------------------------------------------ */

  function openMark() {
    if (!state.selected.size) {
      toast("Сначала выберите строки", true);
      return;
    }
    $("mark-title").textContent = "Пометить: " + state.selected.size +
      (state.selected.size === 1 ? " строку" : " строк");
    $("mark-body").innerHTML =
      '<div class="mark-grid">' +
      '<div class="mark-row"><span class="field-label">Рейтинг</span>' +
      '<span class="stars" data-stars data-touched="" data-value="0">' +
      starButtons(0) + "</span>" +
      '<button class="link-btn" data-mark-clear="rating">сбросить</button></div>' +
      '<div class="mark-row"><span class="field-label">Теги</span>' +
      '<input type="text" id="mark-tags" placeholder="через запятую" ' +
      'style="flex:1 1 240px"></div>' +
      '<div class="mark-row"><span class="field-label">Заметка</span>' +
      '<textarea id="mark-notes" rows="2" style="flex:1 1 320px"></textarea></div>' +
      '<div class="mark-row"><label class="check">' +
      '<input type="checkbox" id="mark-watched"><span>просмотрено</span></label>' +
      '<button class="link-btn" data-mark-clear="watched">снять отметку</button></div>' +
      '<div class="muted">Пустые поля не меняются: отмечается только то, ' +
      "что вы заполнили. По тегу ищется поиском в строке поиска.</div>" +
      "</div>" +
      '<div class="card-actions">' +
      '<button class="btn" data-mark="close">Отмена</button>' +
      '<button class="btn primary" data-mark="apply">Применить</button></div>';
    $("mark-overlay").hidden = false;
    bindMark();
  }

  function bindMark() {
    bindStars(document.querySelector("#mark-body [data-stars]"));
    Array.prototype.forEach.call(
      document.querySelectorAll("#mark-body [data-mark-clear]"),
      function (btn) {
        btn.addEventListener("click", function () {
          var what = btn.dataset.markClear;
          if (what === "rating") {
            var stars = document.querySelector("#mark-body [data-stars]");
            stars.dataset.value = "0";
            stars.dataset.touched = "1";
            Array.prototype.forEach.call(stars.querySelectorAll(".star"),
              function (star) { star.classList.remove("on"); });
          } else if (what === "watched") {
            var watched = document.getElementById("mark-watched");
            watched.checked = false;
            watched.dataset.touched = "1";
          }
        });
      });
    Array.prototype.forEach.call(
      document.querySelectorAll("#mark-body [data-mark]"), function (btn) {
        btn.addEventListener("click", function () {
          if (btn.dataset.mark === "close") {
            $("mark-overlay").hidden = true;
            return;
          }
          var fields = {};
          var stars = document.querySelector("#mark-body [data-stars]");
          if (stars && stars.dataset.touched) {
            fields.user_rating = Number(stars.dataset.value || 0);
          }
          var watched = document.getElementById("mark-watched");
          if (watched && watched.dataset.touched) fields.watched = watched.checked;
          var tags = document.getElementById("mark-tags");
          if (tags && tags.value.trim()) fields.user_tags = tags.value;
          var notes = document.getElementById("mark-notes");
          if (notes && notes.value.trim()) fields.notes = notes.value;
          if (!Object.keys(fields).length) {
            toast("Ничего не заполнено", true);
            return;
          }
          call("save_fields", { ids: Array.from(state.selected), fields: fields })
            .then(function (res) {
              if (!res || res.error) { toast(res && res.error, true); return; }
              toast("Помечено строк: " + (res.updated || 0));
              $("mark-overlay").hidden = true;
              renderGrid();
            });
        });
      });
  }

  /* ------------------------------------------------------------------ *
   *  Установка ffmpeg (по кнопке: GPL не вшиваем)
   * ------------------------------------------------------------------ */

  function renderFfmpegState() {
    var ff = state.ffmpeg || {};
    if (ff.found) state.ffmpegNoteDismissed = false;
    var span = document.querySelector("#settings-body [data-ffmpeg-state]");
    if (span) {
      span.textContent = ff.found ? "найден: " + (ff.path || "")
        : (ff.running ? "скачивается " + (ff.pct || 0) + "%" : "не установлен");
    }
    refreshTranscodeField();
    var note = $("ffmpeg-note");
    if (note) {
      note.hidden = !(ff.degraded && !ff.found && !ff.running) ||
        !!state.ffmpegNoteDismissed;
    }
    if (!$("ffmpeg-overlay").hidden) renderFfmpegBody();
  }

  function refreshTranscodeField() {
    // Список кодировщиков живой: что реально есть в сборке ffmpeg - то и
    // выбираемостно, остальное гасим с пояснением (не молчаливый пропуск).
    var select = document.querySelector(
      '#settings-body select[data-key="transcode"]');
    if (!select) return;
    var ff = state.ffmpeg || {};
    Array.prototype.forEach.call(select.options, function (opt) {
      if (opt.dataset.label === undefined) opt.dataset.label = opt.textContent;
      var label = opt.dataset.label;
      if (opt.value === "none") {
        opt.disabled = false;
        opt.textContent = label;
        return;
      }
      var missing = !ff.found ||
        (ff.encoders && ff.encoders.indexOf(opt.value) < 0);
      opt.disabled = !!missing;
      opt.textContent = label + (missing
        ? (!ff.found ? " (нет ffmpeg)" : " (нет в сборке)") : "");
    });
    var note = document.querySelector(
      "#settings-body [data-transcode-note]");
    if (note) note.hidden = !!ff.found;
  }

  function openFfmpegOverlay() {
    $("ffmpeg-overlay").hidden = false;
    renderFfmpegBody();
  }

  function renderFfmpegBody() {
    var ff = state.ffmpeg || {};
    var head =
      "<p>Склейка видео и аудио, метаданные, субтитры <b>внутрь файла</b> и " +
      "перекодировка требуют <b>ffmpeg</b> - внешнего инструмента под GPL.</p>" +
      '<p class="muted">Omnistash его не содержит и не распространяет: ' +
      "вы скачиваете его сами с gyan.dev (при недоступности - зеркало BtbN " +
      "на GitHub). Файлы встают в <code>%LOCALAPPDATA%\\Omnistash\\bin</code> " +
      "(порядка 100 МБ на диске), удаляются простым удалением этой папки. " +
      "Без ffmpeg всё остальное работает: качаются готовые потоки, просто " +
      "без склейки.</p>";
    var actions;
    var progress = "";
    if (ff.found) {
      actions = '<div class="card-actions">' +
        '<button class="btn" data-ffmpeg="close">Закрыть</button></div>';
      progress = '<div class="notice"><span>ffmpeg уже найден: ' +
        esc(ff.path || "") + "</span></div>";
    } else if (ff.running) {
      var pct = ff.pct || 0;
      var phases = { start: "начинаем", download: "скачиваем",
                     extract: "распаковываем", verify: "проверяем",
                     done: "готово" };
      progress = '<div class="scan-bar"><div class="scan-track">' +
        '<div class="scan-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="scan-text">' + esc(phases[ff.phase] || ff.phase || "") +
        " · " + pct + "%</div></div>";
      actions = '<div class="card-actions">' +
        '<button class="btn" data-ffmpeg="stop">Остановить</button></div>';
    } else if (ff.error) {
      progress = '<div class="notice"><span>Не удалось: ' + esc(ff.error) +
        "</span></div>";
      actions = '<div class="card-actions">' +
        '<button class="btn" data-ffmpeg="install">Повторить</button>' +
        '<button class="btn" data-ffmpeg="close">Закрыть</button></div>';
    } else {
      actions = '<div class="card-actions">' +
        '<button class="btn" data-ffmpeg="close">Отмена</button>' +
        '<button class="btn primary" data-ffmpeg="install">' +
        "Скачать FFmpeg</button></div>";
    }
    $("ffmpeg-body").innerHTML = head + progress + actions;
  }

  /* ------------------------------------------------------------------ *
   *  Аккаунт Google: куки для загрузки (см. app/google_auth)
   * ------------------------------------------------------------------ */

  function renderAccountState() {
    var acc = state.account || {};
    var box = document.querySelector("#settings-body [data-account-list]");
    if (box) {
      var accounts = acc.accounts || [];
      var rows = accounts.map(function (item) {
        return '<div class="account-row">' +
          '<span class="muted">' + esc(item.label || item.id) + "</span>" +
          '<span class="muted">· ' + esc(shortTime(item.since)) + "</span>" +
          '<span class="spacer"></span>' +
          '<button class="link-btn" data-account-forget="' +
            esc(item.id) + '">забыть</button></div>';
      }).join("");
      if (acc.logging_in) {
        rows += '<div class="muted">Идёт вход в открытом окне…</div>';
      }
      if (acc.note) {
        rows += '<div class="muted">' + esc(acc.note) + "</div>";
      }
      if (acc.browser_warning) {
        // Честное предупреждение: браузеры с App-Bound Encryption.
        rows += '<div class="notice warn">' + esc(acc.browser_warning) +
          "</div>";
      }
      box.innerHTML = rows || '<div class="muted">Аккаунтов нет: куки ' +
        "применяются только у источников, к которым аккаунт привязан.</div>";
    }
    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-act-account]"),
      function (btn) {
        var act = btn.dataset.actAccount;
        if (act !== "login" && act !== "abort") return;
        // В момент входа кнопка «Войти» превращается в «Прервать»:
        // закрыли окно сами или передумали - состояние снимается сразу,
        // а не через таймаут ожидания.
        btn.dataset.actAccount = acc.logging_in ? "abort" : "login";
        btn.textContent = acc.logging_in ? "Прервать вход" : "Войти в окне…";
      });
  }

  function updateBotNote() {
    // Подсказка видна, когда площадка просила вход, а аккаунтов нет вовсе:
    // с привязкой разбираются в диалоге добавления источника.
    var acc = state.account || {};
    var dl = state.dl || {};
    var note = $("bot-note");
    if (note) {
      note.hidden = !dl.bot_hint || !!(acc.accounts || []).length ||
        state.botNoteDismissed;
    }
  }

  function openAccountOverlay() {
    $("account-overlay").hidden = false;
    renderAccountOverlayBody();
  }

  function renderAccountOverlayBody() {
    var acc = state.account || {};
    $("account-body").innerHTML =
      "<p>Чтобы качать возрастной контент и не получать «подтвердите, что вы " +
      "не бот», качалке нужны куки вашей сессии YouTube.</p>" +
      '<p class="muted">Как это работает: откроется отдельное окно на ' +
      "странице входа YouTube - войдите там. Когда площадка выдаст сессию, " +
      "куки снимутся <b>сами</b>, окно закроется, а копия сохранится " +
      "<b>зашифрованной</b> (DPAPI - привязка к вашей Windows-учётке, на " +
      "другой машине файл бесполезен). Мы ничего не отправляем наружу: и " +
      "запросы, и хранение - только на этом компьютере. Войдя, вы " +
      "соглашаетесь с правилами площадки; аккаунт можно забыть в любой " +
      "момент.</p>" +
      '<p class="muted">Если Google откажет во входе из окна («This ' +
      "browser might not be secure») - путь через импорт cookies.txt. " +
      "Внимание: из свежих Chrome/Edge экспорт кук сломан (шифрование " +
      "v127+), для экспорта работает Firefox.</p>" +
      '<div data-account-facts></div>' +
      (acc.logging_in
        ? '<div class="notice">Идёт вход: войдите в аккаунте в открытом ' +
          "окне. Если окно закрыли или передумали - прервите и начните " +
          "заново.</div>" +
          '<div class="card-actions">' +
          '<button class="btn ghost" data-account-act="facts">' +
          "Проверить, что видит программа</button>" +
          '<button class="btn" data-account-act="close">Отмена</button>' +
          '<button class="btn primary" data-account-act="abort">' +
          "Прервать вход</button></div>"
        : '<div class="card-actions">' +
          '<button class="btn ghost" data-account-act="facts">' +
          "Проверить, что видит программа</button>" +
          '<button class="btn ghost" data-account-act="capture">' +
          "Я вошёл - забрать куки</button>" +
          '<button class="btn" data-account-act="import">' +
          "Импортировать cookies.txt…</button>" +
          '<button class="btn" data-account-act="close">Отмена</button>' +
          '<button class="btn primary" data-account-act="login">' +
          "Открыть окно входа</button></div>");
  }

  function importCookies() {
    call("account_import").then(function (res) {
      if (!res) return;
      if (res.error) { toast(res.error, true); return; }
      if (res.cancelled) return;
      toast("Аккаунт добавлен из файла");
    });
  }

  function startAccountLogin() {
    call("account_login_start").then(function (res) {
      if (res && res.error) { toast(res.error, true); return; }
      toast("Окно входа открыто - войдите в аккаунт, окно закроется само");
    });
  }

  function checkAccountVisible() {
    // Диагностика поимки: что окно входа видит сейчас (без значений кук).
    call("account_visible").then(function (res) {
      var box = document.querySelector("#account-body [data-account-facts]");
      if (!box) return;
      if (res && res.error) {
        box.innerHTML = '<div class="notice err">' + esc(res.error) + "</div>";
        return;
      }
      box.innerHTML = '<div class="notice">Окно видит кук: <b>' +
        (res.total || 0) + "</b> (Google/YouTube: " + (res.google || 0) +
        (res.format ? ", формат: " + esc(res.format) : "") +
        "), аккаунтских (SID и пр.): <b>" + (res.markers || 0) + "</b>.<br>" +
        '<span class="muted">Домены: ' +
        esc((res.domains || []).join(", ") || "нет") + "</span></div>" +
        ((res.markers || 0) === 0 && (res.total || 0) > 0
          ? '<div class="notice warn">Куки есть, но аккаунтских нет - ' +
            "скорее всего вы ещё не вошли на этой странице.</div>"
          : "");
    });
  }

  /* ------------------------------------------------------------------ *
   *  Перенос между хранилищами
   * ------------------------------------------------------------------ */

  function openMigrate() {
    if (!state.selected.size) {
      toast("Сначала выберите строки", true);
      return;
    }
    state.migratePreview = null;
    state.migrateTarget = state.migrateTarget || defaultStorageId();
    $("migrate-overlay").hidden = false;
    renderMigrateBody();
    refreshMigratePreview();
  }

  function refreshMigratePreview() {
    if (!state.migrateTarget) { renderMigrateBody(); return; }
    call("migrate_preview", { video_ids: Array.from(state.selected),
                              target_storage_id: state.migrateTarget })
      .then(function (res) {
        state.migratePreview = res;
        renderMigrateBody();
      });
  }

  function migrateList(items, key, showTarget) {
    if (!items || !items.length) return "";
    return '<div class="migrate-list">' + items.map(function (item) {
      var text = showTarget
        ? item.path + "  →  " + (item.target || "")
        : (item[key] || item.path || "");
      return '<div class="migrate-item' + (showTarget ? " warn" : "") +
        '" title="' + esc(text) + '">' + esc(text) + "</div>";
    }).join("") + "</div>";
  }

  function renderSchedule(sched) {
    // Таймер живёт, пока открыто окно; для работы без окна есть
    // omnistash.py --sync под планировщик Windows.
    var line = $("schedule-line");
    if (!line) return;
    if (!sched) { line.textContent = ""; return; }
    var part = function (key, name) {
      var entry = sched[key] || {};
      if (!entry.interval) return name + ": <b>выключен</b>";
      var text = name + ": каждые <b>" + entry.interval + " мин</b>";
      if (entry.next_in !== null && entry.next_in !== undefined) {
        // «12:00» читается как время - показываем в минутах.
        var minutes = Math.max(1, Math.round(entry.next_in / 60));
        text += " · следующий через " + minutes + " мин";
      }
      if (entry.last) text += " · последний " + shortTime(entry.last);
      return text;
    };
    line.innerHTML = part("scan", "Автоскан") + " &nbsp;·&nbsp; " +
      part("sync", "Автосинк") +
      " <span class=\"muted\">(работает, пока открыто окно; без окна — " +
      "omnistash.py --sync)</span>";
  }

  function renderMigrateBody() {
    var mig = state.migrate;
    var body = $("migrate-body");
    if (!body) return;
    var html = "";

    if (mig && mig.running) {
      var pct = mig.bytes_total
        ? Math.round(mig.bytes_done * 100 / mig.bytes_total) : 0;
      html = '<div class="migrate-progress">' +
        '<div class="scan-track"><div class="scan-fill" style="width:' + pct +
        '%"></div></div>' +
        '<div class="scan-text">' + mig.done + " / " + mig.total +
        " файл(ов) · " + humanSize(mig.bytes_done) + " из " +
        humanSize(mig.bytes_total) + "</div>" +
        '<div class="migrate-item">' + esc(mig.current || "") + "</div>" +
        "</div>" +
        '<div class="card-actions"><button class="btn" data-migrate="stop">' +
        "Остановить</button></div>";
      body.innerHTML = html;
      bindMigrate();
      return;
    }

    if (mig && mig.summary) {
      html = '<div class="notice">' + esc(mig.summary) + "</div>" +
        '<div class="card-actions"><button class="btn primary" ' +
        'data-migrate="close">Закрыть</button></div>';
      body.innerHTML = html;
      bindMigrate();
      return;
    }

    var preview = state.migratePreview;
    html = '<div class="migrate-target"><span class="muted">Перенести в:</span>' +
      '<select id="migrate-storage">' +
      storageOptionsHtml(state.migrateTarget) + "</select></div>";
    if (!preview) {
      html += '<div class="muted">Считаю, что и сколько переедет…</div>';
    } else if (preview.error) {
      html += '<div class="notice err">' + esc(preview.error) + "</div>" +
        '<div class="card-actions"><button class="btn" data-migrate="close">' +
        "Закрыть</button></div>";
    } else {
      html += '<dl class="migrate-facts">' +
        "<dt>Файлов</dt><dd>" + preview.count + "</dd>" +
        "<dt>Объём</dt><dd>" + humanSize(preview.bytes) + "</dd>" +
        "<dt>Конфликтов имён</dt><dd>" + preview.conflict_total + "</dd>" +
        "<dt>Уже на месте</dt><dd>" + preview.already + "</dd>" +
        "<dt>Не найдено на диске</dt><dd>" + preview.missing_total + "</dd>" +
        "</dl>";
      if (preview.conflict_total) {
        html += '<div class="notice warn">Конфликты пропускаются: в цели ' +
          "уже лежит файл с тем же именем, но другим содержимым.</div>" +
          migrateList(preview.conflicts, "target", true);
      }
      if (preview.missing_total) {
        html += '<div class="notice err">Этих файлов больше нет: их ' +
          "перенести некуда, следующий скан отметит их как пропавшие.</div>" +
          migrateList(preview.missing, "path", false);
      }
      html += '<div class="muted">Копия проверяется контрольной суммой, ' +
        "оригинал удаляется последним - только после успешной проверки. " +
        "Остановку можно нажать в любой момент.</div>" +
        '<div class="card-actions">' +
        '<button class="btn" data-migrate="close">Отмена</button>' +
        '<button class="btn primary" data-migrate="start"' +
        (preview.count ? "" : " disabled") + ">Начать перенос (" +
        preview.count + ")</button></div>";
    }
    body.innerHTML = html;
    bindMigrate();
  }

  function bindMigrate() {
    var body = $("migrate-body");
    var select = document.getElementById("migrate-storage");
    if (select) {
      select.addEventListener("change", function () {
        state.migrateTarget = select.value;
        state.migratePreview = null;
        renderMigrateBody();
        refreshMigratePreview();
      });
    }
    Array.prototype.forEach.call(
      body.querySelectorAll("[data-migrate]"), function (btn) {
        btn.addEventListener("click", function () {
          var action = btn.dataset.migrate;
          if (action === "close") { $("migrate-overlay").hidden = true; return; }
          if (action === "stop") { call("migrate_stop"); return; }
          if (action === "start") {
            call("migrate_start", { video_ids: Array.from(state.selected),
                                    target_storage_id: state.migrateTarget })
              .then(function (res) {
                if (!res) return;
                if (res.error) { toast(res.error, true); return; }
                toast("Перенос начат: " + res.count + " файл(ов)");
                clearSelection();
                renderMigrateBody();
              });
          }
        });
      });
  }

  /* ------------------------------------------------------------------ *
   *  Переупаковка по шаблону
   * ------------------------------------------------------------------ */

  function openRepack() {
    state.repackPreview = null;
    $("repack-overlay").hidden = false;
    renderRepackBody();
    refreshRepackPreview();
  }

  function refreshRepackPreview() {
    call("repack_preview", repackRequest()).then(function (res) {
      state.repackPreview = res;
      renderRepackBody();
    });
  }

  function repackRequest() {
    // Выделение сильнее области: отметили строки - переупаковываем их.
    if (state.selected.size) return { ids: Array.from(state.selected) };
    return { scope: state.scope };
  }

  function repackTargetLabel() {
    if (state.selected.size) return "выделенные строки: " + state.selected.size;
    var scope = state.scope || { type: "pool" };
    if (scope.type === "channel") {
      var ch = ((state.tree && state.tree.channels) || [])
        .filter(function (c) { return c.id === scope.id; })[0];
      return "канал «" + ((ch && ch.title) || scope.id) + "»";
    }
    if (scope.type === "playlist") {
      var pl = ((state.tree && state.tree.playlists) || [])
        .filter(function (p) { return p.id === scope.id; })[0];
      return "плейлист «" + ((pl && pl.title) || scope.id) + "»";
    }
    return "весь Пул";
  }

  function renderRepackBody() {
    var state_ = state.repack;
    var body = $("repack-body");
    if (!body) return;
    var html = "";

    if (state_ && state_.running) {
      var pct = state_.total ? Math.round(state_.done * 100 / state_.total) : 0;
      html = '<div class="migrate-progress">' +
        '<div class="scan-track"><div class="scan-fill" style="width:' + pct +
        '%"></div></div>' +
        '<div class="scan-text">' + state_.done + " / " + state_.total +
        " файл(ов)</div>" +
        '<div class="migrate-item">' + esc(state_.current || "") + "</div>" +
        "</div>" +
        '<div class="card-actions"><button class="btn" data-repack="stop">' +
        "Остановить</button></div>";
      body.innerHTML = html;
      bindRepack();
      return;
    }

    if (state_ && state_.summary) {
      html = '<div class="notice">' + esc(state_.summary) + "</div>";
      if (state_.errors && state_.errors.length) {
        html += '<div class="notice err">' +
          state_.errors.slice(0, 5).map(esc).join("<br>") + "</div>";
      }
      html += '<div class="card-actions"><button class="btn primary" ' +
        'data-repack="close">Закрыть</button></div>';
      body.innerHTML = html;
      bindRepack();
      return;
    }

    var preview = state.repackPreview;
    html = '<div class="muted">Область: ' + esc(repackTargetLabel()) + "</div>" +
      '<div class="migrate-item" title="Шаблон из настроек">шаблон: ' +
      esc(preview && preview.template ? preview.template : "") + "</div>";
    if (!preview) {
      html += '<div class="muted">Считаю, что и как переименуется…</div>';
    } else if (preview.error) {
      html += '<div class="notice err">' + esc(preview.error) + "</div>" +
        '<div class="card-actions"><button class="btn" data-repack="close">' +
        "Закрыть</button></div>";
    } else {
      html += '<dl class="migrate-facts">' +
        "<dt>Будет переименовано</dt><dd>" + preview.count + "</dd>" +
        "<dt>Уже по шаблону</dt><dd>" + preview.unchanged + "</dd>" +
        "<dt>Конфликтов имён</dt><dd>" + preview.conflict_total + "</dd>" +
        "<dt>Без метаданных</dt><dd>" + preview.no_meta_total + "</dd>" +
        "<dt>Объём</dt><dd>" + humanSize(preview.bytes) + "</dd>" +
        "</dl>";

      if (preview.rename.length) {
        html += '<div class="migrate-list">' + preview.rename.map(function (row) {
          return '<div class="migrate-item renamed"><b>' + esc(row.title || "") +
            '</b><span class="arrow">→</span><span>' + esc(row.to) + "</span>" +
            '<div class="muted">' + esc(row.from) + "</div></div>";
        }).join("") + "</div>";
        if (preview.rename_total > preview.rename.length) {
          html += '<div class="muted">… и ещё ' +
            (preview.rename_total - preview.rename.length) + "</div>";
        }
      }
      if (preview.conflict_total) {
        html += '<div class="notice warn">Конфликты пропускаются: в целевой ' +
          "папке уже лежит чужой файл с таким именем.</div>" +
          '<div class="migrate-list">' + preview.conflicts.map(function (row) {
            return '<div class="migrate-item warn" title="' + esc(row.target) +
              '">' + esc(row.title || row.path) + "</div>";
          }).join("") + "</div>";
      }
      if (preview.no_meta_total) {
        html += '<div class="notice">Без метаданных: этих файлов касается ' +
          "шаблон, но построить путь не из чего - они остаются как есть.</div>" +
          '<div class="migrate-list">' + preview.no_meta.map(function (row) {
            return '<div class="migrate-item warn">' + esc(row.title || "?") +
              " — " + esc(row.reason) + "</div>";
          }).join("") + "</div>";
      }

      html += '<div class="muted">Переименование идёт внутри того же ' +
        "хранилища: сайдкар, субтитры и обложка меняют имя вместе с видео, " +
        "папки создаются сами, пустые убираются. Отмена безопасна. Чтобы " +
        "уехать на другой диск - «Перенести…».</div>" +
        '<div class="card-actions">' +
        '<button class="btn" data-repack="close">Отмена</button>' +
        '<button class="btn primary" data-repack="start"' +
        (preview.count ? "" : " disabled") + ">Начать (" + preview.count +
        ")</button></div>";
    }
    body.innerHTML = html;
    bindRepack();
  }

  function bindRepack() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#repack-body [data-repack]"), function (btn) {
        btn.addEventListener("click", function () {
          var action = btn.dataset.repack;
          if (action === "close") { $("repack-overlay").hidden = true; return; }
          if (action === "stop") { call("repack_stop"); return; }
          if (action === "start") {
            call("repack_start", repackRequest()).then(function (res) {
              if (!res) return;
              if (res.error) { toast(res.error, true); return; }
              toast("Переименованию: " + res.count);
              renderRepackBody();
            });
          }
        });
      });
  }

  /* ------------------------------------------------------------------ *
   *  Сверка: возможные переезды и дубликаты
   * ------------------------------------------------------------------ */

  function openDedupe() {
    call("duplicates").then(function (data) {
      data = data || { groups: [], count: 0, files: 0 };
      // Разобранные строки показываем только до пересканирования.
      var moves = ((state.scan && state.scan.possible_moves) || [])
        .filter(function (move) {
          return !state.dedupeResolved.has(move.path);
        });
      $("dedupe-body").innerHTML = dedupeHtml(data, moves);
      $("dedupe-overlay").hidden = false;
      bindDedupe(data, moves);
      renderDedupeNote(state.scan);
    });
  }

  function dedupeHtml(data, moves) {
    var html = "";
    if (moves.length) {
      html += '<div class="field-label">Возможные переезды (' + moves.length + ")</div>" +
        '<div class="field-hint">Файл совпал по хешу с записью, которая ' +
        "указывает на недоступное хранилище: проверить нечем, поэтому я не " +
        "стал гадать. Скажите, что произошло.</div>" +
        '<div class="dedupe-group">' + moves.map(function (move, index) {
          return '<div class="dedupe-row gone">' +
            '<span class="dedupe-title">' + esc(move.title || move.path) + "</span>" +
            '<span class="dedupe-actions">' +
              '<button class="btn" data-move-keep="' + index + '">Переехал</button>' +
              '<button class="link-btn" data-move-copy="' + index + '">Это копия</button>' +
            "</span>" +
            '<span class="dedupe-meta"><span class="path">' + esc(move.path) +
            "</span><span>запись была в: " +
            esc(move.from || "неизвестном месте") + "</span></span>" +
            "</div>";
        }).join("") + "</div>";
    }

    if (data.count) {
      html += '<div class="field-label">Дубликаты (' + data.count +
        " групп, " + data.files + " файлов)</div>" +
        '<div class="field-hint">Оставьте одну копию - остальные будут ' +
        "удалены с диска вместе с их сайдкарами.</div>" +
        '<div class="dedupe-group">' + data.groups.map(function (group) {
          return '<div class="dedupe-row" data-group="' + group.video_id + '">' +
            '<span class="dedupe-title" title="' + esc(group.title) + '">' +
            esc(group.title || group.key) + "</span>" +
            '<span class="dedupe-actions">' +
              '<button class="btn" data-dedupe="' + group.video_id +
              '">Оставить выбранную</button></span>' +
            '<span class="dedupe-meta">' + group.files.map(function (file, index) {
              return "<label><input type=\"radio\" name=\"keep-" + group.video_id +
                "\" value=\"" + file.id + "\"" + (index === 0 ? " checked" : "") +
                '><span class="path">' + esc(file.path) + "</span><span>" +
                humanSize(file.size) +
                (file.storage ? " · " + esc(file.storage) : "") +
                "</span></label>";
            }).join("") + "</span></div>";
        }).join("") + "</div>";
    }

    if (!html) {
      html = '<div class="muted">Копий не найдено: у каждого видео на диске ' +
        "лежит одна копия.</div>";
    }
    return html;
  }

  function bindDedupe(data, moves) {
    var body = $("dedupe-body");

    Array.prototype.forEach.call(
      body.querySelectorAll("[data-move-keep]"), function (btn) {
        btn.addEventListener("click", function () {
          var move = moves[Number(btn.dataset.moveKeep)];
          if (!move) return;
          call("dedupe_resolve", { video_id: move.video_id, keep_path: move.path })
            .then(function (res) {
              if (!res || res.error) { toast(res && res.error, true); return; }
              state.dedupeResolved.add(move.path);
              toast("Оставлена копия: " + res.kept);
              openDedupe();
            });
        });
      });

    Array.prototype.forEach.call(
      body.querySelectorAll("[data-move-copy]"), function (btn) {
        btn.addEventListener("click", function () {
          var row = btn.closest(".dedupe-row");
          var index = Number(btn.dataset.moveCopy);
          var move = moves[index];
          if (move) state.dedupeResolved.add(move.path);
          if (row) row.remove();
          toast("Принято: пара остаётся в разделе «Дубликаты»");
          renderDedupeNote(state.scan);
        });
      });

    Array.prototype.forEach.call(
      body.querySelectorAll("[data-dedupe]"), function (btn) {
        btn.addEventListener("click", function () {
          var row = btn.closest(".dedupe-row");
          var keep = row ? row.querySelector("input:checked") : null;
          if (!keep) { toast("Выберите, какую копию оставить", true); return; }
          var label = keep.closest("label");
          var where = label ? label.querySelector(".path").textContent : "";
          if (!confirm("Удалить остальные копии с диска?\n\nОстанется:\n" + where)) {
            return;
          }
          call("dedupe_resolve", {
            video_id: Number(btn.dataset.dedupe),
            keep_file_id: Number(keep.value)
          }).then(function (res) {
            if (!res || res.error) { toast(res && res.error, true); return; }
            toast("Оставлена копия: " + res.kept +
              (res.errors && res.errors.length ? " (часть не удалилась)" : ""));
            openDedupe();
          });
        });
      });
  }

  function renderTree(tree) {
    if (!tree) return;
    state.tree = tree;   // названия каналов/плейлистов для диалогов
    var current = JSON.stringify(state.scope);
    var html = [];
    var pool = tree.pool || {};
    html.push(item({ type: "pool" }, "📥", "Пул", pool.total, current));

    html.push('<div class="tree-group">Каналы</div>');
    if (!(tree.channels || []).length) {
      html.push('<div class="tree-item muted"><span class="name">нет каналов</span></div>');
    }
    (tree.channels || []).forEach(function (ch) {
      html.push(item({ type: "channel", id: ch.id }, "▶", ch.title || "Без названия",
        ch.total, current, ch.downloaded));
    });

    html.push('<div class="tree-group">Плейлисты</div>');
    if (!(tree.playlists || []).length) {
      html.push('<div class="tree-item muted"><span class="name">нет плейлистов</span></div>');
    }
    (tree.playlists || []).forEach(function (pl) {
      html.push(item({ type: "playlist", id: pl.id }, "☰", pl.title || "Без названия",
        pl.total, current, pl.downloaded));
    });

    $("tree").innerHTML = html.join("");

    Array.prototype.forEach.call($("tree").querySelectorAll(".tree-item[data-scope]"),
      function (node) {
        node.addEventListener("click", function () {
          state.scope = JSON.parse(node.dataset.scope);
          state.pages = {};
          renderTree(tree);
          renderGrid(true);
        });
      });

    function item(scope, ico, name, total, current, done) {
      var active = JSON.stringify(scope) === current ? " active" : "";
      var count = total === undefined || total === null ? "" :
        (done !== undefined && done !== null && done !== total
          ? done + "/" + total : String(total));
      return '<div class="tree-item' + active + '" data-scope=\'' +
        JSON.stringify(scope).replace(/'/g, "&apos;") + '\'>' +
        '<span class="ico">' + ico + "</span>" +
        '<span class="name" title="' + esc(name) + '">' + esc(name) + "</span>" +
        '<span class="count">' + count + "</span></div>";
    }
  }

  /* ------------------------------------------------------------------ *
   *  Таблица библиотеки (виртуализация): список и плитка с превью
   * ------------------------------------------------------------------ */

  function tileConf() {
    return TILE[state.tileSize] || TILE.medium;
  }

  function tileCols() {
    // Колонок столько, сколько влезло: ширина окна решает, не настройка.
    var cfg = tileConf();
    var width = $("grid-body").clientWidth - 2;   // минус рамка контейнера
    return Math.max(1, Math.floor((width + cfg.gap) / (cfg.w + cfg.gap)));
  }

  function applyViewState() {
    // Кнопки вида/размера и служебные элементы - по текущим настройкам.
    var grid = state.viewMode === "grid";
    $("view-list-btn").classList.toggle("active", !grid);
    $("view-grid-btn").classList.toggle("active", grid);
    $("size-toggle").hidden = !grid;
    $("grid-head").hidden = grid;      // заголовки колонок списку нужны
    Array.prototype.forEach.call(
      document.querySelectorAll("#size-toggle .size-btn"), function (btn) {
        btn.classList.toggle("active", btn.dataset.size === state.tileSize);
      });
  }

  // -- превью: локальные обложки, грузятся по появлению на экране ---------

  var thumbCache = new Map();     // id -> dataURI ("" = нет/не читается)
  var thumbQueue = new Set();     // id, ждущие батча
  var thumbTimer = null;

  function rememberThumb(id, uri) {
    if (thumbCache.has(id)) thumbCache.delete(id);   // LRU: свежие в конец
    thumbCache.set(id, uri);
    if (thumbCache.size > THUMB_CACHE_MAX) {
      thumbCache.delete(thumbCache.keys().next().value);
    }
  }

  function paintCachedThumbs() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#grid-rows img.tile-thumb[data-vid]"),
      function (img) {
        var cached = thumbCache.get(Number(img.dataset.vid));
        if (cached === undefined) return;             // ещё ждём ответа
        if (cached) img.src = cached;
        else img.replaceWith(noThumbNode());
        img.removeAttribute("data-vid");
      });
  }

  function noThumbNode() {
    var div = document.createElement("div");
    div.className = "tile-thumb tile-nothumb";
    div.innerHTML = "<span>нет обложки</span>";
    return div;
  }

  function scheduleThumbLoad() {
    if (thumbTimer) return;
    thumbTimer = setTimeout(function () {
      thumbTimer = null;
      var ids = Array.from(thumbQueue).slice(0, THUMB_BATCH);
      ids.forEach(function (id) { thumbQueue.delete(id); });
      if (!ids.length) return;
      call("get_thumbs", { ids: ids }).then(function (res) {
        var thumbs = (res && res.thumbs) || {};
        Object.keys(thumbs).forEach(function (key) {
          rememberThumb(Number(key), thumbs[key]);
        });
        // Чего не было в ответе (нет файла/битая) - кэшируем пустышкой,
        // чтобы не спрашивать одно и то же на каждом скролле.
        ids.forEach(function (id) {
          if (!(String(id) in thumbs)) rememberThumb(id, "");
        });
        paintCachedThumbs();
      }).catch(function () {
        ids.forEach(function (id) { thumbQueue.delete(id); });
      });
    }, 120);
  }

  function requestThumbsForWindow() {
    // Грузим обложки ровно для окна виртуализации: оно по определению
    // равно видимой области плюс один ряд запаса на быстрый скролл, то
    // есть «появилось на экране». IntersectionObserver здесь не берём:
    // он не отдаёт события в скрытых документах (проверено в превью), а
    // виртуализация и так не рисует невидимого.
    Array.prototype.forEach.call(
      document.querySelectorAll("#grid-rows img.tile-thumb[data-vid]"),
      function (img) {
        var id = Number(img.dataset.vid);
        var cached = thumbCache.get(id);
        if (cached !== undefined) {
          if (cached) img.src = cached;
          else img.replaceWith(noThumbNode());
          img.removeAttribute("data-vid");
          return;
        }
        thumbQueue.add(id);
      });
    if (thumbQueue.size) scheduleThumbLoad();
  }

  /* ------------------------------------------------------------------ *
   *  Хранилища, мультивыбор и стартовый диалог
   * ------------------------------------------------------------------ */

  function activeStorages() {
    return (state.storages || []).filter(function (storage) {
      return storage.status === "active" && storage.enabled;
    });
  }

  function defaultStorageId() {
    var active = activeStorages();
    if (!active.length) return "";
    var chosen = state.settings.default_storage_id;
    if (chosen && active.some(function (s) { return s.id === chosen; })) {
      return chosen;
    }
    // Одно хранилище - сомневаться не о чем; несколько - выбираем первое
    // видимым селектом, пользователь всегда может поменять.
    return active[0].id;
  }

  function storageOptionsHtml(selected) {
    var active = activeStorages();
    if (!active.length) {
      return '<option value="">нет хранилищ - добавьте в настройках</option>';
    }
    if (!selected) selected = defaultStorageId();
    return active.map(function (storage) {
      var label = storage.label + (storage.available ? "" : " (нет носителя)");
      return '<option value="' + storage.id + '"' +
        (storage.id === selected ? " selected" : "") + ">" +
        esc(label) + "</option>";
    }).join("");
  }

  function fillStorageSelects() {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-storage-select]"), function (el) {
        var preferred = el.id === "sel-storage"
          ? (state.selStorage || defaultStorageId())
          : (el.dataset.prefer || defaultStorageId());
        var current = el.value || preferred;
        el.innerHTML = storageOptionsHtml(current);
        if (current && !el.value) el.value = current;
      });
  }

  function renderStorages(list) {
    state.storages = list || [];
    fillStorageSelects();
    updateWizard();
    // Виджет в настройках рисуется из этого списка - перерисовываем,
    // если карточка открыта (иначе кнопки останутся на старых данных).
    if (state.tab === "settings") renderSettings();
  }

  function renderSelBar() {
    var count = state.selected.size;
    $("sel-bar").hidden = count === 0;
    $("sel-count").textContent = "Выбрано: " + count;
    fillStorageSelects();
  }

  function toggleSelected(id, on, node) {
    if (on) state.selected.add(id);
    else state.selected.delete(id);
    if (node) node.classList.toggle("picked", on);
    renderSelBar();
  }

  function clearSelection() {
    state.selected.clear();
    var all = $("sel-all");
    if (all) all.checked = false;
    renderSelBar();
    paintGrid();
  }

  function updateWizard() {
    // Первый запуск (и пока хранилищ нет): спрашиваем, где хранить.
    var need = (state.storages || []).length === 0 && !state.wizardDismissed;
    $("storage-overlay").hidden = !need;
  }

  function enqueueSelection() {
    var ids = Array.from(state.selected);
    if (!ids.length) return;
    var storageId = $("sel-storage").value;
    if (!storageId) {
      toast("Не выбрано хранилище - добавьте папку в настройках", true);
      switchTab("settings");
      return;
    }
    call("enqueue", { ids: ids, storage_id: storageId }).then(function (res) {
      if (!res) return;
      if (res.error) { toast(res.error, true); return; }
      toast("В очередь поставлено: " + res.queued);
      clearSelection();
      switchTab("queue");
    });
  }

  function renderGrid(reset) {
    if (reset) {
      state.pages = {};
      state.total = 0;
      $("grid-body").scrollTop = 0;
      $("grid-rows").style.transform = "translateY(0)";
      // Смена контекста (фильтр/канал/поиск) сбрасывает выделение:
      // id из другого набора в очереди ничего не значат.
      state.selected.clear();
      renderSelBar();
    }
    loadPage(0);
  }

  function ensureRange(from, to, done) {
    var first = Math.max(0, Math.floor(from / PAGE));
    var last = Math.floor(Math.max(0, to - 1) / PAGE);
    var missing = [];
    for (var i = first; i <= last; i++) if (!state.pages[i]) missing.push(i);
    if (!missing.length) { done(); return; }
    Promise.all(missing.map(function (page) {
      return call("list_videos", {
        scope: state.scope, status: state.status, query: state.query,
        rating_min: state.ratingMin || null,
        offset: page * PAGE, limit: PAGE
      }).then(function (res) {
        if (res) {
          state.pages[page] = res.rows || [];
          state.total = res.total || 0;
        }
        return res;
      });
    })).then(function () { done(); }).catch(function () { done(); });
  }

  function loadPage(page) {
    if (state.pages[page]) { paintGrid(); return; }
    ensureRange(page * PAGE, page * PAGE + PAGE, paintGrid);
  }

  function paintGrid() {
    var body = $("grid-body");
    var rows = $("grid-rows");
    var total = state.total;
    var grid = state.viewMode === "grid";
    var cfg = tileConf();
    var cols = grid ? tileCols() : 1;
    var rowH = grid ? cfg.row : ROW_H;
    var lineCount = grid ? Math.ceil(total / cols) : total;
    rows.classList.toggle("tiles", grid);
    $("grid-spacer").style.height = (lineCount * rowH) + "px";
    $("list-total").textContent = total
      ? total + " " + plural(total, ["запись", "записи", "записей"]) : "";

    var empty = $("grid-empty");
    if (!total) {
      empty.hidden = false;
      empty.innerHTML = hasLibrary()
        ? "Ничего не найдено.<br><span class=\"muted\">Измените фильтр или запрос.</span>"
        : "Библиотека пуста.<br><span class=\"muted\">Укажите папки в настройках и нажмите «Переиндексировать».</span>";
      rows.innerHTML = "";
      return;
    }
    empty.hidden = true;

    // Окно видимого с запасом: в списке - строки, в плитке - ряды плиток.
    var overscan = grid ? 1 : 4;
    var start = Math.max(0, Math.floor(body.scrollTop / rowH) - overscan);
    var end = Math.min(lineCount,
      Math.ceil((body.scrollTop + body.clientHeight) / rowH) + overscan);
    var firstItem = start * cols;
    var lastItem = Math.min(total, end * cols);
    ensureRange(firstItem, lastItem, function () {
      var html = [];
      var index;
      if (grid) {
        // Колонки фиксированной ширины из настройки (М/С/К), а не 1fr:
        // плитка должна быть ровно cfg.w, иначе смена размера ничего не
        // меняет визуально. minmax(0, Wpx) - на узком окне колонка
        // сжимается, не вылезая за край.
        rows.style.gridTemplateColumns = "repeat(" + cols + ", minmax(0, " +
          cfg.w + "px))";
        rows.style.gap = cfg.gap + "px";
        for (index = firstItem; index < lastItem; index++) {
          var tile = rowAt(index);
          if (tile) html.push(tileHtml(tile, index));
        }
      } else {
        for (index = start; index < end; index++) {
          var row = rowAt(index);
          if (row) html.push(rowHtml(row, index));
        }
      }
      rows.style.transform = "translateY(" + (start * rowH) + "px)";
      rows.innerHTML = html.join("");
      bindGridItems();
      if (grid) requestThumbsForWindow();
    });
  }

  function bindGridItems() {
    // Один биндинг на список и плитку: одиночный клик - карточка, двойной -
    // воспроизведение. Задержка нужна, чтобы отличить одно нажатие от двух.
    Array.prototype.forEach.call(
      document.querySelectorAll("#grid-rows .row, #grid-rows .tile"),
      function (node) {
        var timer = null;
        node.addEventListener("click", function (event) {
          if (event.target.closest(".row-check, [data-play]")) return;
          if (timer) { clearTimeout(timer); timer = null; return; }  // ждём второго
          timer = setTimeout(function () {
            timer = null;
            openDetail(Number(node.dataset.id));
          }, 240);
        });
        node.addEventListener("dblclick", function (event) {
          if (event.target.closest(".row-check")) return;
          if (timer) { clearTimeout(timer); timer = null; }
          playVideo(Number(node.dataset.id));
        });
        // Чекбокс переключает выделение и НЕ открывает карточку.
        var box = node.querySelector(".row-check");
        if (box) {
          box.addEventListener("click", function (event) {
            event.stopPropagation();
            toggleSelected(Number(box.dataset.id), box.checked, node);
          });
        }
        var play = node.querySelector("[data-play]");
        if (play) {
          play.addEventListener("click", function (event) {
            event.stopPropagation();
            playVideo(Number(play.dataset.play));
          });
        }
      });
  }

  function playVideo(id) {
    // Системный плеер по умолчанию: файл открывает Windows, не окно.
    call("open_file", { id: id }).then(function (res) {
      if (res && res.error) toast(res.error, true);
    });
  }

  function rowAt(index) {
    var page = Math.floor(index / PAGE);
    var list = state.pages[page];
    if (!list) return null;
    return list[index - page * PAGE] || null;
  }

  function rowHtml(row, index) {
    var picked = state.selected.has(row.id);
    // Рейтинг - значок рядом с названием: видно, что размечено, не открывая
    // карточку; полные пометки живут там же.
    var stars = starBadge(row);
    var watched = watchBadge(row);
    return '<div class="row' + (picked ? " picked" : "") + '" data-id="' +
      row.id + '" data-i="' + index + '">' +
      '<span class="c-check"><input type="checkbox" class="row-check" data-id="' +
        row.id + '"' + (picked ? " checked" : "") +
        ' aria-label="Выбрать строку"></span>' +
      '<span class="cell status" data-s="' + esc(row.status) + '">' +
        esc(row.status_label || statusLabel(row.status)) + "</span>" +
      '<span class="cell c-title" title="' + esc(row.title) + '">' +
        esc(row.title || "—") + stars + watched + "</span>" +
      '<span class="cell c-channel" title="' + esc(row.channel) + '">' + esc(row.channel || "—") + "</span>" +
      '<span class="cell c-dur">' + humanDuration(row.duration_s) + "</span>" +
      '<span class="cell c-date">' + shortDate(row.uploaded_at) + "</span>" +
      '<span class="cell c-size">' + (row.size ? humanSize(row.size) : "—") + "</span>" +
      "</div>";
  }

  function starBadge(row) {
    return row.user_rating
      ? '<span class="star-badge" title="рейтинг ' + row.user_rating +
        '">★' + row.user_rating + "</span>"
      : "";
  }

  function watchBadge(row) {
    return row.watched_at
      ? '<span class="star-badge" title="просмотрено" style="color:var(--ok)">✓</span>'
      : "";
  }

  function tileHtml(row, index) {
    // Плитка: обложка 16:9 (грузится лениво, см. observeThumbs), честная
    // заглушка, если обложки нет, и те же пометки, что в списке.
    var picked = state.selected.has(row.id);
    var thumb = row.has_thumb
      ? '<img class="tile-thumb" data-vid="' + row.id + '" alt="" ' +
        'draggable="false">'
      : '<div class="tile-thumb tile-nothumb"><span>нет обложки</span></div>';
    return '<div class="tile' + (picked ? " picked" : "") + '" data-id="' +
      row.id + '" data-i="' + index + '">' +
      '<span class="c-check tile-check-wrap"><input type="checkbox" ' +
        'class="row-check tile-check" data-id="' + row.id + '"' +
        (picked ? " checked" : "") + ' aria-label="Выбрать плитку"></span>' +
      thumb +
      '<button class="tile-play" data-play="' + row.id +
        '" title="Открыть в плеере" aria-label="Открыть в плеере">▶</button>' +
      '<span class="tile-badges">' +
        '<span class="status status-dot" data-s="' + esc(row.status) +
        '" title="' + esc(row.status_label || statusLabel(row.status)) +
        '"></span>' + starBadge(row) + watchBadge(row) + "</span>" +
      '<div class="tile-title" title="' + esc(row.title) + '">' +
        esc(row.title || "—") + "</div>" +
      '<div class="tile-sub" title="' + esc(row.channel) + '">' +
        esc(row.channel || "") +
        (row.duration_s ? " · " + humanDuration(row.duration_s) : "") +
      "</div>" +
      "</div>";
  }

  function hasLibrary() {
    return !!(state.settings && state.settings.library_roots &&
              state.settings.library_roots.length);
  }

  /* ------------------------------------------------------------------ *
   *  Карточка видео
   * ------------------------------------------------------------------ */

  function openDetail(id) {
    call("get_video", id).then(function (data) {
      if (!data) return;
      $("detail-title").textContent = data.title || "Без названия";
      var kv = [];
      var add = function (label, value) {
        if (value === null || value === undefined || value === "") return;
        kv.push("<dt>" + esc(label) + "</dt><dd>" + esc(value) + "</dd>");
      };
      add("Статус", data.status_label || statusLabel(data.status));
      add("Ключ", data.key);
      add("Канал", data.channel);
      add("Загружено", shortDate(data.uploaded_at));
      add("Длительность", data.duration_s ? humanDuration(data.duration_s) : "");
      add("Просмотров", data.view_count === null ? "" : Number(data.view_count).toLocaleString("ru-RU"));
      add("Происхождение", data.origin);
      add("URL", data.webpage_url);
      add("Первый раз замечено", shortTime(data.first_seen_at));
      add("Скачано", shortTime(data.downloaded_at));
      add("Обновлено", shortTime(data.updated_at));
      add("Плейлисты", (data.playlists || []).map(function (p) {
        return p.title + (p.removed_at ? " (убран)" : " #" + p.position);
      }).join(", "));
      add("Файлы", (data.files || []).map(function (f) {
        return f.kind + ": " + f.path + (f.missing ? " [пропал]" : "");
      }).join("\n"));

      var html = '<dl class="kv">' + kv.join("") + "</dl>";

      // Действия по статусу: качаем только то, чего нет; скачанное -
      // открываем там, где оно лежит.
      var actions = [];
      if (["known", "failed", "missing", "detached"].indexOf(data.status) >= 0) {
        // Выбор хранилища - здесь же: фоновая очередь спрашивать не может.
        actions.push('<select class="detail-storage" data-storage-select ' +
          'data-prefer="' + esc(defaultStorageId()) + '" aria-label="Хранилище">' +
          storageOptionsHtml(defaultStorageId()) + "</select>");
        actions.push('<button class="btn primary" data-detail="download">Скачать</button>');
      }
      if (data.status === "queued" || data.status === "downloading") {
        actions.push('<button class="btn" data-detail="queue">К очереди</button>');
      }
      if (data.status === "downloaded") {
        var localFile = (data.files || []).filter(function (f) {
          return f.kind === "video" && !f.missing;
        })[0];
        if (localFile) {
          actions.push('<button class="btn primary" data-detail="play">' +
            "Открыть</button>");
          actions.push('<button class="btn" data-detail="folder">Открыть папку</button>');
        }
      }
      if (data.webpage_url) {
        actions.push('<button class="btn ghost" data-detail="source">Открыть на площадке</button>');
      }
      if (actions.length) {
        html = '<div class="card-actions detail-actions">' +
          actions.join("") + "</div>" + html;
      }

      var raw = data.raw && Object.keys(data.raw).length
        ? '<div class="muted">raw_json (полные метаданные площадки)</div>' +
          '<pre class="json">' + esc(JSON.stringify(data.raw, null, 1)) + "</pre>"
        : '<div class="muted">raw_json пока пуст: карточка заполнится после ' +
          "синхронизации или загрузки.</div>";
      $("detail-body").innerHTML = detailThumbHtml(data) + html +
        detailMarksHtml(data) + raw;
      $("detail-overlay").hidden = false;
      bindDetail(data);
    });
  }

  function detailThumbHtml(data) {
    return '<div class="detail-thumb" id="detail-thumb">' +
      '<div class="muted">обложка…</div></div>';
  }

  function starButtons(value) {
    var html = "";
    for (var i = 1; i <= 5; i++) {
      html += '<button type="button" class="star' + (i <= value ? " on" : "") +
        '" data-star="' + i + '" title="' + i + '">★</button>';
    }
    return html;
  }

  function detailMarksHtml(data) {
    var rating = Number(data.user_rating || 0);
    return '<div class="detail-edit">' +
      '<div class="detail-tools"><span>Рейтинг</span>' +
      '<span class="stars" data-stars data-touched="" data-value="' + rating +
        '">' + starButtons(rating) + "</span>" +
      '<span class="muted">повторный клик по звезде снимает оценку</span></div>' +
      '<div class="detail-tools">' +
      '<label class="check"><input type="checkbox" id="detail-watched"' +
        (data.watched_at ? " checked" : "") +
        "><span>просмотрено</span></label></div>" +
      '<label>Теги (через запятую)<input type="text" id="detail-tags" ' +
        'value="' + esc(data.user_tags || "") +
        '" placeholder="живое, избранное"></label>' +
      '<label>Заметка<textarea id="detail-notes" rows="2">' +
        esc(data.notes || "") + "</textarea></label>" +
      '<div class="card-actions"><button class="btn primary" ' +
        'data-detail="save-marks">Сохранить пометки</button></div>' +
      "</div>";
  }

  function bindStars(root, onPick) {
    if (!root) return;
    Array.prototype.forEach.call(root.querySelectorAll(".star"),
      function (btn) {
        btn.addEventListener("click", function () {
          var value = Number(root.dataset.value || 0);
          var picked = Number(btn.dataset.star);
          // Повторный клик по той же звезде снимает оценку.
          value = (value === picked) ? 0 : picked;
          root.dataset.value = value;
          root.dataset.touched = "1";
          Array.prototype.forEach.call(root.querySelectorAll(".star"),
            function (star) {
              star.classList.toggle("on", Number(star.dataset.star) <= value);
            });
          if (onPick) onPick(value);
        });
      });
  }

  function loadThumb() {
    // Обложка отдаётся data-URI из локального файла: так не летит ни один
    // сетевой запрос на площадку ради картинки.
    var box = document.getElementById("detail-thumb");
    if (!box) return;
    var id = box.dataset.vid;
    call("get_thumb", { id: Number(id) }).then(function (res) {
      if (res && res.ok) {
        box.innerHTML = '<img src="' + res.data + '" alt="обложка">';
      } else {
        box.innerHTML = '<div class="muted">' +
          esc((res && res.reason) || "обложки нет") + "</div>";
      }
    });
  }

  function detailFields() {
    var fields = {};
    var stars = document.querySelector("#detail-body [data-stars]");
    if (stars && stars.dataset.touched) {
      fields.user_rating = Number(stars.dataset.value || 0);
    }
    var watched = document.getElementById("detail-watched");
    if (watched && watched.dataset.touched) fields.watched = watched.checked;
    var tags = document.getElementById("detail-tags");
    if (tags && tags.value.trim()) fields.user_tags = tags.value;
    var notes = document.getElementById("detail-notes");
    if (notes && notes.value.trim()) fields.notes = notes.value;
    return fields;
  }

  function bindDetail(data) {
    var thumb = document.getElementById("detail-thumb");
    if (thumb) thumb.dataset.vid = data.id;
    bindStars(document.querySelector("#detail-body [data-stars]"));
    var watched = document.getElementById("detail-watched");
    if (watched) {
      watched.addEventListener("change", function () {
        watched.dataset.touched = "1";
      });
    }
    loadThumb();

    Array.prototype.forEach.call(
      document.querySelectorAll("#detail-body [data-detail]"), function (btn) {
        btn.addEventListener("click", function () {
          var action = btn.dataset.detail;
          if (action === "save-marks") {
            var fields = detailFields();
            if (!Object.keys(fields).length) {
              toast("Ничего не изменено", true);
              return;
            }
            call("save_fields", { ids: [data.id], fields: fields })
              .then(function (res) {
                if (!res || res.error) { toast(res && res.error, true); return; }
                toast("Пометки сохранены");
                openDetail(data.id);      // перечитать: строки в списке обновятся
                renderGrid();
              });
            return;
          }
          if (action === "download") {
            var pick = document.querySelector(
              "#detail-body [data-storage-select]");
            var storageId = pick ? pick.value : defaultStorageId();
            call("enqueue", { ids: [data.id], storage_id: storageId })
              .then(function (res) {
                if (res && res.error) { toast(res.error, true); return; }
                toast("В очередь поставлено: " + (res ? res.queued : 0));
                $("detail-overlay").hidden = true;
                switchTab("queue");
              });
          } else if (action === "queue") {
            $("detail-overlay").hidden = true;
            switchTab("queue");
          } else if (action === "folder") {
            // Проводник открывается с выделенным самим файлом - так видно,
            // что именно лежит на диске.
            call("open_folder", { id: data.id }).then(function (res) {
              if (res && res.error) toast(res.error, true);
            });
          } else if (action === "play") {
            playVideo(data.id);
          } else if (action === "source" && data.webpage_url) {
            call("open_url", data.webpage_url);
          }
        });
      });
  }

  /* ------------------------------------------------------------------ *
   *  Вкладки: синхронизация, очередь, настройки
   * ------------------------------------------------------------------ */

  function renderSources(list) {
    var body = $("sources-body");
    if (!list) return;
    $("sources-empty").hidden = list.length > 0;
    body.innerHTML = list.map(function (src) {
      return "<tr>" +
        "<td>" + esc(src.title || src.remote_id) + "</td>" +
        "<td>" + esc(src.kind_label) + "</td>" +
        "<td>" + modeLabel(src.sync_mode) + "</td>" +
        // Назначение источника: куда уйдут «Полная» и будущие синки; аккаунт -
        // чьи куки уходят в синк/загрузку этого источника.
        '<td class="muted">' + esc(src.storage_label || "по умолчанию") +
        (src.account_label
          ? '<div class="muted">аккаунт: ' + esc(src.account_label) + "</div>"
          : "") + "</td>" +
        '<td class="num">' + (src.total || 0) + "</td>" +
        '<td class="num">' + (src.downloaded || 0) + "</td>" +
        '<td class="num">' + (src.pending || 0) + "</td>" +
        "<td>" + shortTime(src.last_synced_at) + "</td>" +
        "</tr>";
    }).join("");
  }

  function modeLabel(mode) {
    return { full: "Полная", partial: "Частичная", manual: "Ручная" }[mode] || mode;
  }

  function renderRuns(list) {
    var body = $("runs-body");
    if (!list) return;
    $("runs-empty").hidden = list.length > 0;
    body.innerHTML = list.map(function (run) {
      var stats = run.stats || {};
      var summary = Object.keys(stats).map(function (k) {
        return k + ": " + stats[k];
      }).join(", ") || "—";
      return "<tr><td>" + esc(run.kind) + "</td><td>" + shortTime(run.started_at) +
        "</td><td>" + shortTime(run.finished_at) + "</td><td>" +
        esc(summary) + "</td></tr>";
    }).join("");
  }

  function renderQueue(list) {
    var body = $("queue-body");
    if (!list) return;
    $("queue-empty").hidden = list.length > 0;
    body.innerHTML = list.map(function (row) {
      // Причина падения живёт в строке: «ошибка» без объяснения вынуждает
      // лезть в журнал, а там её уже нет (конец очереди, другой запуск).
      var reason = row.last_error
        ? '<div class="row-reason" title="' + esc(row.last_error) + '">' +
          esc(row.last_error) + "</div>"
        : "";
      var retry = row.status === "failed"
        ? '<div class="row-retry"><button class="link-btn" data-retry="' +
          row.id + '">повторить эту</button></div>'
        : "";
      return "<tr><td><span class=\"status\" data-s=\"" + esc(row.status) + "\">" +
        esc(row.status_label) + "</span></td>" +
        "<td>" + esc(row.title || "—") + reason + "</td>" +
        '<td class="muted">' + esc(row.key) + retry + "</td></tr>";
    }).join("");
    Array.prototype.forEach.call(
      body.querySelectorAll("[data-retry]"), function (btn) {
        btn.addEventListener("click", function () {
          // Без storage_id: цель остаётся прежней (coalesce в enqueue).
          call("enqueue", { ids: [Number(btn.dataset.retry)] })
            .then(function (res) {
              if (!res) return;
              if (res.error) { toast(res.error, true); return; }
              toast(res.queued ? "В очередь снова: " + res.queued
                               : "Не удалось поставить в очередь");
            });
        });
      });
  }

  function humanSpeed(bytes) {
    return humanSize(bytes) + "/с";
  }

  var STAGE_TITLES = { playlist: "Плейлист", channels: "Авторы",
                       videos: "Видео", links: "Связи" };

  function renderSync(sync) {
    if (!sync) return;
    $("sync-all-btn").hidden = !!sync.running;
    $("sync-stop-btn").hidden = !sync.running;

    var results = sync.results || [];
    var bar = $("sync-bar");
    if (sync.running) {
      bar.hidden = false;
      var percent = 0;
      if (sync.fetch && sync.fetch.total) {
        percent = Math.round(sync.fetch.got * 100 / sync.fetch.total);
      } else if (sync.stage && sync.stage.total) {
        percent = Math.round(sync.stage.current * 100 / sync.stage.total);
      } else if (sync.total) {
        percent = Math.round(Math.max(0, (sync.index || 0) - 1) * 100 / sync.total);
      }
      $("sync-fill").style.width = percent + "%";
      $("sync-title").textContent = sync.current || "…";
      var sub = "источник " + Math.max(sync.index || 0, 1) + " / " + (sync.total || 1);
      if (sync.fetch && sync.fetch.total) {
        sub += " · записей " + sync.fetch.got + " / " + sync.fetch.total;
      }
      if (sync.stage && STAGE_TITLES[sync.stage.id]) {
        sub += " · " + STAGE_TITLES[sync.stage.id] +
          (sync.stage.total ? " " + sync.stage.current + " / " + sync.stage.total : "");
      }
      $("sync-sub").textContent = sub;
      $("sync-counters").textContent = results.length
        ? "новых " + sync.new_total : "";
    } else if (results.length) {
      bar.hidden = false;
      $("sync-fill").style.width = "100%";
      $("sync-title").textContent = sync.error ? "Синхронизация прервана" : "Синхронизация завершена";
      $("sync-sub").textContent = sync.error ? sync.error : "";
      $("sync-counters").textContent = "источников " + results.length +
        " / " + (sync.total || results.length) + " · новых " + (sync.new_total || 0);
    } else {
      bar.hidden = true;
    }

    // Итог: таблица по источникам + кнопка постановки новых в очередь.
    var wrap = $("sync-results-wrap");
    wrap.hidden = !results.length;
    if (!results.length) return;
    $("sync-results-body").innerHTML = results.map(function (row) {
      return "<tr><td>" + esc(row.title) + "</td>" +
        "<td>" + esc(modeLabel(row.mode)) + "</td>" +
        '<td class="num">' + (row.new || 0) + "</td>" +
        '<td class="num">' + (row.known || 0) + "</td>" +
        '<td class="num">' + (row.removed || 0) + "</td>" +
        '<td class="num">' + (row.queued || 0) + "</td>" +
        "<td class=\"muted\">" + esc(row.error || "") + "</td></tr>";
    }).join("");

    var pending = Math.max(0, (sync.new_total || 0) - (sync.queued || 0));
    var queueBtn = $("sync-queue-btn");
    queueBtn.hidden = !(pending > 0 && !sync.running);
    queueBtn.textContent = "Поставить в очередь (" + pending + ")";
  }

  function renderDl(dl) {
    if (!dl) return;
    state.dl = dl;   // снимок нужен подсказке про аккаунт (bot_hint)
    // Кнопки отражают состояние воркера: пока идёт - можно только стоп.
    $("queue-start-btn").hidden = !!dl.running;
    $("queue-stop-btn").hidden = !dl.running;
    var failed = dl.failed || 0;
    var retry = $("queue-retry-btn");
    retry.hidden = !failed;
    retry.textContent = "Повторить упавшие (" + failed + ")";

    var bar = $("dl-bar");
    if (!dl.running && !dl.current) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    var current = dl.current;
    $("dl-title").textContent = current ? current.title : "…";
    $("dl-stage").textContent = current ? (current.stage || "") : "";
    var percent = current ? Number(current.percent || 0) : 0;
    $("dl-fill").style.width = percent + "%";
    $("dl-speed").textContent = current
      ? percent + "%" + (current.speed ? " · " + humanSpeed(current.speed) : "") +
        (current.eta ? " · осталось " + humanDuration(current.eta) : "")
      : "";
    $("dl-counters").textContent =
      "скачано " + (dl.done || 0) + " · ошибок " + failed +
      (dl.attempted ? " · в этой сессии " + dl.attempted : "");
  }

  function renderSettings() {
    var body = $("settings-body");
    if (!state.schema.length) return;
    var sections = [];
    var bySection = {};
    state.schema.forEach(function (field) {
      // Скрытые поля backend в схему не присылает; мок обязан врать так же
      // честно, иначе превью показывает то, чего в окне нет.
      if (field.hidden) return;
      var name = field.section || "Прочее";
      if (!bySection[name]) { bySection[name] = []; sections.push(name); }
      bySection[name].push(field);
    });
    body.innerHTML = sections.map(function (name) {
      return '<div class="set-section"><h3>' + esc(name) + "</h3>" +
        bySection[name].map(fieldHtml).join("") + "</div>";
    }).join("");
    bindSettings();
    // Строки живут вне схемы: после перерисовки тела подтягиваем их
    // состояние заново (ffmpeg-строка и аккаунт Google).
    renderFfmpegState();
    renderAccountState();
  }

  function fieldHtml(field) {
    var value = state.settings[field.key];
    var control = "";
    if (field.type === "bool") {
      control = '<label class="check"><input type="checkbox" data-key="' + field.key +
        '"' + (value ? " checked" : "") + "><span>включено</span></label>";
    } else if (field.type === "choice") {
      control = '<select data-key="' + field.key + '">' +
        field.choices.map(function (pair) {
          return '<option value="' + esc(pair[0]) + '"' +
            (value === pair[0] ? " selected" : "") + ">" + esc(pair[1]) + "</option>";
        }).join("") + "</select>";
      if (field.key === "transcode") {
        // Живая пометка: без ffmpeg перекодировки нет вовсе; какие кодеки
        // есть в конкретной сборке - уточняет refreshTranscodeField().
        control += '<div class="field-hint warn" data-transcode-note' +
          (state.ffmpeg && state.ffmpeg.found ? " hidden" : "") +
          ">ffmpeg не найден - перекодировка недоступна. " +
          '<button class="link-btn" data-action="ffmpeg-install">' +
          "Установить ffmpeg</button></div>";
      }
    } else if (field.type === "int") {
      control = '<input type="number" data-key="' + field.key + '" value="' +
        esc(value) + '"' +
        (field.min !== undefined ? ' min="' + field.min + '"' : "") +
        (field.max !== undefined ? ' max="' + field.max + '"' : "") + ">";
    } else if (field.type === "path") {
      control = '<input type="text" data-key="' + field.key + '" value="' +
        esc(value) + '"><button class="btn ghost" data-browse="' + field.key +
        '">…</button>';
    } else if (field.type === "action") {
      // Поле-кнопка (transient): значение не читаем, действие - по data-action.
      control = '<button class="btn ghost" data-action="' +
        esc(field.action || "") + '">' +
        esc(field.action_label || "Выполнить") + "</button>" +
        '<span class="muted" data-ffmpeg-state></span>';
    } else if (field.type === "account") {
      // Виджет аккаунтов: живой список (метка/дата/удаление) + добавление.
      // Куки применяются там, где аккаунт привязан к источнику.
      control = '<div class="account-list" data-account-list></div>' +
        '<button class="btn ghost" data-act-account="login">' +
        "Войти в окне…</button>" +
        '<button class="btn ghost" data-act-account="import">' +
        "Импортировать cookies.txt…</button>";
    } else if (field.type === "storages") {
      control = storagesHtml();
    } else {
      control = '<input type="text" data-key="' + field.key + '" value="' +
        esc(value) + '">';
    }
    return '<div class="field"><div><div class="field-label">' +
      esc(field.label) + '</div><div class="field-hint">' +
      esc(field.hint || "") + "</div></div>" +
      '<div class="field-control">' + control + "</div></div>";
  }

  function storagesHtml() {
    var rows = (state.storages || []).map(function (storage) {
      var active = storage.status === "active";
      var dot = !active ? "off" : (storage.available ? "" : "bad");
      var cls = "storage-row" + (storage.enabled ? "" : " off") +
        (active ? "" : " detached");
      var free;
      if (!active) {
        free = "отвязано";
      } else if (!storage.available) {
        free = "нет носителя";
      } else if (storage.free_bytes) {
        free = humanSize(storage.free_bytes) + " свободно";
      } else {
        free = "—";
      }
      var actions = [];
      if (active) {
        actions.push('<label class="storage-default"><input type="radio" ' +
          'name="storage-default" data-storage-default="' + storage.id + '"' +
          (state.settings.default_storage_id === storage.id ? " checked" : "") +
          "><span>по умолчанию</span></label>");
        actions.push('<button class="link-btn" data-storage-toggle="' +
          storage.id + '">' + (storage.enabled ? "отключить" : "включить") +
          "</button>");
        actions.push('<button class="link-btn" data-storage-path="' +
          storage.id + '">путь…</button>');
        actions.push('<button class="link-btn" data-storage-detach="' +
          storage.id + '">отвязать</button>');
      } else {
        actions.push('<button class="link-btn" data-storage-restore="' +
          storage.id + '">вернуть</button>');
        actions.push('<button class="link-btn" data-storage-forget="' +
          storage.id + '">забыть навсегда</button>');
      }
      return '<div class="' + cls + '">' +
        '<span class="storage-dot ' + dot + '" title="' +
        (active ? (storage.available ? "доступно" : "недоступно") : "отвязано") +
        '"></span>' +
        '<span class="storage-name"><b>' + esc(storage.label) + "</b>" +
        '<span class="storage-path" title="' + esc(storage.path) + '">' +
        esc(storage.path) + "</span></span>" +
        '<span class="storage-free">' + esc(free) + "</span>" +
        '<span class="storage-actions">' + actions.join("") + "</span>" +
        "</div>";
    }).join("");
    if (!rows) {
      rows = '<div class="muted">Хранилищ нет. Добавьте папку, в которую ' +
        "будете скачивать и которую будем индексировать.</div>";
    }
    return '<div class="storages">' + rows +
      '<div class="storage-toolbar">' +
      '<button class="btn ghost" id="storage-add">+ Добавить папку</button>' +
      '<button class="btn ghost" id="storage-check">Проверить доступность</button>' +
      "</div></div>";
  }

  function bindSettings() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-key]"), function (input) {
        input.addEventListener("change", function () {
          var key = input.dataset.key;
          var value = input.type === "checkbox" ? input.checked : input.value;
          saveSetting(key, value);
        });
      });

    // Поля-кнопки: действие опознаём по data-action (сейчас одно - ffmpeg).
    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-action]"), function (btn) {
        btn.addEventListener("click", function () {
          if (btn.dataset.action === "ffmpeg-install") openFfmpegOverlay();
        });
      });

    // Виджет аккаунтов Google: вход/импорт + удаление конкретного аккаунта.
    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-act-account]"),
      function (btn) {
        btn.addEventListener("click", function () {
          var act = btn.dataset.actAccount;
          if (act === "login") openAccountOverlay();
          else if (act === "import") importCookies();
          else if (act === "abort") {
            // Прерывание: закрывает окно (если оно ещё живо) и немедленно
            // снимает состояние входа - кнопка снова становится «Войти».
            call("account_login_stop").then(function () {
              toast("Вход прерван");
            });
          }
        });
      });
    // Удаление аккаунта - делегированием: список перерисовывается целиком
    // (renderAccountState), прямые слушатели на кнопках «забыть» отвалились.
    var accountBox = document.querySelector(
      "#settings-body [data-account-list]");
    if (accountBox) {
      accountBox.addEventListener("click", function (event) {
        var btn = event.target && event.target.closest
          ? event.target.closest("[data-account-forget]") : null;
        if (!btn) return;
        call("account_forget", { id: btn.dataset.accountForget })
          .then(function (res) {
            toast(res && res.error ? res.error : "Аккаунт забыт");
          });
      });
    }

    bindStorageActions(document.getElementById("settings-body"));
  }

  function bindStorageActions(root) {
    if (!root) return;
    var on = function (selector, handler) {
      Array.prototype.forEach.call(root.querySelectorAll(selector), function (el) {
        el.addEventListener("click", handler);
      });
    };

    on("#storage-add, .storage-add", function () { pickAndAddStorage(); });
    on("#storage-check", function () {
      call("storage_check").then(function () { toast("Проверяем…"); });
    });
    on("[data-storage-default]", function (ev) {
      call("storage_set_default", { id: ev.currentTarget.dataset.storageDefault })
        .then(function (res) {
          if (res && res.error) toast(res.error, true);
        });
    });
    on("[data-storage-toggle]", function (ev) {
      var id = ev.currentTarget.dataset.storageToggle;
      var storage = findStorage(id);
      call("storage_enable", { id: id, enabled: !(storage && storage.enabled) });
    });
    on("[data-storage-path]", function (ev) {
      var id = ev.currentTarget.dataset.storagePath;
      pickFolder().then(function (path) {
        if (!path) return;
        call("storage_set_path", { id: id, path: path }).then(function (res) {
          if (res && res.error) { toast(res.error, true); return; }
          toast("Переехало файлов: " + res.files + ", найдено на месте: " +
            res.present);
        });
      });
    });
    on("[data-storage-detach]", function (ev) {
      var id = ev.currentTarget.dataset.storageDetach;
      call("storage_preview_detach", { id: id }).then(function (preview) {
        if (!preview || preview.error) {
          toast(preview && preview.error, true);
          return;
        }
        var text = "Отвязать «" + preview.label + "»?\n\n" +
          "Будет забыто файлов: " + preview.files + "\n" +
          "Видео останется без файла («откреплено»): " + preview.detached + "\n" +
          "Будут удалены как локальные (без площадочной личности): " +
          preview.local_deleted + "\n" +
          "Копия в другом хранилище не пострадает: " + preview.kept_elsewhere;
        // Шаг 1: согласие на отвязку (Отмена = ничего не делать).
        if (!confirm(text + "\n\nОтвязать?")) return;
        // Шаг 2: что оставить про саму папку - спрашиваем каждый раз.
        var keepTrace = confirm(
          "Оставить след папки?\n\n" +
          "ОК - оставить след: при следующем добавлении я узнаю папку и " +
          "предложю вернуть её без перекачки.\n" +
          "Отмена - забыть совсем: не останется ничего, восстановление " +
          "будет только обычной переиндексацией.");
        call("storage_detach", { id: id, keep_trace: keepTrace })
          .then(function (res) {
            if (res && res.error) { toast(res.error, true); return; }
            toast("Отвязано «" + res.label + "»: забыто файлов " +
              res.files_removed + (res.kept_trace ? ", след остался" : ""));
          });
      });
    });
    on("[data-storage-restore]", function (ev) {
      call("storage_restore", { id: ev.currentTarget.dataset.storageRestore })
        .then(function (res) {
          if (res && res.error) { toast(res.error, true); return; }
          toast("Возвращено - запустите переиндексацию, чтобы вернуть файлы");
        });
    });
    on("[data-storage-forget]", function (ev) {
      var id = ev.currentTarget.dataset.storageForget;
      if (!confirm("Удалить след папки совсем? Восстановить её после этого " +
                   "получится только обычной переиндексацией.")) return;
      call("storage_forget", { id: id }).then(function (res) {
        if (res && res.error) toast(res.error, true);
      });
    });
  }

  function findStorage(id) {
    return (state.storages || []).filter(function (s) { return s.id === id; })[0];
  }

  function pickFolder() {
    // Ответ - {path} | {cancelled} | {error}: ошибку показываем тостом,
    // отмену принимаем молча. Раньше всё это было «null», и сломанный
    // диалог выглядел как «нажал кнопку и ничего не произошло».
    return call("pick_folder").then(function (res) {
      if (res && res.error) { toast(res.error, true); return null; }
      return res && res.path ? res.path : null;
    });
  }

  function pickAndAddStorage() {
    pickFolder().then(function (path) {
      if (!path) return;
      call("storage_add", { path: path }).then(function (res) {
        if (!res) return;
        if (res.error) { toast(res.error, true); return; }
        if (res.hint === "already") {
          toast("Это хранилище уже добавлено");
          return;
        }
        // Папку раньше отвязали: спрашиваем, вернуть ли её. Это и есть
        // «переинициализация» - дальше файлы вернёт обычный скан.
        if (res.hint === "detached" || (res.hint === "known_root" &&
                                        res.storage.status !== "active")) {
          var label = res.storage.label;
          if (confirm("Папка уже была в библиотеке как «" + label + "».\n\n" +
                      "Вернуть её? Файлы восстановятся при переиндексации, " +
                      "без перекачки.")) {
            call("storage_restore", { id: res.storage.id }).then(function (r) {
              if (r && r.error) { toast(r.error, true); return; }
              toast("«" + label + "» возвращено - запустите переиндексацию");
            });
          }
          return;
        }
        // Маркер нашёлся у активного хранилища с другим путём: это переезд
        // (диск переименовали) - предлагаем указать новый путь.
        if (res.hint === "known_root" && res.storage.path !== path) {
          if (confirm("Это хранилище «" + res.storage.label + "», но раньше " +
                      "оно лежало по другому пути:\n" + res.storage.path +
                      "\n\nУказать новый путь? Файлы перепривяжу, ничего " +
                      "не перекачивая.")) {
            call("storage_set_path", { id: res.storage.id, path: path })
              .then(function (r) {
                if (r && r.error) { toast(r.error, true); return; }
                toast("Переехало файлов: " + r.files + ", найдено на месте: " +
                  r.present);
              });
          }
          return;
        }
        toast("Хранилище добавлено: " + (res.storage ? res.storage.label : ""));
      });
    });
  }

  function syncViewFromSettings() {
    // Вид/размер плитки - обычные настройки; возвращает true, если что-то
    // изменилось (тогда нужна перерисовка без сброса страниц и выделения).
    var newView = state.settings.view_mode === "grid" ? "grid" : "list";
    var newSize = state.settings.tile_size || "medium";
    var changed = (state.viewMode !== newView) || (state.tileSize !== newSize);
    state.viewMode = newView;
    state.tileSize = newSize;
    return changed;
  }

  function saveSetting(key, value) {
    call("save_setting", { key: key, value: value }).then(function (res) {
      if (!res) return;
      state.settings = res.settings || state.settings;
      state.rev = res.settings_rev;
      document.body.dataset.theme = state.settings.theme === "light" ? "light" : "dark";
      // Вид библиотеки - тоже настройка, применяем сразу: у следующего
      // опроса rev уже совпадёт, ветка в applySnapshot не сработает.
      var viewChanged = syncViewFromSettings();
      applyViewState();
      renderSettings();
      if (viewChanged) paintGrid();
      toast("Сохранено");
    }).catch(function (err) { toast("Не удалось сохранить: " + err, true); });
  }

  /* ------------------------------------------------------------------ *
   *  Лог
   * ------------------------------------------------------------------ */

  function appendLogs(lines) {
    if (!lines || !lines.length) return;
    state.logLines = state.logLines.concat(lines);
    if (state.logLines.length > 2000) state.logLines = state.logLines.slice(-2000);
    if ($("log").hidden) return;
    $("log").textContent = state.logLines.join("\n");
    $("log").scrollTop = $("log").scrollHeight;
  }

  /* ------------------------------------------------------------------ *
   *  Добавление источника: A индексация -> B диалог -> C стадии -> D итог
   * ------------------------------------------------------------------ */

  var addOpen = false;       // окно открыл пользователь
  var lastAddFlow = null;
  var addForm = { url: "", mode: "partial", storageId: "", accountId: "",
                  picked: {} };

  var ADD_MODES = [
    ["partial", "Частичная", "Новые видео попадают в индекс, контент выбираете вручную"],
    ["full", "Полная", "Новые видео сразу встают в очередь загрузки"],
    ["manual", "Ручная", "Источник обновляется только по вашей кнопке"]
  ];

  function storageRowHtml() {
    // «Куда качать» видно и до индексации, и при подтверждении: выбор
    // запоминается у источника, его читают синк и режим «Полная».
    var preferred = addForm.storageId || defaultStorageId();
    return '<div class="field-label">Куда качать</div>' +
      '<select id="add-storage" data-storage-select data-prefer="' +
        esc(preferred) + '" ' +
        'aria-label="Хранилище для загрузок этого источника">' +
        storageOptionsHtml(preferred) + "</select>" +
      '<div class="muted">Выбор запоминается у источника: синхронизация ' +
      "и режим «Полная» будут качать именно сюда.</div>";
  }

  function accountRowHtml() {
    // Аккаунт привязывается к источнику: его куки используются при синке
    // этого источника и загрузке его видео. Глобального аккаунта нет.
    var acc = state.account || {};
    var accounts = acc.accounts || [];
    var preferred = addForm.accountId || "";
    var options = ['<option value="">без аккаунта</option>'].concat(
      accounts.map(function (item) {
        return '<option value="' + esc(item.id) + '"' +
          (item.id === preferred ? " selected" : "") + ">" +
          esc(item.label || item.id) + "</option>";
      }));
    return '<div class="field-label">Аккаунт Google</div>' +
      '<select id="add-account" aria-label="Аккаунт для входа на площадку">' +
      options.join("") + "</select>" +
      (accounts.length
        ? '<div class="muted">Куки аккаунта уходят в синк и загрузку ' +
          "только этого источника.</div>"
        : '<div class="muted">Аккаунтов нет: если площадка просит вход ' +
          "(«подтвердите, что вы не бот»), войдите в настройках.</div>");
  }

  function modeRadios(current, name) {
    return '<div class="mode-list">' + ADD_MODES.map(function (mode) {
      return '<label class="mode-item"><input type="radio" name="' + name +
        '" value="' + mode[0] + '"' + (current === mode[0] ? " checked" : "") +
        '><span><b>' + mode[1] + '</b><span class="muted">' + mode[2] +
        "</span></span></label>";
    }).join("") + "</div>";
  }

  function countRow(label, value, note) {
    if (!value && !note) return "";
    return "<tr><td>" + esc(label) + '</td><td class="num"><b>' + esc(value) +
      "</b></td><td class=\"muted\">" + esc(note || "") + "</td></tr>";
  }

  function renderAddFlow(flow) {
    lastAddFlow = flow || lastAddFlow;
    flow = flow || { phase: "idle" };
    var overlay = $("add-overlay");
    overlay.hidden = !(addOpen || flow.phase !== "idle");

    var title = $("add-title");
    var body = $("add-body");
    var html = "";

    if (flow.phase === "fetching") {
      title.textContent = "Индексация источника";
      var fetch = flow.fetch || { got: 0, total: 0 };
      var pct = fetch.total ? Math.round(fetch.got * 100 / fetch.total) : 6;
      html = '<div class="muted">' + esc(flow.url) + "</div>" +
        '<div class="scan-bar"><div class="scan-track">' +
        '<div class="scan-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="scan-text">' +
        (fetch.total ? "Получаем записи " + fetch.got + " / " + fetch.total
                     : "Получаем записи " + fetch.got) +
        " · затем посчитаем, чего в библиотеке ещё нет</div></div>" +
        '<div class="card-actions"><button class="btn" data-act="close">Отмена</button></div>';

    } else if (flow.phase === "confirm") {
      title.textContent = "Точно добавить?";
      var plan = flow.plan || {};
      var counts = plan.counts || {};
      var rows = [
        countRow("Плейлист", 1, plan.exists ? "уже есть - обновится" : "новый"),
        countRow("Авторы", counts.new_channels,
                 counts.known_channels ? "новых, ещё " + counts.known_channels +
                   " уже в индексе" : "новых"),
        countRow("Видео", counts.new_videos,
                 counts.known_videos ? "новых, уже в библиотеке " +
                   counts.known_videos : "новых"),
        countRow("Связи", counts.links_to_create,
                 counts.links_existing ? "создать, уже связано " +
                   counts.links_existing : "создать"),
        countRow("Пропущено", counts.skipped,
                 counts.skipped ? "записей недоступно (приватно/удалено)" : ""),
        countRow("Повторы в списке", counts.dupes, counts.dupes ? "не будут добавлены" : "")
      ].join("");

      var warn = "";
      if (plan.is_mix) {
        warn += '<div class="notice warn">Это микс: YouTube пересобирает его ' +
          "состав почти каждый день.</div>";
      }
      if (counts.skipped) {
        warn += '<div class="notice">' + counts.skipped +
          " записей площадка не отдаёт - они останутся счётчиком, но не станут строками.</div>";
      }

      html = '<div class="add-head"><b>' + esc(plan.title || flow.url) + "</b>" +
        '<div class="muted">' + esc(plan.channel || "") +
        (plan.kind === "uploads" ? " · загрузки канала" : " · плейлист") +
        (plan.item_count ? " · " + plan.item_count + " записей" : "") + "</div></div>" +
        '<table class="table add-counts"><tbody>' + rows + "</tbody></table>" + warn +
        '<div class="field-label">Режим синхронизации</div>' +
        modeRadios(addForm.mode, "add-mode") +
        storageRowHtml() +
        accountRowHtml() +
        '<div class="card-actions">' +
        '<button class="btn" data-act="close">Отмена</button>' +
        '<button class="btn primary" data-act="confirm">Добавить</button></div>';

    } else if (flow.phase === "committing") {
      title.textContent = "Создание в базе";
      html = '<div class="muted">Одна транзакция: падение на любой стадии ' +
        "откатит всё, отмена тоже.</div><ul class=\"stages\">" +
        (flow.stages || []).map(function (stage) {
          var pct = stage.total ? Math.round(stage.current * 100 / stage.total)
            : (stage.state === "done" ? 100 : 0);
          var icon = stage.state === "done" ? "✓"
            : (stage.state === "active" ? "…" : "○");
          var count = stage.total
            ? stage.current + " / " + stage.total
            : (stage.state === "done" ? "готово" : "");
          return '<li class="stage ' + stage.state + '">' +
            '<span class="stage-ico">' + icon + "</span>" +
            '<span class="stage-name">' + esc(stage.title) + "</span>" +
            '<span class="stage-bar"><span class="stage-fill" style="width:' +
            pct + '%"></span></span>' +
            '<span class="stage-count">' + esc(count) + "</span></li>";
        }).join("") + "</ul>" +
        '<div class="card-actions"><button class="btn" data-act="close">Отмена</button></div>';

    } else if (flow.phase === "done") {
      title.textContent = "Готово";
      var result = flow.result || {};
      var stats = result.stats || {};
      var lines = [
        "Плейлист: " + (result.title || "—"),
        "Новых видео: " + (stats.new_videos || 0),
        "Уже было в библиотеке: " + (stats.known_videos || 0),
        "Связей создано: " + (stats.links_to_create || 0),
        "Куда качать: " + (result.storage_label || "по умолчанию"),
        "Аккаунт: " + (result.account_label || "не привязан"),
        (stats.removed ? "Убрано из плейлиста: " + stats.removed : "")
      ].filter(Boolean).map(function (line) {
        return "<li>" + esc(line) + "</li>";
      }).join("");

      html = '<ul class="kv-list">' + lines + "</ul>";

      if (result.mode === "full") {
        html += '<div class="notice ok">В очередь загрузки поставлено: <b>' +
          (result.queued || 0) + "</b>" +
          (result.storage_label ? " → " + esc(result.storage_label) : "") +
          "</div>" +
          '<div class="card-actions">' +
          '<button class="btn" data-act="close">Закрыть</button>' +
          '<button class="btn primary" data-act="queue">К очереди</button></div>';
      } else if (result.picker_total) {
        var shown = (result.picker || []).length;
        html += '<div class="field-label">Что скачать сейчас: <span id="pick-count">0</span>' +
          (result.picker_total > shown ? " (показаны первые " + shown + " из " +
            result.picker_total + ")" : "") + "</div>" +
          '<div class="picker">' + (result.picker || []).map(function (item) {
            return '<label class="pick-row"><input type="checkbox" data-pick="' +
              item.id + '">' +
              '<span class="pick-title">' + esc(item.title || item.key) + "</span>" +
              '<span class="muted">' + humanDuration(item.duration_s) + "</span></label>";
          }).join("") + "</div>" +
          '<div class="card-actions">' +
          '<button class="btn ghost" data-act="pick-all">Выбрать все</button>' +
          '<select data-storage-select data-prefer="' +
            esc(addForm.storageId || defaultStorageId()) +
            '" id="pick-storage" aria-label="Хранилище">' +
            storageOptionsHtml(addForm.storageId || defaultStorageId()) +
          "</select>" +
          '<button class="btn" data-act="close">Позже</button>' +
          '<button class="btn primary" data-act="download" id="pick-go" disabled>Загрузить выбранные</button>' +
          "</div>";
      } else {
        html += '<div class="card-actions">' +
          '<button class="btn primary" data-act="close">Закрыть</button></div>';
      }

    } else if (flow.phase === "error") {
      title.textContent = "Не получилось";
      html = '<div class="notice err">' + esc(flow.error || "Неизвестная ошибка") +
        "</div><div class=\"card-actions\">" +
        '<button class="btn" data-act="close">Закрыть</button>' +
        '<button class="btn primary" data-act="retry">Попробовать снова</button></div>';

    } else {
      title.textContent = "Добавить источник";
      html = '<label class="field-label" for="add-url">Ссылка на плейлист или канал</label>' +
        '<input type="text" id="add-url" autocomplete="off" spellcheck="false" ' +
        'placeholder="https://youtube.com/playlist?list=… или https://youtube.com/@channel" ' +
        'value="' + esc(addForm.url) + '">' +
        '<div class="muted">Поддерживаются ссылки на плейлист и на канал. ' +
        "Метаданные снимаются до записи в базу, поэтому «Отмена» ничего не стоит.</div>" +
        '<div class="field-label">Режим синхронизации</div>' +
        modeRadios(addForm.mode, "add-mode") +
        storageRowHtml() +
        accountRowHtml() +
        '<div class="card-actions">' +
        '<button class="btn" data-act="close">Отмена</button>' +
        '<button class="btn primary" data-act="run">Индексировать</button></div>';
    }

    body.innerHTML = html;
    bindAdd();
  }

  function bindAdd() {
    var body = $("add-body");
    var urlInput = document.getElementById("add-url");
    if (urlInput) {
      urlInput.addEventListener("input", function () { addForm.url = urlInput.value; });
      urlInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") runAdd();
      });
      if (addOpen && !addForm.url) setTimeout(function () { urlInput.focus(); }, 50);
    }

    Array.prototype.forEach.call(
      body.querySelectorAll('input[name="add-mode"]'),
      function (radio) {
        radio.addEventListener("change", function () { addForm.mode = radio.value; });
      });

    var addStorage = document.getElementById("add-storage");
    if (addStorage) {
      addStorage.addEventListener("change", function () {
        addForm.storageId = addStorage.value;
      });
    }
    var addAccount = document.getElementById("add-account");
    if (addAccount) {
      addAccount.addEventListener("change", function () {
        addForm.accountId = addAccount.value;
      });
    }

    Array.prototype.forEach.call(body.querySelectorAll("[data-act]"), function (btn) {
      btn.addEventListener("click", function () { addAction(btn.dataset.act); });
    });

    Array.prototype.forEach.call(body.querySelectorAll("[data-pick]"), function (box) {
      box.addEventListener("change", function () {
        if (box.checked) addForm.picked[box.dataset.pick] = true;
        else delete addForm.picked[box.dataset.pick];
        updatePickCount();
      });
    });
    updatePickCount();
  }

  function updatePickCount() {
    var count = Object.keys(addForm.picked).length;
    var label = document.getElementById("pick-count");
    if (label) label.textContent = count;
    var go = document.getElementById("pick-go");
    if (go) {
      go.disabled = !count;
      go.textContent = count ? "Загрузить выбранные (" + count + ")"
                             : "Загрузить выбранные";
    }
  }

  function currentAddStorage() {
    // Значение берём с экрана, а не из addForm: селект всегда показывает
    // конкретную папку, и «не трогал» не должен молча означать «по
    // умолчанию» - что видно, то и сохраняется.
    var sel = document.getElementById("add-storage");
    return sel ? sel.value : addForm.storageId;
  }

  function runAdd() {
    if (!addForm.url.trim()) { toast("Вставьте ссылку", true); return; }
    addForm.storageId = currentAddStorage();
    call("add_start", { url: addForm.url.trim(), mode: addForm.mode,
                        storage_id: addForm.storageId,
                        account_id: addForm.accountId })
      .then(function (res) {
        if (res && res.error) toast(res.error, true);
        else addOpen = true;
      });
  }

  function addAction(action) {
    if (action === "close" || action === "cancel") { closeAdd(); return; }
    if (action === "run") { runAdd(); return; }
    if (action === "confirm") {
      addForm.storageId = currentAddStorage();
      call("add_confirm", { mode: addForm.mode,
                            storage_id: addForm.storageId,
                            account_id: addForm.accountId }).then(function (res) {
        if (res && res.error) toast(res.error, true);
      });
      return;
    }
    if (action === "retry") { closeAdd(); setTimeout(openAdd, 60); return; }
    if (action === "queue") { closeAdd(); switchTab("queue"); return; }
    if (action === "pick-all") {
      Array.prototype.forEach.call(
        document.querySelectorAll("#add-body [data-pick]"), function (box) {
          box.checked = true;
          addForm.picked[box.dataset.pick] = true;
        });
      updatePickCount();
      return;
    }
    if (action === "download") {
      var ids = Object.keys(addForm.picked).map(Number);
      var pick = document.getElementById("pick-storage");
      call("enqueue", { ids: ids, storage_id: pick ? pick.value : "" })
        .then(function (res) {
        if (res && res.error) { toast(res.error, true); return; }
        toast("В очередь поставлено: " + (res ? res.queued : 0));
        addForm.picked = {};
        closeAdd();
        switchTab("queue");
      });
    }
  }

  function openAdd() {
    addOpen = true;
    renderAddFlow(lastAddFlow);
  }

  function closeAdd() {
    addOpen = false;
    $("add-overlay").hidden = true;
    call("add_close");
  }

  /* ------------------------------------------------------------------ *
   *  Действия
   * ------------------------------------------------------------------ */

  function startScan() {
    if (!(state.settings.library_roots || []).length) {
      toast("Сначала добавьте папки библиотеки в настройках", true);
      switchTab("settings");
      return;
    }
    call("start_scan").then(function (res) {
      if (res && res.error) toast(res.error, true);
    });
  }

  function switchTab(name) {
    state.tab = name;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.classList.toggle("active", tab.dataset.tab === name);
    });
    ["library", "sync", "queue", "settings"].forEach(function (panel) {
      $("panel-" + panel).hidden = panel !== name;
    });
    if (name === "library") paintGrid();
    // Карточка настроек рисуется из schema+storages: на вкладке её надо
    // перерисовать, иначе виджет хранилищ останется пустым (он рисовался
    // до того, как пришли данные).
    if (name === "settings") renderSettings();
  }

  /* ------------------------------------------------------------------ *
   *  Инициализация
   * ------------------------------------------------------------------ */

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.addEventListener("click", function () { switchTab(tab.dataset.tab); });
    });

    $("search").addEventListener("input", debounce(function () {
      state.query = $("search").value;
      renderGrid(true);
    }, 260));

    $("status-filter").addEventListener("change", function () {
      state.status = $("status-filter").value;
      renderGrid(true);
    });

    $("rating-filter").addEventListener("change", function () {
      state.ratingMin = $("rating-filter").value;
      renderGrid(true);
    });

    $("grid-body").addEventListener("scroll", function () {
      if (!paintGrid._raf) {
        paintGrid._raf = requestAnimationFrame(function () {
          paintGrid._raf = 0;
          paintGrid();
        });
      }
    });

    // Вид библиотеки: список/плитка и размер плитки - в настройках,
    // чтобы выбор пережил перезапуск окна.
    $("view-list-btn").addEventListener("click", function () {
      if (state.viewMode !== "list") saveSetting("view_mode", "list");
    });
    $("view-grid-btn").addEventListener("click", function () {
      if (state.viewMode !== "grid") saveSetting("view_mode", "grid");
    });
    Array.prototype.forEach.call(
      document.querySelectorAll("#size-toggle .size-btn"), function (btn) {
        btn.addEventListener("click", function () {
          if (state.tileSize !== btn.dataset.size) {
            saveSetting("tile_size", btn.dataset.size);
          }
        });
      });
    // Сколько колонок плитки поместилось - решает ширина окна, не настройка.
    if (window.ResizeObserver) {
      var lastCols = 0;
      new ResizeObserver(function () {
        if (state.viewMode !== "grid") return;
        var cols = tileCols();
        if (cols === lastCols) return;
        lastCols = cols;
        if (paintGrid._raf) return;
        paintGrid._raf = requestAnimationFrame(function () {
          paintGrid._raf = 0;
          paintGrid();
        });
      }).observe($("grid-body"));
    }

    $("rescan-btn").addEventListener("click", startScan);
    $("stop-scan-btn").addEventListener("click", function () { call("stop_scan"); });

    // Мультивыбор в таблице библиотеки.
    $("sel-clear").addEventListener("click", clearSelection);
    $("sel-download").addEventListener("click", enqueueSelection);
    $("sel-mark").addEventListener("click", openMark);
    $("mark-close").addEventListener("click", function () {
      $("mark-overlay").hidden = true;
    });
    $("sel-move").addEventListener("click", openMigrate);
    $("migrate-close").addEventListener("click", function () {
      // Идёт перенос - закрыть окно нельзя: процесс должен быть виден.
      if (state.migrate && state.migrate.running) {
        toast("Перенос идёт - остановите его или дождитесь конца", true);
        return;
      }
      $("migrate-overlay").hidden = true;
    });
    $("sel-storage").addEventListener("change", function () {
      state.selStorage = $("sel-storage").value;
    });
    $("sel-all").addEventListener("change", function () {
      var on = $("sel-all").checked;
      Array.prototype.forEach.call(
        document.querySelectorAll("#grid-rows .row-check"), function (box) {
          box.checked = on;
          toggleSelected(Number(box.dataset.id), on, box.closest(".row"));
        });
    });

    // Стартовый выбор хранилища.
    $("wizard-pick").addEventListener("click", function () {
      pickAndAddStorage();
    });
    $("wizard-later").addEventListener("click", function () {
      state.wizardDismissed = true;
      updateWizard();
      switchTab("settings");
    });

    // Сверка: копии файлов и «похоже на переезд».
    $("dedupe-btn").addEventListener("click", openDedupe);
    $("dedupe-note-btn").addEventListener("click", openDedupe);
    $("dedupe-close").addEventListener("click", function () {
      $("dedupe-overlay").hidden = true;
    });

    // Переупаковка по шаблону (область = выделение или текущий фильтр).
    $("repack-btn").addEventListener("click", openRepack);
    $("repack-close").addEventListener("click", function () {
      if (state.repack && state.repack.running) {
        toast("Переупаковка идёт - остановите её или дождитесь конца", true);
        return;
      }
      $("repack-overlay").hidden = true;
    });

    // Журнал: в памяти окна и файлом на диске.
    $("log-open").addEventListener("click", function () {
      call("open_log").then(function (res) {
        if (!res) return;
        if (res.error) { toast(res.error, true); return; }
        toast("Журнал: " + res.path);
      });
    });

    // Проверка целостности: сверка файлов с хешем в индексе.
    $("verify-btn").addEventListener("click", runVerify);
    $("verify-repair-btn").addEventListener("click", repairBroken);
    $("verify-close-btn").addEventListener("click", function () {
      $("verify-note").hidden = true;
    });

    // Установка ffmpeg: overlay живёт по снимку, кнопки - делегированием.
    $("ffmpeg-close").addEventListener("click", function () {
      $("ffmpeg-overlay").hidden = true;
    });
    $("ffmpeg-body").addEventListener("click", function (event) {
      var btn = event.target && event.target.closest
        ? event.target.closest("[data-ffmpeg]") : null;
      if (!btn) return;
      var action = btn.dataset.ffmpeg;
      if (action === "close") { $("ffmpeg-overlay").hidden = true; return; }
      if (action === "install") {
        call("ffmpeg_start").then(function (res) {
          if (res && res.error) toast(res.error, true);
        });
        return;
      }
      if (action === "stop") call("ffmpeg_stop");
    });
    $("ffmpeg-note-btn").addEventListener("click", openFfmpegOverlay);
    $("ffmpeg-note-close").addEventListener("click", function () {
      state.ffmpegNoteDismissed = true;
      $("ffmpeg-note").hidden = true;
    });

    // Аккаунт Google: оверлей входа и подсказка в очереди.
    $("account-close").addEventListener("click", function () {
      $("account-overlay").hidden = true;
    });
    $("account-body").addEventListener("click", function (event) {
      var btn = event.target && event.target.closest
        ? event.target.closest("[data-account-act]") : null;
      if (!btn) return;
      var action = btn.dataset.accountAct;
      if (action === "close") { $("account-overlay").hidden = true; return; }
      if (action === "import") { importCookies(); return; }
      if (action === "facts") { checkAccountVisible(); return; }
      if (action === "abort") {
        call("account_login_stop").then(function () {
          toast("Вход прерван");
          $("account-overlay").hidden = true;
        });
        return;
      }
      if (action === "capture") {
        // Ручная поимка: окно могли закрыть, а могли и войти молча.
        call("account_capture_now").then(function (res) {
          var box = document.querySelector("#account-body [data-account-facts]");
          if (res && res.error) {
            if (box) {
              box.innerHTML = '<div class="notice err">' + esc(res.error) +
                "</div>";
            }
            toast(res.error, true);
            return;
          }
          toast("Аккаунт добавлен: " +
            ((res.account && res.account.label) || ""));
          $("account-overlay").hidden = true;
        });
        return;
      }
      if (action === "login") {
        $("account-overlay").hidden = true;
        startAccountLogin();
      }
    });
    $("bot-note-btn").addEventListener("click", function () {
      switchTab("settings");
    });
    $("bot-note-close").addEventListener("click", function () {
      state.botNoteDismissed = true;
      $("bot-note").hidden = true;
    });

    $("queue-start-btn").addEventListener("click", function () {
      call("queue_start").then(function (res) {
        if (res && res.error) toast(res.error, true);
      });
    });
    $("queue-stop-btn").addEventListener("click", function () { call("queue_stop"); });
    $("queue-retry-btn").addEventListener("click", function () { call("queue_retry"); });

    $("sync-all-btn").addEventListener("click", function () {
      call("sync_start").then(function (res) {
        if (res && res.error) toast(res.error, true);
        else switchTab("sync");
      });
    });
    $("sync-stop-btn").addEventListener("click", function () { call("sync_stop"); });
    $("sync-queue-btn").addEventListener("click", function () {
      var pick = document.getElementById("sync-storage");
      call("sync_queue_new", { storage_id: pick ? pick.value : "" })
        .then(function (res) {
          if (res && res.error) { toast(res.error, true); return; }
          toast("В очередь поставлено: " + (res ? res.queued : 0));
          switchTab("queue");
        });
    });

    $("add-btn").addEventListener("click", function () {
      addOpen = true;
      renderAddFlow(lastAddFlow);
    });
    $("add-close").addEventListener("click", closeAdd);
    // Клик мимо карточки закрывает диалог - как в других оверлеях (плюс Esc).
    $("add-overlay").addEventListener("click", function (event) {
      if (event.target === $("add-overlay")) closeAdd();
    });

    $("detail-close").addEventListener("click", function () { $("detail-overlay").hidden = true; });
    $("detail-overlay").addEventListener("click", function (event) {
      if (event.target === $("detail-overlay")) $("detail-overlay").hidden = true;
    });

    $("log-toggle").addEventListener("click", function () {
      var log = $("log");
      log.hidden = !log.hidden;
      $("log-toggle").textContent = log.hidden ? "Журнал" : "Скрыть журнал";
      if (!log.hidden) {
        log.textContent = state.logLines.join("\n");
        log.scrollTop = log.scrollHeight;
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        $("detail-overlay").hidden = true;
        closeAdd();
      }
      if (event.key === "F5" || (event.ctrlKey && event.key.toLowerCase() === "r")) {
        event.preventDefault();
        renderGrid(true);
      }
    });
  }

  function boot() {
    bind();
    call("get_initial").then(function (data) {
      if (!data) return;
      state.settings = data.settings || {};
      state.schema = data.schema || [];
      state.rev = data.settings_rev;
      document.body.dataset.theme = state.settings.theme === "light" ? "light" : "dark";
      $("version").textContent = "Omnistash " + (data.version || "");
      renderSettings();
      renderGrid(true);
    });
    setInterval(tick, POLL_MS);
    tick();
  }

  /* ------------------------------------------------------------------ *
   *  Превью в браузере: если pywebview не подключился - рисуем выдуманными
   *  данными, чтобы разметку можно было смотреть без запуска приложения.
   * ------------------------------------------------------------------ */

  function installPreview() {
    if (window.pywebview || mock) return;
    var fixtures = {
      total: 5, by_status: { known: 3, downloaded: 2, queued: 0 },
      downloaded: 2, queued: 0, playlists: 1, channels: 2, files: 2, bytes: 512000000
    };
    var rows = [
      { id: 1, key: "youtube:aaa111bbb22", title: "Ночной дождик", status: "downloaded",
        status_label: "скачано", duration_s: 3725, uploaded_at: "2025-01-14",
        channel: "Автор А", size: 240000000, user_rating: 5, has_thumb: true,
        watched_at: "2026-01-02T10:00:00" },
      { id: 2, key: "youtube:ccc333ddd44", title: "Утренний туман", status: "known",
        status_label: "в индексе", duration_s: 130, uploaded_at: "2024-11-02",
        channel: "Автор Б", size: 0, user_rating: 0, watched_at: null,
        has_thumb: false },
      { id: 3, key: "youtube:eee555fff66", title: "Долгая дорога", status: "downloaded",
        status_label: "скачано", duration_s: 540, uploaded_at: "2023-06-30",
        channel: "Автор А", size: 272000000, user_rating: 3, watched_at: null,
        has_thumb: true },
      { id: 4, key: "local:abc", title: "Файл без личности", status: "downloaded",
        status_label: "скачано", duration_s: 61, uploaded_at: null,
        channel: null, size: 0, user_rating: 0, watched_at: null,
        has_thumb: false },
      { id: 5, key: "youtube:ggg777hhh88", title: "Снятый клип", status: "missing",
        status_label: "файл пропал", duration_s: 200, uploaded_at: "2022-02-02",
        channel: "Автор В", size: 0, user_rating: 0, watched_at: null,
        has_thumb: false }
    ];
    var settings = {
      library_roots: [{ path: "D:\\видео\\библиотека", recursive: true, enabled: true }],
      default_storage_id: "st_preview1",
      default_sync_mode: "partial", keep_sidecar: true, compute_hash: true,
      dest_dir: "D:\\видео\\downloads",
      output_template: "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s",
      quality: "high", subtitles: "none", transcode: "none",
      view_mode: "list", tile_size: "medium",
      use_google_cookies: true,
      delay_ms: 500, retries: 3, resume_queue: true,
      scan_interval_min: 0, sync_interval_min: 30,
      theme: "dark", app_version: ""
    };
    var counter = 0;
    var settingsRev = 1;
    // Симуляция качалки: в превью нет сети, но поведение панели очереди
    // (кнопки, полоса, счётчики) должно быть проверяемо.
    var mockDl = { running: false, done: 3, failed: 1, attempted: 4,
                   current: null, error: null };
    // Симуляция синхронизации: fetch растёт, затем падает в результат.
    var mockSync = { running: false, index: 0, total: 0, current: null,
                     fetch: null, stage: null, results: [], new_ids: [],
                     new_total: 0, queued: 0, error: null };
    // Симуляция добавления источника: индексация -> подтверждение -> итог.
    // В превью нет сети, но экраны (селект «Куда качать», итог с меткой,
    // пикер) должны быть проверяемы.
    var mockAdd = { phase: "idle", mode: "partial", storage_id: null, url: "",
                    fetch: null, plan: null, stages: [], result: null,
                    error: null };
    var mockAddTimer = null;
    // Симуляция аккаунтов Google: реестр + вход «сам снимает куки».
    var mockAccount = { accounts: [], labels: {}, logging_in: false,
                        note: "", visible: null, browser_warning: "" };
    function mockStorageLabel(id) {
      var hit = mockStorages.filter(function (s) { return s.id === id; })[0];
      return hit ? hit.label : null;
    }
    // Хранилища в превью: одно активное, одно отвязанное - чтобы видеть
    // оба состояния списка в настройках.
    var mockStorages = [
      { id: "st_preview1", path: "D:\\видео\\библиотека", label: "библиотека",
        kind: "local", status: "active", enabled: 1, recursive: 1,
        available: 1, free_bytes: 412345678901, total_bytes: 999000000000,
        missing_since: null, detached_at: null, root_key: "rt_preview" },
      { id: "st_preview2", path: "E:\\Внешний 4ТБ", label: "Внешний 4ТБ",
        kind: "removable", status: "detached", enabled: 1, recursive: 1,
        available: 0, free_bytes: null, total_bytes: null,
        missing_since: null, detached_at: "2026-10-05T12:00:00",
        root_key: "rt_preview2" }
    ];
    // ?empty=1 - «первый запуск»: показать стартовый диалог выбора папки.
    if (location.search.indexOf("empty") >= 0) mockStorages.length = 0;
    // Симуляция скана: заканчивается с находками сверки, чтобы были
    // видны уведомление и раздел «Возможные переезды».
    var mockScan = { running: false, done: 0, total: 0, summary: null,
                     duplicates: [], possible_moves: [],
                     dup_count: 0, move_count: 0 };
    // Симуляция переноса: превью фиксированное, старт «переезжает» файлы.
    var mockMigrate = { running: false, done: 0, total: 0, bytes_done: 0,
                        bytes_total: 0, current: "", summary: null, errors: [] };
    var mockMigrateTimer = null;
    // Переупаковка: превью с готовыми «переименованиями», старт - имитация.
    var mockRepack = { running: false, done: 0, total: 0, current: "",
                       summary: null, error: null, errors: [] };
    var mockRepackTimer = null;
    // Проверка целостности: симулируем один битый файл.
    var mockVerify = { running: false, done: 0, total: 0, current: "",
                       checked: 0, filled: 0, broken: [], broken_total: 0,
                       missing: [], missing_total: 0, summary: null,
                       error: null };
    var mockVerifyTimer = null;
    // Установка ffmpeg: в превью её имитируем, чтобы оверлей было видно.
    var mockFfmpeg = { running: false, phase: "", pct: 0, error: null,
                       found: false, path: null, degraded: true,
                       // в превью - сборка без QSV, чтобы была видна пометка
                       encoders: ["libx265", "nvenc", "amf"] };
    var mockFfmpegTimer = null;
    // Data-URI «обложки» для превью: настоящая картинка моку не нужна.
    function mockThumbUri() {
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="135">' +
        '<rect width="240" height="135" fill="#23444b"/>' +
        '<text x="120" y="72" fill="#dcdedd" text-anchor="middle" ' +
        'font-size="14">обложка (превью)</text></svg>';
      return "data:image/svg+xml;base64," +
        btoa(unescape(encodeURIComponent(svg)));
    }
    var mockDupes = [
      { video_id: 7, key: "youtube:dup00000001", title: "Два раза",
        copies: 2, bytes: 400000,
        files: [
          { id: 101, path: "D:\\видео\\библиотека\\Два раза [dup00000001].mp4",
            size: 200000, storage: "библиотека", available: 1 },
          { id: 102, path: "E:\\Внешний 4ТБ\\Два раза [dup00000001].mp4",
            size: 200000, storage: "Внешний 4ТБ", available: 0 }
        ] }
    ];
    // Схема нужна, чтобы в превью рисовалась карточка настроек.
    // Должна совпадать с app/settings_schema.py (ключи и типы полей).
    var schema = [
      { key: "_storages", type: "storages", section: "Библиотека",
        label: "Хранилища",
        hint: "Папки, которые индексируются и в которые можно качать.",
        transient: true, default: [] },
      { key: "default_storage_id", type: "str", section: "", label: "",
        hint: "", hidden: true, default: "" },
      { key: "default_sync_mode", type: "choice", section: "Библиотека",
        label: "Режим синхронизации по умолчанию",
        hint: "Что делать с новыми видео источника.",
        choices: [["partial", "Частичная"], ["full", "Полная"],
                  ["manual", "Ручная"]], default: "partial" },
      { key: "keep_sidecar", type: "bool", section: "Библиотека",
        label: "Сохранять post.json рядом с файлом",
        hint: "Позволяет переиндексировать библиотеку после переезда.",
        default: true },
      { key: "dest_dir", type: "path", section: "Загрузка",
        label: "Куда скачивать", hint: "нет - заменено хранилищами",
        hidden: true, default: "D:\\видео\\downloads" },
      { key: "quality", type: "choice", section: "Загрузка", label: "Качество",
        choices: [["best", "Исходное"], ["high", "Высокое"],
                  ["mid", "Среднее"], ["low", "Низкое"]], default: "high" },
      { key: "_ffmpeg", type: "action", section: "Загрузка", label: "FFmpeg",
        hint: "Склейка, метаданные, субтитры в файл и перекодировка требуют " +
              "ffmpeg. Мы его НЕ вшиваем (GPL) - ставите сами кнопкой.",
        action: "ffmpeg-install", action_label: "Скачать ffmpeg",
        transient: true, default: [] },
      { key: "transcode", type: "choice", section: "Загрузка",
        label: "Перекодировка",
        hint: "Перекодировать скачанное в HEVC (H.265): место меньше. " +
              "Недоступные в вашей сборке кодеки помечены в списке.",
        choices: [["none", "Не перекодировать"],
                  ["libx265", "HEVC (x265, программный)"],
                  ["nvenc", "HEVC NVIDIA NVENC"],
                  ["amf", "HEVC AMD AMF"],
                  ["qsv", "HEVC Intel QSV"]],
        default: "none" },
      { key: "delay_ms", type: "int", section: "Загрузка",
        label: "Пауза между запросами, мс",
        hint: "Меньше - быстрее, но площадка может начать резать поток.",
        min: 0, max: 60000, default: 500 },
      { key: "scan_interval_min", type: "int", section: "Расписание",
        label: "Автоскан каждые, мин",
        hint: "0 - выключено. Работает, пока открыто окно.",
        min: 0, max: 1440, default: 0 },
      { key: "sync_interval_min", type: "int", section: "Расписание",
        label: "Автосинк каждые, мин",
        hint: "0 - выключено. Новые видео в очередь - только в «Полной».",
        min: 0, max: 1440, default: 30 },
      { key: "view_mode", type: "choice", section: "Библиотека",
        label: "Вид библиотеки",
        hint: "Список - строки; плитка - обложки (превью берутся только из " +
              "локальных файлов и только когда попадают на экран).",
        choices: [["list", "Список"], ["grid", "Плитка с превью"]],
        default: "list" },
      { key: "tile_size", type: "choice", section: "Библиотека",
        label: "Размер плитки",
        hint: "Действует в режиме «Плитка»; колонок столько, сколько влезло.",
        choices: [["small", "Мелкая"], ["medium", "Средняя"],
                  ["large", "Крупная"]],
        default: "medium" },
      { key: "use_google_cookies", type: "bool", section: "Аккаунт Google",
        label: "Использовать аккаунт при загрузках",
        hint: "Куки аккаунта уходят в качалку: возрастной контент и «подтвердите, " +
              "что вы не бот». Копия хранится зашифрованной (DPAPI).",
        default: true },
      { key: "_google_account", type: "account", section: "Аккаунт Google",
        label: "Вход в Google",
        hint: "Вход в окне приложения (куки снимаются сами) или импорт cookies.txt.",
        transient: true, default: [] },
      { key: "theme", type: "choice", section: "Внешний вид", label: "Тема",
        choices: [["dark", "Тёмная"], ["light", "Светлая"]], default: "dark" }
    ];
    mock = {
      get_initial: function () {
        return Promise.resolve({
          version: "0.1.1 (preview)", settings: settings,
          schema: schema, settings_rev: settingsRev
        });
      },
      poll: function () {
        return Promise.resolve({
          log_cursor: ++counter, logs: counter === 1 ? ["превью: данные вымышленные"] : [],
          status: "Превью", busy: false, stats: fixtures,
          tree: {
            pool: fixtures,
            channels: [{ id: 1, title: "Автор А", total: 2, downloaded: 2 },
                       { id: 2, title: "Автор Б", total: 1, downloaded: 0 }],
            playlists: [{ id: 1, title: "Тестовый плейлист", kind: "remote",
                          total: 3, downloaded: 1, sync_mode: "partial",
                          last_synced_at: "2026-10-05T21:00:00" }]
          },
          scan: JSON.parse(JSON.stringify(mockScan)),
          sources: [{ id: 1, title: "Тестовый плейлист", kind_label: "плейлист",
                      sync_mode: "partial", total: 3, downloaded: 1, pending: 2,
                      storage_label: "библиотека",
                      account_label: "привязанный@example.com",
                      last_synced_at: "2026-10-06T16:38:45" },
                    { id: 2, title: "Старый канал", kind_label: "загрузки канала",
                      sync_mode: "full", total: 9, downloaded: 0, pending: 9,
                      storage_label: null,
                      last_synced_at: null }],
          runs: [{ id: 1, kind: "add", started_at: "2026-10-06T16:38:45",
                   finished_at: "2026-10-06T16:38:46",
                   stats: { new_videos: 3, links_to_create: 3 } }],
          queue: [
            { id: 51, key: "youtube:aaa111bbb22", title: "Проблемное видео",
              status: "failed", status_label: "ошибка",
              updated_at: "2026-10-07T02:10:00",
              last_error: "нет хранилища: Внешний 4ТБ не подключён" },
            { id: 52, key: "youtube:ccc333ddd44", title: "Ждёт очереди",
              status: "queued", status_label: "в очереди",
              updated_at: "2026-10-07T02:11:00", last_error: null }
          ],
          dl: (function () {
            if (mockDl.running && mockDl.current) {
              mockDl.current.percent += 9;
              if (mockDl.current.percent >= 100) {
                mockDl.done += 1;
                mockDl.current.percent = 0;
              }
            }
            return { running: mockDl.running, done: mockDl.done,
                     failed: mockDl.failed, attempted: mockDl.attempted,
                     current: mockDl.current, error: null,
                     // площадка просила вход - подсказка видна в превью
                     bot_hint: true };
          })(),
          sync: (function () {
            if (mockSync.running) {
              mockSync.fetch = mockSync.fetch || { got: 0, total: 3 };
              mockSync.fetch.got += 1;
              if (mockSync.fetch.got === 2) {
                mockSync.stage = { id: "videos", state: "active",
                                   current: 1, total: 3 };
              }
              if (mockSync.fetch.got >= mockSync.fetch.total) {
                mockSync.running = false;
                mockSync.fetch = null;
                mockSync.stage = null;
                mockSync.results = [{ title: "Тестовый плейлист",
                                      kind: "плейлист", mode: "partial",
                                      new: 2, known: 1, removed: 0,
                                      queued: 0, error: null }];
                mockSync.new_total = 2;
                mockSync.new_ids = [11, 12];
              }
            }
            return JSON.parse(JSON.stringify(mockSync));
          })(),
          verify: JSON.parse(JSON.stringify(mockVerify)),
          ffmpeg: JSON.parse(JSON.stringify(mockFfmpeg)),
          account: JSON.parse(JSON.stringify(mockAccount)),
          schedule: { scan: { interval: 0, next_in: null, last: null },
                      sync: { interval: 30, next_in: 720,
                              last: "2026-10-07T15:04:00" } },
          repack: JSON.parse(JSON.stringify(mockRepack)),
          migrate: JSON.parse(JSON.stringify(mockMigrate)),
          add_flow: JSON.parse(JSON.stringify(mockAdd)),
          storages: JSON.parse(JSON.stringify(mockStorages)),
          settings: settings, settings_rev: settingsRev
        });
      },
      list_videos: function (req) {
        req = req || {};
        var list = rows.filter(function (row) {
          if (req.status && row.status !== req.status) return false;
          if (req.rating_min && (row.user_rating || 0) < Number(req.rating_min)) return false;
          if (req.query && row.title.toLowerCase().indexOf(req.query.toLowerCase()) < 0) return false;
          return true;
        });
        return Promise.resolve({
          total: list.length,
          rows: list.slice(req.offset || 0, (req.offset || 0) + (req.limit || PAGE))
        });
      },
      get_video: function (id) {
        var row = rows.filter(function (r) { return r.id === id; })[0] || rows[0];
        return Promise.resolve(Object.assign({}, row, {
          origin: "preview",
          webpage_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
          files: [{ kind: "video", size: 240000000, missing: 0,
                    path: "D:\\видео\\библиотека\\Ночной дождик [dQw4w9WgXcQ].mp4" }],
          raw: { preview: true, id: row.key }
        }));
      },
      enqueue: function (req) {
        return Promise.resolve({ queued: ((req || {}).ids || []).length });
      },
      open_path: function () { return Promise.resolve(null); },
      open_url: function () { return Promise.resolve(null); },
      save_setting: function (pair) {
        settings[pair.key] = pair.value;
        // rev растёт, как в настоящем окне: иначе после первого сохранения
        // ветка смены настроек в превью перестанет срабатывать.
        return Promise.resolve({ settings: settings,
                                 settings_rev: ++settingsRev });
      },
      account_login_start: function () {
        mockAccount.logging_in = true;
        mockAccount.note = "";
        setTimeout(function () {
          // «Авто-поимка»: площадка выдала сессию - аккаунт в реестре.
          mockAccount.logging_in = false;
          mockAccount.accounts = mockAccount.accounts.concat([{
            id: "acc_preview", label: "превью@example.com",
            since: "2026-10-10T12:00:00"
          }]);
          mockAccount.note = "Аккаунт добавлен: превью@example.com - " +
            "привяжите его к источнику";
        }, 1800);
        return Promise.resolve({ ok: true });
      },
      account_capture_now: function () {
        // Ручная поимка в моке: если «окно входа» не открывали - честная
        // ошибка, иначе - аккаунт.
        if (!mockAccount.logging_in) {
          return Promise.resolve({ error: "Окно входа не открыто" });
        }
        mockAccount.accounts = mockAccount.accounts.concat([{
          id: "acc_capture", label: "человек@example.com",
          since: "2026-10-10T12:00:00"
        }]);
        mockAccount.logging_in = false;
        mockAccount.note = "Аккаунт добавлен: человек@example.com";
        return Promise.resolve({ ok: true,
                                 account: { label: "человек@example.com" } });
      },
      account_login_stop: function () {
        mockAccount.logging_in = false;
        return Promise.resolve({ ok: true });
      },
      account_visible: function () {
        if (!mockAccount.logging_in) {
          return Promise.resolve({ error: "Окно входа не открыто" });
        }
        return Promise.resolve({ total: 4, google: 4, markers: 3,
                                 domains: ["google.com", "youtube.com"],
                                 names: ["SID", "HSID", "SAPISID"] });
      },
      account_import: function () {
        mockAccount.accounts = mockAccount.accounts.concat([{
          id: "acc_file", label: "импорт cookies.txt",
          since: "2026-10-10T12:00:00"
        }]);
        mockAccount.note = "Аккаунт добавлен из файла";
        return Promise.resolve({ ok: true });
      },
      account_forget: function (req) {
        var id = (req || {}).id;
        mockAccount.accounts = mockAccount.accounts.filter(function (a) {
          return a.id !== id;
        });
        mockAccount.note = "Аккаунт забыт";
        return Promise.resolve({ ok: true, removed: true });
      },
      ffmpeg_start: function () {
        if (mockFfmpeg.found) {
          return Promise.resolve({ ok: true, already: true,
                                   path: mockFfmpeg.path });
        }
        mockFfmpeg.running = true;
        mockFfmpeg.phase = "download";
        mockFfmpeg.pct = 0;
        mockFfmpeg.error = null;
        if (mockFfmpegTimer) clearInterval(mockFfmpegTimer);
        mockFfmpegTimer = setInterval(function () {
          mockFfmpeg.pct = Math.min(100, mockFfmpeg.pct + 20);
          if (mockFfmpeg.phase === "download" && mockFfmpeg.pct >= 60) {
            mockFfmpeg.phase = "extract";
          }
          if (mockFfmpeg.pct >= 100) {
            clearInterval(mockFfmpegTimer);
            mockFfmpegTimer = null;
            mockFfmpeg.running = false;
            mockFfmpeg.phase = "done";
            mockFfmpeg.found = true;
            mockFfmpeg.degraded = false;
            mockFfmpeg.path = "C:\\превью\\AppData\\Local\\Omnistash\\bin\\ffmpeg.exe";
          }
        }, 500);
        return Promise.resolve({ ok: true });
      },
      ffmpeg_stop: function () {
        if (mockFfmpegTimer) {
          clearInterval(mockFfmpegTimer);
          mockFfmpegTimer = null;
        }
        mockFfmpeg.running = false;
        mockFfmpeg.phase = "cancelled";
        mockFfmpeg.pct = 0;
        return Promise.resolve({ ok: true });
      },
      verify_start: function () {
        mockVerify = { running: true, done: 0, total: 5,
                       current: "файл-1.mp4", checked: 0, filled: 0,
                       broken: [], broken_total: 0, missing: [],
                       missing_total: 0, summary: null, error: null };
        var step = 0;
        mockVerifyTimer = setInterval(function () {
          step += 1;
          mockVerify.done = step;
          mockVerify.checked = step;
          mockVerify.current = "файл-" + step + ".mp4";
          if (step >= 5) {
            clearInterval(mockVerifyTimer);
            mockVerify.running = false;
            mockVerify.broken_total = 1;
            mockVerify.broken = [{
              video_id: 4, file_id: 9, title: "Файл без личности",
              path: "D:\\видео\\библиотека\\битое.mp4",
              expected: "sha256:aa", actual: "sha256:bb"
            }];
            mockVerify.summary = "Проверено 5 из 5 · битых 1 · без хеша 0 · нет файла 0";
          }
        }, 600);
        return Promise.resolve({ ok: true });
      },
      verify_stop: function () {
        if (mockVerifyTimer) clearInterval(mockVerifyTimer);
        mockVerify.running = false;
        mockVerify.summary = "Остановлено: проверено " + mockVerify.checked +
          " из " + mockVerify.total + " · битых " + mockVerify.broken_total;
        return Promise.resolve({ ok: true });
      },
      repair_broken: function () {
        return Promise.resolve({ queued: mockVerify.broken_total });
      },
      save_fields: function (req) {
        return Promise.resolve({ ok: true,
                                 updated: ((req || {}).ids || []).length });
      },
      get_thumb: function () {
        return Promise.resolve({ ok: true, data: mockThumbUri() });
      },
      get_thumbs: function (req) {
        // В моке обложки есть у «нечётных» id - как has_thumb в фикстурах.
        var thumbs = {};
        (((req || {}).ids) || []).forEach(function (id) {
          if (id % 2 === 1) thumbs[String(id)] = mockThumbUri();
        });
        return Promise.resolve({ thumbs: thumbs });
      },
      open_file: function (req) {
        return Promise.resolve({ ok: true,
                                 path: "(превью) видео " + (req || {}).id + ".mp4" });
      },
      open_folder: function (req) {
        return Promise.resolve({ ok: true,
                                 path: "(превью) папка для " + (req || {}).id });
      },
      pick_folder: function () {
        // В превью «выбираем» новую папку - тот же контракт, что у окна.
        return Promise.resolve({ path: "D:\\видео\\подборки" });
      },
      open_log: function () {
        return Promise.resolve({ ok: true, path: "(превью) omnistash.log" });
      },
      start_scan: function () {
        mockScan = { running: true, done: 0, total: 120,
                     path: "D:\\видео\\библиотека", summary: null,
                     duplicates: [], possible_moves: [], dup_count: 0,
                     move_count: 0 };
        setTimeout(function () {
          mockScan = {
            running: false, done: 120, total: 120, path: "",
            summary: "Готово: скан 120 файлов, копий найдено 1, " +
                     "похоже на переезд 1",
            duplicates: [], dup_count: 1, move_count: 1,
            possible_moves: [
              { video_id: 7, title: "Переехавшее видео",
                path: "D:\\видео\\подборки\\Переехавшее [mov00000001].mp4",
                other: "E:\\Внешний 4ТБ\\Переехавшее [mov00000001].mp4",
                from: "Внешний 4ТБ" }
            ]
          };
        }, 1600);
        return Promise.resolve({ ok: true });
      },
      stop_scan: function () { return Promise.resolve({ ok: true }); },
      duplicates: function () {
        return Promise.resolve({ groups: JSON.parse(JSON.stringify(mockDupes)),
                                 count: mockDupes.length, files: 2 });
      },
      dedupe_resolve: function (req) {
        mockDupes = mockDupes.filter(function (g) {
          return g.video_id !== Number(req.video_id);
        });
        return Promise.resolve({ ok: true, removed: ["путь/к/файлу"],
                                 errors: [], kept: "путь/к/оставленному",
                                 title: "Два раза" });
      },
      migrate_preview: function (req) {
        var target = mockStorages.filter(function (s) {
          return s.id === (req || {}).target_storage_id && s.status === "active";
        })[0];
        return Promise.resolve({
          ok: true, count: 2, bytes: 1234567,
          conflicts: [{ path: "D:\\видео\\библиотека\\а.mp4",
                        target: (target ? target.path : "E:\\") + "\\а.mp4" }],
          conflict_total: 1, already: 1, missing: [], missing_total: 0,
          same_storage: 0,
          target: { id: target ? target.id : "", path: target ? target.path : "",
                    label: target ? target.label : "" }
        });
      },
      migrate_start: function () {
        mockMigrate = { running: true, done: 0, total: 2, bytes_done: 0,
                        bytes_total: 1234567, current: "первый.mp4",
                        summary: null, errors: [] };
        var step = 0;
        mockMigrateTimer = setInterval(function () {
          step += 1;
          mockMigrate.done = Math.min(step, 2);
          mockMigrate.bytes_done = Math.min(step * 620000, 1234567);
          mockMigrate.current = "файл-" + step + ".mp4";
          if (step >= 2) {
            clearInterval(mockMigrateTimer);
            mockMigrate.running = false;
            mockMigrate.summary = "Перенесено 2 из 2 (1.2 МиБ)";
          }
        }, 700);
        return Promise.resolve({ ok: true, count: 2, bytes: 1234567,
                                 conflict_total: 1 });
      },
      migrate_stop: function () {
        if (mockMigrateTimer) clearInterval(mockMigrateTimer);
        mockMigrate.running = false;
        mockMigrate.summary =
          "Перенос остановлен - уже перенесённое осталось в цели";
        return Promise.resolve({ ok: true });
      },
      repack_preview: function () {
        return Promise.resolve({
          ok: true,
          template: "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s",
          selected: 4, count: 2, rename_total: 2, unchanged: 1, bytes: 734003200,
          rename: [
            { video_id: 1, title: "Первый ролик",
              from: "D:\\видео\\библиотека\\Первый ролик [aaa111bbb22].mp4",
              to: "D:\\видео\\библиотека\\Автор\\20250101 - Первый ролик [aaa111bbb22].mp4" },
            { video_id: 2, title: "Второй ролик",
              from: "D:\\видео\\библиотека\\Второй ролик [ccc333ddd44].mp4",
              to: "D:\\видео\\библиотека\\Автор\\20250202 - Второй ролик [ccc333ddd44].mp4" }
          ],
          conflicts: [{ path: "x", target: "y", title: "Чужой файл" }],
          conflict_total: 1,
          no_meta: [{ title: "Без имени", reason: "нет метаданных площадки (локальный файл)" }],
          no_meta_total: 1
        });
      },
      repack_start: function () {
        mockRepack = { running: true, done: 0, total: 3, current: "первый.mp4",
                       summary: null, error: null, errors: [] };
        var step = 0;
        mockRepackTimer = setInterval(function () {
          step += 1;
          mockRepack.done = Math.min(step * 2, 3);
          mockRepack.current = "файл-" + step;
          if (step >= 2) {
            clearInterval(mockRepackTimer);
            mockRepack.running = false;
            mockRepack.summary = "Переименовано 2 из 2, удалено пустых папок 1";
          }
        }, 700);
        return Promise.resolve({ ok: true, count: 2, conflict_total: 1,
                                 no_meta_total: 1 });
      },
      repack_stop: function () {
        if (mockRepackTimer) clearInterval(mockRepackTimer);
        mockRepack.running = false;
        mockRepack.summary = "Переупаковка остановлена - уже переименованное осталось";
        return Promise.resolve({ ok: true });
      },
      storage_add: function (req) {
        if ((req || {}).path === "D:\\видео\\библиотека") {
          return Promise.resolve({ hint: "already" });
        }
        var created = { id: "st_" + Math.random().toString(16).slice(2, 10),
                        path: req.path, label: "подборки", kind: "local",
                        status: "active", enabled: 1, recursive: 1,
                        available: 1, free_bytes: 12345678901,
                        missing_since: null, detached_at: null };
        mockStorages.push(created);
        return Promise.resolve({ ok: true, storage: created,
                                 default_storage_id: settings.default_storage_id });
      },
      storage_check: function () { return Promise.resolve({ ok: true }); },
      storage_set_path: function (req) {
        var s = mockStorages.filter(function (x) { return x.id === req.id; })[0];
        if (s) s.path = req.path;
        return Promise.resolve({ ok: true, files: 12, present: 12,
                                 old_path: "старый", new_path: req.path });
      },
      storage_preview_detach: function (req) {
        var s = mockStorages.filter(function (x) { return x.id === req.id; })[0] || {};
        return Promise.resolve({ label: s.label || "?", path: s.path || "",
                                 files: 1240, detached: 1180,
                                 local_deleted: 60, kept_elsewhere: 0,
                                 status: s.status });
      },
      storage_detach: function (req) {
        var s = mockStorages.filter(function (x) { return x.id === req.id; })[0];
        if (s) s.status = "detached";
        return Promise.resolve({ ok: true, label: s ? s.label : "?",
                                 files_removed: 1240,
                                 kept_trace: !!req.keep_trace });
      },
      storage_restore: function (req) {
        var s = mockStorages.filter(function (x) { return x.id === req.id; })[0];
        if (s) s.status = "active";
        return Promise.resolve({ ok: true, storage: s });
      },
      storage_forget: function (req) {
        mockStorages = mockStorages.filter(function (x) { return x.id !== req.id; });
        return Promise.resolve({ ok: true, label: "забыто" });
      },
      storage_enable: function (req) {
        var s = mockStorages.filter(function (x) { return x.id === req.id; })[0];
        if (s) s.enabled = req.enabled ? 1 : 0;
        return Promise.resolve({ ok: true });
      },
      storage_set_default: function (req) {
        settings.default_storage_id = req.id;
        return Promise.resolve({ ok: true, default_storage_id: req.id });
      },
      queue_start: function () {
        mockDl.running = true;
        mockDl.attempted += 1;
        mockDl.current = { id: 1, title: "Ночной дождик [dQw4w9WgXcQ].mp4",
                           percent: 0, stage: "файл", speed: 1048576, eta: 7 };
        return Promise.resolve({ ok: true, queued: 3 });
      },
      queue_stop: function () {
        mockDl.running = false;
        mockDl.current = null;
        return Promise.resolve({ ok: true });
      },
      queue_retry: function () {
        var retried = mockDl.failed;
        mockDl.failed = 0;
        return Promise.resolve({ ok: true, retried: retried });
      },
      sync_start: function () {
        mockSync.running = true;
        mockSync.index = 1;
        mockSync.total = 1;
        mockSync.current = "Тестовый плейлист";
        mockSync.results = [];
        mockSync.new_total = 0;
        mockSync.queued = 0;
        mockSync.fetch = { got: 0, total: 3 };
        return Promise.resolve({ ok: true, sources: 1 });
      },
      sync_stop: function () {
        mockSync.running = false;
        mockSync.fetch = null;
        mockSync.stage = null;
        return Promise.resolve({ ok: true });
      },
      sync_queue_new: function () {
        mockSync.queued = mockSync.new_total;
        return Promise.resolve({ queued: mockSync.new_total });
      },
      add_start: function (req) {
        req = req || {};
        if (!(req.url || "").trim()) {
          return Promise.resolve(
            { error: "Вставьте ссылку на плейлист или канал" });
        }
        if (mockAddTimer) clearInterval(mockAddTimer);
        mockAdd = { phase: "fetching", mode: req.mode || "partial",
                    storage_id: req.storage_id || null,
                    account_id: req.account_id || null,
                    url: req.url,
                    fetch: { got: 0, total: 40 }, plan: null, stages: [],
                    result: null, error: null };
        var got = 0;
        mockAddTimer = setInterval(function () {
          got += 8;
          mockAdd.fetch.got = Math.min(got, 40);
          if (got >= 40) {
            clearInterval(mockAddTimer);
            mockAddTimer = null;
            mockAdd.phase = "confirm";
            mockAdd.plan = { title: "(превью) Плейлист", channel: "Автор А",
                             kind: "remote", item_count: 40, exists: false,
                             is_mix: false,
                             counts: { new_channels: 2, known_channels: 0,
                                       new_videos: 12, known_videos: 3,
                                       links_to_create: 12, links_existing: 0,
                                       skipped: 1, dupes: 0 } };
          }
        }, 200);
        return Promise.resolve({ ok: true });
      },
      add_confirm: function (req) {
        req = req || {};
        if (req.storage_id !== undefined) {
          mockAdd.storage_id = req.storage_id || null;
        }
        if (req.account_id !== undefined) {
          mockAdd.account_id = req.account_id || null;
        }
        if (req.mode) mockAdd.mode = req.mode;
        mockAdd.phase = "committing";
        setTimeout(function () {
          var full = mockAdd.mode === "full";
          var account = (mockAccount.accounts || []).filter(function (a) {
            return a.id === mockAdd.account_id;
          })[0];
          mockAdd.phase = "done";
          mockAdd.result = {
            stats: { new_videos: 12, known_videos: 3, links_to_create: 12,
                     removed: 0 },
            mode: mockAdd.mode,
            queued: full ? 12 : 0,
            picker: full ? [] : [{ id: 71, title: "(превью) ролик 1",
                                   duration_s: 610 },
                                 { id: 72, title: "(превью) ролик 2",
                                   duration_s: 130 }],
            picker_total: full ? 0 : 12,
            storage_id: mockAdd.storage_id,
            storage_label: mockStorageLabel(mockAdd.storage_id),
            account_id: mockAdd.account_id,
            account_label: account ? account.label : "",
            title: mockAdd.plan ? mockAdd.plan.title : "(превью) Плейлист",
            url: mockAdd.url
          };
        }, 700);
        return Promise.resolve({ ok: true });
      },
      add_close: function () {
        if (mockAddTimer) { clearInterval(mockAddTimer); mockAddTimer = null; }
        mockAdd = { phase: "idle", mode: mockAdd.mode,
                    storage_id: mockAdd.storage_id, url: mockAdd.url,
                    fetch: null, plan: null, stages: [], result: null,
                    error: null };
        return Promise.resolve({ ok: true });
      }
    };
    $("preview-badge").hidden = false;
    bootOnce();
  }

  /* Старт: ждём мост pywebview; если его нет через 2 с - рисуем превью.
     Оба пути сходятся в bootOnce, поэтому окно не инициализируется дважды. */
  var booted = false;

  function bootOnce() {
    if (booted) return;
    booted = true;
    boot();
  }

  document.addEventListener("pywebviewready", function () {
    clearTimeout(previewTimer);
    bootOnce();
  });

  var previewTimer = setTimeout(function () {
    if (window.pywebview) { bootOnce(); return; }
    installPreview();   // выставит mock, сам вызовет boot()
  }, 2000);
})();
