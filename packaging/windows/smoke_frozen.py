"""CI check for the packaged app: run inside JobApply.exe (`JobApply.exe -c "..."`), an Agent with a scripted model drives
a local headless browser. The REPL worker and the browser-harness daemon are started through the exe itself
(jobapply/frozen.py), so this fails when the bundle misses a module or package metadata they need.

Usage: JobApply.exe -c "import runpy, sys; sys.argv = ['smoke', r'<chrome.exe>']; runpy.run_path(r'<this file>', run_name='__main__')"
"""

import asyncio
import sys

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from browser_use import Agent, Browser

TURNS = [
	('python', {'code': "new_tab('data:text/html,<h1>frozen ok</h1>'); print(js('document.querySelector(\"h1\").textContent'))"}),
	('finish', {'success': True, 'result': 'done'}),
]


async def main() -> None:
	seen = []

	def fn(messages, info):
		seen.append(messages)
		name, args = TURNS[len(seen) - 1]
		return ModelResponse(parts=[ToolCallPart(name, args)])

	browser = Browser('chromium', headless=True, executable_path=sys.argv[1] if len(sys.argv) > 1 else None)
	result = await Agent('smoke', FunctionModel(fn), browser=browser).run()
	ok = result.status == 'completed' and 'frozen ok' in str(seen[1][-1])
	print('frozen smoke', 'ok' if ok else f'FAILED: {result!r}')
	if not ok:
		raise SystemExit(1)


asyncio.run(main())
