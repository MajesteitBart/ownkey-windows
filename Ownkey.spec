# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


a = Analysis(
    ['ownkey.py'],
    pathex=[],
    binaries=collect_dynamic_libs('sherpa_onnx'),
    datas=[('assets/tray', 'assets/tray'), ('assets/fonts', 'assets/fonts'), ('meetings/ui', 'meetings/ui')]
          + copy_metadata('sherpa-onnx') + copy_metadata('sherpa-onnx-core')
          + collect_data_files('soundcard'),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Model weights are a separate, explicit download, never a build input.
for entry in a.datas + a.binaries:
    if entry[0].lower().endswith(('.onnx', '.gguf', '.tar.bz2')):
        raise RuntimeError('Model files must not be bundled: ' + entry[0])
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Ownkey',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='installer\\assets\\ownkey.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Ownkey',
)
