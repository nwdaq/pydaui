# -*- mode: python ; coding: utf-8 -*-

import os
from glob import glob
from PyInstaller.utils.hooks import collect_all, copy_metadata

src = os.path.join(SPECPATH, '..', 'src', 'pydaui')

# Application resources. Only the CMSIS packs present in the tree are bundled
datas = [
	(os.path.join(src, 'assets'), 'pydaui/assets'),
	(os.path.join(src, 'icons'), 'pydaui/icons'),
] + [(pack, 'pydaui') for pack in glob(os.path.join(src, '*.pack'))]
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
