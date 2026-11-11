"""applications.csv: one row per job the loop looked at. It is the daily cap's count and the duplicate check."""

from __future__ import annotations

import csv
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# A job counts toward the daily cap once the loop worked on it; a job skipped for not matching the criteria does not.
COUNTED = frozenset({'filled', 'email_drafted', 'needs_input', 'submitted'})
STATUSES = COUNTED | {'skipped', 'failed'}  # submitted: the user marked it done (it is never applied to again)
MAX_FAILED = 2  # attempts per job: after that a failing posting counts as seen (a board may keep listing it)
# A cell starting with one of these is a formula in Excel: written with a leading apostrophe, read back without it
FORMULA_START = ('=', '+', '-', '@', '\t', '\r')
# query parameters that identify a job on the boards; every other one (utm_*, refId, trackingId, ...) is dropped
ID_PARAMS = frozenset({'jk', 'vjk', 'currentjobid', 'gh_jid', 'jobid', 'job_id', 'id', 'token', 'lever-source'})


def job_key(url: str) -> str:
	"""The same job seen through different links (tracking parameters, a trailing slash, http/https) is one key."""
	parts = urlsplit(url.strip())
	query = sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() in ID_PARAMS and k.lower() != 'lever-source')
	host = parts.netloc.lower().removeprefix('www.')
	path = parts.path.rstrip('/') or '/'
	return urlunsplit(('https', host, path, urlencode(query), ''))


@dataclass
class Row:
	date: str = ''
	time: str = ''
	company: str = ''
	title: str = ''
	url: str = ''
	board: str = ''
	status: str = ''
	tab_id: str = ''
	resume: str = ''
	cover_letter: str = ''
	unanswered_questions: str = ''
	notes: str = ''
	check: str = ''  # the final check: required fields still empty, error messages on the form
	screenshot: str = ''  # the form as Jobify left it (PNG)


COLUMNS = [f.name for f in fields(Row)]


class Tracker:
	def __init__(self, path: Path):
		self.path = path
		self._lock = threading.Lock()

	def rows(self) -> list[Row]:
		try:
			with self.path.open(newline='', encoding='utf-8-sig') as f:
				return [Row(**{k: _from_cell(r.get(k) or '') for k in COLUMNS}) for r in csv.DictReader(f)]
		except FileNotFoundError:
			return []

	def add(self, row: Row) -> Row:
		if row.status not in STATUSES:
			raise ValueError(f'status must be one of {", ".join(sorted(STATUSES))}, not {row.status!r}')
		now = datetime.now()
		row.date, row.time = row.date or now.strftime('%Y-%m-%d'), row.time or now.strftime('%H:%M')
		with self._lock:
			self._upgrade_header()
			new = not self.path.exists() or self.path.stat().st_size == 0
			# utf-8-sig on a new file: Excel on Windows reads it as UTF-8 (accented names stay readable)
			with self.path.open('a', newline='', encoding='utf-8-sig' if new else 'utf-8') as f:
				writer = csv.DictWriter(f, fieldnames=COLUMNS)
				if new:
					writer.writeheader()
				writer.writerow({k: _to_cell(v) for k, v in asdict(row).items()})
		return row

	def _upgrade_header(self) -> None:
		"""A file from an older version (fewer columns) is rewritten with the current header before anything is
		appended; appending under the old header would shift every column."""
		try:
			with self.path.open(newline='', encoding='utf-8-sig') as f:
				header = next(csv.reader(f), None)
		except FileNotFoundError:
			return
		if header is None or header == COLUMNS:
			return
		rows = self.rows()
		tmp = self.path.with_suffix('.tmp')
		with tmp.open('w', newline='', encoding='utf-8-sig') as f:
			writer = csv.DictWriter(f, fieldnames=COLUMNS)
			writer.writeheader()
			writer.writerows({k: _to_cell(v) for k, v in asdict(r).items()} for r in rows)
		tmp.replace(self.path)

	def latest(self, url: str) -> Row | None:
		"""The newest row for this job (any link to it)."""
		key = job_key(url)
		return next((r for r in reversed(self.rows()) if r.url and job_key(r.url) == key), None)

	def update(self, url: str, **values: str) -> Row:
		"""Change the newest row of this job in place (a refill, or the user marking it submitted)."""
		if 'status' in values and values['status'] not in STATUSES:
			raise ValueError(f'status must be one of {", ".join(sorted(STATUSES))}, not {values["status"]!r}')
		key = job_key(url)
		with self._lock:
			rows = self.rows()
			index = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].url and job_key(rows[i].url) == key), None)
			if index is None:
				raise ValueError('That job is not in the list.')
			for name, value in values.items():
				if name not in COLUMNS:
					raise ValueError(f'unknown column {name!r}')
				setattr(rows[index], name, value)
			tmp = self.path.with_suffix('.tmp')
			with tmp.open('w', newline='', encoding='utf-8-sig') as f:
				writer = csv.DictWriter(f, fieldnames=COLUMNS)
				writer.writeheader()
				writer.writerows({k: _to_cell(v) for k, v in asdict(r).items()} for r in rows)
			tmp.replace(self.path)
			return rows[index]

	def count_today(self, today: str | None = None) -> int:
		today = today or datetime.now().strftime('%Y-%m-%d')
		return sum(1 for r in self.rows() if r.date == today and r.status in COUNTED)

	def seen(self, url: str) -> bool:
		"""Done, skipped or in progress before; or failed MAX_FAILED times (a failed job gets one more try)."""
		key = job_key(url)
		failed = 0
		for r in self.rows():
			if r.url and job_key(r.url) == key:
				if r.status != 'failed':
					return True
				failed += 1
		return failed >= MAX_FAILED


def _to_cell(value: str) -> str:
	return "'" + value if value.startswith(FORMULA_START) else value


def _from_cell(value: str) -> str:
	return value[1:] if value.startswith("'") and value[1:].startswith(FORMULA_START) else value
