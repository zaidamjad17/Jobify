# PyInstaller spec for Jobify.exe (one folder, windowed). Build from the repo root:
#   uv sync --locked --group jobapply
#   uv run --with pyinstaller==6.22.3 pyinstaller packaging/windows/jobapply.spec --noconfirm
# The exe also runs the helper processes browser-use starts with sys.executable (jobapply/frozen.py), so every module
# they run (browser_use.repl.worker, browser_harness.daemon) must be bundled: collect_submodules below.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = SPECPATH + '/../..'

hidden = []
for package in ('jobapply', 'browser_use', 'browser_harness', 'pydantic_ai', 'webview', 'genai_prices', 'websockets', 'google.genai'):
    hidden += collect_submodules(package, filter=lambda name: '.tests' not in name and '.test_' not in name)

datas = [
    (ROOT + '/jobapply/ui', 'jobapply/ui'),
    (ROOT + '/jobapply/templates', 'jobapply/templates'),
]
for package in ('browser_use', 'browser_harness', 'genai_prices', 'certifi', 'webview'):
    datas += collect_data_files(package)
# importlib.metadata.version() at runtime (genai-prices, Pydantic AI, browser-use telemetry) needs the dist-info
for dist in ('browser-use', 'browser-harness', 'genai-prices', 'pydantic-ai-slim', 'pydantic', 'google-genai', 'openai',
             'anthropic', 'httpx', 'mcp', 'websockets', 'pywebview'):
    try:
        datas += copy_metadata(dist)
    except Exception:  # not installed in this environment
        pass

a = Analysis(
    [SPECPATH + '/launch.py'],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Jobify',
    icon=SPECPATH + '/jobify.ico',
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='Jobify', upx=False)
