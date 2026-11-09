@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

rem ============================================================
rem  Omnistash - консоль управления проектом
rem  debug.bat          - меню
rem  debug.bat 4        - сразу выполнить пункт (для скриптов/CI)
rem  Подсказка: в текстах меню нет спецсимволов cmd (| & >),
rem  а метки только латиницей - так надёжнее с UTF-8.
rem ============================================================

set "scripted="
set "sel="
if not "%~1"=="" (set "scripted=1" & set "sel=%~1")

set "branch=?"
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "branch=%%b"
set "dirty=0"
for /f %%c in ('git status --porcelain 2^>nul ^| find /c /v ""') do set "dirty=%%c"

if defined scripted goto dispatch

:menu
cls
echo.
echo  ===============================================================
echo    Omnistash - консоль управления проектом
echo  ===============================================================
echo    ветка: %branch%    не закоммичено файлов: %dirty%
echo  ---------------------------------------------------------------
echo    [1] Запустить приложение (окно)
echo    [2] Запустить с отладкой WebView (--debug)
echo    [3] Переиндексация без окна (--scan)
echo    [4] Тесты: офлайн
echo    [5] Тесты: все, включая живые на YouTube
echo    [6] Проверка синтаксиса: python и node
echo    [7] Собрать превью интерфейса и открыть
echo    [8] Превью первого запуска (диалог выбора папки)
echo    [G] Flutter: сверка токенов, колонок и табов с источниками
echo    [H] Flutter: анализ и тесты экрана (нужен SDK на сборочное время; RAM под лимитом)
echo    [I] Flutter: собрать release exe окна (нужен SDK; RAM под лимитом)
echo    [J] Ассеты: иконки .ico, SVG, тема Бренд, токены Flutter
echo    [K] Бандл клиента и юр-пак: сверка и воркфлоу
echo    [L] Запустить окно с живым фасадом (--serve)
echo    [E] Синхронизация без окна (--sync)
echo    [F] Собрать exe и сверить состав по TOC
echo  ---------------------------------------------------------------
echo    [9] git: статус и последние коммиты
echo    [A] git: добавить всё, закоммитить, запушить
echo    [B] Остановить процессы Omnistash
echo    [C] Копия базы библиотеки (backup)
echo    [D] Открыть папку профиля
echo  ---------------------------------------------------------------
echo    [0] Выход
echo.
set "sel="
set /p "sel=Выбор: "
if not defined sel goto menu
if "%sel%"=="0" exit /b 0

:dispatch
if "%sel%"=="1" goto op_run
if "%sel%"=="2" goto op_debug
if "%sel%"=="3" goto op_scan
if "%sel%"=="4" goto op_tests
if "%sel%"=="5" goto op_tests_all
if "%sel%"=="6" goto op_syntax
if "%sel%"=="7" goto op_preview
if "%sel%"=="8" goto op_preview_first
if "%sel%"=="G" goto op_flutter_mirror
if "%sel%"=="g" goto op_flutter_mirror
if "%sel%"=="H" goto op_flutter_check
if "%sel%"=="h" goto op_flutter_check
if "%sel%"=="I" goto op_flutter_build
if "%sel%"=="i" goto op_flutter_build
if "%sel%"=="J" goto op_assets
if "%sel%"=="j" goto op_assets
if "%sel%"=="K" goto op_packs
if "%sel%"=="k" goto op_packs
if "%sel%"=="L" goto op_serve
if "%sel%"=="l" goto op_serve
if "%sel%"=="E" goto op_sync
if "%sel%"=="e" goto op_sync
if "%sel%"=="F" goto op_build_exe
if "%sel%"=="f" goto op_build_exe
if "%sel%"=="9" goto op_git_status
if "%sel%"=="A" goto op_git_push
if "%sel%"=="a" goto op_git_push
if "%sel%"=="B" goto op_kill
if "%sel%"=="b" goto op_kill
if "%sel%"=="C" goto op_backup
if "%sel%"=="c" goto op_backup
if "%sel%"=="D" goto op_profile
if "%sel%"=="d" goto op_profile
if defined scripted (echo Неизвестный пункт: %~1 & exit /b 2)
goto menu

rem ---------------------------------------------------------------
rem  Запуск
rem ---------------------------------------------------------------

:op_run
echo Запуск Omnistash...
python omnistash.py
set "rc=%errorlevel%"
goto after

:op_debug
echo Запуск с отладкой WebView (откроется консоль JS)...
python omnistash.py --debug
set "rc=%errorlevel%"
goto after

:op_scan
echo Переиндексация без окна...
python omnistash.py --scan
set "rc=%errorlevel%"
goto after

