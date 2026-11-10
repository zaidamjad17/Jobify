"""After a job: answers the profile does not give are taken back.

A model can type a salary, a start date or "Yes, authorized to work" that is not in the profile (seen live with a
flash-lite model despite the instructions). For each filled field whose label is about one of TOPICS while the profile
states nothing on that topic, the field is emptied in the page and its label comes back as a question for the user."""

from __future__ import annotations

import json
import re

from jobapply.cdp import CDP

# topic -> (label pattern in the form, pattern for a profile line that answers it)
TOPICS: dict[str, tuple[str, str]] = {
	'salary': (r'salary|compensation|pay expectation|expected pay|desired pay|pay range|hourly rate', r'salary|compensation|pay'),
	'notice': (
		r'notice period|start date|available to start|earliest start|when can you start',
		r'notice|start date|available|start',
	),
	'relocation': (r'relocat', r'relocat'),
	'sponsorship': (r'sponsor|visa', r'sponsor|visa'),
	'authorization': (
		r'authori[sz]ed to work|work authori[sz]ation|right to work|eligible to work|work permit|legally (?:able|allowed)',
		r'authori[sz]|right to work|eligible to work|work permit|citizen|permanent resident',
	),
}

AUDIT_JS = r"""
((topics) => {
	const labelOf = el => {
		const parts = [];
		if (el.id) document.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]').forEach(l => parts.push(l.innerText));
		const wrap = el.closest('label'); if (wrap) parts.push(wrap.innerText);
		for (const id of (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)) {
			const l = document.getElementById(id); if (l) parts.push(l.innerText);
		}
		parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '');
		if (!parts.join('').trim()) {  // a question in the element just before the field's container
			const box = el.closest('div, fieldset, li'); const prev = box && box.previousElementSibling;
			if (prev) parts.push(prev.innerText);
		}
		return parts.join(' ').replace(/\s+/g, ' ').trim().slice(0, 300);
	};
	const cleared = [];
	for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=submit]):not([type=button]), textarea, select')) {
		const checkable = el.type === 'checkbox' || el.type === 'radio';
		if (checkable ? !el.checked : !el.value) continue;
		const label = labelOf(el);
		const topic = Object.keys(topics).find(t => new RegExp(topics[t], 'i').test(label));
		if (!topic) continue;
		if (checkable) el.checked = false; else el.value = '';
		el.dispatchEvent(new Event('input', { bubbles: true }));
		el.dispatchEvent(new Event('change', { bubbles: true }));
		cleared.push({ topic, label });
	}
	return JSON.stringify(cleared);
})
"""


def answered(profile: str, pattern: str) -> bool:
	"""A profile line on the topic with a value: 'Desired salary: 70k', or a saved answer whose question names it."""
	lines = profile.splitlines()
	for i, line in enumerate(lines):
		if not re.search(pattern, line, re.I):
			continue
		if line.strip().lstrip('-* ').startswith('Q:'):  # saved answer: the next line holds it
			nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
			if nxt.startswith('A:') and nxt[2:].strip():
				return True
			continue
		value = line.split(':', 1)[1].strip() if ':' in line else ''
		if value and not value.startswith('('):
			return True
	return False


def missing_topics(profile: str) -> dict[str, str]:
	"""topic -> label pattern, for the topics the profile does not answer."""
	return {topic: label for topic, (label, answer) in TOPICS.items() if not answered(profile, answer)}


def take_back(ws_url: str, tabs: list[str], profile: str) -> list[dict]:
	"""Empty fields in these tabs that answer a topic the profile does not. Returns [{'topic', 'label'}]."""
	topics = missing_topics(profile)
	if not topics or not tabs:
		return []
	cleared: list[dict] = []
	with CDP(ws_url) as cdp:
		for tab in tabs:
			try:
				session = cdp.send('Target.attachToTarget', targetId=tab, flatten=True)['sessionId']
				result = cdp.send('Runtime.evaluate', session, expression=f'{AUDIT_JS}({json.dumps(topics)})', returnByValue=True)
				cleared += json.loads(result.get('result', {}).get('value') or '[]')
				cdp.send('Target.detachFromTarget', sessionId=session)
			except Exception:  # a tab closed meanwhile
				continue
	return cleared


