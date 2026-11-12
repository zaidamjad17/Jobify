"""The loop: pick the next job (pasted URLs first, then board searches), open it in a new tab, let the agent fill it up to
Submit, hand the tab to the user (the guard unlocks it), log the row, and go on until the daily cap.

Runs on its own thread with its own event loop; the UI calls start(), pause(), resume() and stop() and gets events
through on_event(dict)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jobapply import audit, documents, records
from jobapply.cdp import print_pdf
from jobapply.guard import SubmitGuard
from jobapply.llm import agent_llm, writer_model
from jobapply.paths import app_home, sub
from jobapply.profile import Question, Questions, read_text, tone_from_criteria
from jobapply.profile import _norm as norm_question
from jobapply.prompts import APPLY_TASK, BOARD_HINTS, REFILL_TASK, RULES, SEARCH_TASK, FoundJobs, JobOutcome
from jobapply.settings import BOARDS, Settings
from jobapply.tracker import Row, Tracker, TrackerLocked, job_key

logger = logging.getLogger('jobapply')
APPLY_MAX_STEPS, APPLY_TIMEOUT_S = 90, 1500
# A provider that is busy (503, 429, overloaded) after the library's own retry: wait and run the job again
RETRY_WAITS_S = (20.0, 60.0)
BUSY = re.compile(
	r'ModelUnavailableError|ModelRateLimitError|ModelServerError|ModelConnectionError|\b(429|500|502|503|504)\b|overloaded|high demand|RESOURCE_EXHAUSTED|UNAVAILABLE'
)
# The browser crashed or was closed under a running job: start it again and run the job once more
CRASHED = re.compile(
	r'browser is gone|browser\b.{0,40}\b(exited|crashed|closed)|no close frame|DevTools port|ConnectionClosed', re.I
)
SEARCH_MAX_STEPS, SEARCH_TIMEOUT_S = 40, 600
# Pre-flight: a page per board (and Gmail) that shows whether the user is logged in and not blocked. Boards searched
# through Google share its page, so one check covers them.
GOOGLE_CHECK = 'https://www.google.com/search?q=software+engineer+jobs'
PREFLIGHT: dict[str, tuple[str, str]] = {
	'linkedin': ('LinkedIn', 'https://www.linkedin.com/feed/'),
	'indeed': ('Indeed', 'https://www.indeed.com/'),
	'greenhouse': ('Google search (Greenhouse)', GOOGLE_CHECK),
	'lever': ('Google search (Lever)', GOOGLE_CHECK),
	'ashby': ('Google search (Ashby)', GOOGLE_CHECK),
	'wellfound': ('Wellfound', 'https://wellfound.com/jobs'),
	'gmail': ('Gmail', 'https://mail.google.com/mail/u/0/'),
}
LOGIN_PAGE = re.compile(r'login|signin|sign-in|sign_in|authwall|checkpoint|ServiceLogin|/auth\b|/uas/', re.I)
BOT_CHECK = re.compile(
	r'captcha|verify you are (a )?human|unusual traffic|are you a robot|just a moment|security check|additional verification|/sorry/',
	re.I,
)
PAGE_STATE_JS = (
	"JSON.stringify({url: location.href, title: document.title, text: (document.body ? document.body.innerText : '')"
	".slice(0, 3000), password: !!document.querySelector('input[type=password]')})"
)


def page_problem(state: dict, label: str) -> str | None:
	"""What a pre-flight page shows: None when usable, else what the user has to do."""
	where = f'{state.get("url", "")} {state.get("title", "")}'
	if BOT_CHECK.search(where) or BOT_CHECK.search(state.get('text', '')[:1500]):
		return f'{label} shows a bot check (captcha / "verify you are human"): solve it in the Jobify browser'
	if state.get('password') or LOGIN_PAGE.search(state.get('url', '')):
		return f'Log in to {label} in the Jobify browser'
	return None


SEARCH_BATCH = 10
MAX_EMPTY_ROUNDS = 3  # a board is dropped after this many rounds in a row without a new job
MAX_ROUNDS = 10  # result pages per board in one run
COUNTED_OUTCOMES = ('filled', 'email_drafted', 'needs_input')
HELP_WAIT_S = 60  # how long the agent waits for the user on a captcha / login before it parks the job
STUCK_REPEATS = 5  # the same step this many times in a row: the agent is stuck, the job is parked
HELP_KINDS = ('login', 'help')  # questions closed with Done (no answer): the user did something in the browser
MAX_ERRORS_IN_ROW = 3  # jobs or searches failing in a row on errors (not on the page): the loop holds and asks the user
MAX_SEARCH_FAILURES = 3  # a board whose search fails this many times in a row is left out of this run
MAX_CRASHES_IN_ROW = 5  # loop steps in a row that raised: something is broken for good, the run ends with the reason
MAX_REST_CYCLES = 8  # searches again after a rest at most this many times in a row without a new job
# The model refused the key or the account: every job would fail the same way, so the loop holds at once
MODEL_REJECTED = re.compile(
	r'\b401\b|API key not valid|invalid[_ ]api[_ ]key|Incorrect API key|PERMISSION_DENIED|Unauthorized|'
	r'API_KEY_INVALID|billing|insufficient_quota|account is not active|deactivated',
	re.I,
)


class SearchFailed(Exception):
	"""A board search ended on an error (the model, the browser), not on the page: it is not 'nothing new'."""


class Interrupted(Exception):
	"""The agent's run was stopped on purpose: 'hold' (the user took over) or 'stuck' (repeating itself)."""

	def __init__(self, reason: str):
		super().__init__(reason)
		self.reason = reason