rem ---------------------------------------------------------------
rem  Проверки
rem ---------------------------------------------------------------

:op_tests
echo Тесты офлайн (сеть не нужна, параллельно по ядрам)...
python tools/run_tests.py
set "rc=%errorlevel%"
goto after

:op_tests_all
echo Тесты все: офлайн и живые на YouTube...
set "OMNISTASH_LIVE=1"
python -m unittest discover -t . -s tests
set "rc=%errorlevel%"
set "OMNISTASH_LIVE="
goto after

:op_syntax
echo Python...
python -m compileall -q app omnistash.py tests tools
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
echo JavaScript...
node --check ui_src\app.js
set "rc=%errorlevel%"
if "%rc%"=="0" echo Синтаксис в порядке.
goto after

rem ---------------------------------------------------------------
rem  Интерфейс
rem ---------------------------------------------------------------

:op_preview
echo Сборка превью и открытие в браузере...
python tools\build_preview.py --open
set "rc=%errorlevel%"
goto after

:op_preview_first
echo Сборка превью в режиме первого запуска...
python tools\build_preview.py --first --open
set "rc=%errorlevel%"
goto after

rem ---------------------------------------------------------------
rem  Flutter (волны W0-W2: стенд, экран, страж) - SDK нужен на сборочное
rem  время и пинится версией и sha256 архива (tools/flutter_pin.json);
rem  рантайм окна ничего не скачивает. Пункты G-K гоняются и без SDK: G
rem  читает ассет как текст (работает на GitHub-раннере), J и K сверяют
rem  ассеты и паки; H и I требуют локальный SDK.
rem ---------------------------------------------------------------

:op_flutter_mirror
echo Сверка Dart-ассета с реестром тем, CSS, LIST_COLS, ORDER_COLS, схемой и табами...
python tools\check_flutter_mirror.py
set "rc=%errorlevel%"
goto after

:op_flutter_check
echo Flutter: анализ и widget-тесты экрана (analyze; test по сьютам)...
cd ui_flutter
call flutter analyze
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
rem Экран библиотеки разделён на группы (test/harness.dart + 6 файла): изолятор
rem живёт на весь файл и копится линейно по тестам - один файл 34 теста дал
rem 12708 MB commit (факт прогона при потолке 12288). Ряд рейтинга переложен на
rem SegmentedButton: факты probe'ов шага 4 - DropdownButton статично 8.1 GB и с
rem выбором 9.3 GB, DropdownMenu с выбором 9.0 GB, ToggleButtons с тапом 8.35 GB,
rem Radio-ряд 7.8-8.4 GB, SegmentedButton с тапами и setState 660 MB в том же
rem DialogScaffold. Наблюдённые пики групп после переложения: 306-791 MB
rem (мелкие сьюты 4096 MB, группы экрана 6144 MB) - жёсткий kill выше потолка
rem держится. Job-лимит не применился: вложенный CreateJobW отказан в песочнице
rem (факт smoke), toolhelp там же ERROR_MORE_DATA (факт dbg) - снимок читает WMI
rem через один живой воркер. Трим resident не включён - он гоняет подкачку и
rem грузит диск (сообщение человека).
python ..\tools\run_capped.py --cmd "flutter test test\widget_test.dart" --max-mem-mb 4096
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\theme_test.dart" --max-mem-mb 4096
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\tabs_test.dart" --max-mem-mb 4096
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\video_card_test.dart" --max-mem-mb 4096
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
rem Экран библиотеки разделён на группы (test/harness.dart + 6 файла): изолятор
rem живёт на весь файл и копится линейно по тестам - один файл 34 теста дал
rem 12708 MB commit (факт прогона при потолке 12288), а группа пометок с
rem dropdown рейтинга - 6265 MB при 6144 и 6496 MB в отдельном файле из трёх
rem тестов (факт прогона: spike не накопление, а стоимость диалога с
rem overlay). Поэтому группы гонятся отдельными процессами: 13 core, 7 grid,
rem 4 bulk под 6144 MB (наблюдённые пики 729-757 MB); после переложения ряда
rem рейтинга на SegmentedButton дешёвые и диалоговые группы - 3 marks, 3
rem dialogs и 4 danger тоже под 6144 (наблюдённые пики 745-756 MB), kill выше
rem потолка держится.
python ..\tools\run_capped.py --cmd "flutter test test\library_view_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\library_grid_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\library_bulk_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\library_marks_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\library_dialogs_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
python ..\tools\run_capped.py --cmd "flutter test test\library_danger_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
if not "%rc%"=="0" (cd .. & goto after)
rem Шаг 4б: вид библиотеки и плитки с обложками - 21 тест (17 widget + 4 pure).
rem Первый прогон группы падал на RenderFlex overflow ряда переключателей вида
rem (Row с overflow печатает ошибку каждый кадр - факт probe'а: 8.45 GB commit,
rem тот же класс раздувания, что строка канона). Ряд переложен на Wrap (канон
rem toolbar не переносится при min-width 940 - расхождение формы названо), и
rem SegmentedButton дёшев с перестройкой: наблюдённый пик группы 768 MB под
rem 6144 (flutter_tester 214 MB).
python ..\tools\run_capped.py --cmd "flutter test test\library_tiles_test.dart" --max-mem-mb 6144
set "rc=%errorlevel%"
cd ..
goto after

