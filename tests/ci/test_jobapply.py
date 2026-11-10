"""JobApply without a browser: settings, the applications CSV, questions, templates, documents, ChatGPT sign-in."""

import json
import threading
import urllib.parse
import urllib.request

import pytest

from jobapply import chatgpt_auth, documents, frozen
from jobapply.profile import Question, Questions, save_answer, tone_from_criteria
from jobapply.render import TemplateError, render
from jobapply.settings import ChatGPTLogin, Settings
from jobapply.tracker import Row, Tracker, job_key

PROFILE = """# Profile
- Full name: Ada Lovelace
- Email: ada@example.com
## Experience
### Engineer, Analytical Engines Ltd (2019 - Present)
- Cut build times by 40% for 12 teams.
## Education
### BSc Mathematics, University of London (2018)
## Saved answers
"""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
	monkeypatch.setenv('JOBAPPLY_HOME', str(tmp_path / 'home'))
	return tmp_path / 'home'


def test_settings_round_trip_and_hide_keys():
	s = Settings(gemini_api_key='secret', daily_cap=20, job_urls=[' https://a.example/job/1 ', ''])
	s.save()
	loaded = Settings.load()
	assert loaded.gemini_api_key == 'secret' and loaded.job_urls == ['https://a.example/job/1']
	assert loaded.public()['gemini_api_key'] is True and loaded.public()['openai_api_key'] is False
	assert isinstance(loaded.chatgpt, ChatGPTLogin)


@pytest.mark.parametrize('bad', [{'provider': 'x'}, {'daily_cap': 0}, {'daily_cap': True}, {'boards': ['monster']}])
def test_settings_reject_bad_values(bad):
	with pytest.raises(ValueError):
		Settings.from_dict(bad)


def test_job_key_ignores_tracking_parameters():
	assert job_key('https://www.indeed.com/viewjob?jk=abc&from=serp&utm_source=x') == job_key('http://indeed.com/viewjob/?jk=abc')
	assert job_key('https://www.indeed.com/viewjob?jk=abc') != job_key('https://www.indeed.com/viewjob?jk=abd')
	assert job_key('https://www.linkedin.com/jobs/view/123/?refId=9&trackingId=8') == job_key(
		'https://linkedin.com/jobs/view/123'
	)


def test_tracker_counts_today_and_dedupes(tmp_path):
	t = Tracker(tmp_path / 'a.csv')
	t.add(Row(company='Ácme', url='https://x.example/jobs/1', status='filled'))
	t.add(Row(url='https://x.example/jobs/2', status='skipped'))
	t.add(Row(url='https://x.example/jobs/3', status='needs_input'))
	t.add(Row(url='https://x.example/jobs/4', status='failed'))
	t.add(Row(url='https://x.example/jobs/5', status='email_drafted', date='2000-01-01'))
	assert t.count_today() == 2  # skipped and failed do not count; another day does not either
	assert t.seen('https://x.example/jobs/1?utm_source=feed') and t.seen('https://x.example/jobs/2')
	assert not t.seen('https://x.example/jobs/4')  # a failed job is tried again
	assert t.rows()[0].company == 'Ácme'  # utf-8-sig (Excel) round-trips
	assert (tmp_path / 'a.csv').read_bytes().startswith(b'\xef\xbb\xbf')
	with pytest.raises(ValueError):
		t.add(Row(url='u', status='submitted'))


def test_questions_merge_repeats_and_answers_go_to_profile(tmp_path):
	profile = tmp_path / 'profile.md'
	profile.write_text('# Me\n', encoding='utf-8')
	qs = Questions(tmp_path / 'q.json')
	a = qs.ask(Question('What is your desired salary?', company='A'))
	b = qs.ask(Question('What is your desired  salary', company='B'))
	assert a.id == b.id and qs.open()[0].jobs == 2
	qs.answer(a.id, '90,000 EUR', profile)
	assert qs.open() == []
	text = profile.read_text(encoding='utf-8')
	assert '## Saved answers' in text and 'Q: What is your desired salary?' in text and 'A: 90,000 EUR' in text
	save_answer(profile, 'Relocate?', 'Yes')
	assert profile.read_text(encoding='utf-8').count('## Saved answers') == 1


