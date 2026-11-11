"""Sign in with ChatGPT: the user's ChatGPT plan pays for the model, with no API key.

OpenAI lets open-source and locally run apps use a signed-in user's ChatGPT plan for Responses API requests
(developers.openai.com/siwc/token-sharing-open-source). Pydantic AI implements the flow
(`pydantic_ai.providers.openai_chatgpt`), and this module wires it into the app:

- No client id to set up: the first sign-in registers Jobify for the user's ChatGPT account (dynamic registration,
  identified by this installation's host id), and OpenAI returns the client id to reuse on later sign-ins.
- The browser opens OpenAI's sign-in page; the code comes back to http://127.0.0.1:1455/auth/callback (OpenAI fixes
  the scheme, host and path; only the port may vary) and is exchanged with PKCE.
- The credentials (client id, tokens, ID token, granted scopes) are kept in chatgpt_auth.json in the app folder; the
  provider refreshes the access token and saves the new set."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jobapply.paths import app_home

APP_NAME = 'Jobify'
REDIRECT_PORT = 1455
LOGIN_TIMEOUT_S = 300


class AuthError(RuntimeError):
	pass


def token_file() -> Path:
	return app_home() / 'chatgpt_auth.json'


def host_id() -> str:
	"""A stable, opaque id for this installation (created once): OpenAI ties the registration to it."""
	path = app_home() / 'chatgpt_host_id'
	try:
		value = path.read_text(encoding='utf-8').strip()
		if value.startswith('urn:uuid:'):
			return value
	except OSError:
		pass
	value = uuid.uuid4().urn
	path.write_text(value, encoding='utf-8')
	return value


def load_credentials() -> Any:
	"""The saved OpenAIChatGPTCredentials, or None."""
	from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTCredentials

	try:
		data = json.loads(token_file().read_text(encoding='utf-8'))
		return OpenAIChatGPTCredentials(**data)
	except (OSError, ValueError, TypeError):
		return None


def save_credentials(credentials: Any) -> None:
	path = token_file()
	tmp = path.with_suffix('.tmp')
	tmp.write_text(json.dumps(asdict(credentials)), encoding='utf-8')
	tmp.replace(path)


class CredentialFile:
	"""OpenAIChatGPTCredentialSource on chatgpt_auth.json: the provider saves refreshed tokens here."""

	async def load(self) -> Any:
		credentials = load_credentials()
		if credentials is None:
			raise AuthError('Not signed in with ChatGPT: sign in from Setup.')
		return credentials

	async def save(self, credentials: Any) -> None:
		save_credentials(credentials)


def status() -> dict[str, Any]:
	credentials = load_credentials()
	if credentials is None:
		return {'signed_in': False}
	return {'signed_in': True, 'email': _email(credentials.id_token), 'plan_usage': credentials.plan_usage_granted}


def _email(id_token: str | None) -> str:
	"""The account's email from the ID token (for display only; the token was checked when it was issued)."""
	try:
		payload = (id_token or '').split('.')[1]
		return str(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('email', ''))
	except (IndexError, ValueError, AttributeError):
		return ''


def logout() -> None:
	"""Forget the tokens. The host id stays, so signing in again reuses this installation's registration."""
	token_file().unlink(missing_ok=True)


async def login_async(open_browser: Callable[[str], Any] = webbrowser.open, ask_consent: bool = False) -> dict[str, Any]:
	from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTOAuthFlow

	previous = load_credentials()
	flow = OpenAIChatGPTOAuthFlow(
		app_name=APP_NAME,
		host_id=host_id(),
		client_id=previous.client_id if previous else None,  # reuse the registration
		redirect_uri=f'http://127.0.0.1:{REDIRECT_PORT}/auth/callback',
	)
	# A user who declined plan usage before is asked again (prompt=consent)
	ask = ask_consent or bool(previous and not previous.plan_usage_granted)
	open_browser(flow.authorization_url(extra_params={'prompt': 'consent'} if ask else None))
	try:
		credentials = await asyncio.wait_for(flow.exchange_code_from_callback(), LOGIN_TIMEOUT_S)
	except TimeoutError:
		raise AuthError('ChatGPT sign-in timed out. Try again.') from None
	except OSError as e:  # the callback port is taken
		raise AuthError(f'Port {REDIRECT_PORT} is busy ({e}); close the app using it (e.g. Codex CLI) and try again.') from None
	except Exception as e:  # denied, or the token exchange failed
		raise AuthError(f'ChatGPT sign-in failed: {e}') from None
	save_credentials(credentials)  # saved even without plan usage: the next sign-in reuses the registration
	if not credentials.plan_usage_granted:
		raise AuthError(
			'Signed in, but "Use your ChatGPT plan" was not allowed. Sign in again and approve it: '
			'Jobify needs it to run the AI on your plan.'
		)
	return status()


def login(open_browser: Callable[[str], Any] = webbrowser.open) -> dict[str, Any]:
	"""Blocks until the user finishes signing in in their browser (or LOGIN_TIMEOUT_S)."""
	return asyncio.run(login_async(open_browser))


def model(model_name: str) -> Any:
	"""The Pydantic AI model that runs on the signed-in user's ChatGPT plan."""
	from pydantic_ai.models.openai import OpenAIResponsesModel
	from pydantic_ai.providers.openai_chatgpt import OpenAIChatGPTProvider

	credentials = load_credentials()
	if credentials is None:
		raise AuthError('Sign in with ChatGPT in Setup first.')
	if not credentials.plan_usage_granted:
		raise AuthError('Sign in with ChatGPT again and allow "Use your ChatGPT plan".')
	return OpenAIResponsesModel(model_name, provider=OpenAIChatGPTProvider(credential_source=CredentialFile()))
