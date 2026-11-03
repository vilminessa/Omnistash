/* Страница воспроизведения клиента (docs/SERVER-CLIENT.md §6, §7 - S4).
   Рантайм грузит только свой адрес: /api/*, /media, /subs, /hls, /app.css,
   /vendor/hls.js - ни одного чужого хоста и ни одной загрузки из сети.
   Транспорт тот же, что в app.js: POST /api/<имя>, Bearer из localStorage,
   аргумент позиционным списком ([arg]), пустое тело - "{}" когда аргумента
   нет. Отказ сервер называет сам ({error, code}); когда тела нет, страница
   подписывает по коду - без выдуманных данных и без молчания. */
(function () {
  "use strict";

  var mark = window.OMNISTASH_HTTP || null;
  var base = (mark && mark.base) ? mark.base : "";

  var statusEl = document.getElementById("watch-status");
  var videoEl = document.getElementById("watch-video");
  var audioEl = document.getElementById("watch-audio");
  var titleEl = document.getElementById("watch-title");
  var captionsBtn = document.getElementById("watch-captions");
  var subsNote = document.getElementById("watch-subs-note");
  var filesEl = document.getElementById("watch-files");

  var texts = {};
  function t(key, fallback) {
    return texts[key] || fallback;
  }
  function say(text, bad) {
    statusEl.textContent = text;
    statusEl.className = "watch-status" + (bad ? " watch-error" : "");
  }

  function token() {
    try {
      return localStorage.getItem("omnistash.token") || "";
    } catch (err) {
      // localStorage недоступен (приватный режим): ключа нет - честный отказ.
      return "";
    }
  }
  function headers() {
    var sent = { "Content-Type": "application/json" };
    var secret = token();
    if (secret) sent.Authorization = "Bearer " + secret;
    return sent;
  }

  /* Подпись по коду ответа, когда сервер молчит телом: каждая строка есть
     в словаре i18n (ключи http.*), и ни одна не выдумывает успех. */
  function problemText(response, payload) {
    if (payload && payload.error) return String(payload.error);
    var code = response.status;
    if (code === 401) return t("http.pairing", "ключ доступа не принят - привязка не завершена");
    if (code === 403) return t("http.scope", "право не выдано сервером");
    if (code === 404) return t("http.missing", "не найдено");
    if (code === 413) return t("http.too_large", "слишком большой запрос для сервера");
    if (code === 415) return t("http.subs_format", "формат субтитров браузер не читает");
    if (code === 416) return t("http.range", "запрошенная часть файла не читается");
    if (code === 429) return t("http.rate", "сервер ограничил отдачу (лимит скорости или зрителей)");
    if (code >= 500) return t("http.server", "сервер ответил ошибкой");
    return t("http.bad_answer", "неожиданный ответ сервера") + " (" + code + ")";
  }

  function readBody(response) {
    return response.json().catch(function () {
      return null;   // не JSON: подписываем по коду, а не «данные получены»
    }).then(function (payload) {
      if (response.status >= 400 || payload === null) {
        return { error: problemText(response, payload), code: "http" };
      }
      return payload;
    });
  }

  function call(name, arg) {
    if (!token()) {
      return Promise.resolve({ error: t("http.no_token",
                                        "ключ доступа не получен - привязка не завершена"),
                               code: "pairing" });
    }
    // Тот же нюанс, что с pywebview: аргумент едет позиционным списком,
    // аргумента нет - пустой объект, чтобы метод без параметров не получил null.
    var body = (arg === undefined || arg === null) ? "{}" : JSON.stringify([arg]);
    return fetch(base + "/api/" + name, {
      method: "POST", headers: headers(), body: body, credentials: "same-origin"
    }).then(readBody).catch(function () {
      return { error: t("http.offline", "сервер не отвечает"), code: "transport" };
    });
  }

  function humanSize(bytes) {
    var n = Number(bytes) || 0;
    var units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? n + " " + units[0] : n.toFixed(1) + " " + units[i];
  }

  function baseName(path) {
    var text = String(path || "").replace(/\\/g, "/");
    return text.slice(text.lastIndexOf("/") + 1);
  }

  function subtitleLanguage(path) {
    // Язык из хвоста имени файла (clip.ru.vtt -> ru); если его нет - не выдумываем.
    var parts = baseName(path).split(".");
    var candidate = parts.length > 2 ? parts[parts.length - 2] : "";
    return /^[a-z]{2,3}$/.test(candidate) ? candidate : "";
  }

  function applyTexts(payload) {
    if (!payload || !payload.texts) return;
    texts = payload.texts;
    document.documentElement.lang = payload.language || "ru";
    Array.prototype.forEach.call(document.querySelectorAll("[data-l]"), function (node) {
      var value = texts[node.dataset.l];
      if (value) node.textContent = value;
    });
    filesEl.setAttribute("aria-label", t("watch.files_head", "Файлы этого видео"));
    if (!captionsBtn.hidden) {
      captionsBtn.textContent = captionsBtn.getAttribute("aria-pressed") === "true"
        ? t("watch.subs_hide", "Скрыть субтитры")
        : t("watch.subs_show", "Показать субтитры");
    }
  }

  function renderFiles(row, videoId) {
    var files = (row && row.files) || [];
    if (!files.length) {
      filesEl.textContent = t("watch.files_none", "файлы не найдены");
      return;
    }
    filesEl.innerHTML = "";
    files.forEach(function (file) {
      var line = document.createElement("div");
      line.className = "watch-file";
      var name = document.createElement("span");
      name.textContent = baseName(file.path);
      line.appendChild(name);
      var meta = document.createElement("span");
      var bits = [humanSize(file.size)];
      if (file.missing) bits.push(t("watch.gone", "файл пропал с диска"));
      bits.push(file.hash ? t("watch.hash", "хеш выдан")
                          : t("watch.hash_none", "хеш не вычислен"));
      meta.textContent = bits.join(", ");
      line.appendChild(meta);
      // Ссылки только на маршруты, которые сервер раздаёт байтами
      // (дорожка, субтитры, обложка): для остальных файлов ссылки нет.
      if (!file.missing && file.kind === "video") {
        var link = document.createElement("a");
        link.className = "link-btn";
        link.href = base + "/media/" + file.id;
        link.textContent = t("watch.bytes", "байты файла");
        line.appendChild(link);
      } else if (!file.missing && file.kind === "subtitle") {
        var sub = document.createElement("a");
        sub.className = "link-btn";
        sub.href = base + "/subs/" + file.id;
        sub.textContent = t("watch.bytes", "байты файла");
        line.appendChild(sub);
      } else if (file.kind === "thumbnail") {
        var thumb = document.createElement("a");
        thumb.className = "link-btn";
        thumb.href = base + "/thumb/" + videoId;
        thumb.textContent = t("watch.thumb", "обложка");
        line.appendChild(thumb);
      }
      filesEl.appendChild(line);
    });
  }

  function attachCaptions(subtitle) {
    var track = document.createElement("track");
    track.kind = "captions";
    track.src = base + "/subs/" + subtitle.id;
    track.label = t("watch.captions", "Субтитры");
    var language = subtitleLanguage(subtitle.path);
    if (language) track.language = language;
    track.mode = "hidden";        // по умолчанию скрыты: включаются кнопкой
    videoEl.appendChild(track);
    captionsBtn.hidden = false;
    captionsBtn.addEventListener("click", function () {
      var showing = captionsBtn.getAttribute("aria-pressed") !== "true";
      track.mode = showing ? "showing" : "hidden";
      captionsBtn.setAttribute("aria-pressed", showing ? "true" : "false");
      captionsBtn.textContent = showing
        ? t("watch.subs_hide", "Скрыть субтитры")
        : t("watch.subs_show", "Показать субтитры");
    });
  }

  function playHls(videoId) {
    var url = base + "/hls/" + videoId + "/index.m3u8";
    // Плейлист проверяется до плеера: сервер отказывает по кодексу или
    // инструменту подписью - hls.js не должен «висеть» на пустом ответе.
    return fetch(url, { headers: headers(), credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 200) {
          attachPlaylist(url);
          return null;
        }
        return response.json().catch(function () { return null; })
          .then(function (payload) {
            say(problemText(response, payload), true);
          });
      })
      .catch(function () {
        say(t("http.offline", "сервер не отвечает"), true);
      });
  }

  function attachPlaylist(url) {
    var Hls = window.Hls;
    if (Hls && Hls.isSupported()) {
      var player = new Hls();
      player.loadSource(url);
      player.attachMedia(videoEl);
      player.on(Hls.Events.ERROR, function (event, data) {
        if (!data.fatal) return;
        var detail = (data.response && data.response.code)
          ? String(data.response.code) : String(data.type || "ошибка");
        say(t("watch.failed", "воспроизведение не получилось") + " (" + detail + ")", true);
      });
      say(t("watch.loading", "загрузка…"));
      return;
    }
    if (videoEl.canPlayType("application/vnd.apple.mpegurl")) {
      // Safari играет HLS сам: hls.js ему не нужен.
      videoEl.src = url;
      say(t("watch.loading", "загрузка…"));
      return;
    }
    say(t("watch.browser_hls",
          "браузер не играет HLS - нужен hls.js или Safari"), true);
  }

  var match = location.pathname.match(/^\/watch\/(\d+)$/);
  if (!match) {
    say(t("http.missing", "не найдено") + " (нет id видео в адресе)", true);
    return;
  }
  var videoId = Number(match[1]);

  call("ui_texts").then(function (textsPayload) {
    applyTexts(textsPayload);
    return call("get_video", videoId);
  }).then(function (row) {
    if (row && row.error) {
      say(row.error, true);
      return;
    }
    if (!row) {
      say(t("http.missing", "не найдено") + " (строки " + videoId + " нет в индексе)", true);
      return;
    }
    var title = row.title || t("watch.title", "Воспроизведение");
    titleEl.textContent = title;
    document.title = "Omnistash — " + title;
    renderFiles(row, videoId);

    var trackRow = null;
    var subtitleRow = null;
    (row.files || []).forEach(function (file) {
      if (file.missing) return;
      if (file.kind === "video" && !trackRow) trackRow = file;
      if (file.kind === "subtitle" && !subtitleRow &&
          /\.vtt$/i.test(file.path)) subtitleRow = file;
      if (file.kind === "subtitle" && !subtitleRow &&
          /\.srt$/i.test(file.path)) subtitleRow = file;
    });
    if (subtitleRow) attachCaptions(subtitleRow);
    else subsNote.textContent = t("http.subs_missing", "субтитры не найдены");

    if (row.media_kind === "audio") {
      // Аудио-строка играется прямо байтами с Range: HLS ей не нужен (§6 п.4).
      if (!trackRow) {
        say(t("watch.no_track", "аудио не скачано"), true);
        return;
      }
      videoEl.hidden = true;
      audioEl.hidden = false;
      audioEl.src = base + "/media/" + trackRow.id;
      say(t("watch.audio", "аудио"));
      return;
    }
    if (!trackRow) {
      say(t("watch.no_track", "видео не скачано"), true);
      return;
    }
    playHls(videoId);
  }).catch(function () {
    say(t("http.offline", "сервер не отвечает"), true);
  });
})();