def test_tone_from_criteria():
	assert tone_from_criteria('- Titles: x\n- Cover letter tone: friendly, short\n') == 'friendly, short'
	assert tone_from_criteria('- Cover letter tone: (e.g. formal)\n') is None
	assert tone_from_criteria('nothing') is None


def test_render_sections_escaping_and_errors():
	data = {'name': '<Ada>', 'items': ['a', 'b'], 'jobs': [{'t': 'x', 'b': ['1']}], 'none': [], 'raw': '<b>'}
	tpl = '{{name}}|{{{raw}}}|{{#items}}[{{.}}]{{/items}}|{{#jobs}}{{t}}:{{#b}}{{.}}{{/b}}:{{name}}{{/jobs}}|{{^none}}empty{{/none}}|{{#items.0}}yes{{/items.0}}'
	assert render(tpl, data) == '&lt;Ada&gt;|<b>|[a][b]|x:1:&lt;Ada&gt;|empty|yes'
	assert render('{{#company}}{{company}}/{{title}}{{/company}}', {'company': 'Acme', 'title': 'Dev'}) == 'Acme/Dev'
	assert render('{{start}}{{#start}}{{#end}} - {{/end}}{{/start}}{{end}}', {'start': '', 'end': '2018'}) == '2018'
	with pytest.raises(TemplateError):
		render('{{#a}}', {})
	with pytest.raises(TemplateError):
		render('{{/a}}', {})


def _docs(**resume) -> documents.TailoredDocs:
	base = {
		'name': 'Ada Lovelace',
		'email': 'ada@example.com',
		'experience': [
			{'title': 'Engineer', 'company': 'Analytical Engines Ltd', 'bullets': ['Cut build times by 40% for 12 teams.']}
		],
		'education': [{'degree': 'BSc Mathematics', 'school': 'University of London'}],
		'skills': [{'name': 'Languages', 'items': ['Python', 'C']}],
	}
	return documents.TailoredDocs(
		resume=documents.ResumeData(**{**base, **resume}), cover_letter='Dear Acme,\n\nHello.\n\nAda', email_subject='Application'
	)


def test_fact_check_rejects_invented_employers_and_numbers():
	documents.check_facts(_docs().resume, PROFILE)
	bad = _docs(experience=[{'title': 'CTO', 'company': 'Google', 'bullets': ['Grew revenue 300%.']}]).resume
	with pytest.raises(documents.FactError, match='Google.*300'):
		documents.check_facts(bad, PROFILE)


def test_builtin_template_renders_every_section():
	html = documents.render_resume(_docs(languages=['English', 'French']).resume)
	for text in (
		'Ada Lovelace',
		'Analytical Engines Ltd',
		'Cut build times by 40%',
		'University of London',
		'Python, C',
		'English, French',
	):
		assert text in html
	assert 'Projects' not in html  # an empty section is left out
	assert '{{' not in html


def test_write_documents_makes_a_folder_per_job(home):
	printed = []

	def fake_pdf(src, dest):
		printed.append(src.name)
		dest.write_bytes(b'%PDF')
		return dest

	paths = documents.write_documents(_docs(), 'Acme Inc.', 'Senior Engineer', None, fake_pdf)
	again = documents.write_documents(_docs(), 'Acme Inc.', 'Senior Engineer', None, fake_pdf)
	assert paths['folder'] != again['folder']
	assert paths['resume_pdf'].endswith('Ada_Lovelace_Resume.pdf') and printed == ['resume.html', 'cover_letter.html'] * 2
	assert 'Dear Acme' in paths['cover_letter_text']


