# -*- mode: python ; coding: utf-8 -*-

import datetime
import importlib.metadata
import json
import os
import subprocess
import sys
from glob import glob
from PyInstaller.utils.hooks import collect_all, copy_metadata

src = os.path.join(SPECPATH, '..', 'src', 'pydaui')

# Application resources. Only the CMSIS packs present in the tree are bundled
datas = [
	(os.path.join(src, 'assets'), 'pydaui/assets'),
	(os.path.join(src, 'icons'), 'pydaui/icons'),
] + [(pack, 'pydaui') for pack in glob(os.path.join(src, '*.pack'))]

# The version and the build date, read by pydaui.build_info
def _version():
	try:
		return subprocess.check_output(['git', 'describe', '--tags', '--always', '--dirty'],
			cwd=SPECPATH, text=True, stderr=subprocess.DEVNULL).strip()
	except (OSError, subprocess.CalledProcessError):
		return importlib.metadata.version('pydaui')

_epoch = os.environ.get('SOURCE_DATE_EPOCH')
_date = datetime.datetime.fromtimestamp(int(_epoch), datetime.UTC) if _epoch else datetime.datetime.now(datetime.UTC)
_build_info = os.path.join(workpath, 'build.json')
os.makedirs(workpath, exist_ok=True)
with open(_build_info, 'w') as f:
	json.dump({'version': _version(), 'date': _date.strftime('%Y-%m-%d')}, f)
datas.append((_build_info, 'pydaui'))

# The change log, read by pydaui.changelog
sys.path.insert(0, os.path.join(SPECPATH, '..', 'src'))
from pydaui import changelog
_changelog = os.path.join(workpath, 'changelog.json')
changelog.write(_changelog, SPECPATH)
datas.append((_changelog, 'pydaui'))

binaries = []
hiddenimports = []

# Packages discovered through entry points need their metadata and data files
for package in ['fonticon_mdi7', 'pyocd', 'cmsis_pack_manager', 'pylink']:
	d, b, h = collect_all(package)
	datas += d
	binaries += b
	hiddenimports += h
datas += copy_metadata('fonticon-materialdesignicons7')


a = Analysis(
    ['../src/pydaui/__main__.py'],
    pathex=['../src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pydaui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name='pydaui',
)