class StepWatch(logging.Handler):
	"""Sees each agent step (browser_use logs 'step N: <first code line> [-> error]') and calls on_stuck when the same
	step repeats STUCK_REPEATS times in a row."""

	def __init__(self, on_stuck: Callable[[str], None]):
		super().__init__(logging.INFO)
		self.on_stuck = on_stuck
		self.last: list[str] = []
		self.fired = False

	def emit(self, record: logging.LogRecord) -> None:
		message = record.getMessage()
		if not message.startswith('step ') or ': ' not in message:
			return
		step = ' '.join(message.split(': ', 1)[1].split())
		self.last = [*self.last[-(STUCK_REPEATS - 1) :], step] if STUCK_REPEATS > 1 else [step]
		if not self.fired and len(self.last) >= STUCK_REPEATS and len(set(self.last)) == 1:
			self.fired = True
			self.on_stuck(step)


def tracker() -> Tracker:
	return Tracker(app_home() / 'applications.csv')


def questions() -> Questions:
	return Questions(app_home() / 'questions.json')


DISMISSED = '(dismissed)'


def _acted(q: Question) -> bool:
	"""Answered, or a login request the user marked Done: something the agent can now continue with."""
	return bool(q.answer) and (q.answer != DISMISSED or q.field_hint in HELP_KINDS)


def _job_questions(row: Row) -> dict[str, list[Question]]:
	"""The questions in this job's row, by state: open, acted (answered / login done), skipped."""
	names = {norm_question(q) for q in row.unanswered_questions.split(' | ') if q.strip()}
	by_state: dict[str, list[Question]] = {'open': [], 'acted': [], 'skipped': []}
	for q in questions().all():
		if norm_question(q.question) in names:
			by_state['open' if not q.answer else 'acted' if _acted(q) else 'skipped'].append(q)
	return by_state


def skipped_questions(url: str) -> list[str]:
	"""Questions of this job the user chose not to answer (Skip): left empty and not asked again."""
	row = tracker().latest(url)
	return [q.question for q in _job_questions(row)['skipped']] if row else []


def ready_jobs() -> list[str]:
	"""Jobs waiting on questions that are all closed now, at least one by an answer (or a login marked Done): the loop
	can go back and fill them. Skips alone add nothing to fill."""
	latest: dict[str, Row] = {}
	for row in tracker().rows():
		if row.url:
			latest[job_key(row.url)] = row
	ready = []
	for row in latest.values():
		if row.status != 'needs_input' or not row.unanswered_questions:
			continue
		state = _job_questions(row)
		if not state['open'] and state['acted']:
			ready.append(row.url)
	return ready


def refill_task(url: str, row: Row) -> str:
	state = _job_questions(row)
	answered = ' | '.join(q.question for q in state['acted']) or row.unanswered_questions or '(none listed)'
	skipped = ' | '.join(q.question for q in state['skipped']) or '(none)'
	return REFILL_TASK.replace('{url}', url).replace('{questions}', answered).replace('{skipped}', skipped)


def freshest_first(jobs: list[dict], max_age_days: int) -> list[dict]:
	"""Newest postings first (they get the most replies); older than max_age_days dropped; unknown age last."""
	kept = [j for j in jobs if j.get('days') is None or j['days'] <= max_age_days]
	return sorted(kept, key=lambda j: (j.get('days') is None, j.get('days') or 0))


