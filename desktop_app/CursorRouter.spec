# -*- mode: python ; coding: utf-8 -*-


import os

_SPEC_DIR = os.path.abspath(SPECPATH)
_ROOT_DIR = os.path.abspath(os.path.join(_SPEC_DIR, '..'))

a = Analysis(
    [os.path.join(_SPEC_DIR, 'desktop_main.py')],
    pathex=[
        os.path.join(_ROOT_DIR, 'src'),
        _ROOT_DIR,
        _SPEC_DIR,
    ],
    binaries=[],
    datas=[
        (os.path.join(_ROOT_DIR, 'templates'), 'templates'),
        (os.path.join(_ROOT_DIR, 'Cookies'), 'Cookies'),
        (os.path.join(_ROOT_DIR, 'src'), 'src'),
    ],
    hiddenimports=[
        'flask', 'jinja2', 'requests', 'psutil', 'sqlite3', 'ctypes', 'webview',
        'aiohttp', 'aiohttp.web',
        'ai_optimizer', 'updater', 'cursor_settings', 'account_pool',
        'cursor_reloader', 'rotating_proxy', 'token_pool', 'smart_task_filter',
        'chat_lock_detector'
    ],
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
    name='CursorRouter',
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
)
