"""Where Jobify keeps its data: %APPDATA%\\Jobify on Windows, ~/.jobify elsewhere (JOBIFY_HOME overrides).

The app was called JobApply before: an existing %APPDATA%\\JobApply (or ~/.jobapply) stays in use, so logins, the
profile and the history survive the rename. JOBAPPLY_HOME is still honored."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_home() -> Path:
	if env := os.environ.get('JOBIFY_HOME') or os.environ.get('JOBAPPLY_HOME'):
		home = Path(env).expanduser()
	else:
		if sys.platform == 'win32':
			base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
			home, legacy = base / 'Jobify', base / 'JobApply'
		else:
			home, legacy = Path.home() / '.jobify', Path.home() / '.jobapply'
		if not home.exists() and legacy.is_dir():
			home = legacy
	home.mkdir(parents=True, exist_ok=True)
	return home


def sub(name: str) -> Path:
	path = app_home() / name
	path.mkdir(parents=True, exist_ok=True)
	return path


def resource(name: str) -> Path:
	"""A file shipped with the app (templates/, ui/), also inside a PyInstaller bundle."""
	base = Path(getattr(sys, '_MEIPASS', '')) / 'jobapply' if getattr(sys, 'frozen', False) else Path(__file__).parent
	return base / name


def replace_retrying(tmp: Path, target: Path, attempts: int = 20, wait_s: float = 0.25) -> None:
	"""tmp.replace(target), retried for a few seconds: on Windows a file open in Excel, or being read by an antivirus
	scanner or a backup tool, refuses to be replaced for a moment ("PermissionError: [WinError 5]")."""
	import time

	for attempt in range(attempts):
		try:
			tmp.replace(target)
			return
		except PermissionError:
			if attempt == attempts - 1:
				raise
			time.sleep(wait_s)


def write_text_atomic(path: Path, text: str, encoding: str = 'utf-8') -> None:
	"""Write a temp file next to path, then swap it in: a crash never leaves half a file."""
	tmp = path.with_name(path.name + '.tmp')
	tmp.write_text(text, encoding=encoding)
	replace_retrying(tmp, path)