async def test_tailor_sends_invented_facts_back_to_the_model():
	from pydantic_ai.messages import ModelResponse, ToolCallPart
	from pydantic_ai.models.function import FunctionModel

	good = _docs().model_dump()
	bad = _docs(experience=[{'title': 'CTO', 'company': 'Google'}]).model_dump()
	calls = []

	def fn(messages, info):
		calls.append(messages)
		return ModelResponse(parts=[ToolCallPart('final_result', bad if len(calls) == 1 else good)])

	docs = await documents.tailor(FunctionModel(fn), PROFILE, 'We need Python.', 'Acme', 'Engineer', 'friendly')
	assert len(calls) == 2 and docs.resume.experience[0].company == 'Analytical Engines Ltd'
	assert 'Google' in str(calls[1][-1])


def test_frozen_dispatch_runs_module_and_code(monkeypatch, tmp_path, capsys):
	assert frozen.dispatch(['JobApply.exe']) is False
	assert frozen.dispatch(['JobApply.exe', '-I', '-c', 'print("hi", __name__)']) is True
	(tmp_path / 'jobapply_probe.py').write_text('import sys\nprint("mod", sys.argv[1:], __name__)\n')
	monkeypatch.syspath_prepend(str(tmp_path))
	assert frozen.dispatch(['JobApply.exe', '-P', '-m', 'jobapply_probe', 'x']) is True
	assert capsys.readouterr().out.split('\n')[:2] == ['hi __main__', "mod ['x'] __main__"]


async def test_chatgpt_sign_in_registers_itself_and_runs_on_the_plan(monkeypatch):
	"""Sign in with ChatGPT for local apps needs no client id: the first sign-in registers the app (dynamic_agent_client,
	this installation's host id, the app name) and the callback returns the issued client id, which later sign-ins
	reuse. Plan usage (chatgpt.tokens.use.direct) is required; a sign-in without it asks for consent next time."""
	import base64
	import socket
	import time

	from pydantic_ai.providers import openai_chatgpt

	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		monkeypatch.setattr(chatgpt_auth, 'REDIRECT_PORT', sock.getsockname()[1])
	granted = ['openid email offline_access resource.invoke chatgpt.tokens.use.direct']
	urls, posted = [], []

	def jwt(claims):
		part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')  # noqa: E731
		return f'{part({"alg": "none"})}.{part(claims)}.sig'

	async def token_endpoint(url, form, ta, **kw):
		posted.append(form)
		nonce = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[-1]).query))['nonce']
		claims = {
			'iss': 'https://auth.openai.com',
			'aud': form['client_id'],
			'sub': 'user-1',
			'nonce': nonce,
			'exp': int(time.time()) + 3600,
			'email': 'ada@example.com',
		}
		return openai_chatgpt._TokenResponse(access_token='at', refresh_token='rt', id_token=jwt(claims), scope=granted[0])

	monkeypatch.setattr(openai_chatgpt, '_post_token_request', token_endpoint)

	def browser(url):  # the user approves; OpenAI redirects to the loopback callback with the issued client id
		urls.append(url)
		q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
		back = f'http://127.0.0.1:{chatgpt_auth.REDIRECT_PORT}/auth/callback?code=c0de&state={q["state"]}&client_id=app_issued'

		def hit():
			for _ in range(50):
				try:
					urllib.request.urlopen(back, timeout=2).read()
					return
				except OSError:
					time.sleep(0.05)

		threading.Thread(target=hit, daemon=True).start()

	status = await chatgpt_auth.login_async(open_browser=browser)
	assert status == {'signed_in': True, 'email': 'ada@example.com', 'plan_usage': True}
	first = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[0]).query))
	assert urls[0].startswith('https://auth.openai.com/api/accounts/authorize?')
	assert first['client_id'] == 'dynamic_agent_client' and first['agent_name_hint'] == 'JobApply'
	assert first['ext_agent_host_id'].startswith('urn:uuid:') and first['code_challenge_method'] == 'S256'
	assert 'chatgpt.tokens.use.direct' in first['scope'].split()
	assert first['redirect_uri'] == f'http://127.0.0.1:{chatgpt_auth.REDIRECT_PORT}/auth/callback'
	assert posted[0]['client_id'] == 'app_issued' and posted[0]['code'] == 'c0de'

	model = chatgpt_auth.model('gpt-6-luna')  # requests go to api.openai.com with the plan's token, no API key
	assert model.system == 'openai-chatgpt' and model.model_name == 'gpt-6-luna'

	granted[0] = 'openid email offline_access'  # signing in again: the registration is reused; plan use declined
	with pytest.raises(chatgpt_auth.AuthError, match='Use your ChatGPT plan'):
		await chatgpt_auth.login_async(open_browser=browser)
	again = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[1]).query))
	assert again['client_id'] == 'app_issued' and 'agent_name_hint' not in again
	assert again['ext_agent_host_id'] == first['ext_agent_host_id']
	with pytest.raises(chatgpt_auth.AuthError, match='Use your ChatGPT plan'):
		chatgpt_auth.model('gpt-6-luna')
	granted[0] = 'openid email offline_access chatgpt.tokens.use.direct'
	await chatgpt_auth.login_async(open_browser=browser)
	assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[2]).query))['prompt'] == 'consent'

	chatgpt_auth.logout()
	assert chatgpt_auth.status() == {'signed_in': False}
	with pytest.raises(chatgpt_auth.AuthError, match='Sign in'):
		chatgpt_auth.model('gpt-6-luna')


