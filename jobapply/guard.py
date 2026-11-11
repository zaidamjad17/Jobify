"""Submit guard: while the agent works, no click, key or script can press a final Submit / Send button in any tab.

It runs on its own CDP connection (the agent's cdp() calls through the browser-harness daemon cannot turn it off),
auto-attaches to every tab and iframe before their scripts run (waitForDebuggerOnStart), and installs GUARD_JS in each
document. GUARD_JS blocks, in the capture phase, pointer/mouse/click/touch events on a control whose label reads
submit/send (but not "send code" or "verify"), Ctrl/Cmd+Enter (Gmail's send shortcut), form submit events from such a
control and form.submit() / requestSubmit() of a form holding one. When a job is done, release() unlocks the tabs that
job used so the user can review and submit there; every other tab stays locked.

A guard against an aligned model's mistakes, not a sandbox: a site whose final button reads neither submit nor send
(e.g. "Apply") is not blocked, which is why the agent's instructions also forbid it."""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)
START_TIMEOUT_S = 15

GUARD_JS = r"""
(() => {
	const KEY = '__KEY__';
	if (window[KEY]) return;
	const state = { locked: true, toast: 0 };
	Object.defineProperty(window, KEY, { value: state, enumerable: false });
	const FINAL = /\b(submit|send)\b/i;
	const ALLOWED = /\b(send|resend|submit)\s+(a\s+|the\s+)?(code|otp|pin|link|verification|again)\b|\bverify\b|\bsearch\b/i;
	const label = el => [el.innerText, el.value, el.getAttribute && el.getAttribute('aria-label'),
		el.getAttribute && el.getAttribute('title'), el.getAttribute && el.getAttribute('data-tooltip')]
		.filter(v => typeof v === 'string' && v).join(' ').replace(/\s+/g, ' ').slice(0, 200);
	const control = e => {
		const t = (e.composedPath && e.composedPath()[0]) || e.target;  // inside a shadow root too
		const el = t && (t.nodeType === 1 ? t : t.parentElement);
		return el ? el.closest('button, input[type=submit], input[type=button], input[type=image], [role=button], a') : null;
	};
	const isFinal = el => { if (!el) return false; const text = label(el); return FINAL.test(text) && !ALLOWED.test(text); };
	const finalIn = form => form && Array.from(form.querySelectorAll('button, input[type=submit], [role=button]')).find(isFinal);
	const report = text => {
		try { if (window.__jobapplyBlocked) window.__jobapplyBlocked(String(text || '').slice(0, 120)); } catch (e) {}
		const now = Date.now();
		if (now - state.toast < 1500 || !document.body) return;
		state.toast = now;
		const box = document.createElement('div');
		box.textContent = 'Jobify: "' + (text || 'Submit') + '" is left for you. Review this tab, then press it yourself.';
		box.style.cssText = 'position:fixed;z-index:2147483647;left:50%;bottom:16px;pointer-events:none;transform:translateX(-50%);background:#12304d;' +
			'color:#fff;font:14px/1.4 Segoe UI,Arial,sans-serif;padding:10px 16px;border-radius:8px;box-shadow:0 4px 16px #0005;max-width:80vw';
		document.body.appendChild(box);
		setTimeout(() => box.remove(), 4000);
	};
	const stop = (e, el) => {
		e.preventDefault(); e.stopImmediatePropagation(); e.stopPropagation();
		if (e.type === 'click' || e.type === 'submit' || e.type === 'keydown') report(el ? label(el) : 'Send');
	};
	for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick', 'touchstart', 'touchend']) {
		window.addEventListener(type, e => {
			if (!state.locked) return;
			const el = control(e);
			if (isFinal(el)) stop(e, el);
		}, true);
	}
	window.addEventListener('keydown', e => {
		if (!state.locked) return;
		if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) return stop(e, null);
		const el = control(e);
		if ((e.key === 'Enter' || e.key === ' ') && isFinal(el)) stop(e, el);
	}, true);
	window.addEventListener('submit', e => {
		if (!state.locked) return;
		const el = e.submitter && isFinal(e.submitter) ? e.submitter : (!e.submitter && finalIn(e.target));
		if (el) stop(e, el);
	}, true);
	const proto = HTMLFormElement.prototype, submit = proto.submit, requestSubmit = proto.requestSubmit;
	proto.submit = function () { const el = state.locked && finalIn(this); if (el) return report(label(el)); return submit.call(this); };
	proto.requestSubmit = function (s) {
		const el = state.locked && ((s && isFinal(s)) ? s : (!s && finalIn(this)));
		if (el) return report(label(el));
		return s === undefined ? requestSubmit.call(this) : requestSubmit.call(this, s);
	};
})();
"""


