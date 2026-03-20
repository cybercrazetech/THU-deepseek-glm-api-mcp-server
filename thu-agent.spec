# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/mnt/c/Users/USER/Downloads/THU-deepseek-glm-api-mcp-server/agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['IPython', 'PIL', 'PyQt5', 'PyQt6', 'matplotlib', 'numpy', 'pygame', 'pytest', 'tkinter', 'traitlets', 'jedi', 'parso', 'gi', 'cryptography', 'bcrypt'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='thu-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
