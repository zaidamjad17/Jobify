"""The JobApply loop end to end on a local headless Chrome, with scripted models (no provider calls).

One pasted job: the agent writes the documents, sends a question, tries to press Submit (the guard holds it), and
finishes. The row is logged as needs_input, the PDFs exist, the question is open, and the tab is unlocked for the user."""

import asyncio
import threading
import time

import pytest
from conftest import find_chrome, script
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from jobapply import runner as runner_mod
from jobapply.cdp import CDP
from jobapply.settings import Settings

pytestmark = pytest.mark.chrome

JOB = """<!doctype html><html><head><title>Engineer at Acme</title></head><body>
<h1>Engineer</h1><p>Acme needs a Python engineer.</p>
<form onsubmit="window.submits++; return false"><input id=salary name=salary>
<button type=submit id=submit onclick="window.clicks++">Submit application</button></form>
<script>window.clicks = 0; window.submits = 0;</script></body></html>"""

PROFILE = '# Profile\n- Full name: Ada Lovelace\n## Experience\n### Engineer, Analytical Engines Ltd (2019 - Present)\n'

CLICK = """
x, y = js('(() => { const r = document.getElementById("submit").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2] })()')
click_at_xy(x, y)
import time; time.sleep(0.3)
print('clicks', js('window.clicks'))
"""


def test_loop_fills_one_job_and_hands_the_tab_over(tmp_path, monkeypatch):
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	job = tmp_path / 'job.html'
	job.write_text(JOB, encoding='utf-8')
	(tmp_path / 'profile.md').write_text(PROFILE, encoding='utf-8')
	(tmp_path / 'criteria.md').write_text('- Titles: Engineer\n- Cover letter tone: brief\n', encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'profile.md'),
		criteria_path=str(tmp_path / 'criteria.md'),
		gemini_api_key='unused',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	agent_model, seen = script(
		('prepare_documents', {'company': 'Acme', 'title': 'Engineer', 'job_description': 'Acme needs a Python engineer.'}),
		('ask_user', {'question': 'What is your desired salary?', 'field': 'salary'}),
		('python', {'code': CLICK}),
		(
			'finish',
			{
				'success': True,
				'result': {
					'status': 'filled',
					'company': 'Acme',
					'title': 'Engineer',
					'url': job.as_uri(),
					'notes': 'Salary left empty.',
				},
			},
		),
	)
	tones = []

	def writer(messages, info):
		tones.append(str(messages[0]))
		return ModelResponse(
			parts=[
				ToolCallPart(
					'final_result',
					{
						'resume': {
							'name': 'Ada Lovelace',
							'experience': [{'title': 'Engineer', 'company': 'Analytical Engines Ltd'}],
						},
						'cover_letter': 'Dear Acme,\n\nI would like to join.\n\nAda',
						'email_subject': 'Application: Engineer - Ada Lovelace',
					},
				)
			]
		)

	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: agent_model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: FunctionModel(writer))
	events = []
	r = runner_mod.Runner(settings, events.append)
	r.start()
	try:
		deadline = time.monotonic() + 180
		while time.monotonic() < deadline and not any(e['kind'] == 'state' and e['state'] == 'idle' for e in events):
			time.sleep(0.2)
		assert r.state == 'idle', [e for e in events if e['kind'] == 'log']

		rows = runner_mod.tracker().rows()
		assert len(rows) == 1, [e for e in events if e['kind'] == 'log']
		row = rows[0]
		assert (row.status, row.company, row.unanswered_questions) == ('needs_input', 'Acme', 'What is your desired salary?')
		assert row.resume.endswith('Ada_Lovelace_Resume.pdf') and open(row.resume, 'rb').read(4) == b'%PDF'
		assert open(row.cover_letter, 'rb').read(4) == b'%PDF'
		assert 'brief' in tones[0]  # the criteria's tone wins over the app setting
		assert [q.question for q in runner_mod.questions().open()] == ['What is your desired salary?']
		assert 'clicks 0' in str(seen[3][-1])  # the agent's own click was held
		assert any(e['kind'] == 'blocked' and 'Submit application' in e['text'] for e in events)

		ws = r._ws()
		with CDP(ws) as cdp:  # released: the user's click goes through in that tab
			session = cdp.send('Target.attachToTarget', targetId=row.tab_id, flatten=True)['sessionId']
			ev = lambda e: cdp.send('Runtime.evaluate', session, expression=e, returnByValue=True)['result'].get('value')  # noqa: E731
			box = ev('(r => [r.x + r.width / 2, r.y + r.height / 2])(document.getElementById("submit").getBoundingClientRect())')
			for kind in ('mousePressed', 'mouseReleased'):
				cdp.send('Input.dispatchMouseEvent', session, type=kind, x=box[0], y=box[1], button='left', clickCount=1)
			assert ev('window.clicks') == 1 and ev('window.submits') == 1
		assert r.focus_tab(row.tab_id)
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_loop_stops_at_the_daily_cap(tmp_path, monkeypatch):
	"""A full day never starts a browser."""
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'), criteria_path=str(tmp_path / 'p.md'), gemini_api_key='k', daily_cap=1
	)
	runner_mod.tracker().add(runner_mod.Row(url='https://x.example/1', status='filled'))
	events = []
	r = runner_mod.Runner(settings, events.append)
	monkeypatch.setattr(r, '_start_browser', lambda: asyncio.sleep(0))
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	r.start()
	r._thread.join(30)
	assert any('Daily limit reached (1 jobs)' in e.get('text', '') for e in events)
	assert r.browser is None


