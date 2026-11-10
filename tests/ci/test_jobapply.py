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


def test_chatgpt_login_pkce_flow(monkeypatch):
	"""login() opens the authorize URL, takes the localhost redirect, checks state and swaps the code with the
	verifier; access_token() refreshes an expiring token."""
	posted = []

	class Response:
		status_code = 200

		def __init__(self, data):
			self._data, self.text = data, json.dumps(data)

		def json(self):
			return self._data

	def fake_post(url, data, timeout):
		posted.append(data)
		token = 'h.' + __import__('base64').urlsafe_b64encode(b'{"email":"ada@example.com"}').decode().rstrip('=') + '.s'
		n = len(posted)
		return Response({'access_token': f'at{n}', 'refresh_token': f'rt{n}', 'id_token': token, 'expires_in': 60})

	monkeypatch.setattr(chatgpt_auth.httpx, 'post', fake_post)
	cfg = ChatGPTLogin(client_id='app_123', redirect_port=0)
	with pytest.raises(chatgpt_auth.AuthError, match='client id'):
		chatgpt_auth.login(ChatGPTLogin())

	import socket

	with socket.socket() as s:
		s.bind(('127.0.0.1', 0))
		cfg.redirect_port = s.getsockname()[1]

	def browser(url):
		q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
		assert (
			q['client_id'] == 'app_123'
			and q['code_challenge_method'] == 'S256'
			and q['redirect_uri'].startswith('http://localhost:')
		)
		back = f'http://127.0.0.1:{cfg.redirect_port}/auth/callback?code=c0de&state={q["state"]}'
		threading.Thread(target=lambda: urllib.request.urlopen(back).read(), daemon=True).start()

	status = chatgpt_auth.login(cfg, open_browser=browser)
	assert status == {'signed_in': True, 'email': 'ada@example.com', 'expires_at': pytest.approx(status['expires_at'])}
	assert posted[0]['grant_type'] == 'authorization_code' and posted[0]['code'] == 'c0de' and posted[0]['code_verifier']
	# expires in 60 s, inside the refresh margin: refreshed with the refresh token
	assert chatgpt_auth.access_token(cfg) == 'at2'
	assert posted[1] == {'grant_type': 'refresh_token', 'refresh_token': 'rt1', 'client_id': 'app_123'}
	chatgpt_auth.logout()
	assert chatgpt_auth.status() == {'signed_in': False}
	with pytest.raises(chatgpt_auth.AuthError, match='Not signed in'):
		chatgpt_auth.access_token(cfg)
