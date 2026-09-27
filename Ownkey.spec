# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

# Meeting speaker labels load NeMo-Speech.cpp from _internal/nemo_speech.
# scripts/Build-NemoSpeechRuntime.ps1 stages it; the model stays a download.
nemo_speech = os.path.join('vendor', 'nemo-speech', 'windows-x64')
if not os.path.isfile(os.path.join(nemo_speech, 'nemo_speech_asr_c.dll')):
    raise RuntimeError('The speaker label runtime is missing. Run scripts\\Build-NemoSpeechRuntime.ps1 first.')
nemo_speech_binaries = [(os.path.join(nemo_speech, name), 'nemo_speech')
                        for name in os.listdir(nemo_speech) if name.lower().endswith('.dll')]
nemo_speech_datas = [(os.path.join(nemo_speech, 'licenses'), 'nemo_speech/licenses'),
                     (os.path.join(nemo_speech, 'runtime.json'), 'nemo_speech')]


a = Analysis(
    ['ownkey.py'],
    pathex=[],
    binaries=collect_dynamic_libs('sherpa_onnx') + nemo_speech_binaries,
    datas=[('assets/tray', 'assets/tray'), ('assets/fonts', 'assets/fonts'), ('meetings/ui', 'meetings/ui')]
          + copy_metadata('sherpa-onnx') + copy_metadata('sherpa-onnx-core')
          + collect_data_files('soundcard') + nemo_speech_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Dependency analysis also copies the runtime's DLLs to the top level; only
# the nemo_speech folder loads them.
nemo_speech_names = {name.lower() for name in os.listdir(nemo_speech)}
a.binaries = [entry for entry in a.binaries
              if not (os.path.dirname(entry[0]) == '' and entry[0].lower() in nemo_speech_names)]
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
