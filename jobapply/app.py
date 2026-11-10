"""The desktop window (pywebview): Setup, Run, Questions and History screens over the runner.

The page (ui/index.html) calls the Api methods through window.pywebview.api and polls events() once a second."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from jobapply import __version__
from jobapply.paths import app_home, resource, sub
from jobapply.profile import STARTER_CRITERIA, STARTER_PROFILE
from jobapply.runner import Runner, questions, tracker
from jobapply.settings import BOARDS, ChatGPTLogin, Settings

logger = logging.getLogger('jobapply')
MAX_EVENTS = 500
# Pages opened by "Log in to sites": the accounts the loop needs
LOGIN_PAGES = (
	'https://accounts.google.com/ServiceLogin?continue=https://mail.google.com/mail/',
	'https://www.linkedin.com/login',
	'https://secure.indeed.com/auth',
)


def open_path(path: str | Path) -> None:
	path = str(path)
	if sys.platform == 'win32':
		os.startfile(path)  # type: ignore[attr-defined]
	elif sys.platform == 'darwin':
		subprocess.Popen(['open', path])
	else:
		subprocess.Popen(['xdg-open', path])


class Api:
	"""Every public method is callable from the page. Each returns JSON-able data; errors come back as {'error': ...}."""

	def __init__(self) -> None:
		self._window: Any = None
		self._events: deque[dict] = deque(maxlen=MAX_EVENTS)
		self._seq = 0
		self._lock = threading.Lock()
		self._runner: Runner | None = None
		self._login_browser: Any = None  # the profile opened for logins, while that window is open

	# events

	def _event(self, event: dict) -> None:
		with self._lock:
			self._seq += 1
			self._events.append({**event, 'seq': self._seq})

	def events(self, since: int = 0) -> list[dict]:
		with self._lock:
			return [e for e in self._events if e['seq'] > since]

	# state

	def state(self) -> dict:
		from jobapply import chatgpt_auth

		settings = Settings.load()
		t = tracker()
		return {
			'version': __version__,
			'settings': settings.public(),
			'boards': {k: v[0] for k, v in BOARDS.items()},
			'chatgpt': chatgpt_auth.status(),
			'runner': self._runner.state if self._runner else 'idle',
			'current': self._runner.current if self._runner else {},
			'today': t.count_today(),
			'cap': settings.daily_cap,
			'questions': [dataclasses.asdict(q) for q in questions().open()],
			'rows': [dataclasses.asdict(r) for r in reversed(t.rows()[-300:])],
			'home': str(app_home()),
		}

	def save_settings(self, data: dict) -> dict:
		"""Fields left out keep their value; an empty key field keeps the saved key."""
		try:
			current = dataclasses.asdict(Settings.load())
			for key in ('gemini_api_key', 'openai_api_key'):
				if not data.get(key):
					data.pop(key, None)
			if isinstance(data.get('chatgpt'), dict):
				data['chatgpt'] = {**current['chatgpt'], **data['chatgpt']}
				data['chatgpt']['redirect_port'] = int(data['chatgpt'].get('redirect_port') or ChatGPTLogin.redirect_port)
			if 'daily_cap' in data:
				data['daily_cap'] = int(data['daily_cap'])
			if isinstance(data.get('job_urls'), str):
				data['job_urls'] = data['job_urls'].split()
			settings = Settings.from_dict({**current, **data})
			settings.save()
			if self._runner and self._runner.state == 'idle':
				self._runner = None  # the next start uses the new settings
			return {'ok': True}
		except (ValueError, TypeError) as e:
			return {'error': str(e)}

	# files

	def pick_file(self, kind: str) -> dict:
		import webview  # pyright: ignore[reportMissingImports]

		types = {
			'profile': ('Markdown (*.md;*.txt)', 'All files (*.*)'),
			'criteria': ('Markdown (*.md;*.txt)', 'All files (*.*)'),
			'template': ('HTML (*.html;*.htm)', 'All files (*.*)'),
			'browser': ('Programs (*.exe)', 'All files (*.*)'),
		}[kind]
		picked = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
		if not picked:
			return {'path': ''}
		path = picked[0] if isinstance(picked, (list, tuple)) else picked
		field = {'profile': 'profile_path', 'criteria': 'criteria_path', 'template': 'resume_template_path'}.get(
			kind, 'browser_executable'
		)
		return {**self.save_settings({field: path}), 'path': path}

	def create_starter(self, kind: str) -> dict:
		"""A starter profile.md / criteria.md / resume template in the app folder, opened for editing."""
		name, content, field = {
			'profile': ('profile.md', STARTER_PROFILE, 'profile_path'),
			'criteria': ('criteria.md', STARTER_CRITERIA, 'criteria_path'),
			'template': ('resume_template.html', None, 'resume_template_path'),
		}[kind]
		path = app_home() / name
		if not path.exists():
			if content is None:
				shutil.copyfile(resource('templates/resume.html'), path)
			else:
				path.write_text(content, encoding='utf-8')
		result = self.save_settings({field: str(path)})
		with contextlib.suppress(Exception):
			open_path(path)
		return {**result, 'path': str(path)}

	def open_path(self, path: str) -> dict:
		try:
			open_path(path or app_home())
			return {'ok': True}
		except Exception as e:
			return {'error': str(e)}

	def open_csv(self) -> dict:
		path = app_home() / 'applications.csv'
		if not path.exists():
			return {'error': 'No applications yet.'}
		return self.open_path(str(path))

	# loop

	def open_login_browser(self) -> dict:
		"""The JobApply browser profile opened normally, without the remote debugging port the loop drives it through:
		Google refuses to sign in a remote-controlled browser ("This browser or app may not be secure"). The logins are
		saved in the profile, and the loop's browser uses them."""
		if self._runner is not None and self._runner.browser is not None:
			return {'error': 'Stop the loop first: the JobApply browser is in use. Then log in, close that window and Start.'}
		if self._login_open():
			return {'error': 'The login browser is already open.'}
		from browser_use.browser.local import find_binary

		try:
			binary = find_binary(Settings.load().browser_executable or None)
			args = [binary, f'--user-data-dir={sub("browser-profile")}', '--no-first-run', '--no-default-browser-check']
			self._login_browser = subprocess.Popen([*args, *LOGIN_PAGES])
			return {'ok': True}
		except Exception as e:
			return {'error': f'Could not open the browser: {e}'}

	def _login_open(self) -> bool:
		return self._login_browser is not None and self._login_browser.poll() is None

	def start(self) -> dict:
		if self._login_open():
			return {'error': 'Close the login browser window first (it uses the same profile), then press Start.'}
		try:
			settings = Settings.load()
			from jobapply.llm import agent_llm
			from jobapply.profile import read_text

			agent_llm(settings)  # a missing key fails here, with the Setup hint
			read_text(settings.profile_path, 'profile')
			read_text(settings.criteria_path, 'job criteria')
		except Exception as e:
			return {'error': str(e)}
		if self._runner is None:
			self._runner = Runner(settings, self._event)
		else:
			self._runner.settings = settings
		self._runner.start()
		return {'ok': True}

	def pause(self) -> dict:
		if self._runner:
			self._runner.pause()
		return {'ok': True}

	def resume(self) -> dict:
		if self._runner:
			self._runner.resume()
		return {'ok': True}

	def stop(self) -> dict:
		if self._runner:
			self._runner.stop()
		return {'ok': True}

	def focus_tab(self, tab: str) -> dict:
		ok = bool(self._runner and self._runner.focus_tab(tab))
		return {'ok': ok} if ok else {'error': 'That tab is closed, or the browser is not running.'}

	# questions

	def answer(self, question_id: str, text: str) -> dict:
		try:
			settings = Settings.load()
			q = questions().answer(question_id, text, settings.profile_path)
			return {'ok': True, 'question': q.question}
		except (ValueError, OSError) as e:
			return {'error': str(e)}

	# ChatGPT

	def chatgpt_login(self) -> dict:
		from jobapply import chatgpt_auth

		try:
			return chatgpt_auth.login(Settings.load().chatgpt)
		except Exception as e:
			return {'error': str(e)}

	def chatgpt_logout(self) -> dict:
		from jobapply import chatgpt_auth

		chatgpt_auth.logout()
		return {'ok': True}

	def shutdown(self) -> None:
		runner = self._runner
		if runner is None:
			return
		runner.stop()
		if runner.browser is not None:
			with contextlib.suppress(Exception):
				asyncio.run(runner.close_browser())


def setup_logging() -> None:
	if sys.stdout is None:  # the windowed exe has no console
		sys.stdout = open(os.devnull, 'w')
	if sys.stderr is None:
		sys.stderr = open(os.devnull, 'w')
	handler = logging.FileHandler(app_home() / 'jobapply.log', encoding='utf-8')
	handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
	for name in ('jobapply', 'browser_use'):
		logging.getLogger(name).addHandler(handler)
	logging.getLogger('jobapply').setLevel(logging.INFO)


def main() -> None:
	if getattr(sys, 'frozen', False):
		from jobapply.frozen import dispatch

		if dispatch(sys.argv):  # a helper process browser-use started through the exe
			return
	os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
	setup_logging()
	try:
		import webview  # pyright: ignore[reportMissingImports]
	except ImportError:
		raise SystemExit("JobApply's window needs pywebview: uv sync --extra jobapply") from None

	api = Api()
	window = webview.create_window(
		'JobApply',
		url=str(resource('ui/index.html')),
		js_api=api,
		width=1180,
		height=820,
		min_size=(900, 620),
	)
	api._window = window
	assert window is not None
	window.events.closed += api.shutdown
	webview.start()


if __name__ == '__main__':
	main()
