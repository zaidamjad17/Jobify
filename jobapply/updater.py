"""Updates from inside the app: new versions are GitHub releases tagged jobapply-v<version> with the Windows installer
(Jobify-Setup-<version>.exe) attached; .github/workflows/jobapply-windows.yml publishes one when jobapply/__init__.py
gets a new __version__ on main.

check() finds the newest release above the running version, download() fetches its installer (checked against the
SHA-256 GitHub lists for the asset), and install() starts it silently and lets the app close: the installer replaces
the program files, keeps the data folder (paths.app_home()) and starts the new version."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from jobapply import __version__
from jobapply.paths import sub

REPO = 'zaidamjad17/Jobify'
TAG_PREFIX = 'jobapply-v'
API = 'https://api.github.com'
TIMEOUT_S = 20


class UpdateError(RuntimeError):
	pass


@dataclass
class Release:
	version: str
	tag: str
	notes: str
	url: str  # the installer's download URL
	name: str
	size: int
	sha256: str  # '' when GitHub lists no digest
	page: str  # the release page, for the user


def parse_version(text: str) -> tuple[int, ...] | None:
	"""'1.2.10' -> (1, 2, 10); a pre-release or anything else -> None (never offered)."""
	if not re.fullmatch(r'\d+(\.\d+){0,3}', text):
		return None
	return tuple(int(p) for p in text.split('.'))


def check(current: str = __version__, repo: str = REPO, client: httpx.Client | None = None) -> Release | None:
	"""The newest published release with a Windows installer above the running version, or None."""
	own = client is None
	client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
	try:
		response = client.get(
			f'{API}/repos/{repo}/releases', params={'per_page': 30}, headers={'Accept': 'application/vnd.github+json'}
		)
	except httpx.HTTPError as e:
		raise UpdateError(f'Could not reach GitHub to check for updates: {e}') from None
	finally:
		if own:
			client.close()
	if response.status_code != 200:
		raise UpdateError(f'GitHub answered {response.status_code} when checking for updates.')
	running = parse_version(current) or (0,)
	best: tuple[tuple[int, ...], Release] | None = None
	for item in response.json():
		tag = str(item.get('tag_name') or '')
		version = parse_version(tag.removeprefix(TAG_PREFIX)) if tag.startswith(TAG_PREFIX) else None
		if version is None or item.get('draft') or item.get('prerelease') or version <= running:
			continue
		asset = next((a for a in item.get('assets') or [] if str(a.get('name', '')).lower().endswith('.exe')), None)
		if asset is None:  # the build has not attached the installer yet
			continue
		digest = str(asset.get('digest') or '')
		release = Release(
			version='.'.join(map(str, version)),
			tag=tag,
			notes=str(item.get('body') or '')[:4000],
			url=str(asset['browser_download_url']),
			name=str(asset['name']),
			size=int(asset.get('size') or 0),
			sha256=digest.removeprefix('sha256:') if digest.startswith('sha256:') else '',
			page=str(item.get('html_url') or ''),
		)
		if best is None or version > best[0]:
			best = (version, release)
	return best[1] if best else None


def download(release: Release, progress: Callable[[int, int], Any] | None = None, client: httpx.Client | None = None) -> Path:
	"""The installer in the app's updates folder, after its size and SHA-256 matched what GitHub lists."""
	folder = sub('updates')
	name = re.sub(r'[^\w.\-]+', '_', release.name) or 'Jobify-Setup.exe'
	target = folder / name
	partial = target.with_suffix('.part')
	own = client is None
	client = client or httpx.Client(timeout=httpx.Timeout(60, connect=TIMEOUT_S), follow_redirects=True)
	sha = hashlib.sha256()
	done = 0
	try:
		with client.stream('GET', release.url) as response:
			if response.status_code != 200:
				raise UpdateError(f'Download failed: GitHub answered {response.status_code}.')
			total = int(response.headers.get('content-length') or release.size or 0)
			with partial.open('wb') as f:
				for chunk in response.iter_bytes(1 << 16):
					f.write(chunk)
					sha.update(chunk)
					done += len(chunk)
					if progress:
						progress(done, total)
	except httpx.HTTPError as e:
		partial.unlink(missing_ok=True)
		raise UpdateError(f'Download failed: {e}') from None
	finally:
		if own:
			client.close()
	if (release.size and done != release.size) or (release.sha256 and sha.hexdigest() != release.sha256.lower()):
		partial.unlink(missing_ok=True)
		raise UpdateError('The downloaded installer does not match the release (size or checksum); nothing was installed.')
	partial.replace(target)
	return target


def install(installer: Path, popen: Callable[..., Any] = subprocess.Popen) -> None:
	"""Start the installer silently; the caller then closes the app. Inno Setup closes what is still running, replaces
	the program files, keeps the data folder, and starts the new version."""
	if sys.platform != 'win32' and popen is subprocess.Popen:
		raise UpdateError('Updating from the app works on Windows; on this system install the new version by hand.')
	args = [str(installer), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS']
	flags = 0
	if sys.platform == 'win32':  # the installer must outlive the app it replaces
		flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
	popen(args, creationflags=flags, close_fds=True, cwd=os.path.dirname(installer) or None)


def as_dict(release: Release | None) -> dict | None:
	return asdict(release) if release else None