class SubmitGuard:
	def __init__(self, ws_url: str, on_block: Callable[[str, str], None] | None = None):
		self.ws_url = ws_url
		self.key = f'__jobapplyGuard_{secrets.token_hex(6)}'
		self.source = GUARD_JS.replace('__KEY__', self.key)
		self.on_block = on_block  # (tab id, button label)
		self.released: set[str] = set()  # page target ids the user may submit in
		self.pages: dict[str, str] = {}  # page target id -> url
		self.created: list[str] = []  # page target ids in creation order (jobs mark where theirs start)
		self._sessions: dict[str, str] = {}  # session id -> the page target id it belongs to
		self._scripts: dict[str, str] = {}  # session id -> addScriptToEvaluateOnNewDocument identifier
		self._contexts: dict[str, set[int]] = {}  # session id -> default execution contexts (one per document)
		self._attached: set[str] = set()  # target ids with a session of ours
		self._replies: dict[int, Callable[[dict], object]] = {}
		self._id = 0
		self._ws: Any = None
		self._send_lock = threading.Lock()
		self._state = threading.RLock()
		self._closed = threading.Event()
		self._ready = threading.Event()
		self._error: str | None = None
		self._thread = threading.Thread(target=self._run, daemon=True, name='jobapply-submit-guard')

	# lifecycle

	def start(self) -> None:
		self._thread.start()
		if not self._ready.wait(START_TIMEOUT_S) or self._error:
			self.close()
			raise RuntimeError(f'The submit guard could not start ({self._error or "no answer"}); nothing will be filled.')

	def close(self) -> None:
		self._closed.set()
		ws, self._ws = self._ws, None
		if ws is not None:
			with contextlib.suppress(Exception):
				ws.close()

	@property
	def alive(self) -> bool:
		return self._ready.is_set() and not self._error and self._ws is not None and not self._closed.is_set()

	# what the runner calls

	def mark(self) -> int:
		"""Where the tabs of the job about to start begin."""
		with self._state:
			return len(self.created)

	def tabs_since(self, mark: int) -> list[str]:
		with self._state:
			return [t for t in self.created[mark:] if t in self.pages]

	def release(self, targets: list[str]) -> list[str]:
		"""Unlock these tabs (the user submits there). Returns the ones still open."""
		with self._state:
			targets = [t for t in targets if t in self.pages and t not in self.released]
			self.released.update(targets)
			sessions = [(s, t) for s, t in self._sessions.items() if t in targets]
		for session, _ in sessions:
			self._unlock(session)
		return targets

	def lock(self, targets: list[str]) -> list[str]:
		"""Lock released tabs again (the agent goes back to finish them). Returns the ones still open."""
		with self._state:
			targets = [t for t in targets if t in self.pages]
			self.released.difference_update(targets)
			sessions = [s for s, t in self._sessions.items() if t in targets]
		for session in sessions:
			with contextlib.suppress(Exception):
				self._send(
					'Page.addScriptToEvaluateOnNewDocument', session, reply=self._script_added(session), source=self.source
				)
				self._send('Runtime.addBinding', session, name='__jobapplyBlocked')
				# the open document: lock its guard, or install it if this document never had one
				relock = f'window[{self.key!r}] ? (window[{self.key!r}].locked = true) : {self.source}'
				self._send('Runtime.evaluate', session, expression=relock)
				for ctx in list(self._contexts.get(session, ())):
					self._send('Runtime.evaluate', session, expression=relock, contextId=ctx)
		return targets

	# CDP

	def _send(self, method: str, session_id: str | None = None, reply: Callable[[dict], object] | None = None, **params) -> int:
		with self._send_lock:
			self._id += 1
			msg: dict[str, Any] = {'id': self._id, 'method': method, 'params': params}
			if session_id:
				msg['sessionId'] = session_id
			if reply is not None:
				self._replies[self._id] = reply
			if self._ws is None:
				raise RuntimeError('submit guard connection is closed')
			self._ws.send(json.dumps(msg))
			return self._id

	def _connect(self) -> None:
		from websockets.sync.client import connect

		self._ws = connect(self.ws_url, max_size=None, open_timeout=START_TIMEOUT_S, close_timeout=1)
		with self._state:
			self._sessions.clear()
			self._scripts.clear()
			self._contexts.clear()
			self._attached.clear()
		self._replies.clear()
		pending = {
			self._send('Target.setDiscoverTargets', discover=True),
			self._send(
				'Target.setAutoAttach',
				autoAttach=True,
				waitForDebuggerOnStart=True,
				flatten=True,
				filter=[{'type': 'page', 'exclude': False}, {'type': 'iframe', 'exclude': False}],
			),
		}
		deadline = time.monotonic() + START_TIMEOUT_S
		while pending:
			msg = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.monotonic())))
			if msg.get('id') in pending:
				if 'error' in msg:
					raise RuntimeError(msg['error'].get('message', str(msg['error'])))
				pending.discard(msg['id'])
			else:
				self._handle(msg)
		# pages that were open before the guard connected (auto-attach at the browser level covers new ones)
		targets = self._call('Target.getTargets')['targetInfos']
		for info in targets:
			if info.get('type') == 'page':
				self._page_seen(info)
				if info['targetId'] not in self._attached:
					self._send('Target.attachToTarget', targetId=info['targetId'], flatten=True)

	def _call(self, method: str, **params) -> dict:
		"""A command answered before the receive loop runs (only during _connect)."""
		msg_id = self._send(method, **params)
		deadline = time.monotonic() + START_TIMEOUT_S
		while True:
			msg = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.monotonic())))
			if msg.get('id') == msg_id:
				if 'error' in msg:
					raise RuntimeError(msg['error'].get('message', str(msg['error'])))
				return msg.get('result') or {}
			self._handle(msg)

	def _run(self) -> None:
		backoff = 0.5
		while not self._closed.is_set():
			try:
				self._connect()
				self._ready.set()
				backoff = 0.5
				for raw in self._ws:
					self._handle(json.loads(raw))
			except Exception as e:
				with contextlib.suppress(Exception):
					self._ws.close()
				if not self._ready.is_set():
					self._error = str(e) or type(e).__name__
					self._ready.set()
					return
				if not self._closed.is_set():
					logger.warning('submit guard connection lost (%s); reconnecting', e)
			if self._closed.wait(backoff):
				return
			backoff = min(backoff * 2, 5.0)

	def _page_seen(self, info: dict) -> None:
		target = str(info['targetId'])
		with self._state:
			if target not in self.pages:
				self.created.append(target)
			self.pages[target] = str(info.get('url') or 'about:blank')

	def _handle(self, msg: dict) -> None:
		if 'id' in msg:
			reply = self._replies.pop(msg['id'], None)
			if reply is not None and 'result' in msg:
				reply(msg['result'])
			return
		method, params, session = msg.get('method'), msg.get('params') or {}, msg.get('sessionId')
		try:
			if method == 'Target.attachedToTarget':
				self._attached_to(params, parent=session)
			elif method == 'Target.detachedFromTarget':
				with self._state:
					sid = str(params.get('sessionId'))
					self._sessions.pop(sid, None)
					self._scripts.pop(sid, None)
					self._contexts.pop(sid, None)
			elif method in ('Target.targetCreated', 'Target.targetInfoChanged'):
				info = params.get('targetInfo') or {}
				if info.get('type') == 'page':
					self._page_seen(info)
			elif method == 'Target.targetDestroyed':
				with self._state:
					target = str(params.get('targetId'))
					self.pages.pop(target, None)
					self._attached.discard(target)
			elif method == 'Runtime.executionContextCreated' and session:
				ctx = params.get('context') or {}
				if (ctx.get('auxData') or {}).get('isDefault'):
					with self._state:
						self._contexts.setdefault(session, set()).add(int(ctx['id']))
			elif method == 'Runtime.executionContextDestroyed' and session:
				with self._state:
					self._contexts.get(session, set()).discard(int(params.get('executionContextId', -1)))
			elif method == 'Runtime.executionContextsCleared' and session:
				with self._state:
					self._contexts.pop(session, None)
			elif method == 'Runtime.bindingCalled' and params.get('name') == '__jobapplyBlocked' and session:
				page = self._sessions.get(session, '')
				logger.info('Submit guard blocked "%s"', params.get('payload', ''))
				if self.on_block:
					self.on_block(page, str(params.get('payload', '')))
		except Exception as e:  # a closed tab answers nothing; the guard keeps running for the others
			logger.debug('submit guard: %s on %s', e, method)

	def _attached_to(self, params: dict, parent: str | None) -> None:
		session = params['sessionId']
		info = params.get('targetInfo') or {}
		target, kind = str(info.get('targetId')), info.get('type')
		if kind not in ('page', 'iframe'):  # workers, extensions: nothing to guard; let them run
			with contextlib.suppress(Exception):
				self._send('Runtime.runIfWaitingForDebugger', session)
				self._send('Target.detachFromTarget', sessionId=session)
			return
		with self._state:
			page = target if kind == 'page' else self._sessions.get(parent or '', '')
			if kind == 'page':
				self._attached.add(target)
				self._page_seen(info)
			self._sessions[session] = page
			released = page in self.released
		try:
			self._send('Page.enable', session)  # without it Chrome skips the new-document script on some navigations
			if not released:
				self._send(
					'Page.addScriptToEvaluateOnNewDocument', session, reply=self._script_added(session), source=self.source
				)
				self._send('Runtime.addBinding', session, name='__jobapplyBlocked')
				self._send('Runtime.evaluate', session, expression=self.source)  # the document already loaded
			self._send('Runtime.enable', session)
			self._send(
				'Target.setAutoAttach',
				session,
				autoAttach=True,
				waitForDebuggerOnStart=True,
				flatten=True,
				filter=[{'type': 'iframe', 'exclude': False}],
			)
		finally:
			self._send('Runtime.runIfWaitingForDebugger', session)

	def _script_added(self, session: str) -> Callable[[dict], None]:
		def done(result: dict) -> None:
			with self._state:
				if session in self._sessions:
					self._scripts[session] = result.get('identifier', '')

		return done

	def _unlock(self, session: str) -> None:
		expression = f'window[{self.key!r}] && (window[{self.key!r}].locked = false)'
		with contextlib.suppress(Exception):
			if identifier := self._scripts.pop(session, None):
				self._send('Page.removeScriptToEvaluateOnNewDocument', session, identifier=identifier)
			self._send('Runtime.evaluate', session, expression=expression)
			for ctx in list(self._contexts.get(session, ())):
				self._send('Runtime.evaluate', session, expression=expression, contextId=ctx)
