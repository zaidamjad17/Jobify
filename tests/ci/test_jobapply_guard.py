"""JobApply's submit guard on a real headless Chrome: trusted clicks, keys and scripts cannot submit until release()."""

import contextlib
import time
import urllib.parse

import pytest

from jobapply.cdp import CDP, CDPError
from jobapply.guard import SubmitGuard

pytestmark = pytest.mark.chrome

PAGE = """<!doctype html><html><body>
<form id=f method=post action="javascript:void 0" onsubmit="window.submits++; return false">
<input id=name name=name>
<button type=button id=next onclick="window.nexts++">Next</button>
<button type=submit id=submit onclick="window.clicks++">Submit application</button>
<button type=button id=code onclick="window.codes++">Send code</button>
</form>
<script>window.submits = 0; window.nexts = 0; window.clicks = 0; window.codes = 0;</script>
</body></html>"""


def _open(cdp: CDP) -> tuple[str, str]:
	url = 'data:text/html,' + urllib.parse.quote(PAGE)
	target = cdp.send('Target.createTarget', url=url)['targetId']
	session = cdp.send('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
	for _ in range(100):
		try:
			ready = _eval(cdp, session, 'document.readyState === "complete" && typeof window.nexts === "number"')
		except CDPError:  # no execution context yet: the tab is still being created
			ready = False
		if ready:
			return target, session
		time.sleep(0.05)
	raise AssertionError('page did not load')


def _eval(cdp: CDP, session: str, expression: str):
	return cdp.send('Runtime.evaluate', session, expression=expression, returnByValue=True)['result'].get('value')


def _click(cdp: CDP, session: str, element: str) -> None:
	box = _eval(
		cdp,
		session,
		f'(r => [r.x + r.width / 2, r.y + r.height / 2])(document.getElementById("{element}").getBoundingClientRect())',
	)
	for kind in ('mousePressed', 'mouseReleased'):
		cdp.send('Input.dispatchMouseEvent', session, type=kind, x=box[0], y=box[1], button='left', clickCount=1)


def _counts(cdp: CDP, session: str) -> dict:
	return _eval(cdp, session, '({submits, nexts, clicks, codes})')


def test_guard_blocks_submit_until_released(chrome):
	blocked = []
	guard = SubmitGuard(chrome['ws'], on_block=lambda tab, text: blocked.append(text))
	guard.start()
	try:
		with CDP(chrome['ws']) as cdp:
			target, session = _open(cdp)
			_click(cdp, session, 'next')
			_click(cdp, session, 'code')
			_click(cdp, session, 'submit')
			cdp.send(
				'Input.dispatchKeyEvent',
				session,
				type='keyDown',
				key='Enter',
				code='Enter',
				modifiers=2,
				windowsVirtualKeyCode=13,
			)
			_eval(cdp, session, 'document.getElementById("submit").click()')
			_eval(cdp, session, 'document.getElementById("f").requestSubmit()')
			_eval(cdp, session, 'document.getElementById("f").submit()')
			assert _counts(cdp, session) == {'submits': 0, 'nexts': 1, 'clicks': 0, 'codes': 1}
			for _ in range(40):
				if blocked:
					break
				time.sleep(0.05)
			assert blocked and 'Submit application' in blocked[0]

			assert guard.release([target]) == [target]
			time.sleep(0.3)
			_click(cdp, session, 'submit')
			assert _counts(cdp, session)['clicks'] == 1
			assert _counts(cdp, session)['submits'] == 1

			other, other_session = _open(cdp)  # a tab opened after the release is locked
			_click(cdp, other_session, 'submit')
			assert _counts(cdp, other_session)['clicks'] == 0
			for t in (target, other):
				cdp.send('Target.closeTarget', targetId=t)
	finally:
		guard.close()


def test_guard_locks_new_document_before_page_scripts(chrome):
	"""A tab navigated after the guard started gets the guard before the page's own script runs."""
	guard = SubmitGuard(chrome['ws'])
	guard.start()
	try:
		with CDP(chrome['ws']) as cdp:
			target, session = _open(cdp)
			assert _eval(cdp, session, f'!!window[{guard.key!r}] && window[{guard.key!r}].locked') is True
			assert target in guard.tabs_since(0)
			cdp.send('Target.closeTarget', targetId=target)
	finally:
		guard.close()


def test_a_released_tab_can_be_locked_again(chrome):
	"""The agent goes back to a handed-over tab to fill answers the user gave: Submit is held again meanwhile."""
	guard = SubmitGuard(chrome['ws'])
	guard.start()
	try:
		with CDP(chrome['ws']) as cdp:
			target, session = _open(cdp)
			guard.release([target])
			time.sleep(0.3)
			_click(cdp, session, 'submit')
			assert _counts(cdp, session)['clicks'] == 1
			assert guard.lock([target]) == [target]
			time.sleep(0.3)
			_click(cdp, session, 'submit')
			assert _counts(cdp, session)['clicks'] == 1  # held
			cdp.send('Page.reload', session)  # a new document in that tab is locked too
			time.sleep(1)
			for _ in range(40):
				with contextlib.suppress(CDPError):
					if _eval(cdp, session, 'document.readyState === "complete" && typeof window.clicks === "number"'):
						break
				time.sleep(0.05)
			_click(cdp, session, 'submit')
			assert _counts(cdp, session)['clicks'] == 0
			cdp.send('Target.closeTarget', targetId=target)
	finally:
		guard.close()