# contact field -> (label pattern in the form, profile line name pattern, how values compare)
CONTACTS: dict[str, tuple[str, str, str]] = {
	'phone': (r'phone|mobile|telephone|cell', r'phone|mobile|telephone', 'digits'),
	'email': (r'e-?mail', r'e-?mail', 'text'),
	'linkedin': (r'linkedin', r'linkedin', 'url'),
	'github': (r'github', r'github', 'url'),
	'website': (r'portfolio|website|personal site', r'portfolio|website', 'url'),
}

# the profile's "Full name:" line fills these (first word / the rest / all of it)
NAME_LABELS = {
	'first_name': r'first name|given name|forename',
	'last_name': r'last name|surname|family name',
	'full_name': r'full name|legal name|your name',
}

CONTACT_JS = r"""
((contacts) => {
	const labelOf = el => {
		const parts = [];
		if (el.id) document.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]').forEach(l => parts.push(l.innerText));
		const wrap = el.closest('label'); if (wrap) parts.push(wrap.innerText);
		parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '', el.name || '');
		return parts.join(' ').replace(/\s+/g, ' ').trim().slice(0, 200);
	};
	const norm = (v, how) => how === 'digits' ? v.replace(/\D/g, '')
		: how === 'url' ? v.toLowerCase().replace(/^https?:\/\//, '').replace(/^www\./, '').replace(/\/+$/, '')
		: v.trim().toLowerCase();
	const same = (a, b, how) => {
		const x = norm(a, how), y = norm(b, how);
		if (how === 'digits') return x === y || (x.length >= 7 && y.endsWith(x));  // typed without the country code
		return x === y;
	};
	const fixed = [];
	for (const el of document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]), textarea')) {
		if (!el.value) continue;
		const label = labelOf(el);
		const kind = Object.keys(contacts).find(k => new RegExp(contacts[k].label, 'i').test(label));
		if (!kind || same(el.value, contacts[kind].value, contacts[kind].how)) continue;
		const before = el.value;
		const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;  // React-controlled inputs too
		setter.call(el, contacts[kind].value);
		el.dispatchEvent(new Event('input', { bubbles: true }));
		el.dispatchEvent(new Event('change', { bubbles: true }));
		fixed.push({ kind, before, after: contacts[kind].value });
	}
	return JSON.stringify(fixed);
})
"""


def profile_contacts(profile: str) -> dict[str, dict[str, str]]:
	"""kind -> {'label', 'value', 'how'} for the contact details the profile gives."""
	found: dict[str, dict[str, str]] = {}
	for line in profile.splitlines():
		m = re.match(r'^\s*[-*]?\s*([^:]{1,40}):\s*(\S.*?)\s*$', line)
		if not m or m.group(2).startswith('('):
			continue
		for kind, (label, name, how) in CONTACTS.items():
			if kind not in found and re.search(name, m.group(1), re.I):
				found[kind] = {'label': label, 'value': m.group(2), 'how': how}
		if 'full_name' not in found and re.fullmatch(r'(full |legal )?name', m.group(1).strip(), re.I):
			first, _, last = m.group(2).partition(' ')
			found['full_name'] = {'label': NAME_LABELS['full_name'], 'value': m.group(2), 'how': 'text'}
			found['first_name'] = {'label': NAME_LABELS['first_name'], 'value': first, 'how': 'text'}
			if last:
				found['last_name'] = {'label': NAME_LABELS['last_name'], 'value': last, 'how': 'text'}
	return found


def correct_contacts(ws_url: str, tabs: list[str], profile: str) -> list[dict]:
	"""Put the profile's phone / email / LinkedIn / GitHub / website back where the page holds another value."""
	contacts = profile_contacts(profile)
	if not contacts or not tabs:
		return []
	fixed: list[dict] = []
	with CDP(ws_url) as cdp:
		for tab in tabs:
			try:
				session = cdp.send('Target.attachToTarget', targetId=tab, flatten=True)['sessionId']
				result = cdp.send(
					'Runtime.evaluate', session, expression=f'{CONTACT_JS}({json.dumps(contacts)})', returnByValue=True
				)
				fixed += json.loads(result.get('result', {}).get('value') or '[]')
				cdp.send('Target.detachFromTarget', sessionId=session)
			except Exception:
				continue
	return fixed
