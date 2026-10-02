"""Child process that runs one piece of model-written Python.

Started by alex.sandbox.runner with an empty environment and a private working
directory. After the scientific stack is loaded, an audit hook confines the
process: no network, no subprocesses, no native-library loading, file reads
only inside the Python installation, the corpus and the working directory, file
writes only inside the working directory.

The hook is defence in depth, not a security boundary on its own. Public
deployments run this worker in the sandbox container (no network, no secrets,
read-only filesystem); see docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import linecache
import os
import sys
import time
import traceback
from pathlib import Path

MAX_OUTPUT_CHARS = 20_000
MAX_IMAGE_BYTES = 2_000_000
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}

BLOCKED_PREFIXES = (
    "socket.",
    "subprocess.",
    "ctypes.",
    "winreg.",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "nntplib.",
    "telnetlib.",
    "http.client.",
    "urllib.",
    "webbrowser.",
    "pty.",
    "msvcrt.",
    "_winapi.",
    "mmap.",
    "syslog.",
    "ensurepip.",
    "shutil.chown",
    "signal.pthread_kill",
    "os.fork",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.system",
    "os.startfile",
    "os.kill",
    "os.symlink",
    "os.link",
    "os.chmod",
    "os.chown",
    "os.chflags",
    "os.add_dll_directory",
    "os.setxattr",
    "os.removexattr",
    "sys.addaudithook",
    "builtins.input",
    "cpython.PyInterpreterState",
    "_posixsubprocess.",
    "resource.setrlimit",
    "fcntl.",
    "os.mkfifo",
    "os.mknod",
)
PATH_READ_EVENTS = {
    "open",
    "os.listdir",
    "os.scandir",
    "glob.glob",
    "glob.glob/2",
    "os.walk",
    "os.fwalk",
    "pathlib.Path.glob",
    "pathlib.Path.rglob",
}
PATH_WRITE_EVENTS = {
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.truncate",
    "os.utime",
    "shutil.rmtree",
    "shutil.move",
    "shutil.copyfile",
    "shutil.copytree",
    "shutil.make_archive",
    "shutil.unpack_archive",
    "shutil.copymode",
    "shutil.copystat",
    "tempfile.mkstemp",
    "tempfile.mkdtemp",
}
SYSTEM_DATA = ("/usr/share/zoneinfo", "/usr/share/fonts", "/etc/fonts", "/etc/localtime")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class Blocked(PermissionError):
    pass


def limit_resources(memory_mb: int, cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:
        return
    limits = [
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_CORE, 0),
    ]
    if memory_mb:
        limits.append((resource.RLIMIT_AS, memory_mb * 1024 * 1024))
    for name, value in limits:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(name, (value, value))


def resolved(path) -> str:
    if isinstance(path, int):
        return ""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def inside(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def install_guard(workdir: str, readable: list[str], databases: list[str]) -> None:
    workdir_root = resolved(workdir)
    read_roots = [workdir_root] + [resolved(root) for root in readable if root]
    database_files = {resolved(database) for database in databases}
    special = {resolved(os.devnull)}

    def guard(event: str, arguments: tuple) -> None:
        if event == "open":
            path, mode, flags = (list(arguments) + [None, None])[:3]
            target = resolved(path)
            if not target or target in special:
                return
            writing = (isinstance(mode, str) and any(letter in mode for letter in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS
            )
            if writing and not inside(target, [workdir_root]):
                raise Blocked("The sandbox can only write inside its working directory.")
            if not writing and not inside(target, read_roots):
                raise Blocked(f"The sandbox cannot read {os.fspath(path)!r}.")
        elif event in PATH_READ_EVENTS:
            target = resolved(arguments[0]) if arguments and arguments[0] is not None else ""
            if target and not inside(target, read_roots):
                raise Blocked("The sandbox cannot list that directory.")
        elif event in PATH_WRITE_EVENTS:
            for argument in arguments[:2]:
                if (
                    isinstance(argument, (str, bytes, os.PathLike))
                    and argument
                    and not inside(resolved(argument), [workdir_root])
                ):
                    raise Blocked("The sandbox can only change files in its working directory.")
        elif event == "sqlite3.connect":
            database = str(arguments[0]) if arguments else ""
            name = database.removeprefix("file:").split("?")[0]
            if database != ":memory:" and resolved(name) not in database_files:
                raise Blocked("The sandbox can only open the corpus database.")
        elif event == "sqlite3.enable_load_extension" or event == "sqlite3.load_extension":
            raise Blocked("SQLite extensions are not available.")
        elif event.startswith(BLOCKED_PREFIXES):
            raise Blocked(
                f"{event} is blocked: the sandbox has no network, cannot start "
                "processes and cannot load native libraries."
            )

    sys.addaudithook(guard)


def user_traceback(error: BaseException) -> str:
    frames = [
        frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == "<alex>"
    ]
    lines = [f"  line {frame.lineno}: {frame.line or ''}".rstrip() for frame in frames[-4:]]
    return "\n".join([*lines, f"{type(error).__name__}: {error}"])


def collect_images(workdir: Path, artifacts: list[dict]) -> None:
    """Code that never called ehd.show() still shows its pictures: image
    files it saved, or else the matplotlib figures it left open."""
    import base64

    import ehd
    import matplotlib.pyplot as plt

    if any(artifact["type"] in ("image", "chart") for artifact in artifacts):
        return
    for path in sorted(workdir.iterdir()):
        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if (
            media_type
            and path.stat().st_size <= MAX_IMAGE_BYTES
            and len(artifacts) < ehd.MAX_ARTIFACTS
        ):
            artifacts.append(
                {
                    "type": "image",
                    "title": path.stem.replace("_", " "),
                    "media_type": media_type,
                    "data": base64.b64encode(path.read_bytes()).decode(),
                }
            )
    if any(artifact["type"] == "image" for artifact in artifacts):
        return
    for number in plt.get_fignums():
        with contextlib.suppress(Exception):
            ehd.show(plt.figure(number))


def main() -> None:
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    workdir = Path(job["workdir"])
    os.chdir(workdir)
    package_directory = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package_directory.parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    limit_resources(job.get("memory_mb", 0), job.get("timeout_seconds", 40) + 5)

    import matplotlib

    matplotlib.use("Agg")
    import ehd
    import matplotlib.font_manager  # noqa: F401  builds its font list before the guard
    import matplotlib.pyplot as plt
    import numpy  # noqa: F401
    import pandas  # noqa: F401

    ehd._configure(job["store_path"], job.get("corpus_dir", ""))
    with contextlib.suppress(ImportError):
        import plotly.express  # noqa: F401
    with contextlib.suppress(ImportError):
        import alex.corpus.store  # noqa: F401

    font_directories = sorted(
        {str(Path(font.fname).parent) for font in matplotlib.font_manager.fontManager.ttflist}
    )
    readable = [
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        str(package_directory),
        job["store_path"],
        job.get("corpus_dir", ""),
        matplotlib.get_data_path(),
        *font_directories,
        *SYSTEM_DATA,
    ]
    install_guard(str(workdir), readable, [job["store_path"]])

    output = io.StringIO()
    started = time.time()
    error = ""
    namespace = {"__name__": "__main__", "ehd": ehd, "pd": pandas, "np": numpy, "plt": plt}
    linecache.cache["<alex>"] = (len(job["code"]), None, job["code"].splitlines(True), "<alex>")
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile(job["code"], "<alex>", "exec"), namespace)
            collect_images(workdir, ehd._artifacts)
    except BaseException as failure:  # noqa: BLE001  SystemExit and KeyboardInterrupt included
        error = user_traceback(failure)

    printed = output.getvalue()
    if len(printed) > MAX_OUTPUT_CHARS:
        printed = printed[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
    result = {
        "ok": not error,
        "output": printed,
        "error": error,
        "artifacts": ehd._artifacts,
        "duration_s": round(time.time() - started, 2),
    }
    (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