def test_a_busy_model_is_retried_instead_of_failing_the_job(tmp_path, monkeypatch):
	"""Live Gemini run: one 503 'high demand' ended the job as failed. The job now waits and runs again."""
	from pydantic_ai.exceptions import ModelHTTPError

	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	monkeypatch.setattr(runner_mod, 'RETRY_WAITS_S', (0.1, 0.1))
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	job = tmp_path / 'job.html'
	job.write_text(JOB, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	calls = []

	def fn(messages, info):
		calls.append(messages)
		if len(calls) <= 2:  # the library retries once itself; both attempts are busy
			raise ModelHTTPError(503, 'gemini-x', {'error': {'code': 503, 'status': 'UNAVAILABLE'}})
		result = {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': job.as_uri()}
		return ModelResponse(parts=[ToolCallPart('finish', {'success': True, 'result': result})])

	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: FunctionModel(fn))
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	events = []
	r = runner_mod.Runner(settings, events.append)
	r.start()
	try:
		r._thread.join(120)
		rows = runner_mod.tracker().rows()
		assert [row.status for row in rows] == ['filled'], [e for e in events if e['kind'] == 'log']
		assert any('busy' in e.get('text', '') for e in events)
	finally:
		r.stop()
		asyncio.run(r.close_browser())


SALARY_JOB = """<!doctype html><html><body><form>
<label for=s>What are your salary expectations (EUR)? *</label><input id=s name=salary>
<label>Are you authorized to work in the EU? <select name=auth><option value="">-</option><option>Yes</option></select></label>
<label>Years of Python <input name=years></label>
<button type=submit>Submit application</button></form></body></html>"""

FILL_SALARY = """js('''(() => { const s = document.querySelector("[name=salary]"); s.value = "80000";
const a = document.querySelector("[name=auth]"); a.value = "Yes";
document.querySelector("[name=years]").value = "5"; return 1 })()''')"""


@pytest.mark.parametrize('profile_salary', ['', '- Desired salary: 70,000 EUR\n'])
def test_answers_the_profile_does_not_give_are_cleared_and_asked(tmp_path, monkeypatch, profile_salary):
	"""Live Gemini run (flash-lite): the profile had no salary and the model typed 80000 anyway, despite the prompt.
	After the run, a salary / notice / relocation / visa / work authorization field the profile does not answer is
	emptied and sent to the user as a question; the job is needs_input. An answer the profile gives stays."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	profile = tmp_path / 'p.md'
	profile.write_text(PROFILE + '- Authorized to work in: EU (citizen)\n' + profile_salary, encoding='utf-8')
	job = tmp_path / 'job.html'
	job.write_text(SALARY_JOB, encoding='utf-8')
	settings = Settings(
		profile_path=str(profile),
		criteria_path=str(profile),
		gemini_api_key='k',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	result = {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': job.as_uri()}
	model, _ = script(('python', {'code': FILL_SALARY}), ('finish', {'success': True, 'result': result}))
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	r = runner_mod.Runner(settings, lambda e: None)
	r.start()
	try:
		r._thread.join(120)
		row = runner_mod.tracker().rows()[0]
		with CDP(r._ws()) as cdp:
			session = cdp.send('Target.attachToTarget', targetId=row.tab_id, flatten=True)['sessionId']
			value = lambda name: cdp.send(
				'Runtime.evaluate', session, expression=f'document.querySelector("[name={name}]").value', returnByValue=True
			)['result']['value']
			fields = {n: value(n) for n in ('salary', 'auth', 'years')}
		if profile_salary:
			assert row.status == 'filled' and fields == {'salary': '80000', 'auth': 'Yes', 'years': '5'}
			assert runner_mod.questions().open() == []
		else:
			assert row.status == 'needs_input' and fields == {'salary': '', 'auth': 'Yes', 'years': '5'}
			assert [q.question for q in runner_mod.questions().open()] == ['What are your salary expectations (EUR)?']
			assert 'salary' in row.unanswered_questions
	finally:
		r.stop()
		asyncio.run(r.close_browser())


CONTACT_JOB = """<!doctype html><html><body><form>
<label>First name <input name=first></label><label>Last name <input name=last></label>
<label>Phone <input name=phone></label><label>Email <input name=email></label>
<label>LinkedIn profile <input name=linkedin></label><label>GitHub <input name=github></label>
<button type=submit>Submit application</button></form></body></html>"""

FILL_CONTACT = """js('''(() => { const set = (n, v) => document.querySelector(`[name=${n}]`).value = v;
set("phone", "+34600123456"); set("email", "SAM.RIVERA@example.com"); set("linkedin", "https://linkedin.com/in/samrivera");
set("github", "github.com/sam-r/"); set("first", "Ada"); set("last", "Lovelace"); return 1 })()''')"""


def test_contact_details_typed_differently_from_the_profile_are_put_back(tmp_path, monkeypatch):
	"""Live Gemini run (flash-lite): the model typed a name it found in an unrelated file, and the profile said +34 600 000 000 and linkedin.com/in/sam-rivera-example; the model
	typed +34600123456 and linkedin.com/in/samrivera. Contact fields that differ from the profile get the profile's value;
	the same value written another way (case, spaces, scheme, trailing slash) is left alone."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	profile = tmp_path / 'p.md'
	profile.write_text(
		PROFILE.replace('Ada Lovelace', 'Sam Rivera') + '- Email: sam.rivera@example.com\n- Phone: +34 600 000 000\n'
		'- LinkedIn: https://linkedin.com/in/sam-rivera-example\n- GitHub: https://github.com/sam-r\n',
		encoding='utf-8',
	)
	job = tmp_path / 'job.html'
	job.write_text(CONTACT_JOB, encoding='utf-8')
	settings = Settings(
		profile_path=str(profile),
		criteria_path=str(profile),
		gemini_api_key='k',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	result = {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': job.as_uri()}
	model, _ = script(('python', {'code': FILL_CONTACT}), ('finish', {'success': True, 'result': result}))
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	events = []
	r = runner_mod.Runner(settings, events.append)
	r.start()
	try:
		r._thread.join(120)
		row = runner_mod.tracker().rows()[0]
		with CDP(r._ws()) as cdp:
			session = cdp.send('Target.attachToTarget', targetId=row.tab_id, flatten=True)['sessionId']
			value = lambda name: cdp.send(
				'Runtime.evaluate', session, expression=f'document.querySelector("[name={name}]").value', returnByValue=True
			)['result']['value']
			fields = {n: value(n) for n in ('first', 'last', 'phone', 'email', 'linkedin', 'github')}
		assert fields == {
			'first': 'Sam',
			'last': 'Rivera',
			'phone': '+34 600 000 000',
			'email': 'SAM.RIVERA@example.com',
			'linkedin': 'https://linkedin.com/in/sam-rivera-example',
			'github': 'github.com/sam-r/',
		}
		assert row.status == 'filled'
		assert sum('Corrected' in e.get('text', '') for e in events) == 4
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_an_empty_search_round_goes_one_page_deeper_instead_of_ending_the_loop(tmp_path, monkeypatch):
	"""User report: Start, one job, then the loop stopped by itself. Every board was searched on its first result page,
	and one round without anything new ended the loop. A board is now searched one page deeper each round and is
	dropped only after MAX_EMPTY_ROUNDS empty rounds in a row."""
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		boards=['linkedin', 'indeed'],
	)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: None)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	searches, applied = [], []
	r = runner_mod.Runner(settings, lambda e: None)

	async def search(board, llm, round_):
		searches.append((board, round_))
		if (board, round_) == ('linkedin', 2):
			return [{'url': 'https://www.linkedin.com/jobs/view/2/', 'board': board}]
		return []

	async def apply(job, llm, writer):
		applied.append(job['url'])
		r.tracker.add(runner_mod.Row(url=job['url'], status='filled'))

	monkeypatch.setattr(r, '_start_browser', lambda: asyncio.sleep(0))
	monkeypatch.setattr(r, '_search', search)
	monkeypatch.setattr(r, '_apply', apply)
	r.start()
	r._thread.join(30)
	assert applied == ['https://www.linkedin.com/jobs/view/2/']
	empty = runner_mod.MAX_EMPTY_ROUNDS
	assert [s for s in searches if s[0] == 'indeed'] == [('indeed', n) for n in range(1, empty + 1)]
	# linkedin: round 2 found the job, so its empty count restarted; then MAX_EMPTY_ROUNDS more empty rounds
	assert [s for s in searches if s[0] == 'linkedin'] == [('linkedin', n) for n in range(1, empty + 3)]


REFILL_CHECK = """
x, y = js('(() => { const r = document.getElementById("submit").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2] })()')
click_at_xy(x, y)
import time; time.sleep(0.3)
js('document.getElementById("salary").value = "70,000 EUR"')
print('clicks', js('window.clicks'), 'url', js('location.href'))
"""


def test_answering_a_jobs_questions_goes_back_and_finishes_that_application(tmp_path, monkeypatch):
	"""User report: of 14 jobs only one was complete; the rest waited on questions, and answering them changed nothing
	in the open applications. Once every question of a job is answered, the loop goes back to that job's tab (locked
	against Submit again), fills the rest, and updates the job's row instead of adding one."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	job = tmp_path / 'job.html'
	job.write_text(JOB, encoding='utf-8')
	profile = tmp_path / 'profile.md'
	profile.write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(profile),
		criteria_path=str(profile),
		gemini_api_key='k',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	done = {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': job.as_uri()}
	model, seen = script(
		('ask_user', {'question': 'What is your desired salary?', 'field': 'salary'}),
		('finish', {'success': True, 'result': done}),
		('python', {'code': REFILL_CHECK}),
		('finish', {'success': True, 'result': {**done, 'notes': 'Salary filled from your answer.'}}),
	)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	r = runner_mod.Runner(settings, lambda e: None)
	r.start()
	try:
		r._thread.join(120)
		first = runner_mod.tracker().rows()
		assert [(row.status, row.unanswered_questions) for row in first] == [('needs_input', 'What is your desired salary?')]
		assert runner_mod.ready_jobs() == []  # the question is still open

		question = runner_mod.questions().open()[0]
		runner_mod.questions().answer(question.id, '70,000 EUR', profile)
		assert runner_mod.ready_jobs() == [job.as_uri()]
		r.request_refill(job.as_uri())
		r.start(refill_only=True)
		r._thread.join(120)

		task = str(seen[2][0])
		assert 'Continue the application' in task and '70,000 EUR' in task  # the answer is in the profile it reads
		assert f'clicks 0 url {job.as_uri()}' in str(seen[3][-1])  # same tab, Submit held again
		rows = runner_mod.tracker().rows()
		assert len(rows) == 1 and (rows[0].status, rows[0].unanswered_questions) == ('filled', '')
		assert rows[0].tab_id == first[0].tab_id and rows[0].notes == 'Salary filled from your answer.'
		assert runner_mod.ready_jobs() == []
		with CDP(r._ws()) as cdp:  # handed back to the user
			session = cdp.send('Target.attachToTarget', targetId=rows[0].tab_id, flatten=True)['sessionId']
			ev = lambda e: cdp.send('Runtime.evaluate', session, expression=e, returnByValue=True)['result'].get('value')  # noqa: E731
			assert ev('document.getElementById("salary").value') == '70,000 EUR'
			box = ev('(r => [r.x + r.width / 2, r.y + r.height / 2])(document.getElementById("submit").getBoundingClientRect())')
			for kind in ('mousePressed', 'mouseReleased'):
				cdp.send('Input.dispatchMouseEvent', session, type=kind, x=box[0], y=box[1], button='left', clickCount=1)
			assert ev('window.clicks') == 1
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_marking_a_job_submitted_keeps_it_out_of_the_loop(tmp_path, monkeypatch):
	"""Done in History: the job counts as submitted and is never searched up or applied to again."""
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	t = runner_mod.tracker()
	t.add(runner_mod.Row(url='https://jobs.lever.co/acme/1', status='needs_input', unanswered_questions='Salary?'))
	t.update('https://jobs.lever.co/acme/1?lever-source=x', status='submitted')
	assert [r.status for r in t.rows()] == ['submitted']
	assert t.seen('https://jobs.lever.co/acme/1') and runner_mod.ready_jobs() == []


FORM = """<!doctype html><html><body><form onsubmit="window.submits++; return false"><input id=salary name=salary>
<button type=submit id=submit onclick="window.clicks++">Submit application</button></form>
<script>window.clicks = 0; window.submits = 0;</script></body></html>"""

OPEN_FORM = "new_tab({form!r}); wait_for_load(); print('form open')"

FORM_CHECK = """
x, y = js('(() => { const r = document.getElementById("submit").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2] })()')
click_at_xy(x, y)
import time; time.sleep(0.3)
print('on', js('location.href'), 'clicks', js('window.clicks'))
"""


def test_finishing_a_job_continues_in_its_application_tab_and_locks_every_tab_of_it(tmp_path, monkeypatch):
	"""Codex review: when the application opens in a second tab (LinkedIn -> the company site), only the posting tab
	was remembered, so going back re-locked the posting and worked there while the form tab stayed unlocked."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	job, form = tmp_path / 'job.html', tmp_path / 'form.html'
	job.write_text(JOB, encoding='utf-8')
	form.write_text(FORM, encoding='utf-8')
	profile = tmp_path / 'profile.md'
	profile.write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(profile),
		criteria_path=str(profile),
		gemini_api_key='k',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)
	done = {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': job.as_uri()}
	model, seen = script(
		('python', {'code': OPEN_FORM.format(form=form.as_uri())}),
		('ask_user', {'question': 'What is your desired salary?', 'field': 'salary'}),
		('finish', {'success': True, 'result': done}),
		('python', {'code': FORM_CHECK}),
		('finish', {'success': True, 'result': done}),
	)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	r = runner_mod.Runner(settings, lambda e: None)
	r.start()
	try:
		r._thread.join(120)
		tabs = runner_mod.tracker().rows()[0].tab_id.split()
		assert len(tabs) == 2  # the posting and the form
		question = runner_mod.questions().open()[0]
		runner_mod.questions().answer(question.id, '70,000 EUR', profile)
		r.request_refill(job.as_uri())
		r.start(refill_only=True)
		r._thread.join(120)
		assert f'on {form.as_uri()} clicks 0' in str(seen[4][-1])  # continued in the form tab, Submit held there
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_a_skipped_question_is_not_asked_again_and_skips_alone_do_not_start_a_refill(tmp_path, monkeypatch):
	"""Codex review: Skip closed a question without an answer, yet started a refill whose agent asked it again."""
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'home').mkdir()
	profile = tmp_path / 'p.md'
	profile.write_text(PROFILE, encoding='utf-8')
	t, qs = runner_mod.tracker(), runner_mod.questions()
	url = 'https://jobs.lever.co/a/1'
	t.add(runner_mod.Row(url=url, status='needs_input', unanswered_questions='Polish level? | Notice period?'))
	polish = qs.ask(runner_mod.Question('Polish level?', url=url))
	notice = qs.ask(runner_mod.Question('Notice period?', url=url))
	qs.dismiss(polish.id)
	assert runner_mod.ready_jobs() == []  # still waiting on the notice period
	qs.dismiss(notice.id)
	assert runner_mod.ready_jobs() == []  # only skips: nothing new to fill
	login = qs.ask(runner_mod.Question('Please log in to Acme', field_hint='login', url=url))
	t.update(url, unanswered_questions='Polish level? | Notice period? | Please log in to Acme')
	qs.dismiss(login.id)  # Done on a login request: the user did something, so go back
	assert runner_mod.ready_jobs() == [url]

	settings = Settings(profile_path=str(profile), criteria_path=str(profile), gemini_api_key='k')
	r = runner_mod.Runner(settings, lambda e: None)
	asked: list[str] = []
	tools = r._job_tools(url, {}, asked, None, skipped=runner_mod.skipped_questions(url))
	ask = next(tool for tool in tools.functions if tool.name == 'ask_user').function
	assert 'chose not to answer' in ask('What is your Polish level?')
	assert asked == [] and [q.question for q in qs.open()] == []
	assert 'Polish level?' in runner_mod.refill_task(url, t.latest(url))


