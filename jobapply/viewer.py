"""The documents Jobify made, as images for the in-app viewer (History: resume, cover letter, the form's screenshot).

PDF pages are rendered with pdfium (pypdfium2), so the user sees exactly the PDF that was attached, at any zoom."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from jobapply.paths import app_home

MAX_PAGES = 12  # a resume is 1-3 pages; this bounds a stray big file
MAX_BYTES = 25_000_000
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}


class ViewerError(ValueError):
	pass


def _allowed(path: str) -> Path:
	"""Only files inside the Jobify data folder: the page asks by path, and it never needs anything else."""
	if not path:
		raise ViewerError('There is no file for this yet.')
	p = Path(path).expanduser()
	try:
		resolved = p.resolve(strict=True)
	except (OSError, RuntimeError):
		raise ViewerError(f'The file is gone: {p.name}') from None
	home = app_home().resolve()
	if not resolved.is_relative_to(home):
		raise ViewerError('Only files in the Jobify data folder can be shown here; use Open instead.')
	if resolved.stat().st_size > MAX_BYTES:
		raise ViewerError('The file is too big to show here; use Open instead.')
	return resolved


def pages(path: str, scale: float = 2.0) -> dict:
	"""{'pages': [data URL, ...], 'width': px, 'height': px} (the first page's size at this scale)."""
	file = _allowed(path)
	suffix = file.suffix.lower()
	if suffix in IMAGE_TYPES:
		data = base64.b64encode(file.read_bytes()).decode()
		width, height = _image_size(file)
		return {'pages': [f'data:{IMAGE_TYPES[suffix]};base64,{data}'], 'width': width, 'height': height}
	if suffix != '.pdf':
		raise ViewerError(f'{file.suffix or "This"} files cannot be shown here; use Open instead.')
	try:
		import pypdfium2 as pdfium  # pyright: ignore[reportMissingImports]
	except ImportError:
		raise ViewerError('The PDF viewer is not installed; use Open to see the file.') from None
	scale = min(max(float(scale), 0.5), 4.0)
	pdf = pdfium.PdfDocument(str(file))
	try:
		out: list[str] = []
		width = height = 0
		for index in range(min(len(pdf), MAX_PAGES)):
			page = pdf[index]
			try:
				image = page.render(scale=scale).to_pil()  # pyright: ignore[reportArgumentType]  (a float scale works)
			finally:
				page.close()
			if index == 0:
				width, height = image.size
			buffer = io.BytesIO()
			image.save(buffer, format='PNG', optimize=True)
			out.append('data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode())
		return {'pages': out, 'width': width, 'height': height, 'total': len(pdf)}
	finally:
		pdf.close()


def _image_size(file: Path) -> tuple[int, int]:
	"""Width and height of a PNG or JPEG from its header (no imaging library needed)."""
	data = file.read_bytes()[:65536]
	if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
		return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
	i = 2
	while i + 9 < len(data) and data[:2] == b'\xff\xd8':  # JPEG: the first start-of-frame marker holds the size
		if data[i] != 0xFF:
			break
		marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], 'big')
		if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
			return int.from_bytes(data[i + 7 : i + 9], 'big'), int.from_bytes(data[i + 5 : i + 7], 'big')
		i += 2 + length
	return 0, 0