:op_flutter_build
echo Flutter: сборка release exe окна (движок Skia по решению человека)...
cd ui_flutter
rem Сборка тоже под потолком commit: dart compile + clang на linking пикают
rem несколько ГБ; 4096 MB - наблюдённый запас на машине человека.
python ..\tools\run_capped.py --cmd "flutter build windows --release" --max-mem-mb 4096
set "rc=%errorlevel%"
cd ..
echo Артефакт: ui_flutter\build\windows\x64\runner\Release\omnistash_ui.exe
goto after

:op_assets
echo Иконки .ico из темы Бренд, SVG-ассеты, реестр темы и токены Flutter...
python tools\make_icon.py
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\make_icon.py --check
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\build_icon_svgs.py --check
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\build_brand_theme.py --check
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\build_flutter_theme.py --check
set "rc=%errorlevel%"
goto after

:op_packs
echo Бандл клиента и юр-пак: сверка без изменений, затем воркфлоу...
python tools\build_client.py --check
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\build_legal_pack.py --check
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\check_workflows.py
set "rc=%errorlevel%"
goto after

:op_serve
echo Запуск окна с живым фасадом --serve (HTTPS слушатель рядом с окном)...
python omnistash.py --serve
set "rc=%errorlevel%"
goto after

:op_sync
echo Синхронизация без окна (синк + очередь)...
python omnistash.py --sync
set "rc=%errorlevel%"
goto after

rem ---------------------------------------------------------------
rem  Сборка exe и сверка его состава
rem ---------------------------------------------------------------

:op_build_exe
echo Сборка exe (onefile) и сверка состава по TOC...
if not exist build mkdir build
python -m PyInstaller Omnistash.spec --distpath . --workpath build --noconfirm > build\build_log.txt 2>&1
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
python tools\check_exe_contents.py
set "rc=%errorlevel%"
goto after

rem ---------------------------------------------------------------
rem  Git
rem ---------------------------------------------------------------

:op_git_status
git status -sb
echo ---------------------------------------------------------------
git log --oneline -6
set "rc=%errorlevel%"
goto after

:op_git_push
echo Текущие изменения:
git status -sb
echo.
set "msg="
set /p "msg=Сообщение коммита (пусто = отмена): "
if not defined msg goto menu
echo.
git add -A
git commit -m "%msg%"
set "rc=%errorlevel%"
if not "%rc%"=="0" goto after
git push origin main
set "rc=%errorlevel%"
goto after

rem ---------------------------------------------------------------
rem  Процессы и данные
rem ---------------------------------------------------------------

:op_kill
echo Ищу процессы python с omnistash.py...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*omnistash.py*' } | ForEach-Object { Write-Host ('kill ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Готово.
set "rc=0"
goto after

:op_backup
echo Копия базы библиотеки...
powershell -NoProfile -Command "$s = Join-Path $env:LOCALAPPDATA 'Omnistash'; $b = Join-Path $s 'backup'; New-Item -ItemType Directory -Force -Path $b | Out-Null; $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'; $d = Join-Path $b $stamp; New-Item -ItemType Directory -Force -Path $d | Out-Null; Copy-Item (Join-Path $s 'library.db*') $d -Force; Copy-Item (Join-Path $s 'settings.json') $d -Force -ErrorAction SilentlyContinue; Write-Host ('Копия: ' + $d)"
set "rc=%errorlevel%"
goto after

:op_profile
echo Папка профиля: %LOCALAPPDATA%\Omnistash
if exist "%LOCALAPPDATA%\Omnistash" (explorer "%LOCALAPPDATA%\Omnistash") else (echo Профиля ещё нет - запустите приложение)
set "rc=0"
goto after

rem ---------------------------------------------------------------
rem  Возврат в меню
rem ---------------------------------------------------------------

:after
if defined scripted exit /b %rc%
echo.
echo Код возврата: %rc%
pause
goto menu