CAPTCHA_JOB = """<!doctype html><html><body><h1>Verify you are human</h1>
<form><input id=salary><button type=submit id=submit onclick="window.clicks++">Submit application</button></form>
<script>window.clicks = 0;</script></body></html>"""


def _runner_with(tmp_path, monkeypatch, pages: dict[str, str], *turns, **settings_extra):
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	urls = []
	for name, html in pages.items():
		path = tmp_path / name
		path.write_text(html, encoding='utf-8')
		urls.append(path.as_uri())
	profile = tmp_path / 'profile.md'
	profile.write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(profile),
		criteria_path=str(profile),
		gemini_api_key='k',
		boards=[],
		job_urls=urls,
		browser_executable=chrome,
		**settings_extra,
	)
	model, seen = script(*turns)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda llm: None)
	events: list[dict] = []
	return runner_mod.Runner(settings, events.append), urls, seen, events, profile


def _filled(url):
	return ('finish', {'success': True, 'result': {'status': 'filled', 'company': 'Acme', 'title': 'Engineer', 'url': url}})


def test_a_captcha_the_user_misses_parks_the_job_and_the_loop_goes_on(tmp_path, monkeypatch):
	"""Human handoff: the agent calls need_human (captcha); the app plays a sound and brings the tab up. Nobody answers
	within the wait, so the job is parked in Questions (Done brings the agent back) and the next job runs."""
	monkeypatch.setattr(runner_mod, 'HELP_WAIT_S', 0.5)
	r, urls, seen, events, profile = _runner_with(
		tmp_path,
		monkeypatch,
		{'captcha.html': CAPTCHA_JOB, 'job.html': JOB},
		('need_human', {'reason': 'Captcha on Acme: please solve it', 'kind': 'captcha'}),
		('finish', {'success': True, 'result': {'status': 'needs_input', 'company': 'Acme', 'url': 'x', 'notes': 'captcha'}}),
		('finish', {'success': True, 'result': {'status': 'filled', 'company': 'Beta', 'title': 'Dev', 'url': 'y'}}),
	)
	try:
		r.start()
		r._thread.join(120)
		assert 'parked' in str(seen[1][-1]).lower()
		help_events = [e for e in events if e['kind'] == 'help']
		assert help_events and help_events[0]['reason'] == 'Captcha on Acme: please solve it' and help_events[0]['sound']
		rows = runner_mod.tracker().rows()
		assert [(row.status, row.unanswered_questions) for row in rows] == [
			('needs_input', 'Captcha on Acme: please solve it'),
			('filled', ''),
		]
		parked = runner_mod.questions().open()
		assert [(q.question, q.field_hint) for q in parked] == [('Captcha on Acme: please solve it', 'help')]
		runner_mod.questions().dismiss(parked[0].id)  # Done: the user solved it
		assert runner_mod.ready_jobs() == [urls[0]]
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_the_user_answering_the_handoff_in_time_continues_the_same_job(tmp_path, monkeypatch):
	monkeypatch.setattr(runner_mod, 'HELP_WAIT_S', 30)
	r, urls, seen, events, profile = _runner_with(
		tmp_path,
		monkeypatch,
		{'captcha.html': CAPTCHA_JOB},
		('need_human', {'reason': 'Captcha on Acme', 'kind': 'captcha'}),
		_filled('x'),
	)
	r.on_event = lambda e: (events.append(e), e['kind'] == 'help' and threading.Timer(0.3, r.help_done).start())
	try:
		r.start()
		r._thread.join(120)
		assert 'user says it is done' in str(seen[1][-1]).lower()
		assert [row.status for row in runner_mod.tracker().rows()] == ['filled']
		assert runner_mod.questions().open() == []
	finally:
		r.stop()
		asyncio.run(r.close_browser())


