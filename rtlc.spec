# -*- mode: python ; coding: utf-8 -*-
# Продакшен-сборка: один rtlc.exe = Web UI (по умолчанию) + консольный
# режим (run) + служба Windows (service/install/start/...).
# Сборка: .venv/Scripts/python.exe -m PyInstaller rtlc.spec --clean --noconfirm

block_cipher = None

a = Analysis(
    ['rtlc_main.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],
    hiddenimports=[
        'win32timezone',
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'dicom_queue',
        'dicom_sender',
        'pynetdicom',
        'pydicom',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='rtlc',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements=None,
    version='file_version_info.txt',
)
