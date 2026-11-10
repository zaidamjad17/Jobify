"""applications.csv: one row per job the loop looked at. It is the daily cap's count and the duplicate check."""

from __future__ import annotations

import csv
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# A job counts toward the daily cap once the loop worked on it; a job skipped for not matching the criteria does not.
COUNTED = frozenset({'filled', 'email_drafted', 'needs_input'})
STATUSES = COUNTED | {'skipped', 'failed'}
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


COLUMNS = [f.name for f in fields(Row)]


class Tracker:
	def __init__(self, path: Path):
		self.path = path
		self._lock = threading.Lock()

	def rows(self) -> list[Row]:
		try:
			with self.path.open(newline='', encoding='utf-8-sig') as f:
				return [Row(**{k: r.get(k) or '' for k in COLUMNS}) for r in csv.DictReader(f)]
		except FileNotFoundError:
			return []

	def add(self, row: Row) -> Row:
		if row.status not in STATUSES:
			raise ValueError(f'status must be one of {", ".join(sorted(STATUSES))}, not {row.status!r}')
		now = datetime.now()
		row.date, row.time = row.date or now.strftime('%Y-%m-%d'), row.time or now.strftime('%H:%M')
		with self._lock:
			new = not self.path.exists() or self.path.stat().st_size == 0
			# utf-8-sig on a new file: Excel on Windows reads it as UTF-8 (accented names stay readable)
			with self.path.open('a', newline='', encoding='utf-8-sig' if new else 'utf-8') as f:
				writer = csv.DictWriter(f, fieldnames=COLUMNS)
				if new:
					writer.writeheader()
				writer.writerow(asdict(row))
		return row

	def count_today(self, today: str | None = None) -> int:
		today = today or datetime.now().strftime('%Y-%m-%d')
		return sum(1 for r in self.rows() if r.date == today and r.status in COUNTED)

	def seen(self, url: str) -> bool:
		key = job_key(url)
		return any(job_key(r.url) == key for r in self.rows() if r.url and r.status != 'failed')