SLOW = "import time; time.sleep(2); print('working')"


def test_take_over_pauses_the_agent_and_continue_finishes_that_job_first(tmp_path, monkeypatch):
	"""Take over: the agent stops after its current step so the user has the browser; the job waits. Continue: that job
	is finished first, then the loop goes on."""
	r, urls, seen, events, profile = _runner_with(
		tmp_path,
		monkeypatch,
		{'job.html': JOB, 'next.html': JOB},
		('python', {'code': SLOW}),
		_filled('x'),  # after Continue: the interrupted job, finished
		_filled('y'),  # then the next job
	)
	try:
		r.start()
		for _ in range(200):
			if r.current.get('phase') == 'apply' and r._agent_task is not None:
				break
			time.sleep(0.05)
		time.sleep(0.5)
		assert r.take_over()
		for _ in range(200):
			if r.state == 'held':
				break
			time.sleep(0.05)
		assert r.state == 'held'
		time.sleep(0.5)
		rows = runner_mod.tracker().rows()
		assert [row.status for row in rows] == ['needs_input'] and 'took over' in rows[0].notes
		r.resume()
		r._thread.join(120)
		rows = runner_mod.tracker().rows()
		assert [(row.url, row.status) for row in rows] == [(urls[0], 'filled'), (urls[1], 'filled')]
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_an_agent_repeating_itself_is_stopped_and_the_job_parked(tmp_path, monkeypatch):
	"""Stuck detector: the same step over and over (a button that does nothing) used to burn up to 90 steps and the
	AI cost. After a few identical steps the job is parked with the reason, and the loop moves on."""
	monkeypatch.setattr(runner_mod, 'STUCK_REPEATS', 3)
	same = ('python', {'code': "print(js('1'))"})
	r, urls, seen, events, profile = _runner_with(
		tmp_path,
		monkeypatch,
		{'job.html': JOB, 'next.html': JOB},
		same,
		same,
		same,
		same,
		same,
		_filled('y'),
	)
	try:
		r.start()
		r._thread.join(120)
		rows = runner_mod.tracker().rows()
		assert [row.status for row in rows] == ['needs_input', 'filled']
		assert 'stuck' in rows[0].notes.lower()
		assert [q.field_hint for q in runner_mod.questions().open()] == ['help']
	finally:
		r.stop()
		asyncio.run(r.close_browser())


