"""Where JobApply keeps its data: %APPDATA%\\JobApply on Windows, ~/.jobapply elsewhere (JOBAPPLY_HOME overrides)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_home() -> Path:
	if env := os.environ.get('JOBAPPLY_HOME'):
		home = Path(env).expanduser()
	elif sys.platform == 'win32':
		home = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'JobApply'
	else:
		home = Path.home() / '.jobapply'
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
