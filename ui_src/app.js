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
    selected: null
  };

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
    if (changed("scan", snap.scan)) renderScan(snap.scan);
    if (changed("tree", snap.tree)) renderTree(snap.tree);
    if (changed("sources", snap.sources)) renderSources(snap.sources);
    if (changed("runs", snap.runs)) renderRuns(snap.runs);
    if (changed("queue", snap.queue)) renderQueue(snap.queue);
    if (changed("dl", snap.dl)) renderDl(snap.dl);
    if (changed("sync", snap.sync)) renderSync(snap.sync);
    if (changed("add", snap.add_flow)) renderAddFlow(snap.add_flow);
    appendLogs(snap.logs);

    if (snap.settings_rev !== state.rev) {
      state.rev = snap.settings_rev;
      state.settings = snap.settings || state.settings;
      document.body.dataset.theme = state.settings.theme === "light" ? "light" : "dark";
      renderSettings();
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
    var bar = $("scan-bar");
    if (!scan) { bar.hidden = true; return; }
    if (scan.running) {
      bar.hidden = false;
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
  }

  function renderTree(tree) {
    if (!tree) return;
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
   *  Таблица библиотеки (виртуализация)
   * ------------------------------------------------------------------ */

  function renderGrid(reset) {
    if (reset) {
      state.pages = {};
      state.total = 0;
      $("grid-body").scrollTop = 0;
      $("grid-rows").style.transform = "translateY(0)";
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
    $("grid-spacer").style.height = (total * ROW_H) + "px";
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

    var start = Math.max(0, Math.floor(body.scrollTop / ROW_H) - 4);
    var end = Math.min(total, Math.ceil((body.scrollTop + body.clientHeight) / ROW_H) + 4);
    ensureRange(start, end, function () {
      var html = [];
      for (var i = start; i < end; i++) {
        var row = rowAt(i);
        if (!row) continue;
        html.push(rowHtml(row, i));
      }
      rows.style.transform = "translateY(" + (start * ROW_H) + "px)";
      rows.innerHTML = html.join("");
      Array.prototype.forEach.call(rows.querySelectorAll(".row"), function (node) {
        node.addEventListener("click", function () {
          openDetail(Number(node.dataset.id));
        });
      });
    });
  }

  function rowAt(index) {
    var page = Math.floor(index / PAGE);
    var list = state.pages[page];
    if (!list) return null;
    return list[index - page * PAGE] || null;
  }

  function rowHtml(row, index) {
    return '<div class="row" data-id="' + row.id + '" data-i="' + index + '">' +
      '<span class="cell status" data-s="' + esc(row.status) + '">' +
        esc(row.status_label || statusLabel(row.status)) + "</span>" +
      '<span class="cell c-title" title="' + esc(row.title) + '">' + esc(row.title || "—") + "</span>" +
      '<span class="cell c-channel" title="' + esc(row.channel) + '">' + esc(row.channel || "—") + "</span>" +
      '<span class="cell c-dur">' + humanDuration(row.duration_s) + "</span>" +
      '<span class="cell c-date">' + shortDate(row.uploaded_at) + "</span>" +
      '<span class="cell c-size">' + (row.size ? humanSize(row.size) : "—") + "</span>" +
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
      if (data.notes) add("Заметки", data.notes);
      if (data.user_tags) add("Теги", data.user_tags);
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
      if (["known", "failed", "missing"].indexOf(data.status) >= 0) {
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
      $("detail-body").innerHTML = html + raw;
      $("detail-overlay").hidden = false;
      bindDetail(data);
    });
  }

  function bindDetail(data) {
    Array.prototype.forEach.call(
      document.querySelectorAll("#detail-body [data-detail]"), function (btn) {
        btn.addEventListener("click", function () {
          var action = btn.dataset.detail;
          if (action === "download") {
            call("enqueue", { ids: [data.id] }).then(function (res) {
              if (res && res.error) { toast(res.error, true); return; }
              toast("В очередь поставлено: " + (res ? res.queued : 0));
              $("detail-overlay").hidden = true;
              switchTab("queue");
            });
          } else if (action === "queue") {
            $("detail-overlay").hidden = true;
            switchTab("queue");
          } else if (action === "folder") {
            // Папка - это каталог самого видео, а не его родитель от корня.
            var file = (data.files || []).filter(function (f) {
              return f.kind === "video" && !f.missing;
            })[0];
            if (file) {
              call("open_path", file.path.replace(/[\\/][^\\/]*$/, ""));
            }
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
      return "<tr><td><span class=\"status\" data-s=\"" + esc(row.status) + "\">" +
        esc(row.status_label) + "</span></td><td>" + esc(row.title || "—") +
        "</td><td class=\"muted\">" + esc(row.key) + "</td></tr>";
    }).join("");
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
      var name = field.section || "Прочее";
      if (!bySection[name]) { bySection[name] = []; sections.push(name); }
      bySection[name].push(field);
    });
    body.innerHTML = sections.map(function (name) {
      return '<div class="set-section"><h3>' + esc(name) + "</h3>" +
        bySection[name].map(fieldHtml).join("") + "</div>";
    }).join("");
    bindSettings();
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
    } else if (field.type === "int") {
      control = '<input type="number" data-key="' + field.key + '" value="' +
        esc(value) + '"' +
        (field.min !== undefined ? ' min="' + field.min + '"' : "") +
        (field.max !== undefined ? ' max="' + field.max + '"' : "") + ">";
    } else if (field.type === "path") {
      control = '<input type="text" data-key="' + field.key + '" value="' +
        esc(value) + '"><button class="btn ghost" data-browse="' + field.key +
        '">…</button>';
    } else if (field.type === "roots") {
      control = rootsHtml(value || []);
    } else {
      control = '<input type="text" data-key="' + field.key + '" value="' +
        esc(value) + '">';
    }
    return '<div class="field"><div><div class="field-label">' +
      esc(field.label) + '</div><div class="field-hint">' +
      esc(field.hint || "") + "</div></div>" +
      '<div class="field-control">' + control + "</div></div>";
  }

  function rootsHtml(roots) {
    var rows = roots.map(function (root, index) {
      return '<div class="root-row' + (root.enabled ? "" : " off") + '" data-i="' + index + '">' +
        '<span class="root-path" title="' + esc(root.path) + '">' + esc(root.path) + "</span>" +
        '<label class="check"><input type="checkbox" data-root-recursive="' + index + '"' +
          (root.recursive ? " checked" : "") + "><span>вглубь</span></label>" +
        '<label class="check"><input type="checkbox" data-root-enabled="' + index + '"' +
          (root.enabled ? " checked" : "") + "><span>включён</span></label>" +
        '<button class="link-btn" data-root-remove="' + index + '">убрать</button>' +
        "</div>";
    }).join("");
    return '<div class="roots">' + rows +
      '<div><button class="btn ghost" id="roots-add">+ Добавить папку</button></div></div>';
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

    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-browse]"), function (btn) {
        btn.addEventListener("click", function () {
          call("pick_folder").then(function (path) {
            if (!path) return;
            saveSetting(btn.dataset.browse, path);
          });
        });
      });

    var add = document.getElementById("roots-add");
    if (add) add.addEventListener("click", function () {
      call("pick_folder").then(function (path) {
        if (!path) return;
        var roots = (state.settings.library_roots || []).slice();
        roots.push({ path: path, recursive: true, enabled: true });
        saveSetting("library_roots", roots);
      });
    });

    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-root-remove]"), function (btn) {
        btn.addEventListener("click", function () {
          var roots = (state.settings.library_roots || []).slice();
          roots.splice(Number(btn.dataset.rootRemove), 1);
          saveSetting("library_roots", roots);
        });
      });

    Array.prototype.forEach.call(
      document.querySelectorAll("#settings-body [data-root-recursive], " +
        "#settings-body [data-root-enabled]"), function (input) {
        input.addEventListener("change", function () {
          var roots = (state.settings.library_roots || []).map(function (r) {
            return { path: r.path, recursive: r.recursive, enabled: r.enabled };
          });
          var index = Number(input.dataset.rootRecursive !== undefined
            ? input.dataset.rootRecursive : input.dataset.rootEnabled);
          if (input.dataset.rootRecursive !== undefined) roots[index].recursive = input.checked;
          else roots[index].enabled = input.checked;
          saveSetting("library_roots", roots);
        });
      });
  }

  function saveSetting(key, value) {
    call("save_setting", { key: key, value: value }).then(function (res) {
      if (!res) return;
      state.settings = res.settings || state.settings;
      state.rev = res.settings_rev;
      document.body.dataset.theme = state.settings.theme === "light" ? "light" : "dark";
      renderSettings();
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
  var addForm = { url: "", mode: "partial", picked: {} };

  var ADD_MODES = [
    ["partial", "Частичная", "Новые видео попадают в индекс, контент выбираете вручную"],
    ["full", "Полная", "Новые видео сразу встают в очередь загрузки"],
    ["manual", "Ручная", "Источник обновляется только по вашей кнопке"]
  ];

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
        (stats.removed ? "Убрано из плейлиста: " + stats.removed : "")
      ].filter(Boolean).map(function (line) {
        return "<li>" + esc(line) + "</li>";
      }).join("");

      html = '<ul class="kv-list">' + lines + "</ul>";

      if (result.mode === "full") {
        html += '<div class="notice ok">В очередь загрузки поставлено: <b>' +
          (result.queued || 0) + "</b></div>" +
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
        '<div class="card-actions"><button class="btn primary" data-act="run">Индексировать</button></div>';
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

    Array.prototype.forEach.call(body.querySelectorAll('input[name="add-mode"]'),
      function (radio) {
        radio.addEventListener("change", function () { addForm.mode = radio.value; });
      });

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

  function runAdd() {
    if (!addForm.url.trim()) { toast("Вставьте ссылку", true); return; }
    call("add_start", { url: addForm.url.trim(), mode: addForm.mode })
      .then(function (res) {
        if (res && res.error) toast(res.error, true);
        else addOpen = true;
      });
  }

  function addAction(action) {
    if (action === "close" || action === "cancel") { closeAdd(); return; }
    if (action === "run") { runAdd(); return; }
    if (action === "confirm") {
      call("add_confirm", { mode: addForm.mode }).then(function (res) {
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
      call("enqueue", { ids: ids }).then(function (res) {
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

    $("grid-body").addEventListener("scroll", function () {
      if (!paintGrid._raf) {
        paintGrid._raf = requestAnimationFrame(function () {
          paintGrid._raf = 0;
          paintGrid();
        });
      }
    });

    $("rescan-btn").addEventListener("click", startScan);
    $("stop-scan-btn").addEventListener("click", function () { call("stop_scan"); });

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
      call("sync_queue_new").then(function (res) {
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
        channel: "Автор А", size: 240000000 },
      { id: 2, key: "youtube:ccc333ddd44", title: "Утренний туман", status: "known",
        status_label: "в индексе", duration_s: 130, uploaded_at: "2024-11-02",
        channel: "Автор Б", size: 0 },
      { id: 3, key: "youtube:eee555fff66", title: "Долгая дорога", status: "downloaded",
        status_label: "скачано", duration_s: 540, uploaded_at: "2023-06-30",
        channel: "Автор А", size: 272000000 },
      { id: 4, key: "local:abc", title: "Файл без личности", status: "downloaded",
        status_label: "скачано", duration_s: 61, uploaded_at: null,
        channel: null, size: 0 },
      { id: 5, key: "youtube:ggg777hhh88", title: "Снятый клип", status: "missing",
        status_label: "файл пропал", duration_s: 200, uploaded_at: "2022-02-02",
        channel: "Автор В", size: 0 }
    ];
    var settings = {
      library_roots: [{ path: "D:\\видео\\библиотека", recursive: true, enabled: true }],
      default_sync_mode: "partial", keep_sidecar: true, compute_hash: true,
      dest_dir: "D:\\видео\\downloads",
      output_template: "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s",
      quality: "high", subtitles: "none", transcode: "none",
      delay_ms: 500, retries: 3, theme: "dark", app_version: ""
    };
    var counter = 0;
    // Симуляция качалки: в превью нет сети, но поведение панели очереди
    // (кнопки, полоса, счётчики) должно быть проверяемо.
    var mockDl = { running: false, done: 3, failed: 1, attempted: 4,
                   current: null, error: null };
    // Симуляция синхронизации: fetch растёт, затем падает в результат.
    var mockSync = { running: false, index: 0, total: 0, current: null,
                     fetch: null, stage: null, results: [], new_ids: [],
                     new_total: 0, queued: 0, error: null };
    // Схема нужна, чтобы в превью рисовалась карточка настроек.
    var schema = [
      { key: "library_roots", type: "roots", section: "Библиотека",
        label: "Папки библиотеки", hint: "Что сканировать при переиндексации.",
        default: [] },
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
        label: "Куда скачивать", hint: "Корень для новых загрузок.",
        default: "D:\\видео\\downloads" },
      { key: "quality", type: "choice", section: "Загрузка", label: "Качество",
        choices: [["best", "Исходное"], ["high", "Высокое"],
                  ["mid", "Среднее"], ["low", "Низкое"]], default: "high" },
      { key: "delay_ms", type: "int", section: "Загрузка",
        label: "Пауза между запросами, мс",
        hint: "Меньше - быстрее, но площадка может начать резать поток.",
        min: 0, max: 60000, default: 500 },
      { key: "theme", type: "choice", section: "Внешний вид", label: "Тема",
        choices: [["dark", "Тёмная"], ["light", "Светлая"]], default: "dark" }
    ];
    mock = {
      get_initial: function () {
        return Promise.resolve({
          version: "0.1.0 (preview)", settings: settings,
          schema: schema, settings_rev: 1
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
          scan: null,
          sources: [{ id: 1, title: "Тестовый плейлист", kind_label: "плейлист",
                      sync_mode: "partial", total: 3, downloaded: 1, pending: 2,
                      last_synced_at: "2026-10-06T16:38:45" }],
          runs: [{ id: 1, kind: "add", started_at: "2026-10-06T16:38:45",
                   finished_at: "2026-10-06T16:38:46",
                   stats: { new_videos: 3, links_to_create: 3 } }],
          queue: [],
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
                     current: mockDl.current, error: null };
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
          add_flow: { phase: "idle", mode: "partial", url: "", fetch: null,
                      plan: null, stages: [], result: null, error: null },
          settings: settings, settings_rev: 1
        });
      },
      list_videos: function (req) {
        req = req || {};
        var list = rows.filter(function (row) {
          if (req.status && row.status !== req.status) return false;
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
        return Promise.resolve({ settings: settings, settings_rev: 1 });
      },
      pick_folder: function () { return Promise.resolve(null); },
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
      add_start: function () { return Promise.resolve({ error: "в превью недоступно" }); },
      start_scan: function () { return Promise.resolve({ error: "в превью недоступно" }); },
      stop_scan: function () { return Promise.resolve({}); }
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
