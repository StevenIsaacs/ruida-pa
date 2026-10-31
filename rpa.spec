# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for RPA — one-file standalone binary.
Excludes flexiblas (Fedora's BLAS wrapper) so the system version is used.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

block_cipher = None

# Root of the source tree (where the ruida_pa package lives)
# Note: __file__ is not available in spec exec context, use cwd
root_dir = Path.cwd()

a = Analysis(
    [str(root_dir / 'ruida_pa' / 'rpa.py')],
    pathex=[str(root_dir)],
    binaries=[],
    datas=(
        [(str(root_dir / 'LICENSE'), '.')] +
        copy_metadata('ruida-pa')
    ),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pkg_resources',
        'tkinter',
        'test',
        'unittest',
        'distutils',
        'setuptools',
        'pip',
        'jaraco',
    ],
    noarchive=False,
    cipher=block_cipher,
)

# --- Fedora/FlexiBLAS workaround ---
# On Fedora, numpy is linked against libflexiblas.so. If we bundle this library,
# it can't find its runtime backends (dlopen'd from /usr/lib64/flexiblas/).
# Exclude it so the system version is used, which knows how to find its backends.
# This is a no-op on non-Fedora systems since flexiblas won't be in the deps.
a.binaries = [
    (name, path, typecode) for name, path, typecode in a.binaries
    if 'flexiblas' not in name.lower()
]

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name='rpa',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='.',
)
