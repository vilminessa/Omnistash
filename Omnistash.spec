# -*- mode: python ; coding: utf-8 -*-
# Сборка Omnistash.exe (onefile, как в Synfronia): проверки -> exe -> zip/релиз.
# Контракт с кодом: app/paths.py в заморозке ищет ui_src в _MEIPASS (бандл),
# а папкой программы считает каталог самой exe - туда человек кладёт bin/.
import os
import sys
import tempfile

from PyInstaller.utils.hooks import collect_all

# Читаем version.py - единственная правда о номере; вешаем её на exe
sys.path.insert(0, SPECPATH)
from version import __version__  # noqa: E402

_v = [int(x) for x in __version__.split(".")]
_v4 = tuple(_v[:4]) + (0,) * (4 - len(_v))
_version_file = os.path.join(tempfile.gettempdir(), "omnistash-version-info.txt")
with open(_version_file, "w", encoding="utf-8") as _fh:
    _fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_v4[0]}, {_v4[1]}, {_v4[2]}, {_v4[3]}),
    prodvers=({_v4[0]}, {_v4[1]}, {_v4[2]}, {_v4[3]}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'Vilminessa'),
            StringStruct('FileDescription', 'Omnistash - загрузка, синхронизация и организация YouTube-архивов'),
            StringStruct('FileVersion', '{__version__}'),
            StringStruct('InternalName', 'Omnistash'),
            StringStruct('OriginalFilename', 'Omnistash.exe'),
            StringStruct('ProductName', 'Omnistash'),
            StringStruct('ProductVersion', '{__version__}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

datas = []
binaries = []
hiddenimports = []
# yt_dlp собирает обновления по сети (extractor'ы подгружаются сами), webview
# тянет свои DLL и бэкенды - collect_all берёт их целиком, хуки не гадят.
for pkg in ("yt_dlp", "webview"):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Интерфейс: ui.py читает ui_src/{index.html,app.css,app.js} и вшивает в страницу.
datas += [("ui_src", "ui_src")]

a = Analysis(
    ['omnistash.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Omnistash',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_file,
)
