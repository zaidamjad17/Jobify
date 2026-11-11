"""In the packaged app sys.executable is Jobify.exe, not python.exe. browser-use and browser-harness start helper
processes as `sys.executable [-P|-I|-u] -m module ...` or `sys.executable -c code ...` (the REPL worker, the CDP
daemon, watchdogs): dispatch() runs those inside the exe as python would."""

from __future__ import annotations

import os
import runpy
import sys

FLAGS = {'-P', '-I', '-u', '-E', '-s', '-S', '-B', '-O'}


def restore_std_streams() -> None:
	"""A windowed exe starts with sys.stdin/stdout/stderr = None even when its parent passed it pipes or a log file
	(the REPL worker writes there and calls sys.stdout.reconfigure). Wrap the handles Windows gave the process; fall
	back to os.devnull."""
	for number, name, mode in ((-10, 'stdin', 'r'), (-11, 'stdout', 'w'), (-12, 'stderr', 'w')):
		if getattr(sys, name) is not None:
			continue
		stream = None
		if sys.platform == 'win32':
			import ctypes
			import msvcrt

			get = ctypes.windll.kernel32.GetStdHandle  # type: ignore[attr-defined]
			get.restype = ctypes.c_void_p  # a 64-bit handle; the default c_int would truncate it
			handle = get(number)
			if handle not in (None, 0, ctypes.c_void_p(-1).value):  # INVALID_HANDLE_VALUE
				try:
					fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == 'r' else os.O_WRONLY)  # type: ignore[attr-defined]
					stream = os.fdopen(fd, mode, buffering=1, encoding='utf-8', errors='replace')
				except OSError:
					stream = None
		setattr(sys, name, stream or open(os.devnull, mode, encoding='utf-8'))


def dispatch(argv: list[str]) -> bool:
	"""True when argv was a python-style command and has been run."""
	args = list(argv[1:])
	while args and args[0] in FLAGS:
		args.pop(0)
	if len(args) >= 2 and args[0] in ('-m', '-c'):
		restore_std_streams()
	if len(args) >= 2 and args[0] == '-m':
		sys.argv = [args[1], *args[2:]]
		runpy.run_module(args[1], run_name='__main__', alter_sys=True)
		return True
	if len(args) >= 2 and args[0] == '-c':
		sys.argv = ['-c', *args[2:]]
		exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__', '__builtins__': __builtins__})
		return True
	return False
