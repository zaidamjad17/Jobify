"""A minimal synchronous Chrome DevTools Protocol client on its own WebSocket connection to the browser."""

from __future__ import annotations

import base64
import contextlib
import json
import time
from pathlib import Path
from typing import Any


class CDPError(RuntimeError):
	pass


class CDP:
	def __init__(self, ws_url: str, timeout: float = 30):
		from websockets.sync.client import connect

		self.timeout = timeout
		self._ws = connect(ws_url, max_size=None, open_timeout=timeout, close_timeout=1)
		self._id = 0

	def __enter__(self) -> CDP:
		return self

	def __exit__(self, *exc: object) -> None:
		self.close()

	def close(self) -> None:
		with contextlib.suppress(Exception):
			self._ws.close()

	def send(self, method: str, session_id: str | None = None, **params: Any) -> dict:
		"""Send a command and wait for its reply; events that arrive meanwhile are dropped."""
		self._id += 1
		msg: dict[str, Any] = {'id': self._id, 'method': method, 'params': params}
		if session_id:
			msg['sessionId'] = session_id
		self._ws.send(json.dumps(msg))
		deadline = time.monotonic() + self.timeout
		while True:
			left = deadline - time.monotonic()
			if left <= 0:
				raise CDPError(f'{method}: no answer within {self.timeout:g}s')
			reply = json.loads(self._ws.recv(timeout=left))
			if reply.get('id') == self._id:
				if 'error' in reply:
					raise CDPError(f'{method}: {reply["error"].get("message", reply["error"])}')
				return reply.get('result') or {}


def print_pdf(ws_url: str, html_file: Path, pdf_file: Path) -> Path:
	"""Open html_file in a background tab of the browser, print it to pdf_file (A4, backgrounds on), close the tab."""
	with CDP(ws_url, timeout=60) as cdp:
		target = cdp.send('Target.createTarget', url='about:blank', background=True)['targetId']
		try:
			session = cdp.send('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
			cdp.send('Page.enable', session)
			cdp.send('Page.navigate', session, url=html_file.resolve().as_uri())
			deadline = time.monotonic() + 30
			while time.monotonic() < deadline:
				state = cdp.send('Runtime.evaluate', session, expression='document.readyState', returnByValue=True)
				if state.get('result', {}).get('value') == 'complete':
					break
				time.sleep(0.1)
			cdp.send('Runtime.evaluate', session, expression='document.fonts.ready.then(() => 1)', awaitPromise=True)
			data = cdp.send(
				'Page.printToPDF', session, printBackground=True, preferCSSPageSize=True, paperWidth=8.27, paperHeight=11.69
			)['data']
		finally:
			with contextlib.suppress(Exception):
				cdp.send('Target.closeTarget', targetId=target)
	pdf_file.write_bytes(base64.b64decode(data))
	return pdf_file
