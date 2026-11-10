"""A small Mustache renderer for resume templates: {{name}} (HTML-escaped), {{{name}}} (raw), {{#list}}...{{/list}}
(repeat for each item, or show when truthy), {{^name}}...{{/name}} (show when empty or false), {{.}} (the current item)
and dotted names ({{contact.email}}). Enough for a resume; no partials, lambdas or delimiter changes."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

_TAG = re.compile(r'\{\{(\{)?\s*([#^/]?)\s*([\w.\-]+|\.)\s*\}?\}\}')


class TemplateError(ValueError):
	pass


def render(template: str, data: Any) -> str:
	tokens = _parse(template)
	return _render(tokens, [data])


def _parse(template: str) -> list:
	"""-> nested tokens: str | ('var', name, raw) | ('section', name, inverted, children)."""
	root: list = []
	stack: list[tuple[str, list]] = [('', root)]
	pos = 0
	for m in _TAG.finditer(template):
		stack[-1][1].append(template[pos : m.start()])
		pos = m.end()
		raw, kind, name = bool(m.group(1)), m.group(2), m.group(3)
		if kind in ('#', '^'):
			children: list = []
			stack[-1][1].append(('section', name, kind == '^', children))
			stack.append((name, children))
		elif kind == '/':
			if len(stack) == 1 or stack[-1][0] != name:
				raise TemplateError(f'{{{{/{name}}}}} closes a section that is not open')
			stack.pop()
		else:
			stack[-1][1].append(('var', name, raw))
	if len(stack) > 1:
		raise TemplateError(f'{{{{#{stack[-1][0]}}}}} is never closed')
	stack[-1][1].append(template[pos:])
	return root


def _lookup(stack: list, name: str) -> Any:
	if name == '.':
		return stack[-1]
	head, *rest = name.split('.')
	for ctx in reversed(stack):
		if isinstance(ctx, Mapping) and head in ctx:
			value = ctx[head]
			break
		# a str section ({{#company}}) must not answer {{title}} with str.title
		if not isinstance(ctx, (Mapping, str, int, float, bool, list, tuple)) and hasattr(ctx, head):
			value = getattr(ctx, head)
			break
	else:
		return None
	for part in rest:
		if isinstance(value, (list, tuple)) and part.isdigit():  # {{#experience.0}}: the list is not empty
			value = value[int(part)] if int(part) < len(value) else None
		elif isinstance(value, Mapping):
			value = value.get(part)
		else:
			value = getattr(value, part, None)
	return value


def _render(tokens: list, stack: list) -> str:
	out: list[str] = []
	for token in tokens:
		if isinstance(token, str):
			out.append(token)
		elif token[0] == 'var':
			value = _lookup(stack, token[1])
			text = '' if value is None or value is False else str(value)
			out.append(text if token[2] else html.escape(text))
		else:
			_, name, inverted, children = token
			value = _lookup(stack, name)
			empty = not value
			if inverted:
				if empty:
					out.append(_render(children, stack))
			elif isinstance(value, (list, tuple)):
				out.extend(_render(children, [*stack, item]) for item in value)
			elif not empty:
				out.append(_render(children, stack if value is True else [*stack, value]))
	return ''.join(out)
