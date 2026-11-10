"""The user's profile.md and criteria.md, and the questions the agent could not answer from them.

An unanswered question never stops the loop: the agent leaves that field empty, the question goes to questions.json,
and the user answers it in the app. The answer is appended to profile.md under "## Saved answers", so every later
application reads it."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

SAVED_ANSWERS = '## Saved answers'
MAX_PROFILE_CHARS = 60_000  # a profile is a few pages; this keeps a pasted CV dump from filling the model's context

STARTER_PROFILE = """# Profile

## Contact
- Full name:
- Email:
- Phone:
- City, country:
- LinkedIn:
- Portfolio / GitHub:

## Work authorization
- Authorized to work in:
- Needs visa sponsorship (yes/no):
- Notice period / earliest start date:

## Summary
Two or three sentences about you.

## Experience
### Job title, Company (Start - End)
- What you did, with numbers.

## Education
### Degree, School (Year)

## Skills
-

## Projects
### Project name
- What it is and what you built.

## Languages
-

## Standard answers
- Desired salary:
- Willing to relocate:
- How did you hear about us: Job board
- Gender / race / veteran / disability (optional, "Prefer not to say" is fine):

## Saved answers
"""

STARTER_CRITERIA = """# Job criteria

- Titles: (e.g. Software Engineer, Backend Engineer)
- Seniority: (e.g. junior, mid)
- Locations: (cities, countries, or Remote)
- Remote: (remote only / hybrid ok / on-site ok)
- Minimum salary:
- Job type: (full-time, contract, internship)
- Must have keywords:
- Avoid keywords / companies:
- Max years of experience required:
- Cover letter tone: (e.g. formal / friendly / concise; leave empty to use the app setting)
"""


def read_text(path: str | Path, what: str) -> str:
	if not path:
		raise ValueError(f'Choose your {what} file in Setup first.')
	p = Path(path).expanduser()
	try:
		text = p.read_text(encoding='utf-8-sig')
	except FileNotFoundError:
		raise ValueError(f'Your {what} file is missing: {p}') from None
	if not text.strip():
		raise ValueError(f'Your {what} file is empty: {p}')
	if len(text) > MAX_PROFILE_CHARS:
		raise ValueError(f'Your {what} file is {len(text):,} characters; keep it under {MAX_PROFILE_CHARS:,}.')
	return text


def tone_from_criteria(criteria: str) -> str | None:
	"""'- Cover letter tone: friendly' in criteria.md wins over the app setting."""
	m = re.search(r'^\s*[-*]?\s*cover letter tone\s*:\s*(.+?)\s*$', criteria, re.I | re.M)
	value = m and m.group(1).strip()
	return value if value and not value.startswith('(') else None


def save_answer(profile_path: str | Path, question: str, answer: str) -> None:
	"""Append the answer under '## Saved answers' (the heading is added when missing)."""
	p = Path(profile_path).expanduser()
	text = p.read_text(encoding='utf-8-sig')
	line = f'- Q: {_one_line(question)}\n  A: {_one_line(answer)}\n'
	if SAVED_ANSWERS not in text:
		text = text.rstrip('\n') + f'\n\n{SAVED_ANSWERS}\n'
	text = text.rstrip('\n') + '\n' + line
	p.write_text(text, encoding='utf-8')


def _one_line(text: str) -> str:
	return ' '.join(str(text).split())


def _norm(question: str) -> str:
	return re.sub(r'[^a-z0-9]+', ' ', question.lower()).strip()


@dataclass
class Question:
	question: str
	field_hint: str = ''
	company: str = ''
	title: str = ''
	url: str = ''
	id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
	asked_at: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d %H:%M'))
	answer: str = ''
	jobs: int = 1  # how many applications asked it


class Questions:
	"""questions.json: open questions (answer == '') and answered ones (kept for the history)."""

	def __init__(self, path: Path):
		self.path = path
		self._lock = threading.Lock()

	def all(self) -> list[Question]:
		try:
			data = json.loads(self.path.read_text(encoding='utf-8'))
		except (OSError, ValueError):
			return []
		return [Question(**q) for q in data]

	def open(self) -> list[Question]:
		return [q for q in self.all() if not q.answer]

	def _write(self, questions: list[Question]) -> None:
		tmp = self.path.with_suffix('.tmp')
		tmp.write_text(json.dumps([asdict(q) for q in questions], indent=2), encoding='utf-8')
		tmp.replace(self.path)

	def ask(self, question: Question) -> Question:
		"""The same question asked again (another job) is one entry, counted."""
		with self._lock:
			questions = self.all()
			for q in questions:
				if not q.answer and _norm(q.question) == _norm(question.question):
					q.jobs += 1
					self._write(questions)
					return q
			questions.append(question)
			self._write(questions)
			return question

	def answer(self, question_id: str, answer: str, profile_path: str | Path) -> Question:
		if not answer.strip():
			raise ValueError('The answer is empty.')
		with self._lock:
			questions = self.all()
			q = next((q for q in questions if q.id == question_id), None)
			if q is None:
				raise ValueError('That question is gone.')
			save_answer(profile_path, q.question, answer)
			q.answer = answer.strip()
			self._write(questions)
			return q
