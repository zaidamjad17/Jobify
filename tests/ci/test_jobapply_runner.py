"""The JobApply loop end to end on a local headless Chrome, with scripted models (no provider calls).

One pasted job: the agent writes the documents, sends a question, tries to press Submit (the guard holds it), and
finishes. The row is logged as needs_input, the PDFs exist, the question is open, and the tab is unlocked for the user."""

import asyncio
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