CHECK_FORM = """<!doctype html><html><body><form>
<label>Full name <input name=name value="Ada Lovelace" required></label>
<label for=p>Phone *</label><input id=p name=phone>
<label>Portfolio <input name=site></label>
<div role=alert>Please enter a valid phone number</div>
<button type=submit id=submit>Submit application</button></form></body></html>"""


def test_a_finished_job_lists_empty_required_fields_and_errors_and_keeps_a_screenshot(tmp_path, monkeypatch):
	"""Final check: History says whether the form is really ready (required fields still empty, error messages on the
	page) and keeps a screenshot of the form as JobApply left it."""
	r, urls, seen, events, profile = _runner_with(tmp_path, monkeypatch, {'form.html': CHECK_FORM}, _filled('x'))
	try:
		r.start()
		r._thread.join(120)
		row = runner_mod.tracker().rows()[0]
		assert 'Phone' in row.check and 'Portfolio' not in row.check and 'Full name' not in row.check
		assert 'Please enter a valid phone number' in row.check
		assert row.screenshot.endswith('.png') and open(row.screenshot, 'rb').read(4) == b'\x89PNG'
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_a_busy_model_falls_back_to_the_backup_model(tmp_path, monkeypatch):
	"""Live run: Google answered 503 for minutes, so every job failed. With a backup model set, a job whose model is
	still busy after the retries runs on the backup instead."""
	from pydantic_ai.exceptions import ModelHTTPError

	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	monkeypatch.setattr(runner_mod, 'RETRY_WAITS_S', (0.1,))
	job = tmp_path / 'job.html'
	job.write_text(JOB, encoding='utf-8')
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		openai_api_key='o',
		backup_provider='openai',
		boards=[],
		job_urls=[job.as_uri()],
		browser_executable=chrome,
	)

	def busy(messages, info):
		raise ModelHTTPError(503, 'gemini-x', {'error': {'code': 503, 'status': 'UNAVAILABLE'}})

	backup, _ = script(_filled(job.as_uri()))
	used = []

	def llm(s, provider=None):
		used.append(provider or s.provider)
		return backup if provider == 'openai' else FunctionModel(busy)

	monkeypatch.setattr(runner_mod, 'agent_llm', llm)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda m: None)
	events = []
	r = runner_mod.Runner(settings, events.append)
	try:
		r.start()
		r._thread.join(120)
		assert [row.status for row in runner_mod.tracker().rows()] == ['filled']
		assert 'openai' in used and any('backup' in e.get('text', '') for e in events)
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_a_browser_crash_mid_job_restarts_the_browser_and_runs_the_job_again(tmp_path, monkeypatch):
	"""A crashed or closed browser failed the job. The app starts the browser again and retries that job once."""
	from types import SimpleNamespace

	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		boards=[],
		job_urls=['https://jobs.lever.co/acme/1'],
	)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s, provider=None: None)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda m: None)
	r = runner_mod.Runner(settings, lambda e: None)
	starts, runs = [], []

	async def start_browser():
		starts.append(1)

	async def run_agent(task, schema, tools, max_steps, timeout):
		runs.append(task)
		if len(runs) == 1:
			return SimpleNamespace(status='error', error='The browser is gone: chrome.exe exited', structured_output=None)
		return SimpleNamespace(
			status='completed', error=None, structured_output=runner_mod.JobOutcome(status='filled', company='Acme')
		)

	monkeypatch.setattr(r, '_start_browser', start_browser)
	monkeypatch.setattr(r, '_run_agent', run_agent)
	monkeypatch.setattr(r, '_open_tab', lambda url: 'T1')
	monkeypatch.setattr(r, '_finish_checks', lambda *a, **k: {}, raising=False)
	r.start()
	r._thread.join(30)
	assert len(runs) == 2 and len(starts) >= 3  # loop start, the job, the restart
	assert [row.status for row in runner_mod.tracker().rows()] == ['filled']