REVIEW_STATUSES = ('filled', 'email_drafted', 'needs_input')


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
		self._links: deque[str] = deque()  # job links the user added, applied to before any board search
		self._links_lock = threading.Lock()
		self._links_only = False
		self._agent_task: asyncio.Task | None = None  # the agent run in progress (take_over / stuck cancel it)
		self._interrupt: str | None = None  # why it was cancelled: 'hold' or 'stuck'
		self._help_done = threading.Event()  # the user clicked "I did it, continue" on a handoff
		self.help: dict[str, str] | None = None  # the handoff waiting for the user, for the UI
		self._errors_in_row = 0  # jobs / searches that ended on an error in a row (reset by one that worked)
		self._last_error = ''
		self._job_log: list[dict] = []  # the current job's activity, saved in its record
		self._wake = threading.Event()  # links or answers arrived while the loop rests between searches
		self._stop_reason: tuple[str, str] | None = None  # (kind, text): why the loop ended, for the UI
		self.last_stop: dict[str, str] | None = None  # the last run's end: {kind, text, time}

	# control (any thread)

	def start(self, refill_only: bool = False, links_only: bool = False) -> None:
		"""refill_only: only finish the jobs in request_refill(), then stop (no new jobs). links_only: apply to the
		user's job links (settings.job_urls and add_links()), then stop (no board search)."""
		if self._thread and self._thread.is_alive():
			if self.state == 'paused':
				self.resume()
			return
		self._refill_only = refill_only
		self._links_only = links_only
		self._stop = False
		self._stop_reason = None
		self.last_stop = None
		self._errors_in_row = 0
		self._resume.set()
		self._thread = threading.Thread(target=self._main, daemon=True, name='jobapply-runner')
		self._thread.start()

	def request_refill(self, url: str, first: bool = False) -> None:
		"""Go back to this job's application and fill what the user's answers now cover (next, before new jobs)."""
		with self._refills_lock:
			if all(job_key(u) != job_key(url) for u in self._refills):
				(self._refills.appendleft if first else self._refills.append)(url)
		self._wake.set()

	def _next_refill(self) -> str | None:
		with self._refills_lock:
			return self._refills.popleft() if self._refills else None

	def add_links(self, urls: list[str]) -> None:
		"""Job links to apply to next (before any board search), also while the loop runs."""
		with self._links_lock:
			keys = {job_key(u) for u in self._links}
			for url in urls:
				if job_key(url) not in keys:
					keys.add(job_key(url))
					self._links.append(url)
		self._wake.set()

	def remove_link(self, url: str) -> None:
		with self._links_lock:
			key = job_key(url)
			self._links = deque(u for u in self._links if job_key(u) != key)

	def _next_link(self) -> str | None:
		with self._links_lock:
			return self._links.popleft() if self._links else None

	def pause(self) -> None:
		"""Finishes the current job, then waits."""
		if self.state in ('running', 'starting'):
			self._resume.clear()
			self._set_state('paused')

	def resume(self) -> None:
		self._resume.set()
		if self.state in ('paused', 'held'):
			self._set_state('running')

	def take_over(self) -> bool:
		"""The user wants the browser: the agent stops after its current step, the job waits (it is finished first
		after resume()), and the loop holds until resume()."""
		if self.state not in ('running', 'starting'):
			return False
		self._resume.clear()
		self._interrupt = 'hold'
		self._set_state('held')
		if self._loop and self._agent_task:
			self._loop.call_soon_threadsafe(self._agent_task.cancel)
		return True

	def help_done(self) -> None:
		"""The user finished what the agent asked for (captcha, login, code): the agent continues the job."""
		self._help_done.set()

	def stop(self) -> None:
		"""Stops now: the job in progress is cancelled (its tab stays open). The browser stays open with its tabs."""
		self._stop = True
		self._stop_reason = self._stop_reason or ('stopped', 'Stopped by you. Your filled tabs stay open for review.')
		self._resume.set()
		self._wake.set()
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
		if self.current.get('url'):  # the job's own story, kept in its record for History
			self._job_log.append({'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'text': text, 'level': level})
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
			self._stop_reason = ('error', str(e) or type(e).__name__)
		finally:
			with contextlib.suppress(Exception):
				self._loop.run_until_complete(self._loop.shutdown_asyncgens())
			self._loop.close()
			self._loop = self._task = None
			self.current = {}
			kind, text = self._stop_reason or ('done', 'Finished.')
			self.last_stop = {'kind': kind, 'text': text, 'time': datetime.now().strftime('%H:%M:%S')}
			self._emit('stopped', text=text, stop_kind=kind)
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
		queue: deque[dict] = deque()
		if not self._refill_only:
			self.add_links(s.job_urls)
		boards = [] if self._links_only else [b for b in s.boards if b in BOARDS]
		if boards and not self._refill_only:
			blocked = await self._preflight(boards)
			if blocked and not [b for b in boards if b not in blocked]:
				self._log('Every selected board is blocked or logged out; only your job links are applied to.', 'error')
			boards = [b for b in boards if b not in blocked]
		rounds = dict.fromkeys(boards, 0)  # result pages searched per board
		empty = dict.fromkeys(boards, 0)  # rounds in a row without a new job
		failures = dict.fromkeys(boards, 0)  # searches in a row that ended on an error
		board_index = 0
		rests = 0  # rests in a row (searched everything again and found nothing new)
		crashes = 0  # loop steps in a row that raised
		while not self._stop:
			try:
				if crashes:
					await asyncio.sleep(min(5 * crashes, 30))
				await self._paused()
				if self._stop:
					break
				searchable = [b for b in boards if failures[b] < MAX_SEARCH_FAILURES]
				if self._errors_in_row >= MAX_ERRORS_IN_ROW and self._has_work(queue, searchable):
					await self._hold_for_errors()  # something keeps failing: ask instead of burning jobs
					llm, writer = self._reload_models(llm, writer)
					continue
				if refill := self._next_refill():  # answered jobs first, also past the daily cap
					await self._refill(refill, llm, writer)
					continue
				if self._refill_only:
					self._stop_reason = ('done', 'Finished your answered applications.')
					break
				if self.tracker.count_today() >= self.settings.daily_cap:
					text = f'Daily limit reached ({self.settings.daily_cap} jobs). Review your open tabs; the count resets at midnight.'
					self._log(text)
					self._stop_reason = ('cap', text)
					break
				with self._links_lock:
					has_link = bool(self._links)
				if has_link:  # the user's own links first, also ones added while the loop runs
					if await self._hold_for_review():
						continue
					link = self._next_link()
					if link and not self.tracker.seen(link):
						await self._apply({'url': link, 'board': ''}, llm, writer)
					continue
				if not queue:
					live = [
						b
						for b in boards
						if empty[b] < MAX_EMPTY_ROUNDS and rounds[b] < MAX_ROUNDS and failures[b] < MAX_SEARCH_FAILURES
					]
					if not live:
						if not boards:  # links only (or no board selected): the user's links are done
							text = 'Done with your job links.'
							self._log(text)
							self._stop_reason = ('done', text)
							break
						searchable = [b for b in boards if failures[b] < MAX_SEARCH_FAILURES]
						if not searchable:
							text = f'Searching keeps failing on every board (last error: {self._last_error[:200]}).'
							self._log(text, 'error')
							self._stop_reason = ('error', text)
							break
						if self.settings.keep_searching and rests < MAX_REST_CYCLES:
							rests += 1
							await self._rest(self.settings.search_again_minutes)
							rounds = dict.fromkeys(boards, 0)  # newest first again: new postings are on page 1
							empty = dict.fromkeys(boards, 0)
							continue
						text = (
							'No more new jobs matching your criteria on the selected boards (each was searched several '
							'result pages deep). Widen the criteria, select more boards, or add job links on the Run tab.'
						)
						self._log(text)
						self._stop_reason = ('done', text)
						break
					board = live[board_index % len(live)]
					board_index += 1
					rounds[board] += 1
					try:
						found = await self._search(board, llm, rounds[board])
					except SearchFailed as e:
						failures[board] += 1
						self._note_error(str(e))
						label = BOARDS[board][0]
						if failures[board] >= MAX_SEARCH_FAILURES:
							self._log(f'{label}: the search keeps failing; leaving it out of this run.', 'error')
						continue
					failures[board] = 0
					self._errors_in_row = 0
					if found is None:  # interrupted (Take over): search this page again after Continue
						rounds[board] -= 1
						continue
					fresh = [j for j in found if not self.tracker.seen(j['url'])]
					empty[board] = 0 if fresh else empty[board] + 1
					if fresh:
						rests = 0
					queue.extend(fresh)
					continue
				if await self._hold_for_review():
					continue  # the user may have answered questions meanwhile: refills first
				job = queue.popleft()
				if self.tracker.seen(job['url']):
					continue
				await self._apply(job, llm, writer)
				crashes = 0
			except asyncio.CancelledError:
				raise
			except Exception as e:  # one bad job or search never ends the whole run
				logger.exception('loop step failed')
				crashes += 1
				if crashes >= MAX_CRASHES_IN_ROW:  # the same thing breaks every time: end with the reason
					raise
				self.current = {}
				self._note_error(str(e) or type(e).__name__)
				self._log(f'Something went wrong ({e}); Jobify carries on.', 'error')
		if self._refill_only:
			self._log('Done finishing your answered applications.')
		else:
			self._log('Loop finished. Your filled tabs stay open in the Jobify browser for review.')
		self._log(self.summary())

	def _has_work(self, queue: deque, boards: list[str]) -> bool:
		"""Anything left that would run into the same error: a refill, a link, a found job, or a board to search."""
		with self._links_lock, self._refills_lock:
			return bool(self._refills or self._links or queue or (boards and not self._refill_only))

	def _note_error(self, error: str) -> None:
		"""A job or search ended on an error (not on the page). A key the provider refuses holds the loop at once."""
		self._errors_in_row += 1
		self._last_error = error
		if MODEL_REJECTED.search(error):
			self._errors_in_row = max(self._errors_in_row, MAX_ERRORS_IN_ROW)

	def _reload_models(self, llm: Any, writer: Any) -> tuple[Any, Any]:
		"""After the user fixed something in Setup: the new key / model is used without a restart."""
		from jobapply.settings import Settings

		try:
			settings = Settings.load()
			settings.job_urls = self.settings.job_urls
			new = agent_llm(settings)
		except Exception as e:  # still not usable: keep the old ones; the next error holds again
			self._log(f'Setup is still not complete: {e}', 'error')
			return llm, writer
		self.settings = settings
		return new, writer_model(new)

	async def _hold_for_errors(self) -> None:
		"""Jobs or searches keep failing on errors: hold (banner + sound) until the user clicks Continue."""
		error = self._last_error or 'unknown error'
		if MODEL_REJECTED.search(error):
			reason = (
				f'The AI provider refused the request ({error[:160]}). Check the key, model or plan in Setup > AI model, '
				'save, then click Continue.'
			)
		else:
			reason = (
				f'{self._errors_in_row} jobs or searches in a row ended on an error (last: {error[:160]}). Check your '
				'internet, the AI model in Setup and the Jobify browser, then click Continue.'
			)
		self._errors_in_row = 0
		self._resume.clear()
		self._set_state('held')
		self.help = {'reason': reason, 'need': 'errors', 'url': '', 'tab': '', 'company': ''}
		self._log(f'Needs you: {reason}', 'error')
		self._emit('help', sound=True, wait_s=0, **self.help)
		try:
			await self._paused()
		finally:
			self.help = None
			self._emit('help_end')
		if self.state == 'held':
			self._set_state('running')

	async def _rest(self, minutes: int) -> None:
		"""Nothing new on the boards right now: wait, then search again from the newest postings. Job links, answered
		questions and Stop end the wait early."""
		until = datetime.now().timestamp() + minutes * 60
		at = datetime.fromtimestamp(until).strftime('%H:%M')
		self._log(f'No new matching jobs right now. Jobify searches again at {at} (Setup > Jobs: keep searching).')
		self.current = {'phase': 'rest', 'until': at}
		self._emit('current', **self.current)
		self._wake.clear()
		try:
			while not self._stop and datetime.now().timestamp() < until:
				with self._links_lock, self._refills_lock:
					if self._links or self._refills:
						break
				await asyncio.to_thread(self._wake.wait, 1.0)
				self._wake.clear()
		finally:
			self.current = {}
			self._emit('current')

	def summary(self) -> str:
		"""Today in one line: what is ready, what needs the user, what happened to the rest."""
		today = datetime.now().strftime('%Y-%m-%d')
		latest: dict[str, Row] = {}
		for row in self.tracker.rows():
			if row.url and row.date == today:
				latest[job_key(row.url)] = row
		count = lambda *statuses: sum(1 for r in latest.values() if r.status in statuses)  # noqa: E731
		return (
			f'Today: {count("filled", "email_drafted")} ready to submit, {count("needs_input")} need you, '
			f'{count("submitted")} submitted, {count("skipped")} skipped, {count("failed")} failed.'
		)

	def review_waiting(self) -> int:
		"""Filled jobs whose tabs are still open for the user (not yet marked submitted)."""
		latest: dict[str, Row] = {}
		for row in self.tracker.rows():
			if row.url:
				latest[job_key(row.url)] = row
		pages = self.guard.pages if self.guard else {}
		return sum(1 for r in latest.values() if r.status in REVIEW_STATUSES and any(t in pages for t in r.tab_id.split()))

	async def _hold_for_review(self) -> bool:
		"""Too many filled tabs wait for the user: hold the loop (banner + sound) until Continue. True if it held."""
		waiting = self.review_waiting()
		if waiting < self.settings.max_review_tabs:
			return False
		reason = (
			f'{waiting} applications are waiting for your review. Submit them (then Submitted in History) or close '
			'their tabs, then click Continue.'
		)
		self._resume.clear()
		self._set_state('held')
		self.help = {'reason': reason, 'need': 'review', 'url': '', 'tab': '', 'company': ''}
		self._log(f'Needs you: {reason}', 'error')
		self._emit('help', sound=True, wait_s=0, **self.help)
		try:
			await self._paused()
		finally:
			self.help = None
			self._emit('help_end')
		if self.state == 'held':
			self._set_state('running')
		return True

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
		"""Run the agent on the current tab: retried while the model is busy, then on the backup model if one is set.
		Raises Interrupted for take_over() and the stuck watch."""
		from browser_use import Agent

		profile = read_text(self.settings.profile_path, 'profile')  # read per job: answers saved meanwhile count
		criteria = read_text(self.settings.criteria_path, 'job criteria')
		watch = StepWatch(self._stuck)
		steps = logging.getLogger('browser_use.agent.loop')
		if steps.getEffectiveLevel() > logging.INFO:
			steps.setLevel(logging.INFO)
		steps.addHandler(watch)
		self._interrupt = None
		backup = self.settings.backup_provider
		plan: list[tuple[str | None, float | None]] = [(None, w) for w in RETRY_WAITS_S]
		plan.append((None, None))  # the last try on the main model
		if backup:
			plan.append((backup, None))
		try:
			result = None
			for provider, wait in plan:
				if provider:
					self._log(f'The AI model is still busy; using the backup model ({provider}) for this job.', 'error')
				agent = Agent(
					task,
					llm=agent_llm(self.settings, provider) if provider else agent_llm(self.settings),
					browser=self.browser,
					tools=tools,
					output_model_schema=schema,
					extend_system_message=build_rules(profile, criteria),
					workspace=sub('workspace'),
				)
				try:
					self._agent_task = asyncio.ensure_future(agent.run(max_steps=max_steps, timeout=timeout))
					try:
						result = await self._agent_task
					except asyncio.CancelledError:
						if self._interrupt:  # take_over() or the stuck watch, not a stop
							raise Interrupted(self._interrupt) from None
						raise
					finally:
						self._agent_task = None
				finally:
					with contextlib.suppress(Exception):
						await agent.close()
				if result.status != 'error' or not BUSY.search(result.error or ''):
					return result
				if wait is not None:
					self._log(f'The AI model is busy; trying again in {wait:g}s.', 'error')
					await asyncio.sleep(wait)
			return result
		finally:
			steps.removeHandler(watch)

	def _stuck(self, step: str) -> None:
		self._log(f'The agent is repeating itself ("{step[:80]}"); parking this job.', 'error')
		self._interrupt = 'stuck'
		if self._agent_task is not None:
			self._agent_task.cancel()

	def _page_state(self, url: str, settle_s: float = 1.5) -> tuple[str, dict]:
		"""Open url in a new tab and read where it ended up (after redirects): (tab, state)."""
		from jobapply.cdp import CDP

		with CDP(self._ws()) as cdp:
			target = cdp.send('Target.createTarget', url=url)['targetId']
			session = cdp.send('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
			state: dict = {}
			deadline = time.monotonic() + 20
			while time.monotonic() < deadline:
				with contextlib.suppress(Exception):
					ready = cdp.send('Runtime.evaluate', session, expression='document.readyState', returnByValue=True)
					if ready['result'].get('value') == 'complete':
						break
				time.sleep(0.2)
			time.sleep(settle_s)  # client-side redirects (to a login page) happen after load
			with contextlib.suppress(Exception):
				value = cdp.send('Runtime.evaluate', session, expression=PAGE_STATE_JS, returnByValue=True)['result']['value']
				state = json.loads(value)
			return target, state

	async def _preflight(self, boards: list[str]) -> set[str]:
		"""Before searching: each board's page (and Gmail) must be logged in and free of bot checks. Ask the user to
		fix what is not; boards still blocked after the wait are left out of this run. Returns those boards."""
		if self.browser is None or not getattr(self.browser, 'name', None):
			return set()
		checks: dict[str, list[str]] = {}  # url -> the boards (or 'gmail') it covers
		for name in [*boards, 'gmail']:
			if name in PREFLIGHT:
				checks.setdefault(PREFLIGHT[name][1], []).append(name)
		blocked: set[str] = set()
		self._log('Checking that you are logged in and not blocked on the boards and Gmail...')
		for url, names in checks.items():
			label = ' / '.join(dict.fromkeys(PREFLIGHT[n][0] for n in names))
			try:
				tab, state = await asyncio.to_thread(self._page_state, url)
				problem = page_problem(state, label)
				if problem:
					done = await self._alert_and_wait(problem, 'preflight', url, tab)
					if done:  # look again: still blocked means the board is skipped
						await asyncio.to_thread(self._close_tabs, [tab])
						tab, state = await asyncio.to_thread(self._page_state, url, 0.5)
						problem = page_problem(state, label)
				await asyncio.to_thread(self._close_tabs, [tab])
			except Exception as e:  # a page that did not load is checked by the search itself
				logger.warning('pre-flight check of %s failed: %s', url, e)
				continue
			if problem:
				searchable = [n for n in names if n != 'gmail']
				blocked.update(searchable)
				if searchable:
					self._log(f'Skipping {label} this run: {problem.split(":")[0]}.', 'error')
				else:
					self._log('Gmail is not ready: email applications will ask you to log in when they need it.', 'error')
		return blocked

	async def _search(self, board: str, llm: Any, round_: int = 1) -> list[dict] | None:
		"""The jobs found on this board's results page round_; None when the search was interrupted."""
		from browser_use import Tools

		label, start_url = BOARDS[board]
		self._log(f'Searching {label} for new jobs (results page {round_})...')
		self.current = {'phase': 'search', 'board': label}
		self._emit('current', **self.current)
		tools = Tools()

		@tools.action('True when the user already has this job in their list (applied, skipped or in progress).')
		def already_seen(url: str) -> bool:
			return self.tracker.seen(url)

		mark: int | None = None  # where this search's tabs start (None: none opened yet)
		task = (
			SEARCH_TASK.replace('{count}', str(SEARCH_BATCH))
			.replace('{board}', label)
			.replace('{board_id}', board)
			.replace('{start_url}', start_url)
			.replace('{board_hint}', BOARD_HINTS.get(board, ''))
			.replace('{round}', str(round_))
			.replace('{age}', str(self.settings.max_job_age_days))
		)
		try:
			await self._start_browser()
			mark = self.guard.mark() if self.guard else 0
			await asyncio.to_thread(self._open_tab, start_url)
			result = await self._run_agent(task, FoundJobs, tools, SEARCH_MAX_STEPS, SEARCH_TIMEOUT_S)
		except Interrupted as e:  # Take over (or stuck) during a search: no jobs from this round; the loop goes on
			if e.reason == 'hold':
				self._log(f'{label}: search paused; it runs again after Continue.', 'error')
				return None  # searched again: the user interrupted it
			self._log(f'{label}: the search got stuck on this page; moving on.', 'error')
			return []  # counts as an empty round, so a page that always sticks is not retried forever
		except (asyncio.CancelledError, KeyboardInterrupt):
			raise
		except Exception as e:
			logger.exception('search failed')
			self._log(f'{label}: the search failed ({e}).', 'error')
			raise SearchFailed(str(e) or type(e).__name__) from e
		finally:
			if self.guard and mark is not None:  # search tabs are not for review
				with contextlib.suppress(Exception):
					await asyncio.to_thread(self._close_tabs, self.guard.tabs_since(mark))
		output = result.structured_output
		if output is None and result.status == 'error':  # the model or the browser failed: not "nothing new"
			error = result.error or 'the search stopped on an error'
			self._log(f'{label}: the search failed ({error[:200]}).', 'error')
			raise SearchFailed(error)
		found = output.jobs if output else []
		if output and output.blocked:
			self._log(f'{label}: {output.blocked}', 'error')
		jobs = [
			{'url': j.url, 'board': j.board or board, 'company': j.company, 'title': j.title, 'days': j.posted_days_ago}
			for j in found
			if j.url
		]
		jobs = freshest_first(jobs, self.settings.max_job_age_days)
		self._log(f'{label}: {len(jobs)} new job(s) found.' if jobs else f'{label}: nothing new.')
		return jobs

	def _job_tools(
		self, url: str, docs: dict[str, str], asked: list[str], writer: Any, skipped: list[str] | tuple[str, ...] = ()
	) -> Any:
		"""prepare_documents and ask_user for one job; docs and asked collect what they did. skipped: questions the
		user chose not to answer, which ask_user does not send again."""
		skip = {norm_question(q) for q in skipped}
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
			with contextlib.suppress(Exception):  # the posting, for History, even when writing fails below
				records.update(url, company=company, title=title, job_description=job_description)
			try:
				tailored = await documents.tailor(writer, profile, job_description, company, title, tone)
				ws = self._ws()
				paths = await asyncio.to_thread(
					documents.write_documents, tailored, company, title, template, lambda h, p: print_pdf(ws, h, p)
				)
			except Exception as e:
				logger.exception('documents failed')
				self._log(f'Could not write the resume for {company}: {e}', 'error')
				raise
			docs.update(paths)
			self._log(f'Resume and cover letter ready: {paths.get("folder", "")}')
			with contextlib.suppress(Exception):
				summary = tailored.job_summary.model_dump() if tailored.job_summary else {}
				records.update(url, summary=summary, documents=dict(docs))
			self.current.update(company=company, title=title)
			self._emit('current', **self.current)
			return docs

		@tools.action(
			'Send the user a question the profile does not answer (they answer later in the app; the answer is saved to '
			'the profile for next time). Leave the field empty and keep going.'
		)
		def ask_user(question: str, field: str = '') -> str:
			if norm_question(question) in skip or any(k and k in norm_question(question) for k in skip):
				return 'The user chose not to answer this. Leave the field empty and do not ask again.'
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

		@tools.action(
			'Ask the user to do something only a person can: a captcha or "verify you are human", a login or sign-up, an '
			'email or SMS code. kind: captcha | login | code | other. The app alerts them and waits; if they do not come '
			'in time, the job is parked for later and you must finish.'
		)
		async def need_human(reason: str, kind: str = 'captcha') -> str:
			return await self._need_human(url, reason, kind, asked)

		return tools

	async def _alert_and_wait(self, reason: str, kind: str, url: str, tab: str) -> bool:
		"""Alert the user (sound, banner, the tab in front) and wait HELP_WAIT_S for "I did it, continue"."""
		self.help = {'reason': reason, 'need': kind, 'url': url, 'tab': tab, 'company': self.current.get('company', '')}
		self._help_done.clear()
		self._log(f'Needs you: {reason}', 'error')
		self._emit('help', sound=True, wait_s=HELP_WAIT_S, **self.help)
		if tab:
			await asyncio.to_thread(self.focus_tab, tab)
		try:
			return await asyncio.to_thread(self._help_done.wait, HELP_WAIT_S)
		finally:
			self.help = None
			self._emit('help_end')

	async def _need_human(self, url: str, reason: str, kind: str, asked: list[str]) -> str:
		"""need_human: alert and wait; then the agent continues, or the job is parked in Questions."""
		tab = getattr(self.browser, '_agent_tab', '') or ''
		done = await self._alert_and_wait(reason, kind, url, tab)
		if done:
			return 'The user says it is done. Check the page now and continue the application.'
		q = self.questions.ask(
			Question(
				question=reason,
				field_hint='help',
				company=self.current.get('company', ''),
				title=self.current.get('title', ''),
				url=url,
			)
		)
		asked.append(q.question)
		self._emit('question', id=q.id, question=q.question)
		self._log('No answer in time: the job is parked in Questions; the loop goes on.', 'error')
		return (
			'The user is away: this job is parked for them. Finish now with status "needs_input" and notes saying what '
			'is blocking. Do nothing else on this page.'
		)

	async def _apply(self, job: dict, llm: Any, writer: Any) -> None:
		url = job['url']
		self.current = {'phase': 'apply', 'url': url, 'company': job.get('company', ''), 'title': job.get('title', '')}
		self._emit('current', **self.current)
		self._job_log = []
		self._log(f'Applying: {job.get("title") or url} {("at " + job["company"]) if job.get("company") else ""}'.strip())
		docs: dict[str, str] = {}
		asked: list[str] = []
		tools = self._job_tools(url, docs, asked, writer)
		started = time.monotonic()
		outcome: JobOutcome | None = None
		error = ''
		mark, tab = 0, ''
		try:
			await self._start_browser()
			mark = self.guard.mark() if self.guard else 0
			tab = await asyncio.to_thread(self._open_tab, url)
			for attempt in (1, 2):
				result = await self._run_agent(
					APPLY_TASK.replace('{url}', url), JobOutcome, tools, APPLY_MAX_STEPS, APPLY_TIMEOUT_S
				)
				outcome = result.structured_output
				if outcome is not None or attempt == 2 or not CRASHED.search(result.error or ''):
					break
				self._log('The browser closed or crashed; starting it again and retrying this job.', 'error')
				await self._start_browser()
				mark = self.guard.mark() if self.guard else 0
				tab = await asyncio.to_thread(self._open_tab, url)
			if outcome is None:
				error = result.error or f'the agent stopped ({result.status}) before it finished'
		except Interrupted as e:
			self._park(job, tab, mark, docs, asked, e.reason, update=False)
			return
		except asyncio.CancelledError:
			error = 'stopped by you'
			self._finish(job, tab, mark, None, docs, asked, error)
			raise
		except Exception as e:
			logger.exception('apply failed')
			error = str(e)
		self._finish(job, tab, mark, outcome, docs, asked, error)
		self._log(f'Done in {time.monotonic() - started:.0f}s.')

	def _park(self, job: dict, tab: str, mark: int, docs: dict, asked: list[str], reason: str, update: bool) -> None:
		"""An interrupted job waits: after a take-over it is finished first on resume(); when stuck, it waits for the
		user in Questions (Done sends the agent back)."""
		if reason == 'hold':
			notes = 'Paused while you took over; Jobify finishes it when you click Continue.'
			self.request_refill(job['url'], first=True)
		else:
			notes = 'The agent got stuck repeating the same step on this page.'
			q = self.questions.ask(
				Question(
					question=f'Jobify got stuck on {job.get("company") or "this application"}. Finish it in its tab and '
					'click Submitted, or click Done to let Jobify try again.',
					field_hint='help',
					company=job.get('company', ''),
					url=job['url'],
				)
			)
			asked.append(q.question)
			self._emit('question', id=q.id, question=q.question)
		self._finish(job, tab, mark, None, docs, asked, notes, update=update, status='needs_input')

	async def _refill(self, url: str, llm: Any, writer: Any) -> None:
		"""Go back to a job that waited on questions: fill what the answers (now in the profile) cover, in its tab."""
		row = self.tracker.latest(url)
		if row is None or row.status not in ('needs_input', 'failed', 'filled', 'email_drafted'):
			return
		job = {'url': url, 'company': row.company, 'title': row.title, 'board': row.board}
		self.current = {'phase': 'apply', 'url': url, 'company': row.company, 'title': row.title}
		self._emit('current', **self.current)
		self._job_log = []
		self._log(f'Finishing with your answers: {row.title or url}{" at " + row.company if row.company else ""}')
		docs: dict[str, str] = {}
		if row.resume and Path(row.resume).exists():  # the documents made for this job, not new ones
			docs = {'resume_pdf': row.resume, 'cover_letter_pdf': row.cover_letter, 'folder': str(Path(row.resume).parent)}
			letter = Path(row.resume).parent / 'cover_letter.txt'
			if letter.exists():
				docs['cover_letter_text'] = letter.read_text(encoding='utf-8').strip()
		asked: list[str] = []
		tools = self._job_tools(url, docs, asked, writer, skipped=skipped_questions(url))
		outcome: JobOutcome | None = None
		error = ''
		mark, tab = 0, ''
		try:
			await self._start_browser()
			mark = self.guard.mark() if self.guard else 0
			# every tab the job used (the posting, the application form on the company site, a Gmail draft)
			open_tabs = [t for t in row.tab_id.split() if self.guard and t in self.guard.pages]
			task = APPLY_TASK.replace('{url}', url)
			if open_tabs and self.guard:  # lock them all again and continue in the last one: the application form
				self.guard.lock(open_tabs)
				tab = open_tabs[-1]
				try:
					await asyncio.to_thread(self._attach_tab, tab)
					task = refill_task(url, row)
				except Exception:  # closed meanwhile: start the application again
					tab = await asyncio.to_thread(self._open_tab, url)
			else:  # the tabs were closed: start the application again
				tab = await asyncio.to_thread(self._open_tab, url)
			result = await self._run_agent(task, JobOutcome, tools, APPLY_MAX_STEPS, APPLY_TIMEOUT_S)
			outcome = result.structured_output
			if outcome is None:
				error = result.error or f'the agent stopped ({result.status}) before it finished'
		except Interrupted as e:
			self._park(job, tab, mark, docs, asked, e.reason, update=True)
			return
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
		status: str | None = None,
	) -> None:
		"""Audit the filled tabs, hand them to the user, and log the job (update: change its existing row). Never
		raises: a job that cannot be logged is reported, and the loop goes on."""
		if outcome is None and error and status is None and error != 'stopped by you' and 'before it finished' not in error:
			self._note_error(error)  # the model or the browser failed, not the page: counted toward a hold
		elif outcome is not None:
			self._errors_in_row = 0
		try:
			self._finish_job(job, tab, mark, outcome, docs, asked, error, update, status)
		except Exception as e:
			logger.exception('finishing the job failed')
			self._log(f'Could not save this job to History: {e}', 'error')
		finally:
			self.current = {}
			self._job_log = []

	def _finish_job(
		self,
		job: dict,
		tab: str,
		mark: int,
		outcome: JobOutcome | None,
		docs: dict,
		asked: list[str],
		error: str,
		update: bool,
		status: str | None,
	) -> None:
		tabs = self.guard.tabs_since(mark) if self.guard else [tab]
		if update:  # the job's earlier tabs that are still open, then the ones this run opened
			before = (self.tracker.latest(job['url']) or Row()).tab_id.split()
			kept = [t for t in before if not self.guard or t in self.guard.pages]
			tabs = [*kept, *(t for t in tabs if t not in kept)]
			if tab and tab not in tabs:
				tabs.insert(0, tab)
		status = status or (outcome.status if outcome else 'failed')
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
		check: dict = {}
		if status != 'skipped' and tabs and self.browser is not None:
			try:  # what the user will find in the form: empty required fields, errors, and a screenshot of it
				shot = sub('screenshots') / f'{datetime.now():%Y-%m-%d_%H%M%S}_{documents.slug(job.get("company") or "job")}.png'
				check = audit.final_check(self._ws(), tabs[-1], shot)
			except Exception as e:
				logger.warning('final check failed: %s', e)
		if status == 'skipped':
			self._close_tabs(tabs)
			tabs = []
		elif self.guard:  # the user reviews and submits in these tabs
			with contextlib.suppress(Exception):
				self.guard.release(tabs)
		unique: dict[str, str] = {}
		for q in asked:  # the questions in Questions, once each however they were punctuated
			unique.setdefault(norm_question(q), q)
		values = {
			'company': (outcome.company if outcome else '') or job.get('company', ''),
			'title': (outcome.title if outcome else '') or job.get('title', ''),
			'board': (outcome.board if outcome else '') or job.get('board', ''),
			'status': status,
			'tab_id': ' '.join(dict.fromkeys(tabs)),  # every tab of the job; the last is where it ended
			'resume': docs.get('resume_pdf', ''),
			'cover_letter': docs.get('cover_letter_pdf', ''),
			'unanswered_questions': ' | '.join(unique.values()),
			'notes': (outcome.notes if outcome else '') or error,
			'check': check.get('summary', ''),
			'screenshot': check.get('screenshot', ''),
		}
		self._save_record(job, outcome, docs, values, check)
		if update:
			try:
				row = self.tracker.update(job['url'], **values)
			except TrackerLocked:
				raise
			except ValueError:  # its row is gone (the file was edited): log it as new
				row = self.tracker.add(Row(url=job['url'], **values))
		else:
			row = self.tracker.add(Row(url=job['url'], **values))
		self._emit('job', row=row.__dict__, today=self.tracker.count_today(), cap=self.settings.daily_cap)

	def _save_record(self, job: dict, outcome: JobOutcome | None, docs: dict, values: dict, check: dict) -> None:
		"""The job's A-to-Z for History: what it was about, what was filled and attached, and what happened."""
		try:
			url = job['url']
			before = records.load(url)
			summary = {} if (before.get('summary') or {}).get('about') else {'about': outcome.about if outcome else ''}
			submitted = {}
			if check.get('page_url') or check.get('fields') or check.get('files'):
				submitted = {
					'fields': check.get('fields', []),
					'files': check.get('files', []),
					'page_url': check.get('page_url', ''),
					'page_title': check.get('page_title', ''),
				}
			records.update(
				url,
				timeline=list(self._job_log),
				company=values['company'],
				title=values['title'],
				board=values['board'],
				status=values['status'],
				notes=values['notes'],
				check=values['check'],
				screenshot=values['screenshot'],
				summary=summary,
				documents={k: v for k, v in docs.items() if isinstance(v, str)},
				submitted=submitted,
				questions=values['unanswered_questions'].split(' | ') if values['unanswered_questions'] else [],
			)
		except Exception as e:
			logger.warning('job record failed: %s', e)

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
