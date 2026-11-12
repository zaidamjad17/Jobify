"""A record per job, for the History detail view: what the job was about, what Jobify filled in and attached, and
what happened while it worked on it. Kept as jobs/<key>.json in the app folder (applications.csv stays the index).

{url, company, title, board, about, summary: {about, responsibilities, requirements, location, salary,
employment_type, why_match}, job_description, documents: {resume_pdf, cover_letter_pdf, folder, cover_letter_text,
email_subject}, submitted: {fields: [{label, value, kind}], files, page_url, page_title}, timeline: [{time, text,
level}], updated_at}"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from jobapply.paths import sub, write_text_atomic
from jobapply.tracker import job_key

MAX_TIMELINE = 300  # lines kept per job
MAX_DESCRIPTION = 30_000

_lock = threading.Lock()


def path_for(url: str) -> Path:
	return sub('jobs') / f'{hashlib.sha1(job_key(url).encode()).hexdigest()[:16]}.json'


def load(url: str) -> dict[str, Any]:
	"""The job's record, or {} when there is none (a job from before records existed)."""
	if not url:
		return {}
	try:
		data = json.loads(path_for(url).read_text(encoding='utf-8'))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def update(url: str, timeline: list[dict] | None = None, **fields: Any) -> dict[str, Any]:
	"""Merge fields into the record (dicts are merged one level deep; empty values never replace saved ones) and add
	timeline lines."""
	with _lock:
		record = load(url)
		record['url'] = record.get('url') or url
		for key, value in fields.items():
			if value in (None, '', [], {}):
				continue
			if key == 'job_description' and isinstance(value, str):
				value = value[:MAX_DESCRIPTION]
			if isinstance(value, dict) and isinstance(record.get(key), dict):
				merged = dict(record[key])
				merged.update({k: v for k, v in value.items() if v not in (None, '', [], {})})
				record[key] = merged
			else:
				record[key] = value
		if timeline:
			record['timeline'] = [*record.get('timeline', []), *timeline][-MAX_TIMELINE:]
		record['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		write_text_atomic(path_for(url), json.dumps(record, indent=2, ensure_ascii=False))
		return record
