"""The JobApply window's page (jobapply/ui/index.html) in headless Chrome, with a stub of the Python API."""

import json
import time

import pytest

from jobapply.cdp import CDP
from jobapply.paths import resource

pytestmark = pytest.mark.chrome

STATE = {
	'version': '0.2.0',
	'settings': {
		'profile_path': '',
		'criteria_path': '',
		'resume_template_path': '',
		'browser_executable': '',
		'cover_letter_tone': '',
		'provider': 'gemini',
		'gemini_model': 'm',
		'gemini_api_key': True,
		'openai_model': 'm',
		'openai_api_key': False,
		'chatgpt': {'model': 'm'},
		'boards': [],
		'daily_cap': 20,
		'job_urls': [],
	},
	'boards': {},
	'chatgpt': {'signed_in': False},
	'runner': 'running',
	'current': {},
	'today': 1,
	'cap': 20,
	'rows': [
		{
			'date': 'd',
			'time': 't',
			'company': 'Kodifly',
			'title': 'Graduate',
			'url': 'https://jobs.ashbyhq.com/k/1',
			'board': 'ashby',
			'status': 'needs_input',
			'tab_id': 'T1',
			'resume': '',
			'cover_letter': '',
			'unanswered_questions': 'Why us?',
			'notes': '',
		},
		{
			'date': 'd',
			'time': 't',
			'company': 'Duna',
			'title': 'Backend',
			'url': 'https://jobs.ashbyhq.com/d/2',
			'board': 'ashby',
			'status': 'skipped',
			'tab_id': '',
			'resume': '',
			'cover_letter': '',
			'unanswered_questions': '',
			'notes': '5+ years',
		},
	],
	'home': 'h',
	'questions': [
		{
			'id': 'q1',
			'question': 'What are your salary expectations?',
			'field_hint': 'salary',
			'company': 'Acme',
			'title': 'Dev',
			'jobs': 1,
			'asked_at': 't',
		},
		{
			'id': 'q2',
			'question': 'Please log in to Teradata in the JobApply browser',
			'field_hint': 'login',
			'company': 'Teradata',
			'title': 'Dev',
			'jobs': 1,
			'asked_at': 't',
		},
	],
}

STUB = """
window.__calls = [];
window.__state = %s;
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
	window.__calls.push([name, ...args]);
	if (name === 'state') return JSON.parse(JSON.stringify(window.__state));
	if (name === 'events') return [];
	if (name === 'dismiss') { window.__state.questions = window.__state.questions.filter(q => q.id !== args[0]); return { ok: true }; }
	return { ok: true };
} }) };
window.addEventListener('load', () => window.dispatchEvent(new Event('pywebviewready')));
"""


def _page(chrome):
	cdp = CDP(chrome['ws'])
	target = cdp.send('Target.createTarget', url='about:blank')['targetId']
	session = cdp.send('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
	cdp.send('Page.enable', session)
	cdp.send('Page.addScriptToEvaluateOnNewDocument', session, source=STUB % json.dumps(STATE))
	cdp.send('Page.navigate', session, url=resource('ui/index.html').resolve().as_uri())

	def ev(expression):
		reply = cdp.send('Runtime.evaluate', session, expression=expression, returnByValue=True, awaitPromise=True)
		return reply['result'].get('value')

	for _ in range(100):
		try:
			if ev('!!document.getElementById("a_q1")'):
				break
		except Exception:
			pass
		time.sleep(0.05)
	return cdp, session, target, ev


def test_an_answer_being_typed_survives_the_screen_refreshing_while_the_loop_runs(chrome):
	"""User report: in Questions nothing could be typed while the loop ran. The page refreshes every second while
	running and rebuilt the question list each time, replacing the input being typed into."""
	cdp, session, target, ev = _page(chrome)
	try:
		ev('document.querySelector("nav button[data-tab=questions]").click(); document.getElementById("a_q1").focus()')
		cdp.send('Input.insertText', session, text='70,000 EUR')
		time.sleep(2.5)  # two refreshes
		assert ev('document.getElementById("a_q1").value') == '70,000 EUR'
		assert ev('document.activeElement && document.activeElement.id') == 'a_q1'
		ev('document.querySelector("[data-answer=q1]").click()')
		time.sleep(0.2)
		assert ['answer', 'q1', '70,000 EUR'] in ev('window.__calls')
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_a_login_request_is_done_with_one_click_not_an_answer(chrome):
	"""A 'please log in to <site>' question has nothing to type: it shows Done, which removes it without writing to the
	profile."""
	cdp, session, target, ev = _page(chrome)
	try:
		assert ev('!!document.getElementById("a_q2")') is False
		ev('document.querySelector("[data-done=q2]").click()')
		time.sleep(0.3)
		assert ['dismiss', 'q2'] in ev('window.__calls')
		assert not any(c[0] == 'answer' for c in ev('window.__calls'))
		assert ev('!!document.querySelector("[data-done=q2]")') is False
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_history_marks_a_job_submitted_and_sends_it_back_to_be_filled(chrome):
	"""User request: a Done button so a job is not applied to again, and a way to have an application filled again."""
	cdp, session, target, ev = _page(chrome)
	try:
		ev('document.querySelector("nav button[data-tab=history]").click()')
		assert ev('document.querySelectorAll("[data-submitted]").length') == 1  # not on a skipped job
		ev('document.querySelector("[data-refill]").click()')
		ev('document.querySelector("[data-submitted]").click()')
		time.sleep(0.3)
		calls = ev('window.__calls')
		assert ['refill', 'https://jobs.ashbyhq.com/k/1'] in calls and ['mark_submitted', 'https://jobs.ashbyhq.com/k/1'] in calls
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()