def test_profile_topics_count_only_lines_with_a_value():
	from jobapply.audit import missing_topics
	from jobapply.profile import STARTER_PROFILE

	assert set(missing_topics(STARTER_PROFILE)) == set(missing_topics(''))  # an empty starter answers nothing
	filled = '- Desired salary: 70k EUR\n- Needs visa sponsorship (yes/no): no\n- Q: Willing to relocate to Berlin?\n  A: Yes\n'
	assert set(missing_topics(filled)) == {'notice', 'authorization'}


def test_a_job_that_keeps_failing_is_not_tried_forever(tmp_path):
	"""Codex review: a failed posting counted as unseen and failed jobs do not count toward the cap, so a board that
	keeps listing a broken posting kept the loop applying (and paying) forever. One retry, then it counts as seen."""
	t = Tracker(tmp_path / 'a.csv')
	url = 'https://x.example/jobs/9'
	t.add(Row(url=url, status='failed'))
	assert not t.seen(url)  # one more try
	t.add(Row(url=url + '?utm_source=feed', status='failed'))
	assert t.seen(url)


def test_csv_cells_that_excel_would_run_as_formulas_are_written_as_text(tmp_path):
	"""Codex review: a listing titled '=HYPERLINK(...)' became a live formula in Excel."""
	t = Tracker(tmp_path / 'a.csv')
	t.add(
		Row(
			company='=HYPERLINK("http://evil","x")',
			title='+1 Engineer',
			notes='@SUM(A1)',
			url='https://x.example/1',
			status='filled',
		)
	)
	import csv

	with (tmp_path / 'a.csv').open(newline='', encoding='utf-8-sig') as f:
		cells = next(csv.DictReader(f))  # what Excel reads
	assert (cells['company'], cells['title'], cells['notes']) == ('\'=HYPERLINK("http://evil","x")', "'+1 Engineer", "'@SUM(A1)")
	row = t.rows()[0]
	assert (row.company, row.title, row.notes) == ('=HYPERLINK("http://evil","x")', '+1 Engineer', '@SUM(A1)')


