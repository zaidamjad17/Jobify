"""The Jobify window's page (jobapply/ui/index.html) in headless Chrome, with a stub of the Python API."""

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
		'backup_provider': '',
		'max_review_tabs': 15,
		'max_job_age_days': 14,
	},
	'profile_score': {'score': 60, 'missing': ['Salary range', 'Notice period']},
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
	'links': [
		{'url': 'https://jobs.lever.co/acme/1', 'status': 'applying'},
		{'url': 'https://jobs.lever.co/acme/2', 'status': 'waiting'},
	],
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
			'question': 'Please log in to Teradata in the Jobify browser',
			'field_hint': 'login',
			'company': 'Teradata',
			'title': 'Dev',
			'jobs': 1,
			'asked_at': 't',
		},
	],
}

PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

DETAIL = {
	'row': {
		**STATE['rows'][0],
		'resume': 'C:/docs/k/Ada_Resume.pdf',
		'cover_letter': 'C:/docs/k/Ada_Cover_Letter.pdf',
		'screenshot': 'C:/s/k.png',
	},
	'record': {
		'summary': {
			'about': 'Kodifly builds lidar perception software and needs a graduate engineer.',
			'responsibilities': ['Write perception pipelines'],
			'requirements': ['Python', 'A degree in CS'],
			'location': 'Hong Kong',
			'salary': '',
			'employment_type': 'Full-time',
			'why_match': ['Python projects'],
		},
		'job_description': 'Full text of the posting.',
		'submitted': {
			'fields': [
				{'label': 'First name', 'value': 'Ada', 'kind': 'text'},
				{'label': 'Email', 'value': 'ada@example.com', 'kind': 'email'},
			],
			'files': ['C:/docs/k/Ada_Resume.pdf'],
			'page_url': 'https://jobs.ashbyhq.com/k/1/application',
			'page_title': 'Apply to Kodifly',
		},
		'timeline': [{'time': '10:00:01', 'text': 'Opened the posting', 'level': 'info'}],
		'documents': {'folder': 'C:/docs/k', 'cover_letter_text': 'Dear Kodifly,', 'email_subject': ''},
	},
}

STUB = """
window.__calls = [];
window.__state = %s;
window.__detail = %s;
window.__png = %s;
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
	window.__calls.push([name, ...args]);
	if (name === 'state') return JSON.parse(JSON.stringify(window.__state));
	if (name === 'events') { const e = window.__pending || []; window.__pending = []; return e; }
	if (name === 'test_model') return { ok: true, model: 'gemini-x', reply: 'ok' };
	if (name === 'job_detail') return JSON.parse(JSON.stringify(window.__detail));
	if (name === 'document_pages') return { pages: [window.__png, window.__png], width: 600, height: 848 };
	if (name === 'dismiss') { window.__state.questions = window.__state.questions.filter(q => q.id !== args[0]); return { ok: true }; }
	return { ok: true };
} }) };
window.addEventListener('load', () => window.dispatchEvent(new Event('pywebviewready')));
"""