def test_preflight_asks_for_help_on_a_logged_out_board_and_skips_it_when_nobody_comes(tmp_path, monkeypatch):
	"""Pre-flight: before searching, each board (and Gmail) is opened; a login page or a bot check alerts the user. A
	board still blocked after the wait is left out of this run instead of being searched blind ('nothing new')."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	monkeypatch.setattr(runner_mod, 'HELP_WAIT_S', 0.5)
	(tmp_path / 'feed.html').write_text('<h1>Your feed</h1>', encoding='utf-8')
	(tmp_path / 'login.html').write_text('<h1>Sign in</h1><input type=password>', encoding='utf-8')
	(tmp_path / 'check.html').write_text('<h1>Verify you are human</h1>', encoding='utf-8')
	monkeypatch.setattr(
		runner_mod,
		'PREFLIGHT',
		{
			'linkedin': ('LinkedIn', (tmp_path / 'login.html').as_uri()),
			'indeed': ('Indeed', (tmp_path / 'check.html').as_uri()),
			'lever': ('Lever', (tmp_path / 'feed.html').as_uri()),
			'gmail': ('Gmail', (tmp_path / 'feed.html').as_uri()),
		},
	)
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		boards=['linkedin', 'indeed', 'lever'],
		browser_executable=chrome,
	)
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s, provider=None: None)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda m: None)
	events: list[dict] = []
	r = runner_mod.Runner(settings, events.append)
	searched = []

	async def search(board, llm, round_):
		searched.append(board)
		return []

	monkeypatch.setattr(r, '_search', search)
	try:
		r.start()
		r._thread.join(60)
		reasons = [e['reason'] for e in events if e['kind'] == 'help']
		assert any('LinkedIn' in x and 'log in' in x.lower() for x in reasons)
		assert any('Indeed' in x and 'check' in x.lower() for x in reasons)
		assert set(searched) == {'lever'}  # the blocked boards were left out
		assert runner_mod.questions().open() == []  # pre-flight problems are not job questions
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_the_loop_holds_when_too_many_filled_tabs_wait_for_review(tmp_path, monkeypatch):
	"""Review tab limit: with max_review_tabs filled tabs open and not yet submitted, the loop stops and asks the user to
	review; Continue after marking one submitted lets it go on. The day ends with a summary."""
	r, urls, seen, events, profile = _runner_with(
		tmp_path,
		monkeypatch,
		{'job.html': JOB, 'next.html': JOB},
		_filled('x'),
		_filled('y'),
		max_review_tabs=1,
	)
	try:
		r.start()
		for _ in range(300):
			if r.state == 'held':
				break
			time.sleep(0.05)
		assert r.state == 'held'
		assert any(e['kind'] == 'help' and 'review' in e['reason'].lower() for e in events)
		assert [row.status for row in runner_mod.tracker().rows()] == ['filled']
		runner_mod.tracker().update(urls[0], status='submitted')
		r.resume()
		r._thread.join(120)
		assert [row.status for row in runner_mod.tracker().rows()] == ['submitted', 'filled']
		summary = [e['text'] for e in events if e['kind'] == 'log' and e['text'].startswith('Today:')]
		assert summary and '1 ready to submit' in summary[-1] and '1 submitted' in summary[-1]
	finally:
		r.stop()
		asyncio.run(r.close_browser())


def test_take_over_during_a_search_holds_the_loop_instead_of_ending_it(tmp_path, monkeypatch):
	"""Codex review: Take over while a board was being searched cancelled the search, and the Interrupted error ended the
	whole loop. The loop now holds; Continue searches that board again."""
	chrome = find_chrome() or pytest.skip('Chrome not installed')
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	(tmp_path / 'ok.html').write_text('<h1>Jobs</h1>', encoding='utf-8')
	ok = (tmp_path / 'ok.html').as_uri()
	monkeypatch.setattr(runner_mod, 'PREFLIGHT', {'lever': ('Lever', ok), 'gmail': ('Gmail', ok)})
	monkeypatch.setattr(runner_mod, 'BOARDS', {'lever': ('Lever', ok)})
	monkeypatch.setattr(runner_mod, 'MAX_EMPTY_ROUNDS', 1)
	(tmp_path / 'p.md').write_text(PROFILE, encoding='utf-8')
	settings = Settings(
		profile_path=str(tmp_path / 'p.md'),
		criteria_path=str(tmp_path / 'p.md'),
		gemini_api_key='k',
		boards=['lever'],
		browser_executable=chrome,
	)
	model, seen = script(('python', {'code': SLOW}), ('finish', {'success': True, 'result': {'jobs': []}}))
	monkeypatch.setattr(runner_mod, 'agent_llm', lambda s, provider=None: model)
	monkeypatch.setattr(runner_mod, 'writer_model', lambda m: None)
	events: list[dict] = []
	r = runner_mod.Runner(settings, events.append)
	try:
		r.start()
		for _ in range(300):
			if r.current.get('phase') == 'search' and r._agent_task is not None:
				break
			time.sleep(0.05)
		time.sleep(0.5)
		assert r.take_over()
		time.sleep(1.5)
		assert r.state == 'held' and r._thread.is_alive()
		r.resume()
		r._thread.join(60)
		logs = [e['text'] for e in events if e['kind'] == 'log']
		assert any('Loop finished' in x or 'No more new jobs' in x for x in logs)
		assert not any(x.startswith('Stopped:') for x in logs)
	finally:
		r.stop()
		asyncio.run(r.close_browser())