def test_login_browser_opens_the_profile_without_remote_control(home, monkeypatch):
	"""User report: Google refused to sign in ("This browser or app may not be secure") in the JobApply browser, which
	runs with a remote debugging port. Logins now happen in the same profile opened normally, and Start waits until
	that window is closed (one profile can be open in one browser only)."""
	from jobapply import app

	launched = []

	class Proc:
		alive = True

		def __init__(self, args, **kw):
			launched.append(args)

		def poll(self):
			return None if Proc.alive else 0

	monkeypatch.setattr(app.subprocess, 'Popen', Proc)
	monkeypatch.setattr('browser_use.browser.local.find_binary', lambda path=None, **kw: '/opt/chrome')
	Settings(gemini_api_key='k').save()
	api = app.Api()
	assert api.open_login_browser() == {'ok': True}
	args = launched[0]
	assert args[0] == '/opt/chrome' and f'--user-data-dir={home / "browser-profile"}' in args
	assert not any(a.startswith('--remote-debugging') or 'automation' in a for a in args)
	assert any('accounts.google.com' in a for a in args) and any('linkedin.com' in a for a in args)
	assert 'close the login browser' in api.start()['error'].lower()
	Proc.alive = False
	assert 'close the login browser' not in str(api.start().get('error', '')).lower()


def _github(releases: list[dict], files: dict[str, bytes]):
	import httpx

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/repos/zaidamjad17/Jobify/releases':
			return httpx.Response(200, json=releases)
		if request.url.path in files:
			return httpx.Response(200, content=files[request.url.path])
		return httpx.Response(404)

	return httpx.Client(transport=httpx.MockTransport(handler))


def _release(tag: str, data: bytes = b'', **extra) -> dict:
	import hashlib

	asset = {
		'name': f'JobApply-Setup-{tag.removeprefix("jobapply-v")}.exe',
		'browser_download_url': f'https://github.com/dl/{tag}.exe',
		'size': len(data),
		'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
	}
	return {'tag_name': tag, 'body': f'notes {tag}', 'html_url': f'https://github.com/r/{tag}', 'assets': [asset], **extra}


def test_update_check_offers_the_newest_installer_above_the_running_version():
	from jobapply import updater

	releases = [
		_release('jobapply-v0.1.0'),
		_release('jobapply-v0.3.0', draft=True),
		_release('jobapply-v0.2.5', prerelease=True),
		_release('jobapply-v0.2.0'),
		_release('jobapply-v0.10.0', assets=[]),  # the build has not attached the installer yet
		_release('1.0.1'),  # a browser-use release, not the app's
		_release('jobapply-v0.2.0-beta'),
	]
	found = updater.check('0.1.0', client=_github(releases, {}))
	assert found is not None and (found.version, found.name) == ('0.2.0', 'JobApply-Setup-0.2.0.exe')
	assert updater.check('0.2.0', client=_github(releases, {})) is None
	assert (
		updater.check('0.1.0', client=_github([_release('jobapply-v0.9.0'), _release('jobapply-v0.10.0')], {})).version
		== '0.10.0'
	)


def test_update_download_checks_the_installer_and_install_starts_it_silently(home):
	from jobapply import updater

	data = b'MZ installer bytes'
	good = updater.check('0.1.0', client=_github([_release('jobapply-v0.2.0', data)], {}))
	assert good is not None
	seen = []
	path = updater.download(
		good, progress=lambda done, total: seen.append((done, total)), client=_github([], {'/dl/jobapply-v0.2.0.exe': data})
	)
	assert path.read_bytes() == data and path.parent == home / 'updates' and seen[-1] == (len(data), len(data))

	with pytest.raises(updater.UpdateError, match='does not match'):  # a tampered or truncated download installs nothing
		updater.download(good, client=_github([], {'/dl/jobapply-v0.2.0.exe': b'MZ evil bytes!!!!!'}))
	assert not list((home / 'updates').glob('*.part'))

	started = []
	updater.install(path, popen=lambda args, **kw: started.append(args))
	assert started == [[str(path), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS']]
