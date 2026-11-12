"""The desktop window (pywebview): Setup, Run, Questions and History screens over the runner.

The page (ui/index.html) calls the Api methods through window.pywebview.api and polls events() once a second."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
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


LINK = re.compile(r'(?:https?://|www\.)[^\s<>"\'`]+', re.IGNORECASE)


def parse_links(text: str) -> list[str]:
	"""The job links in pasted text (one per line, or mixed with other words), in order, without repeats."""
	from jobapply.tracker import job_key

	links: list[str] = []
	keys: set[str] = set()
	for match in LINK.findall(text or ''):
		url = match.rstrip('.,;:!?)]}>\'"')
		if url.lower().startswith('www.'):
			url = 'https://' + url
		if job_key(url) not in keys:
			keys.add(job_key(url))
			links.append(url)
	return links


def link_status(url: str, rows: list, current: dict) -> str:
	"""'waiting', 'applying', or the status of the job's newest row in the history."""
	from jobapply.tracker import job_key

	key = job_key(url)
	if current.get('url') and job_key(current['url']) == key:
		return 'applying'
	row = next((r for r in reversed(rows) if r.url and job_key(r.url) == key), None)
	return row.status if row else 'waiting'


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
		rows = t.rows()
		current = self._runner.current if self._runner else {}
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
			'rows': [dataclasses.asdict(r) for r in reversed(rows[-300:])],
			'links': [{'url': u, 'status': link_status(u, rows, current)} for u in settings.job_urls],
			'home': str(app_home()),
			'profile_score': self._profile_score(settings),
			'setup': self._setup_status(settings),
			'last_stop': self._runner.last_stop if self._runner else None,
		}

	@staticmethod
	def _setup_status(settings: Settings) -> dict:
		"""What Start needs, for the first-run checklist: the two files and a usable AI model."""

		def usable(path: str) -> bool:
			try:
				return bool(path) and bool(Path(path).expanduser().read_text(encoding='utf-8-sig').strip())
			except OSError:
				return False

		model_error = ''
		if settings.provider == 'gemini' and not settings.gemini_api_key:
			model_error = 'Add your Gemini API key.'
		elif settings.provider == 'openai' and not settings.openai_api_key:
			model_error = 'Add your OpenAI API key.'
		elif settings.provider == 'chatgpt':
			from jobapply import chatgpt_auth

			with contextlib.suppress(Exception):
				if not chatgpt_auth.status().get('signed_in'):
					model_error = 'Sign in with ChatGPT.'
		return {
			'profile': usable(settings.profile_path),
			'criteria': usable(settings.criteria_path),
			'model': not model_error,
			'model_error': model_error,
		}

	# History: one job's A-to-Z, and its documents shown in the app

	def job_detail(self, url: str) -> dict:
		"""The job's newest History row and its record (what it was about, what was filled and attached, what
		happened). A job from before records existed has record {}."""
		from jobapply import records

		row = tracker().latest(url)
		if row is None:
			return {'error': 'That job is not in your History.'}
		record = records.load(url)
		documents = dict(record.get('documents') or {})
		documents.setdefault('resume_pdf', row.resume)
		documents.setdefault('cover_letter_pdf', row.cover_letter)
		if not documents.get('folder') and row.resume:
			documents['folder'] = str(Path(row.resume).parent)
		folder = Path(documents['folder']) if documents.get('folder') else None
		if folder and not documents.get('cover_letter_text') and (folder / 'cover_letter.txt').exists():
			with contextlib.suppress(OSError):
				documents['cover_letter_text'] = (folder / 'cover_letter.txt').read_text(encoding='utf-8').strip()
		if folder and not record.get('summary') and (folder / 'resume.json').exists():
			with contextlib.suppress(Exception):  # documents written before records: the summary is in the model's JSON
				import json

				saved = json.loads((folder / 'resume.json').read_text(encoding='utf-8'))
				if saved.get('job_summary'):
					record['summary'] = saved['job_summary']
		record['documents'] = documents
		for name in ('resume_pdf', 'cover_letter_pdf'):
			documents[name + '_exists'] = bool(documents.get(name)) and Path(documents[name]).exists()
		return {'row': dataclasses.asdict(row), 'record': record}

	def document_pages(self, path: str, scale: float = 2.0) -> dict:
		"""A PDF's pages (or a PNG/JPEG) as images for the in-app viewer: {pages: [data URL], width, height}. Only
		files in the Jobify data folder (the documents and screenshots it made)."""
		from jobapply import viewer

		try:
			return viewer.pages(path, scale=scale)
		except viewer.ViewerError as e:
			return {'error': str(e)}
		except Exception as e:
			logger.exception('document view failed')
			return {'error': f'Could not show this file: {e}'}

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
		"""Open a copy of applications.csv: Excel locks the file it opens, and Jobify could not log the next job."""
		path = app_home() / 'applications.csv'
		if not path.exists():
			return {'error': 'No applications yet.'}
		copy = sub('exports') / 'applications (copy).csv'
		try:
			shutil.copyfile(path, copy)
		except OSError:  # the copy is open in Excel from last time: a new name
			from datetime import datetime

			copy = sub('exports') / f'applications {datetime.now():%Y-%m-%d %H%M%S}.csv'
			try:
				shutil.copyfile(path, copy)
			except OSError as e:
				return {'error': f'Could not copy the history: {e}'}
		return self.open_path(str(copy))

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

	# job links

	def add_job_links(self, text: str, apply_now: bool = False) -> dict:
		"""Add the job links in the pasted text. They are applied to next while the loop runs; apply_now starts a run
		for the links alone when it is idle (no board search)."""
		from jobapply.tracker import job_key

		links = parse_links(text)
		if not links:
			return {'error': 'No job links found. Paste links that start with https://'}
		settings = Settings.load()
		keys = {job_key(u) for u in settings.job_urls}
		t = tracker()
		new = [u for u in links if job_key(u) not in keys and not t.seen(u)]
		done = sum(1 for u in links if t.seen(u))
		if new:
			settings.job_urls = [*settings.job_urls, *new]
			settings.save()
		runner = self._runner
		if new and runner and runner.state != 'idle':
			runner.add_links(new)
		elif apply_now and (new or any(link_status(u, t.rows(), {}) == 'waiting' for u in settings.job_urls)):
			ready = self._ready_runner()
			if isinstance(ready, dict):
				return {**ready, 'added': len(new), 'already_done': done}
			ready.start(links_only=True)
		return {'ok': True, 'added': len(new), 'already_done': done, 'duplicates': len(links) - len(new) - done}

	def remove_job_link(self, url: str) -> dict:
		from jobapply.tracker import job_key

		settings = Settings.load()
		settings.job_urls = [u for u in settings.job_urls if job_key(u) != job_key(url)]
		settings.save()
		if self._runner:
			self._runner.remove_link(url)
		return {'ok': True}

	def clear_finished_links(self) -> dict:
		"""Drop the links that were applied to (or skipped), keep the ones still waiting."""
		settings = Settings.load()
		rows = tracker().rows()
		current = self._runner.current if self._runner else {}
		settings.job_urls = [u for u in settings.job_urls if link_status(u, rows, current) in ('waiting', 'applying')]
		settings.save()
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

	def test_model(self, provider: str = '') -> dict:
		"""One tiny request to the AI model as saved in Setup (or the backup provider): says whether it works before
		a run depends on it."""
		from browser_use.llm import provider_preflight
		from jobapply.llm import agent_llm, writer_model

		try:
			settings = Settings.load()
			model = writer_model(agent_llm(settings, provider or None))
			if error := provider_preflight(model):
				return {'error': f'The AI provider refused it: {error}'}
			from pydantic_ai import Agent

			async def ask() -> str:
				result = await asyncio.wait_for(Agent(model).run('Reply with the single word OK.'), 60)
				return str(result.output)

			reply = asyncio.run(ask())
		except TimeoutError:
			return {'error': 'The AI model did not answer within a minute. Try again, or pick another model.'}
		except Exception as e:
			return {'error': f'The AI model did not work: {e}'}
		name = getattr(model, 'model_name', '') or (provider or settings.provider)
		return {'ok': True, 'model': name, 'reply': reply[:80]}

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
	# The window/taskbar icon on Linux and macOS. Not on Windows: pywebview loads it there with .NET Icon(path), which
	# only reads .ico and crashed the window before it opened; the window takes Jobify.exe's icon (jobify.ico) instead.
	icon = None if sys.platform == 'win32' else str(resource('ui/logo.png'))
	webview.start(api._check_update_in_background, icon=icon)  # runs once the window is up


def show_startup_error(error: BaseException) -> None:
	"""The windowed exe has no console: a crash before the window opens would be silent. Log it and say so."""
	logger.exception('Jobify could not start')
	text = f'Jobify could not start:\n\n{error}\n\nDetails are in {app_home() / "jobapply.log"}'
	if sys.platform == 'win32':
		with contextlib.suppress(Exception):
			import ctypes

			ctypes.windll.user32.MessageBoxW(None, text, 'Jobify', 0x10)  # type: ignore[attr-defined]


def run() -> None:
	try:
		main()
	except SystemExit:
		raise
	except BaseException as e:
		show_startup_error(e)
		raise


if __name__ == '__main__':
	run()
