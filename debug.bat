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
echo Тесты офлайн (сеть не нужна)...
python -m unittest discover -t . -s tests
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