def _page(chrome, state=None, wait='a_q1'):
	cdp = CDP(chrome['ws'])
	target = cdp.send('Target.createTarget', url='about:blank')['targetId']
	session = cdp.send('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
	cdp.send('Page.enable', session)
	cdp.send(
		'Page.addScriptToEvaluateOnNewDocument',
		session,
		source=STUB % (json.dumps(state or STATE), json.dumps(DETAIL), json.dumps(PNG)),
	)
	cdp.send('Page.navigate', session, url=resource('ui/index.html').resolve().as_uri())

	def ev(expression):
		reply = cdp.send('Runtime.evaluate', session, expression=expression, returnByValue=True, awaitPromise=True)
		return reply['result'].get('value')

	for _ in range(100):
		try:
			if ev(f'!!document.getElementById("{wait}")'):
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


def test_a_handoff_shows_the_banner_plays_a_sound_and_continue_tells_the_agent(chrome):
	"""The agent needs a person (captcha): a banner with the reason, a sound, and one button that lets it continue.
	Take over is offered while the loop runs."""
	cdp, session, target, ev = _page(chrome)
	try:
		assert ev('document.getElementById("helpbar").hidden') is True
		assert ev('document.getElementById("takeover").disabled') is False  # the stub state is running
		ev(
			"""window.__pending = [{seq: 100, kind: 'help', time: 't', reason: 'Captcha on Acme', company: 'Acme', sound: true, wait_s: 60}]"""
		)
		for _ in range(40):
			if ev('!document.getElementById("helpbar").hidden'):
				break
			time.sleep(0.1)
		assert 'Captcha on Acme' in ev('document.getElementById("helpbar").innerText')
		assert ev('window.__beeps || 0') == 1
		ev('document.getElementById("helpdone").click()')
		ev('document.getElementById("takeover").click()')
		time.sleep(0.3)
		calls = ev('window.__calls')
		assert ['help_done'] in calls and ['take_over'] in calls
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_setup_shows_profile_completeness_and_history_says_why_jobs_are_not_finished(chrome):
	cdp, session, target, ev = _page(chrome)
	try:
		text = ev('document.getElementById("scoretext").innerText')
		assert '60%' in text and 'Salary range' in text and 'Notice period' in text
		assert ev('document.getElementById("max_job_age_days").value') == '14'
		assert '1 need you' in ev('document.getElementById("rowsummary").innerText')
		assert '1 skipped' in ev('document.getElementById("rowsummary").innerText')
		assert 'waiting on a question for you' in ev('document.getElementById("whybox").innerText')
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_job_links_are_pasted_on_the_run_tab_and_show_their_status(chrome):
	"""User request: add job links in the app and have them applied to. The Run tab lists each link with its status."""
	cdp, session, target, ev = _page(chrome)
	try:
		assert ev('document.querySelectorAll("#links .link").length') == 2
		assert 'applying now' in ev('document.getElementById("links").innerText')
		assert (
			ev('document.getElementById("linkcount").textContent') == '1 waiting, 0 done'
		)  # the one being applied to is neither
		ev('document.getElementById("linkinput").value = "https://boards.greenhouse.io/x/jobs/9"')
		ev('document.getElementById("applylinks").click()')
		ev('document.querySelector("[data-unlink]").click()')
		time.sleep(0.3)
		calls = ev('window.__calls')
		assert ['add_job_links', 'https://boards.greenhouse.io/x/jobs/9', True] in calls
		assert ['remove_job_link', 'https://jobs.lever.co/acme/1'] in calls
		assert ev('document.getElementById("linkinput").value') == ''
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def _wait(ev, expression, timeout=4.0):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if ev(expression):
			return True
		time.sleep(0.05)
	return False


def test_history_opens_a_job_with_its_summary_resume_and_what_was_submitted(chrome):
	"""User request: in History, see each job's CV inside the app (zoomable) and an A-to-Z summary: what the job was
	about and what was submitted."""
	cdp, session, target, ev = _page(chrome)
	try:
		ev('document.querySelector("nav button[data-tab=history]").click()')
		assert ev('document.querySelectorAll("#rows [data-job]").length') == 2
		ev('document.querySelector("#rows [data-job] .ti").click()')  # the row itself, not one of its buttons
		assert _wait(ev, '!document.getElementById("detail").hidden')
		assert _wait(ev, 'document.getElementById("dp_summary").innerText.includes("lidar perception")')
		summary = ev('document.getElementById("dp_summary").textContent')
		assert 'A degree in CS' in summary and 'What we submitted' in summary and 'ada@example.com' in summary
		assert 'Opened the posting' in summary  # the timeline
		assert ['job_detail', 'https://jobs.ashbyhq.com/k/1'] in ev('window.__calls')

		ev('document.querySelector("#dtabs [data-dtab=resume]").click()')
		assert _wait(ev, 'document.querySelectorAll("#v_resume .vpages img").length === 2')
		assert ['document_pages', 'C:/docs/k/Ada_Resume.pdf', 2] in ev('window.__calls')
		before = ev('document.querySelector("#v_resume .vpages img").getBoundingClientRect().width')
		label = ev('document.querySelector("#v_resume .zlabel").textContent')
		ev('document.querySelector("#v_resume [data-z=in]").click()')
		after = ev('document.querySelector("#v_resume .vpages img").getBoundingClientRect().width')
		assert after > before * 1.1
		assert int(ev('document.querySelector("#v_resume .zlabel").textContent').rstrip('%')) > int(label.rstrip('%'))
		ev('document.querySelector("#v_resume [data-z=reset]").click()')
		assert ev('document.querySelector("#v_resume .zlabel").textContent') == '100%'
		ev('document.querySelector("#v_resume [data-v=open]").click()')
		time.sleep(0.2)
		assert ['open_path', 'C:/docs/k/Ada_Resume.pdf'] in ev('window.__calls')

		ev('document.querySelector("#dtabs [data-dtab=submitted]").click()')
		assert _wait(ev, 'document.querySelectorAll("#v_shot .vpages img").length > 0')
		assert 'Apply to Kodifly' in ev('document.getElementById("d_submitted").innerText')
		assert ['document_pages', 'C:/s/k.png', 2] in ev('window.__calls')

		ev('document.querySelector("#dtabs [data-dtab=letter]").click()')
		assert 'Dear Kodifly' in ev('document.getElementById("d_lettertext").innerText')

		ev('document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}))')
		assert ev('document.getElementById("detail").hidden') is True
		assert ev('document.querySelectorAll("[data-submitted]").length') == 1  # the panel's buttons are gone
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_history_filters_rows_by_status_and_search(chrome):
	cdp, session, target, ev = _page(chrome)
	try:
		ev('document.querySelector("nav button[data-tab=history]").click()')
		ev('document.querySelector("#hfilters [data-filter=skipped]").click()')
		assert _wait(ev, 'document.querySelectorAll("#rows [data-job]").length === 1')
		assert 'Duna' in ev('document.getElementById("rows").innerText')
		ev('document.querySelector("#hfilters [data-filter=all]").click()')
		ev('(i => { i.value = "kodi"; i.dispatchEvent(new Event("input")); })(document.getElementById("hsearch"))')
		assert _wait(ev, 'document.querySelectorAll("#rows [data-job]").length === 1')
		assert 'Kodifly' in ev('document.getElementById("rows").innerText')
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_run_shows_the_setup_checklist_and_why_the_loop_last_stopped(chrome):
	"""A new user sees what is left to set up; after a run ends, the Run page says why it stopped."""
	state = {
		**STATE,
		'runner': 'idle',
		'setup': {'profile': True, 'criteria': False, 'model': False, 'model_error': 'Add your Gemini API key in Setup.'},
		'last_stop': {'text': 'Daily limit reached (20 jobs).', 'kind': 'cap', 'time': '18:02:11'},
	}
	cdp, session, target, ev = _page(chrome, state)
	try:
		assert ev('document.getElementById("onboard").hidden') is False
		text = ev('document.getElementById("onboard").innerText')
		assert '1 of 3 done' in text and 'Add your Gemini API key in Setup.' in text
		ev('document.querySelector("#onboard [data-setup]").click()')
		assert ev('document.getElementById("setup").classList.contains("on")') is True
		assert ev('document.getElementById("laststop").hidden') is False
		assert 'Daily limit reached (20 jobs).' in ev('document.getElementById("laststop").innerText')
		ev('document.getElementById("laststop_close").click()')
		assert ev('document.getElementById("laststop").hidden') is True
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()


def test_a_resting_loop_a_hold_after_errors_and_testing_the_model(chrome):
	"""The loop rests between board searches instead of ending; a hold after repeated errors shows Continue without a
	countdown; Setup tests the AI model and saves the keep-searching settings."""
	state = {
		**STATE,
		'current': {'phase': 'rest', 'until': '14:30'},
		'settings': {**STATE['settings'], 'keep_searching': True, 'search_again_minutes': 45},
	}
	cdp, session, target, ev = _page(chrome, state)
	try:
		assert 'searching the boards again at 14:30' in ev('document.getElementById("current").textContent')
		ev(
			"""window.__state.help = {reason: 'Five jobs failed in a row', need: 'errors', company: ''};
			window.__pending = [{seq: 200, kind: 'help', time: 't', reason: 'Five jobs failed in a row', need: 'errors', sound: true, wait_s: 0}]"""
		)
		assert _wait(ev, '!document.getElementById("helpbar").hidden')
		assert ev('document.getElementById("helpdone").textContent') == 'Continue'
		assert 'parks this job' not in ev('document.getElementById("helphint").textContent')

		ev('document.querySelector("nav button[data-tab=setup]").click()')
		assert ev('document.getElementById("search_again_minutes").value') == '45'
		assert ev('document.getElementById("keep_searching").checked') is True
		ev('document.getElementById("testmodel").click()')
		assert _wait(ev, 'document.getElementById("modelstatus").textContent.includes("Works (gemini-x)")')
		calls = ev('window.__calls')
		saved = next(c for c in calls if c[0] == 'save_settings')
		assert saved[1]['keep_searching'] is True and saved[1]['search_again_minutes'] == 45
		assert calls.index(saved) < calls.index(['test_model', ''])
	finally:
		cdp.send('Target.closeTarget', targetId=target)
		cdp.close()
