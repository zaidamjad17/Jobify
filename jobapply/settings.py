"""The app's settings, saved as settings.json in the app folder. Keys are kept there in plain text, readable only by the
Windows user account (the same as a browser's saved passwords without a master password)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from jobapply.paths import app_home

PROVIDERS = ('gemini', 'openai', 'chatgpt')
BOARDS = {  # id -> (label, search URL hint for the agent)
	'linkedin': ('LinkedIn', 'https://www.linkedin.com/jobs/search/'),
	'indeed': ('Indeed', 'https://www.indeed.com/jobs'),
	'greenhouse': (
		'Greenhouse',
		'https://www.google.com/search?q=site%3Aboards.greenhouse.io+OR+site%3Ajob-boards.greenhouse.io',
	),
	'lever': ('Lever', 'https://www.google.com/search?q=site%3Ajobs.lever.co'),
	'ashby': ('Ashby', 'https://www.google.com/search?q=site%3Ajobs.ashbyhq.com'),
	'wellfound': ('Wellfound', 'https://wellfound.com/jobs'),
}


@dataclass
class ChatGPTLogin:
	"""Sign in with ChatGPT (OAuth 2.0 + PKCE, localhost redirect). The client id comes from the OpenAI developer
	platform, where the app is registered; the endpoints are settings so they follow OpenAI's docs without a new build."""

	client_id: str = ''
	authorize_url: str = 'https://auth.openai.com/oauth/authorize'
	token_url: str = 'https://auth.openai.com/oauth/token'
	scope: str = 'openid profile email offline_access'
	api_base: str = 'https://api.openai.com/v1'
	redirect_port: int = 1455
	model: str = 'gpt-6-luna'


@dataclass
class Settings:
	profile_path: str = ''
	criteria_path: str = ''
	resume_template_path: str = ''  # '' = the built-in template
	cover_letter_tone: str = 'professional, warm and concise (about 200 words)'
	provider: str = 'gemini'
	gemini_model: str = 'gemini-3.5-flash'
	gemini_api_key: str = ''
	openai_model: str = 'gpt-6-luna'
	openai_api_key: str = ''
	chatgpt: ChatGPTLogin = field(default_factory=ChatGPTLogin)
	boards: list[str] = field(default_factory=lambda: list(BOARDS))
	daily_cap: int = 20
	job_urls: list[str] = field(default_factory=list)  # pasted URLs, applied to before any board search
	browser_executable: str = ''  # '' = Chrome or Edge, whichever is installed

	@classmethod
	def path(cls) -> Path:
		return app_home() / 'settings.json'

	@classmethod
	def load(cls) -> Settings:
		try:
			data = json.loads(cls.path().read_text(encoding='utf-8'))
		except (OSError, ValueError):
			return cls()
		return cls.from_dict(data)

	@classmethod
	def from_dict(cls, data: dict) -> Settings:
		known = {f.name for f in fields(cls)}
		values = {k: v for k, v in data.items() if k in known}
		if isinstance(values.get('chatgpt'), dict):
			login = {f.name for f in fields(ChatGPTLogin)}
			values['chatgpt'] = ChatGPTLogin(**{k: v for k, v in values['chatgpt'].items() if k in login})
		settings = cls(**values)
		settings.validate()
		return settings

	def validate(self) -> None:
		if self.provider not in PROVIDERS:
			raise ValueError(f'provider must be one of {", ".join(PROVIDERS)}, not {self.provider!r}')
		if isinstance(self.daily_cap, bool) or not isinstance(self.daily_cap, int) or not 1 <= self.daily_cap <= 200:
			raise ValueError(f'daily_cap must be a whole number from 1 to 200, not {self.daily_cap!r}')
		unknown = [b for b in self.boards if b not in BOARDS]
		if unknown:
			raise ValueError(f'unknown job boards: {", ".join(unknown)}')
		self.job_urls = [u.strip() for u in self.job_urls if u.strip()]

	def save(self) -> None:
		self.validate()
		path = self.path()
		tmp = path.with_suffix('.tmp')
		tmp.write_text(json.dumps(asdict(self), indent=2), encoding='utf-8')
		tmp.replace(path)

	def public(self) -> dict:
		"""For the UI: keys are reported as set or not, never sent back."""
		data = asdict(self)
		data['gemini_api_key'] = bool(self.gemini_api_key)
		data['openai_api_key'] = bool(self.openai_api_key)
		return data
