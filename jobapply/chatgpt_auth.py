"""Sign in with ChatGPT: OAuth 2.0 authorization code + PKCE with a localhost redirect, as native apps do.

login() opens the sign-in page in the user's default browser and waits for the redirect on
http://localhost:<port>/auth/callback; tokens are kept in chatgpt_auth.json in the app folder and refreshed before they
expire. The endpoints, scope and client id are settings (ChatGPTLogin): the client id is the one OpenAI issues for this
app when it is registered for Sign in with ChatGPT."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from jobapply.paths import app_home
from jobapply.settings import ChatGPTLogin

CALLBACK_PATH = '/auth/callback'
LOGIN_TIMEOUT_S = 300
REFRESH_MARGIN_S = 120


class AuthError(RuntimeError):
	pass


def token_file() -> Path:
	return app_home() / 'chatgpt_auth.json'


def pkce_pair() -> tuple[str, str]:
	verifier = secrets.token_urlsafe(64)[:96]
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
	return verifier, challenge


def redirect_uri(cfg: ChatGPTLogin) -> str:
	return f'http://localhost:{cfg.redirect_port}{CALLBACK_PATH}'


def authorize_url(cfg: ChatGPTLogin, challenge: str, state: str) -> str:
	query = {
		'response_type': 'code',
		'client_id': cfg.client_id,
		'redirect_uri': redirect_uri(cfg),
		'scope': cfg.scope,
		'code_challenge': challenge,
		'code_challenge_method': 'S256',
		'state': state,
	}
	return f'{cfg.authorize_url}?{urllib.parse.urlencode(query)}'


def _jwt_claims(token: str) -> dict:
	try:
		payload = token.split('.')[1]
		return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
	except (IndexError, ValueError):
		return {}


def _store(data: dict) -> dict:
	tokens = {
		'access_token': data['access_token'],
		'refresh_token': data.get('refresh_token', ''),
		'id_token': data.get('id_token', ''),
		'expires_at': time.time() + float(data.get('expires_in') or 3600),
	}
	old = load_tokens()
	if not tokens['refresh_token'] and old:  # a refresh that returns no new refresh token keeps the old one
		tokens['refresh_token'] = old.get('refresh_token', '')
	path = token_file()
	tmp = path.with_suffix('.tmp')
	tmp.write_text(json.dumps(tokens), encoding='utf-8')
	tmp.replace(path)
	return tokens


def load_tokens() -> dict | None:
	try:
		return json.loads(token_file().read_text(encoding='utf-8'))
	except (OSError, ValueError):
		return None


def logout() -> None:
	token_file().unlink(missing_ok=True)


def status() -> dict[str, Any]:
	tokens = load_tokens()
	if not tokens:
		return {'signed_in': False}
	claims = _jwt_claims(tokens.get('id_token', ''))
	return {'signed_in': True, 'email': claims.get('email', ''), 'expires_at': tokens.get('expires_at', 0)}


def _exchange(cfg: ChatGPTLogin, form: dict) -> dict:
	response = httpx.post(cfg.token_url, data={**form, 'client_id': cfg.client_id}, timeout=30)
	if response.status_code != 200:
		raise AuthError(f'ChatGPT sign-in failed ({response.status_code}): {response.text[:300]}')
	return response.json()


def login(cfg: ChatGPTLogin, open_browser=webbrowser.open) -> dict[str, Any]:
	"""Blocks until the user finishes signing in (or LOGIN_TIMEOUT_S)."""
	if not cfg.client_id:
		raise AuthError('Set the ChatGPT client id in Setup (from your app registration on the OpenAI developer platform).')
	verifier, challenge = pkce_pair()
	state = secrets.token_urlsafe(24)
	result: dict[str, str] = {}
	done = threading.Event()

	class Callback(http.server.BaseHTTPRequestHandler):
		def do_GET(self) -> None:  # noqa: N802
			url = urllib.parse.urlsplit(self.path)
			if url.path != CALLBACK_PATH:
				self.send_error(404)
				return
			params = dict(urllib.parse.parse_qsl(url.query))
			ok = params.get('state') == state and 'code' in params
			result.update(params if ok else {'error': params.get('error_description') or params.get('error') or 'bad state'})
			body = (
				'<h2>Signed in. You can close this tab and go back to JobApply.</h2>'
				if ok
				else '<h2>Sign-in failed. Go back to JobApply and try again.</h2>'
			)
			self.send_response(200)
			self.send_header('Content-Type', 'text/html; charset=utf-8')
			self.end_headers()
			self.wfile.write(body.encode())
			done.set()

		def log_message(self, *args: Any) -> None:
			pass

	try:
		server = http.server.HTTPServer(('127.0.0.1', cfg.redirect_port), Callback)
	except OSError as e:
		raise AuthError(f'Port {cfg.redirect_port} is busy ({e}); close the app using it or change the port in Setup.') from None
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	try:
		open_browser(authorize_url(cfg, challenge, state))
		if not done.wait(LOGIN_TIMEOUT_S):
			raise AuthError('ChatGPT sign-in timed out.')
	finally:
		server.shutdown()
		server.server_close()
	if 'error' in result:
		raise AuthError(f'ChatGPT sign-in failed: {result["error"]}')
	data = _exchange(
		cfg,
		{
			'grant_type': 'authorization_code',
			'code': result['code'],
			'redirect_uri': redirect_uri(cfg),
			'code_verifier': verifier,
		},
	)
	_store(data)
	return status()


_refresh_lock = threading.Lock()


def access_token(cfg: ChatGPTLogin) -> str:
	with _refresh_lock:
		tokens = load_tokens()
		if not tokens:
			raise AuthError('Not signed in with ChatGPT: sign in from Setup.')
		if tokens.get('expires_at', 0) - time.time() > REFRESH_MARGIN_S:
			return tokens['access_token']
		if not tokens.get('refresh_token'):
			raise AuthError('The ChatGPT sign-in expired: sign in again from Setup.')
		return _store(_exchange(cfg, {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}))['access_token']


class TokenAuth(httpx.Auth):
	"""Puts a fresh ChatGPT access token on every model request (refreshing it when it is about to expire)."""

	def __init__(self, cfg: ChatGPTLogin):
		self.cfg = cfg

	def auth_flow(self, request: httpx.Request):
		request.headers['Authorization'] = f'Bearer {access_token(self.cfg)}'
		yield request
