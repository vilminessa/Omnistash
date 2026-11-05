# ui_flutter - Flutter-оболочка Omnistash (W0: измерительный стенд)

Здесь живёт Flutter-оболочка поверх того же Python-бэкенда. План волн и
решения человека - `docs/FLUTTER-TRANSITION.md`; система иконок и палитра
бренда - `docs/ICONS.md`.

**Что сейчас в этом каталоге (W0, spike):**

- `lib/main.dart` - окно с виртуализированным списком (2000 строк),
  автопрокруткой и замером: время до первого кадра, частота кадров, латентность
  каждой цели фасада, отказ называется текстом сервера; статистика печатается
  одной строкой `SPIKE_STATS` в stdout, её собирает
  `build/scratch/spike/measure.py` и `build/scratch/spike/live_serve.py`.
  Это измерительный стенд, не экран программы; производственные экраны
  пишутся на волне W2.
- `lib/api_client.dart` - транспорт к `--serve`: `POST /api/<имя>`, тело `{}`
  когда аргумента нет и `[аргумент]` когда есть (как `ui_src/app.js:547`),
  заголовок `Bearer`, доверие TLS по сверке отпечатка DER-сертификата с
  пином; несовпадение отвергается с названной причиной и не молчит.
- `test/widget_test.dart` - smoke-тест: окно строится без сервера и список
  виртуализуется.

**Сборка и запуск (тулчейн ставится при сборке, рантайм ничего не качает):**

```powershell
$env:Path = "D:\dev\flutter\bin;$env:Path"   # SDK пинится в tools/flutter_pin.json
cd ui_flutter
flutter pub get
flutter analyze
flutter test
flutter build windows --release               # артефакт build\windows\x64\runner\Release\omnistash_ui.exe
```

Стенд замера требует запущенного слушателя: `python build/scratch/spike/mock_api.py`
(проводка контракта и TLS) или `python build/scratch/spike/live_serve.py`
(настоящий `app.http.server.ServeServer`, права решает сервер).

**Юр-часть:** Flutter engine и framework (BSD-3-Clause), Dart SDK
(BSD-3-Clause) и пакет `crypto` (BSD-3-Clause) используются при сборке. Их
тексты лицензий кладутся в `licenses/manifest.json` и `THIRD_PARTY_NOTICES.md`
на волне W5 - вместе с артефактами Flutter: текущая карта описывает бандл exe,
а Flutter в него ещё не входит. Рантайм по-прежнему не скачивает ничего:
SDK и pub-пакеты нужны только при сборке и закреплены версией, sha256 архива
SDK и `pubspec.lock`.

**Размер артефакта (измерено на машине человека, прогон 05.11.2026):**
release-бандл 25.5 МБ, сам exe 0.1 МБ, рабочий набор окна с 2000 строк
105-132 МБ по дереву процессов.
