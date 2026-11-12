"""The Jobify loop keeps going: errors in one job or search, a CSV held open by Excel, a provider that refuses the key.
These run the loop with the browser and the agent stubbed (no Chrome, no model)."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from jobapply import runner as runner_mod
from jobapply import tracker as tracker_mod
from jobapply.prompts import FoundJobs, JobOutcome
from jobapply.settings import Settings

PROFILE = '# Profile\n- Full name: Ada Lovelace\n'


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s, provider=None: object())
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	return tmp_path


def _settings(home, **kw) -> Settings:
	values = {'profile_path': str(home / 'p.md'), 'criteria_path': str(home / 'p.md'), 'gemini_api_key': 'k', 'boards': []}
	return Settings(**{**values, **kw})


def _stub_browser(r, monkeypatch, open_tab=None):
	async def start():
		return None

	monkeypatch.setattr(r, '_start_browser', start)
	monkeypatch.setattr(r, '_open_tab', open_tab or (lambda url: 'T-' + url[-1]))
	monkeypatch.setattr(r, '_preflight', lambda boards: asyncio.sleep(0, result=set()))


def _result(output=None, status='completed', error=None):
	return SimpleNamespace(structured_output=output, status=status, error=error)


def _run(r, timeout=30):
	r.start()
	r._thread.join(timeout)
	assert not r._thread.is_alive(), 'the loop did not end'


def test_a_job_that_raises_does_not_end_the_run(home, monkeypatch):
	"""User report: "it sometimes stops by itself". An exception outside the agent (the tab would not open, the browser
	daemon answered with an error) ended the whole loop. Now that job is logged as failed and the next one runs."""
	r = runner_mod.Runner(_settings(home, job_urls=['https://x.example/1', 'https://x.example/2']), lambda e: None)

	def open_tab(url):
		if url.endswith('1'):
			raise RuntimeError('Target.createTarget: no answer within 30s')
		return 'T2'

	_stub_browser(r, monkeypatch, open_tab)
	outcome = JobOutcome(status='filled', company='Acme', title='Dev', about='A backend role at Acme.')
	monkeypatch.setattr(r, '_run_agent', lambda *a, **k: asyncio.sleep(0, result=_result(outcome)))
	_run(r)
	rows = {row.url: row.status for row in runner_mod.tracker().rows()}
	assert rows == {'https://x.example/1': 'failed', 'https://x.example/2': 'filled'}
	assert r.last_stop and r.last_stop['kind'] == 'done'
	from jobapply import records

	assert records.load('https://x.example/2')['summary']['about'] == 'A backend role at Acme.'


def test_a_failing_search_is_not_nothing_new_and_says_why(home, monkeypatch):
	"""A search that ended on a model error counted as "nothing new": three of them and the run ended saying there were
	no more jobs. Now it is a failure, the board is left out after three, and the run ends naming the error."""
	r = runner_mod.Runner(_settings(home, boards=['lever'], keep_searching=False), lambda e: None)
	_stub_browser(r, monkeypatch)
	calls = []

	async def run_agent(task, schema, *a, **k):
		calls.append(schema)
		return _result(None, 'error', 'ModelHTTPError: status_code: 500, Internal error')

	monkeypatch.setattr(r, '_run_agent', run_agent)
	_run(r)
	assert calls == [FoundJobs] * runner_mod.MAX_SEARCH_FAILURES
	assert r.last_stop['kind'] == 'error' and 'Internal error' in r.last_stop['text']
	assert r.help is None  # it ended with the reason instead of holding with nothing left to do


def test_jobs_failing_on_errors_hold_the_loop_and_continue_goes_on(home, monkeypatch):
	"""Jobs failing one after another on the same error (no internet, the provider down) burned every job of the day.
	After MAX_ERRORS_IN_ROW the loop holds with a banner; Continue goes on with the next job."""
	links = [f'https://x.example/{i}' for i in range(1, 6)]
	events: list[dict] = []
	r = runner_mod.Runner(_settings(home, job_urls=links), events.append)
	_stub_browser(r, monkeypatch)
	monkeypatch.setattr(runner_mod.asyncio, 'sleep', _fast_sleep)

	async def run_agent(*a, **k):
		if len(runner_mod.tracker().rows()) < runner_mod.MAX_ERRORS_IN_ROW:
			return _result(None, 'error', 'ModelConnectionError: Connection refused')
		return _result(JobOutcome(status='filled'))

	monkeypatch.setattr(r, '_run_agent', run_agent)
	r.start()
	deadline = time.monotonic() + 20
	while time.monotonic() < deadline and r.state != 'held':
		time.sleep(0.05)
	assert r.state == 'held' and r.help and 'Connection refused' in r.help['reason']
	assert len(runner_mod.tracker().rows()) == runner_mod.MAX_ERRORS_IN_ROW  # nothing burned while it waits
	r.resume()
	r._thread.join(20)
	assert [row.status for row in runner_mod.tracker().rows()] == ['failed'] * 3 + ['filled'] * 2


def test_a_refused_api_key_holds_at_once(home, monkeypatch):
	"""An invalid key fails every job the same way: the first one holds the loop and says to fix Setup."""
	r = runner_mod.Runner(_settings(home, job_urls=['https://x.example/1', 'https://x.example/2']), lambda e: None)
	_stub_browser(r, monkeypatch)
	error = "ModelHTTPError: status_code: 400, {'message': 'API key not valid. Please pass a valid API key.'}"
	monkeypatch.setattr(r, '_run_agent', lambda *a, **k: asyncio.sleep(0, result=_result(None, 'error', error)))
	r.start()
	deadline = time.monotonic() + 20
	while time.monotonic() < deadline and r.state != 'held':
		time.sleep(0.05)
	try:
		assert r.state == 'held' and 'Setup > AI model' in r.help['reason']
		assert len(runner_mod.tracker().rows()) == 1
	finally:
		r.stop()
		r._thread.join(20)
	assert r.last_stop['kind'] == 'stopped'


def test_with_nothing_new_it_rests_and_a_new_link_wakes_it(home, monkeypatch):
	"""User report: the loop stopped by itself once the boards had nothing new. With keep searching on it rests and
	searches again later; a link added meanwhile is applied to at once."""
	r = runner_mod.Runner(_settings(home, boards=['lever'], search_again_minutes=30), lambda e: None)
	_stub_browser(r, monkeypatch)
	monkeypatch.setattr(runner_mod, 'MAX_EMPTY_ROUNDS', 1)
	applied = []

	async def run_agent(task, schema, *a, **k):
		if schema is FoundJobs:
			return _result(FoundJobs(jobs=[]))
		applied.append(task)
		return _result(JobOutcome(status='filled'))

	monkeypatch.setattr(r, '_run_agent', run_agent)
	r.start()
	try:
		deadline = time.monotonic() + 20
		while time.monotonic() < deadline and r.current.get('phase') != 'rest':
			time.sleep(0.05)
		assert r.current.get('phase') == 'rest' and r.state == 'running'
		r.add_links(['https://x.example/new'])
		deadline = time.monotonic() + 20
		while time.monotonic() < deadline and not applied:
			time.sleep(0.05)
		assert applied and 'https://x.example/new' in applied[0]
	finally:
		r.stop()
		r._thread.join(20)
	assert not r._thread.is_alive()


def test_the_history_file_held_open_by_excel_waits_then_says_so(tmp_path, monkeypatch):
	"""Windows: Excel locks applications.csv while it is open; writing the next job raised PermissionError and ended the
	loop. Writes now wait for the file, then fail with a clear message; Open CSV opens a copy so this does not happen."""
	monkeypatch.setattr(tracker_mod, 'LOCK_WAIT_S', 0.6)
	t = tracker_mod.Tracker(tmp_path / 'applications.csv')
	t.add(tracker_mod.Row(url='https://x.example/1', status='filled'))
	real_open = type(t.path).open
	locked = {'n': 3}

	def flaky_open(self, mode='r', *a, **k):
		if 'a' in mode and locked['n']:
			locked['n'] -= 1
			raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
		return real_open(self, mode, *a, **k)

	monkeypatch.setattr(type(t.path), 'open', flaky_open)
	t.add(tracker_mod.Row(url='https://x.example/2', status='filled'))  # waited for the lock
	assert len(t.rows()) == 2
	locked['n'] = 1000
	with pytest.raises(tracker_mod.TrackerLocked, match='Excel'):
		t.add(tracker_mod.Row(url='https://x.example/3', status='filled'))


def test_open_csv_opens_a_copy_not_the_file_jobify_writes(tmp_path, monkeypatch):
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	from jobapply import app

	opened = []
	monkeypatch.setattr(app, 'open_path', opened.append)
	runner_mod.tracker().add(runner_mod.Row(url='https://x.example/1', status='filled'))
	assert app.Api().open_csv() == {'ok': True}
	assert opened and 'exports' in str(opened[0]) and str(opened[0]).endswith('.csv')
	assert open(opened[0], encoding='utf-8-sig').read().startswith('date,time,company')


def test_setup_status_lists_what_start_still_needs(tmp_path, monkeypatch):
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	from jobapply.app import Api

	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	status = Api._setup_status(Settings(profile_path=str(tmp_path / 'p.md')))
	assert status == {'profile': True, 'criteria': False, 'model': False, 'model_error': 'Add your Gemini API key.'}


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds, result=None):
	return await _real_sleep(0, result)


def test_setup_tests_the_ai_model_before_a_run_depends_on_it(tmp_path, monkeypatch):
	"""A wrong key or model showed up only after Start, as failed jobs. Setup's Test button sends one tiny request."""
	from pydantic_ai.messages import ModelResponse, TextPart
	from pydantic_ai.models.function import FunctionModel

	from jobapply import llm
	from jobapply.app import Api

	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	monkeypatch.setattr(llm, 'agent_llm', lambda s, provider=None: object())
	monkeypatch.setattr(llm, 'writer_model', lambda m: FunctionModel(lambda msgs, info: ModelResponse(parts=[TextPart('OK')])))
	result = Api().test_model()
	assert result['ok'] is True and result['reply'] == 'OK'

	def refuse(msgs, info):
		raise RuntimeError('API key not valid')

	monkeypatch.setattr(llm, 'writer_model', lambda m: FunctionModel(refuse))
	assert 'API key not valid' in Api().test_model()['error']
