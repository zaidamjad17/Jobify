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
from jobapply.settings import BOARDS, Settings

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
		self._release: Any = None  # the newest release found by check_update()
		self._updating = False

	# events

	def _event(self, event: dict) -> None:
		with self._lock:
			self._seq += 1
			self._events.append({**event, 'seq': self._seq})
		if event.get('kind') == 'help':
			self._to_front()

	def _to_front(self) -> None:
		"""The agent needs the user: show the Jobify window over everything for a moment (the page plays a sound)."""
		window = self._window
		if window is None:
			return
		try:
			window.restore()
			window.on_top = True
			threading.Timer(2.0, lambda: setattr(window, 'on_top', False)).start()
		except Exception as e:  # a window being closed
			logger.debug('could not raise the window: %s', e)

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
			'help': self._runner.help if self._runner else None,
			'today': t.count_today(),
			'cap': settings.daily_cap,
			'questions': [dataclasses.asdict(q) for q in questions().open()],
			'rows': [dataclasses.asdict(r) for r in reversed(t.rows()[-300:])],
			'home': str(app_home()),
			'profile_score': self._profile_score(settings),
		}

	@staticmethod
	def _profile_score(settings: Settings) -> dict | None:
		"""How much of what application forms ask the profile already answers."""
		from jobapply.profile import completeness

		try:
			text = Path(settings.profile_path).expanduser().read_text(encoding='utf-8-sig') if settings.profile_path else ''
		except OSError:
			return None
		if not text:
			return None
		score, missing = completeness(text)
		return {'score': score, 'missing': missing}

	def save_settings(self, data: dict) -> dict:
		"""Fields left out keep their value; an empty key field keeps the saved key."""
		try:
			current = dataclasses.asdict(Settings.load())
			for key in ('gemini_api_key', 'openai_api_key'):
				if not data.get(key):
					data.pop(key, None)
			if isinstance(data.get('chatgpt'), dict):
				data['chatgpt'] = {**current['chatgpt'], **data['chatgpt']}
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
		"""The Jobify browser profile opened normally, without the remote debugging port the loop drives it through:
		Google refuses to sign in a remote-controlled browser ("This browser or app may not be secure"). The logins are
		saved in the profile, and the loop's browser uses them."""
		if self._runner is not None and self._runner.browser is not None:
			return {'error': 'Stop the loop first: the Jobify browser is in use. Then log in, close that window and Start.'}
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

	def _ready_runner(self) -> Runner | dict:
		"""The runner with the current settings, or {'error': ...} when they are not complete."""
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
		elif self._runner.state == 'idle':
			self._runner.settings = settings
		return self._runner

	def start(self) -> dict:
		runner = self._ready_runner()
		if isinstance(runner, dict):
			return runner
		runner.start()
		return {'ok': True}

	def refill(self, url: str) -> dict:
		"""Go back to this job's application and fill what the profile (with the user's answers) now covers. Runs next
		when the loop is running; otherwise it runs on its own and stops after."""
		runner = self._ready_runner()
		if isinstance(runner, dict):
			return runner
		runner.request_refill(url)
		if runner.state == 'idle':
			runner.start(refill_only=True)
		return {'ok': True}

	def _refill_answered(self) -> list[str]:
		"""Finish every job whose questions are all answered or closed now."""
		from jobapply.runner import ready_jobs

		urls = ready_jobs()
		for url in urls:
			result = self.refill(url)
			if 'error' in result:
				self._event({'kind': 'log', 'text': f'Could not go back to the application: {result["error"]}', 'level': 'error'})
				break
		return urls

	def mark_submitted(self, url: str) -> dict:
		"""The user submitted this application: it is done, its questions close, and it is never applied to again."""
		from jobapply.tracker import job_key

		try:
			row = tracker().update(url, status='submitted')
			key = job_key(url)
			for q in questions().open():
				if q.url and job_key(q.url) == key:
					questions().dismiss(q.id)
			return {'ok': True, 'row': dataclasses.asdict(row)}
		except (ValueError, OSError) as e:
			return {'error': str(e)}

	def pause(self) -> dict:
		if self._runner:
			self._runner.pause()
		return {'ok': True}

	def resume(self) -> dict:
		if self._runner:
			self._runner.resume()
		return {'ok': True}

	def take_over(self) -> dict:
		"""Stop the agent after its current step so the user can use the browser; resume() continues."""
		if not (self._runner and self._runner.take_over()):
			return {'error': 'Nothing is running.'}
		return {'ok': True}

	def help_done(self) -> dict:
		"""The user did what the agent asked (captcha, login, code)."""
		if self._runner:
			self._runner.help_done()
			if self._runner.state == 'held' and not self._runner.current:  # a hold for review: the loop goes on
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
		except (ValueError, OSError) as e:
			return {'error': str(e)}
		return {'ok': True, 'question': q.question, 'finishing': self._refill_answered()}

	def dismiss(self, question_id: str) -> dict:
		"""Close a question without an answer (a login done in the browser, or one the user will not answer)."""
		try:
			questions().dismiss(question_id)
		except (ValueError, OSError) as e:
			return {'error': str(e)}
		return {'ok': True, 'finishing': self._refill_answered()}

	# ChatGPT

	def chatgpt_login(self) -> dict:
		from jobapply import chatgpt_auth

		try:
			return chatgpt_auth.login()
		except Exception as e:
			return {'error': str(e)}

	def chatgpt_logout(self) -> dict:
		from jobapply import chatgpt_auth

		chatgpt_auth.logout()
		return {'ok': True}

	# updates

	def check_update(self) -> dict:
		from jobapply import updater

		try:
			self._release = updater.check()
		except updater.UpdateError as e:
			return {'error': str(e), 'current': __version__}
		return {'current': __version__, 'release': updater.as_dict(self._release)}

	def install_update(self) -> dict:
		"""Download the newest release's installer, then close the app and let the installer replace it."""
		from jobapply import updater

		if self._updating:
			return {'error': 'The update is already downloading.'}
		if self._release is None:
			found = self.check_update()
			if 'error' in found or self._release is None:
				return found if 'error' in found else {'error': 'You already have the newest version.'}
		if self._runner is not None and self._runner.state not in ('idle',):
			return {'error': 'Stop the loop first (your open tabs stay open until the app closes), then update.'}
		release, self._updating = self._release, True

		def run() -> None:
			try:
				path = updater.download(
					release,
					progress=lambda done, total: self._event(
						{'kind': 'update', 'stage': 'download', 'done': done, 'total': total}
					),
				)
				self._event({'kind': 'update', 'stage': 'installing', 'version': release.version})
				self.shutdown()
				updater.install(path)
				if self._window is not None:
					self._window.destroy()  # the installer starts the new version
			except Exception as e:
				logger.exception('update failed')
				self._event({'kind': 'update', 'stage': 'error', 'text': str(e)})
			finally:
				self._updating = False

		threading.Thread(target=run, daemon=True, name='jobapply-update').start()
		return {'ok': True, 'version': release.version}

	def _check_update_in_background(self) -> None:
		def run() -> None:
			found = self.check_update()
			if found.get('release'):
				self._event({'kind': 'update', 'stage': 'available', 'version': found['release']['version']})

		threading.Thread(target=run, daemon=True, name='jobapply-update-check').start()

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
		raise SystemExit("Jobify's window needs pywebview: uv sync --extra jobapply") from None

	api = Api()
	window = webview.create_window(
		'Jobify',
		url=str(resource('ui/index.html')),
		js_api=api,
		width=1180,
		height=820,
		min_size=(900, 620),
	)
	api._window = window
	assert window is not None
	window.events.closed += api.shutdown
	# icon: the window/taskbar icon on Linux and macOS; on Windows the window takes Jobify.exe's icon (jobify.ico)
	webview.start(api._check_update_in_background, icon=str(resource('ui/logo.png')))  # runs once the window is up


if __name__ == '__main__':
	main()
