"""The loop: pick the next job (pasted URLs first, then board searches), open it in a new tab, let the agent fill it up to
Submit, hand the tab to the user (the guard unlocks it), log the row, and go on until the daily cap.

Runs on its own thread with its own event loop; the UI calls start(), pause(), resume() and stop() and gets events
through on_event(dict)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jobapply import audit, documents
from jobapply.cdp import print_pdf
from jobapply.guard import SubmitGuard
from jobapply.llm import agent_llm, writer_model
from jobapply.paths import app_home, sub
from jobapply.profile import Question, Questions, read_text, tone_from_criteria
from jobapply.profile import _norm as norm_question
from jobapply.prompts import APPLY_TASK, BOARD_HINTS, REFILL_TASK, RULES, SEARCH_TASK, FoundJobs, JobOutcome
from jobapply.settings import BOARDS, Settings
from jobapply.tracker import Row, Tracker, job_key

logger = logging.getLogger('jobapply')
APPLY_MAX_STEPS, APPLY_TIMEOUT_S = 90, 1500
# A provider that is busy (503, 429, overloaded) after the library's own retry: wait and run the job again
RETRY_WAITS_S = (20.0, 60.0)
BUSY = re.compile(
	r'ModelUnavailableError|ModelRateLimitError|ModelServerError|ModelConnectionError|\b(429|500|502|503|504)\b|overloaded|high demand|RESOURCE_EXHAUSTED|UNAVAILABLE'
)
SEARCH_MAX_STEPS, SEARCH_TIMEOUT_S = 40, 600
SEARCH_BATCH = 10
MAX_EMPTY_ROUNDS = 3  # a board is dropped after this many rounds in a row without a new job
MAX_ROUNDS = 10  # result pages per board in one run
COUNTED_OUTCOMES = ('filled', 'email_drafted', 'needs_input')


def tracker() -> Tracker:
	return Tracker(app_home() / 'applications.csv')


def questions() -> Questions:
	return Questions(app_home() / 'questions.json')


def ready_jobs() -> list[str]:
	"""Jobs waiting on questions that are now all answered or closed: the loop can go back and finish them."""
	still_open = {norm_question(q.question) for q in questions().open()}
	latest: dict[str, Row] = {}
	for row in tracker().rows():
		if row.url:
			latest[job_key(row.url)] = row
	return [
		row.url
		for row in latest.values()
		if row.status == 'needs_input'
		and row.unanswered_questions
		and not any(norm_question(q) in still_open for q in row.unanswered_questions.split(' | '))
	]


def build_rules(profile: str, criteria: str) -> str:
	return RULES.replace('{profile}', profile).replace('{criteria}', criteria)  # the files may hold braces


class Runner:
	def __init__(self, settings: Settings, on_event: Callable[[dict], None]):
		self.settings = settings
		self.on_event = on_event
		self.state = 'idle'  # idle | starting | running | paused | stopping
		self.tracker, self.questions = tracker(), questions()
		self._thread: threading.Thread | None = None
		self._loop: asyncio.AbstractEventLoop | None = None
		self._task: asyncio.Task | None = None
		self._resume = threading.Event()
		self._resume.set()
		self._stop = False
		self.browser: Any = None
		self.guard: SubmitGuard | None = None
		self.current: dict[str, str] = {}
		self._refills: deque[str] = deque()  # jobs to finish with the user's answers, before any new job
		self._refills_lock = threading.Lock()
		self._refill_only = False

	# control (any thread)

	def start(self, refill_only: bool = False) -> None:
		"""refill_only: only finish the jobs in request_refill(), then stop (no new jobs)."""
		if self._thread and self._thread.is_alive():
			if self.state == 'paused':
				self.resume()
			return
		self._refill_only = refill_only
		self._stop = False
		self._resume.set()
		self._thread = threading.Thread(target=self._main, daemon=True, name='jobapply-runner')
		self._thread.start()

	def request_refill(self, url: str) -> None:
		"""Go back to this job's application and fill what the user's answers now cover (next, before new jobs)."""
		with self._refills_lock:
			if all(job_key(u) != job_key(url) for u in self._refills):
				self._refills.append(url)

	def _next_refill(self) -> str | None:
		with self._refills_lock:
			return self._refills.popleft() if self._refills else None

	def pause(self) -> None:
		"""Finishes the current job, then waits."""
		if self.state in ('running', 'starting'):
			self._resume.clear()
			self._set_state('paused')

	def resume(self) -> None:
		self._resume.set()
		if self.state == 'paused':
			self._set_state('running')

	def stop(self) -> None:
		"""Stops now: the job in progress is cancelled (its tab stays open). The browser stays open with its tabs."""
		self._stop = True
		self._resume.set()
		self._set_state('stopping')
		if self._loop and self._task:
			self._loop.call_soon_threadsafe(self._task.cancel)

	# internals

	def _emit(self, kind: str, **data: Any) -> None:
		with contextlib.suppress(Exception):
			self.on_event({'kind': kind, 'time': datetime.now().strftime('%H:%M:%S'), **data})

	def _set_state(self, state: str) -> None:
		self.state = state
		self._emit('state', state=state, today=self.tracker.count_today(), cap=self.settings.daily_cap)

	def _log(self, text: str, level: str = 'info') -> None:
		getattr(logger, 'warning' if level == 'error' else 'info')(text)
		self._emit('log', text=text, level=level)

	def _main(self) -> None:
		self._loop = asyncio.new_event_loop()
		try:
			self._task = self._loop.create_task(self._run())
			self._loop.run_until_complete(self._task)
		except asyncio.CancelledError:
			self._log('Stopped.')
		except Exception as e:
			logger.exception('runner failed')
			self._log(f'Stopped: {e}', 'error')
		finally:
			with contextlib.suppress(Exception):
				self._loop.run_until_complete(self._loop.shutdown_asyncgens())
			self._loop.close()
			self._loop = self._task = None
			self.current = {}
			self._set_state('idle')

	async def _run(self) -> None:
		self._set_state('starting')
		s = self.settings
		llm = agent_llm(s)
		writer = writer_model(llm)
		read_text(s.profile_path, 'profile')
		read_text(s.criteria_path, 'job criteria')
		await self._start_browser()
		self._set_state('running' if self._resume.is_set() else 'paused')
		queue: deque[dict] = deque() if self._refill_only else deque({'url': u, 'board': ''} for u in s.job_urls)
		boards = [b for b in s.boards if b in BOARDS]
		rounds = dict.fromkeys(boards, 0)  # result pages searched per board
		empty = dict.fromkeys(boards, 0)  # rounds in a row without a new job
		board_index = 0
		while not self._stop:
			await self._paused()
			if self._stop:
				break
			if refill := self._next_refill():  # answered jobs first, also past the daily cap
				await self._refill(refill, llm, writer)
				continue
			if self._refill_only:
				break
			if self.tracker.count_today() >= s.daily_cap:
				self._log(f'Daily limit reached ({s.daily_cap} jobs). Review your open tabs; the count resets at midnight.')
				break
			if not queue:
				live = [b for b in boards if empty[b] < MAX_EMPTY_ROUNDS and rounds[b] < MAX_ROUNDS]
				if not live:
					self._log(
						'No more new jobs matching your criteria on the selected boards (each was searched several result '
						'pages deep). Widen the criteria, select more boards, or paste job URLs in Setup, then Start again.'
					)
					break
				board = live[board_index % len(live)]
				board_index += 1
				rounds[board] += 1
				found = await self._search(board, llm, rounds[board])
				fresh = [j for j in found if not self.tracker.seen(j['url'])]
				empty[board] = 0 if fresh else empty[board] + 1
				queue.extend(fresh)
				continue
			job = queue.popleft()
			if self.tracker.seen(job['url']):
				continue
			await self._apply(job, llm, writer)
		self._log(
			'Done finishing your answered applications.'
			if self._refill_only
			else 'Loop finished. Your filled tabs stay open in the JobApply browser for review.'
		)

	async def _paused(self) -> None:
		await asyncio.to_thread(self._resume.wait)  # a threading.Event: pause()/resume() come from the UI thread

	async def _start_browser(self) -> None:
		from browser_use import Browser

		if self.browser is None:
			self.browser = Browser(
				'chromium',
				user_data_dir=sub('browser-profile'),  # logins (LinkedIn, Indeed, Gmail) are kept here
				keep_alive=True,
				executable_path=self.settings.browser_executable or None,
			)
		first = self.guard is None
		await self.browser.start()  # also starts it again when the user closed the window
		ws = self._ws()
		if self.guard is None or self.guard.ws_url != ws or not self.guard.alive:
			if self.guard is not None:
				self.guard.close()
			self.guard = SubmitGuard(ws, on_block=lambda tab, text: self._emit('blocked', tab=tab, text=text))
			await asyncio.to_thread(self.guard.start)
		if first:
			self._log(
				'Browser ready. Not logged in somewhere? Stop, use Setup > Log in to sites, close that window, Start again.'
			)

	def _ws(self) -> str:
		return self.browser._daemon_env['BU_CDP_WS']

	def _open_tab(self, url: str) -> str:
		"""A new tab on url, and the agent's REPL attached to it (the next run starts there)."""
		from browser_use.browser.browser import _daemon_cdp, _daemon_request

		name = self.browser.name
		target = _daemon_cdp(name, 'Target.createTarget', url=url)['targetId']
		session = _daemon_cdp(name, 'Target.attachToTarget', targetId=target, flatten=True)['sessionId']
		_daemon_request(name, {'meta': 'set_session', 'session_id': session, 'target_id': target})
		self.browser._agent_tab = target
		return target

	def _attach_tab(self, target: str) -> None:
		"""The agent's REPL on an open tab (a job it goes back to), brought to the front."""
		from browser_use.browser.browser import _daemon_cdp, _daemon_request

		name = self.browser.name
		session = _daemon_cdp(name, 'Target.attachToTarget', targetId=target, flatten=True)['sessionId']
		_daemon_request(name, {'meta': 'set_session', 'session_id': session, 'target_id': target})
		with contextlib.suppress(Exception):
			_daemon_cdp(name, 'Target.activateTarget', targetId=target)
		self.browser._agent_tab = target

	def _close_tabs(self, targets: list[str]) -> None:
		from browser_use.browser.browser import _daemon_cdp

		for t in targets:
			with contextlib.suppress(Exception):
				_daemon_cdp(self.browser.name, 'Target.closeTarget', targetId=t)

	async def _run_agent(self, task: str, schema: Any, tools: Any, max_steps: int, timeout: float) -> Any:
		from browser_use import Agent

		profile = read_text(self.settings.profile_path, 'profile')  # read per job: answers saved meanwhile count
		criteria = read_text(self.settings.criteria_path, 'job criteria')
		agent = Agent(
			task,
			llm=agent_llm(self.settings),
			browser=self.browser,
			tools=tools,
			output_model_schema=schema,
			extend_system_message=build_rules(profile, criteria),
			workspace=sub('workspace'),
		)
		try:
			for wait in (*RETRY_WAITS_S, None):
				result = await agent.run(max_steps=max_steps, timeout=timeout)
				if wait is None or result.status != 'error' or not BUSY.search(result.error or ''):
					return result
				self._log(f'The AI model is busy; trying again in {wait:g}s.', 'error')
				await asyncio.sleep(wait)
			raise AssertionError('unreachable')
		finally:
			with contextlib.suppress(Exception):
				await agent.close()

	async def _search(self, board: str, llm: Any, round_: int = 1) -> list[dict]:
		from browser_use import Tools

		label, start_url = BOARDS[board]
		self._log(f'Searching {label} for new jobs (results page {round_})...')
		self.current = {'phase': 'search', 'board': label}
		self._emit('current', **self.current)
		tools = Tools()

		@tools.action('True when the user already has this job in their list (applied, skipped or in progress).')
		def already_seen(url: str) -> bool:
			return self.tracker.seen(url)

		await self._start_browser()
		mark = self.guard.mark() if self.guard else 0
		await asyncio.to_thread(self._open_tab, start_url)
		task = (
			SEARCH_TASK.replace('{count}', str(SEARCH_BATCH))
			.replace('{board}', label)
			.replace('{board_id}', board)
			.replace('{start_url}', start_url)
			.replace('{board_hint}', BOARD_HINTS.get(board, ''))
			.replace('{round}', str(round_))
		)
		try:
			result = await self._run_agent(task, FoundJobs, tools, SEARCH_MAX_STEPS, SEARCH_TIMEOUT_S)
		finally:
			if self.guard:  # search tabs are not for review
				await asyncio.to_thread(self._close_tabs, self.guard.tabs_since(mark))
		output = result.structured_output
		found = output.jobs if output else []
		if output and output.blocked:
			self._log(f'{label}: {output.blocked}', 'error')
		jobs = [{'url': j.url, 'board': j.board or board, 'company': j.company, 'title': j.title} for j in found if j.url]
		self._log(f'{label}: {len(jobs)} new job(s) found.' if jobs else f'{label}: nothing new.')
		return jobs

	def _job_tools(self, url: str, docs: dict[str, str], asked: list[str], writer: Any) -> Any:
		"""prepare_documents and ask_user for one job; docs and asked collect what they did."""
		from browser_use import Tools

		s = self.settings
		tools = Tools()

		@tools.action(
			'Make a resume PDF and cover letter PDF tailored to this job from the profile. Call once per job with the '
			'full job description. Returns the file paths and the letter text.'
		)
		async def prepare_documents(company: str, title: str, job_description: str) -> dict:
			if docs:
				return docs
			profile = read_text(s.profile_path, 'profile')
			criteria = read_text(s.criteria_path, 'job criteria')
			tone = tone_from_criteria(criteria) or s.cover_letter_tone
			template = read_text(s.resume_template_path, 'resume template') if s.resume_template_path else None
			self._log(f'Writing resume and cover letter for {company}...')
			tailored = await documents.tailor(writer, profile, job_description, company, title, tone)
			ws = self._ws()
			paths = await asyncio.to_thread(
				documents.write_documents, tailored, company, title, template, lambda h, p: print_pdf(ws, h, p)
			)
			docs.update(paths)
			self.current.update(company=company, title=title)
			self._emit('current', **self.current)
			return docs

		@tools.action(
			'Send the user a question the profile does not answer (they answer later in the app; the answer is saved to '
			'the profile for next time). Leave the field empty and keep going.'
		)
		def ask_user(question: str, field: str = '') -> str:
			q = self.questions.ask(
				Question(
					question=question,
					field_hint=field,
					company=self.current.get('company', ''),
					title=self.current.get('title', ''),
					url=url,
				)
			)
			asked.append(question)
			self._emit('question', id=q.id, question=q.question)
			return 'Sent to the user. Leave this field empty and continue with the rest of the form.'

		return tools

	async def _apply(self, job: dict, llm: Any, writer: Any) -> None:
		url = job['url']
		self.current = {'phase': 'apply', 'url': url, 'company': job.get('company', ''), 'title': job.get('title', '')}
		self._emit('current', **self.current)
		self._log(f'Applying: {job.get("title") or url} {("at " + job["company"]) if job.get("company") else ""}'.strip())
		docs: dict[str, str] = {}
		asked: list[str] = []
		tools = self._job_tools(url, docs, asked, writer)
		await self._start_browser()
		mark = self.guard.mark() if self.guard else 0
		tab = await asyncio.to_thread(self._open_tab, url)
		started = time.monotonic()
		outcome: JobOutcome | None = None
		error = ''
		try:
			result = await self._run_agent(APPLY_TASK.replace('{url}', url), JobOutcome, tools, APPLY_MAX_STEPS, APPLY_TIMEOUT_S)
			outcome = result.structured_output
			if outcome is None:
				error = result.error or f'the agent stopped ({result.status}) before it finished'
		except asyncio.CancelledError:
			error = 'stopped by you'
			self._finish(job, tab, mark, None, docs, asked, error)
			raise
		except Exception as e:
			logger.exception('apply failed')
			error = str(e)
		self._finish(job, tab, mark, outcome, docs, asked, error)
		self._log(f'Done in {time.monotonic() - started:.0f}s.')

	async def _refill(self, url: str, llm: Any, writer: Any) -> None:
		"""Go back to a job that waited on questions: fill what the answers (now in the profile) cover, in its tab."""
		row = self.tracker.latest(url)
		if row is None or row.status not in ('needs_input', 'failed', 'filled', 'email_drafted'):
			return
		job = {'url': url, 'company': row.company, 'title': row.title, 'board': row.board}
		self.current = {'phase': 'apply', 'url': url, 'company': row.company, 'title': row.title}
		self._emit('current', **self.current)
		self._log(f'Finishing with your answers: {row.title or url}{" at " + row.company if row.company else ""}')
		docs: dict[str, str] = {}
		if row.resume and Path(row.resume).exists():  # the documents made for this job, not new ones
			docs = {'resume_pdf': row.resume, 'cover_letter_pdf': row.cover_letter, 'folder': str(Path(row.resume).parent)}
			letter = Path(row.resume).parent / 'cover_letter.txt'
			if letter.exists():
				docs['cover_letter_text'] = letter.read_text(encoding='utf-8').strip()
		asked: list[str] = []
		tools = self._job_tools(url, docs, asked, writer)
		await self._start_browser()
		mark = self.guard.mark() if self.guard else 0
		tab = row.tab_id
		if tab and self.guard and tab in self.guard.pages:  # its tab is still open: lock it again and continue there
			self.guard.lock([tab])
			await asyncio.to_thread(self._attach_tab, tab)
			task = REFILL_TASK.replace('{url}', url).replace('{questions}', row.unanswered_questions or '(none listed)')
		else:  # the tab was closed: start the application again
			tab = await asyncio.to_thread(self._open_tab, url)
			task = APPLY_TASK.replace('{url}', url)
		outcome: JobOutcome | None = None
		error = ''
		try:
			result = await self._run_agent(task, JobOutcome, tools, APPLY_MAX_STEPS, APPLY_TIMEOUT_S)
			outcome = result.structured_output
			if outcome is None:
				error = result.error or f'the agent stopped ({result.status}) before it finished'
		except asyncio.CancelledError:
			self._finish(job, tab, mark, None, docs, asked, 'stopped by you', update=True)
			raise
		except Exception as e:
			logger.exception('refill failed')
			error = str(e)
		self._finish(job, tab, mark, outcome, docs, asked, error, update=True)

	def _finish(
		self,
		job: dict,
		tab: str,
		mark: int,
		outcome: JobOutcome | None,
		docs: dict,
		asked: list[str],
		error: str,
		update: bool = False,
	) -> None:
		"""Audit the filled tabs, hand them to the user, and log the job (update: change its existing row)."""
		tabs = self.guard.tabs_since(mark) if self.guard else [tab]
		if update and tab not in tabs:
			tabs.insert(0, tab)
		status = outcome.status if outcome else 'failed'
		if status in ('filled', 'email_drafted', 'needs_input', 'failed') and self.browser is not None:
			try:  # answers the profile does not give (salary, start date, visa...) are emptied and asked
				profile = read_text(self.settings.profile_path, 'profile')
				for item in audit.correct_contacts(self._ws(), tabs, profile):
					self._log(
						f'Corrected {item["kind"]}: "{item["before"][:60]}" -> "{item["after"][:60]}" (from your profile).',
						'error',
					)
				for item in audit.take_back(self._ws(), tabs, profile):
					question = (item['label'] or item['topic']).rstrip(' *')
					self.questions.ask(
						Question(question=question, field_hint=item['topic'], company=job.get('company', ''), url=job['url'])
					)
					asked.append(question)
					self._log(
						f'Cleared a guessed answer ({item["topic"]}): "{question[:80]}". It is in Questions for you.', 'error'
					)
			except Exception as e:
				logger.warning('answer audit failed: %s', e)
		if not asked and outcome and outcome.status == 'needs_input':
			# the agent reported questions without sending them: send them, so they can be answered
			for text in outcome.unanswered_questions:
				q = self.questions.ask(Question(question=text, company=job.get('company', ''), url=job['url']))
				asked.append(q.question)
				self._emit('question', id=q.id, question=q.question)
		if status == 'filled' and asked:  # a field was left empty for the user
			status = 'needs_input'
		if update and status == 'failed':  # a job that was filled before stays the user's to finish
			status = 'needs_input'
		if status == 'skipped':
			self._close_tabs(tabs)
			tabs = []
		elif self.guard:  # the user reviews and submits in these tabs
			self.guard.release(tabs)
		unique: dict[str, str] = {}
		for q in asked:  # the questions in Questions, once each however they were punctuated
			unique.setdefault(norm_question(q), q)
		values = {
			'company': (outcome.company if outcome else '') or job.get('company', ''),
			'title': (outcome.title if outcome else '') or job.get('title', ''),
			'board': (outcome.board if outcome else '') or job.get('board', ''),
			'status': status,
			'tab_id': tab if tabs else '',
			'resume': docs.get('resume_pdf', ''),
			'cover_letter': docs.get('cover_letter_pdf', ''),
			'unanswered_questions': ' | '.join(unique.values()),
			'notes': (outcome.notes if outcome else '') or error,
		}
		if update:
			row = self.tracker.update(job['url'], **values)
		else:
			row = self.tracker.add(Row(url=job['url'], **values))
		self.current = {}
		self._emit('job', row=row.__dict__, today=self.tracker.count_today(), cap=self.settings.daily_cap)

	def focus_tab(self, tab: str) -> bool:
		"""Bring a job's tab to the front for review."""
		if not (self.browser and self.browser.name and tab):
			return False
		from browser_use.browser.browser import _daemon_cdp

		try:
			_daemon_cdp(self.browser.name, 'Target.activateTarget', targetId=tab)
			return True
		except Exception:
			return False

	async def close_browser(self) -> None:
		if self.guard:
			self.guard.close()
			self.guard = None
		if self.browser:
			with contextlib.suppress(Exception):
				await self.browser.stop()
			self.browser = None
