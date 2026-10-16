"""The filesystem connector.

Recovered from a built v1 image (`/app/server.py`), whose source
survived nowhere else.

3 tools, 2 of them proposed read-only. Tool names and input schemas are
carried across from v1 verbatim via `declare_schema`.

## Where a path may point

Two bounds, and a path must satisfy both:

1. `FILESYSTEM_ROOT`, set by whoever runs the server, is the ceiling: the one
   directory tree this process is willing to expose at all. Unset, every tool
   refuses. v1 had no ceiling: the caller's `X-Allowed-Paths` was the only
   bound, refused only an enumerated list of system roots, and so accepted
   `/proc` (the server's own environment), `/app` (its source) or `/srv`.
2. `X-Allowed-Paths`, sent per call, narrows that ceiling to the directories a
   connection was configured with. Each entry must resolve inside the ceiling.

Every path is resolved (symlinks followed, `..` collapsed) **before** the
check, the file is opened with `O_NOFOLLOW`, and the opened file is checked
again against the path it was resolved to, so a symlink swapped in between
the check and the open is refused rather than followed. A listing never
follows a symlink: it reports one as `symlink` and does not descend into it.

Reads and writes are bounded in size, a binary file is refused rather than
returned as mojibake, and `write_file` is not a read-only tool, so
`MCP_READ_ONLY=1` refuses it.
"""

import codecs
import fcntl
import os
import stat
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema

SERVER_NAME = "filesystem"
SERVER_VERSION = "2.0.0"

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="Read, write and list files under a directory the operator exposes.",
)

CREDENTIAL_HEADERS = ["X-Allowed-Paths"]

ROOT_ENV = "FILESYSTEM_ROOT"
MAX_READ_ENV = "FILESYSTEM_MAX_READ_BYTES"
MAX_WRITE_ENV = "FILESYSTEM_MAX_WRITE_BYTES"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
MAX_LIST_ENTRIES = 5000
# v1 recursed while depth <= 2, which lists three levels: the directory asked
# for and two below it. Kept as v1 behaved, not as its description read.
MAX_LIST_DEPTH = 2

# A ceiling or an allowed directory may never BE one of these, nor contain one.
SYSTEM_DIRS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/opt",
        "/private",
        "/private/etc",
        "/private/tmp",
        "/private/var",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",  # noqa: S108
        "/usr",
        "/var",
        "/Library",
        "/System",
        "/Users",
        "/Volumes",
    }
)
# ...and may never sit inside one of these: kernel and device interfaces,
# system configuration, and the runtime tree where mounted secrets live
# (/run/secrets, the service-account token under /var/run/secrets).
FORBIDDEN_TREES = (
    "/proc",
    "/sys",
    "/dev",
    "/etc",
    "/run",
    "/var/run",
    "/boot",
    "/root",
    "/private/etc",
    "/private/var/run",
)


class FilesystemRefusal(ValueError):
    """A request this connector will not serve, worded for the caller."""


def _limit(env_name: str) -> int:
    try:
        value = int(os.getenv(env_name, ""))
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _refuse_system(real: Path, label: str) -> None:
    text = str(real)
    if text in SYSTEM_DIRS:
        raise FilesystemRefusal(f"{label} is too broad: {text!r} is a system directory")
    for tree in FORBIDDEN_TREES:
        if text == tree or text.startswith(tree + "/"):
            raise FilesystemRefusal(f"{label} {text!r} is inside {tree}, which is never exposed")
        if tree.startswith(text.rstrip("/") + "/"):
            raise FilesystemRefusal(f"{label} {text!r} contains {tree}, which is never exposed")


def _ceiling() -> Path:
    raw = os.getenv(ROOT_ENV, "").strip()
    if not raw:
        raise FilesystemRefusal(
            f"this server exposes no directory: {ROOT_ENV} is not set by its operator"
        )
    if not os.path.isabs(raw):
        raise FilesystemRefusal(f"{ROOT_ENV} must be an absolute path")
    real = Path(os.path.realpath(raw))
    if not real.is_dir():
        raise FilesystemRefusal(f"{ROOT_ENV} does not name an existing directory")
    _refuse_system(real, ROOT_ENV)
    return real


def allowed_roots(credentials: dict[str, str]) -> list[Path]:
    """The directories this call may touch, each resolved and inside the ceiling.

    A relative entry is taken relative to the ceiling. An entry that is itself a
    symlink is resolved to its target first, so a link inside the ceiling that
    points outside it is refused here rather than trusted.
    """
    ceiling = _ceiling()
    raw = (credentials.get("allowed_paths") or "").strip()
    if not raw:
        raise FilesystemRefusal(
            "X-Allowed-Paths is required: the connection names the directories it may use"
        )
    roots: list[Path] = []
    for entry in (p.strip() for p in raw.split(",")):
        if not entry:
            continue
        if "\x00" in entry:
            raise FilesystemRefusal("an allowed path contains a NUL byte")
        candidate = Path(entry) if os.path.isabs(entry) else ceiling / entry
        real = Path(os.path.realpath(candidate))
        if not _within(real, ceiling):
            raise FilesystemRefusal(
                f"allowed path {entry!r} resolves outside the directory this server exposes"
            )
        _refuse_system(real, "allowed path")
        if not real.is_dir():
            raise FilesystemRefusal(f"allowed path {entry!r} is not an existing directory")
        roots.append(real)
    if not roots:
        raise FilesystemRefusal("X-Allowed-Paths names no directory")
    return roots


def resolve(path: str, roots: list[Path]) -> Path:
    """Resolve `path` and confirm it lies inside one of `roots`.

    A relative path is taken relative to the first allowed directory. v1 took
    it relative to the server's working directory, which a caller cannot know.
    """
    if not isinstance(path, str) or not path.strip():
        raise FilesystemRefusal("a path is required")
    if "\x00" in path:
        raise FilesystemRefusal("the path contains a NUL byte")
    candidate = Path(path) if os.path.isabs(path) else roots[0] / path
    real = Path(os.path.realpath(candidate))
    if not any(_within(real, root) for root in roots):
        raise FilesystemRefusal(f"Path {path!r} is not within the allowed directories")
    return real


def _confirm_opened(fd: int, real: Path, roots: list[Path]) -> os.stat_result:
    """The file behind `fd` is still the one `real` named, inside the roots.

    The second half of "resolve before and after": a symlink swapped into the
    path between `resolve` and `os.open` either fails the open (`O_NOFOLLOW`
    on the last component) or is caught here, because the path no longer
    resolves to itself or no longer names the inode that was opened.
    """
    opened = os.fstat(fd)
    again = Path(os.path.realpath(real))
    if again != real or not any(_within(again, root) for root in roots):
        raise FilesystemRefusal("the path changed while it was being opened")
    current = os.stat(again, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise FilesystemRefusal("the path changed while it was being opened")
    return opened


def _text_encoding(encoding: str) -> str:
    try:
        info = codecs.lookup(encoding or "utf-8")
    except LookupError as exc:
        raise FilesystemRefusal(f"unknown encoding {encoding!r}") from exc
    if not getattr(info, "_is_text_encoding", True):
        raise FilesystemRefusal(f"{encoding!r} is not a text encoding")
    return info.name


def _open_flags(base: int) -> int:
    return base | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK


def _blocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)


# credential_headers: ['X-Allowed-Paths']
@server.tool(
    name="read_file",
    description=(
        "Read the contents of a file. Returns the file content as text. A relative path "
        "is resolved against the first allowed directory. Binary files and files larger "
        "than the server's read limit are refused."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_files",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: utf-8)",
                "default": "utf-8",
            },
        },
        "required": ["path"],
    }
)
async def read_file(credentials: dict[str, str], path: str, encoding: str = "utf-8") -> dict:
    """Read a regular file inside the allowed directories, as text."""
    try:
        roots = allowed_roots(credentials)
        real = resolve(path, roots)
        codec = _text_encoding(encoding)
    except FilesystemRefusal as exc:
        return {"success": False, "error": str(exc)}

    limit = _limit(MAX_READ_ENV)
    try:
        fd = os.open(real, _open_flags(os.O_RDONLY))
    except FileNotFoundError:
        return {"success": False, "error": f"File not found: {path}"}
    except OSError as exc:
        return {"success": False, "error": f"Failed to read file: {exc.strerror or exc}"}
    try:
        opened = _confirm_opened(fd, real, roots)
        if not stat.S_ISREG(opened.st_mode):
            return {"success": False, "error": f"Path is not a file: {path}"}
        if opened.st_size > limit:
            return {
                "success": False,
                "error": f"File is {opened.st_size} bytes; this server reads at most {limit}",
            }
        _blocking(fd)
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except FilesystemRefusal as exc:
        return {"success": False, "error": str(exc)}
    except OSError as exc:
        return {"success": False, "error": f"Failed to read file: {exc.strerror or exc}"}
    finally:
        os.close(fd)

    if len(data) > limit:
        return {"success": False, "error": f"File grew past this server's read limit ({limit})"}
    if b"\x00" in data:
        return {"success": False, "error": f"{path} is a binary file; only text is returned"}
    try:
        content = data.decode(codec)
    except UnicodeDecodeError:
        return {
            "success": False,
            "error": f"{path} is not valid {codec} text; binary files are not returned",
        }
    return {"success": True, "path": str(real), "content": content, "size": len(data)}


# credential_headers: ['X-Allowed-Paths']
@server.tool(
    name="write_file",
    description=(
        "Write content to a file. Creates the file if it doesn't exist, overwrites if it "
        "does. A relative path is resolved against the first allowed directory. Content "
        "larger than the server's write limit is refused."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="write_files",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
            "encoding": {
                "type": "string",
                "description": "File encoding (default: utf-8)",
                "default": "utf-8",
            },
            "create_dirs": {
                "type": "boolean",
                "description": "Create parent directories if they don't exist",
                "default": True,
            },
        },
        "required": ["path", "content"],
    }
)
async def write_file(
    credentials: dict[str, str],
    path: str,
    content: str,
    encoding: str = "utf-8",
    create_dirs: bool = True,
) -> dict:
    """Write a regular file inside the allowed directories."""
    try:
        roots = allowed_roots(credentials)
        real = resolve(path, roots)
        codec = _text_encoding(encoding)
        if any(real == root for root in roots):
            raise FilesystemRefusal("cannot write to an allowed directory itself")
        if real.is_dir():
            raise FilesystemRefusal(f"Path is a directory: {path}")
        data = (content or "").encode(codec)
    except FilesystemRefusal as exc:
        return {"success": False, "error": str(exc)}
    except UnicodeEncodeError:
        return {"success": False, "error": f"content cannot be encoded as {encoding}"}

    limit = _limit(MAX_WRITE_ENV)
    if len(data) > limit:
        return {
            "success": False,
            "error": f"content is {len(data)} bytes; this server writes at most {limit}",
        }

    try:
        if not real.parent.exists():
            if not create_dirs:
                return {"success": False, "error": f"Parent directory does not exist: {path}"}
            real.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(real, _open_flags(os.O_WRONLY | os.O_CREAT), 0o644)
    except OSError as exc:
        return {"success": False, "error": f"Failed to write file: {exc.strerror or exc}"}
    try:
        opened = _confirm_opened(fd, real, roots)
        if not stat.S_ISREG(opened.st_mode):
            return {"success": False, "error": f"Path is not a regular file: {path}"}
        _blocking(fd)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except FilesystemRefusal as exc:
        return {"success": False, "error": str(exc)}
    except OSError as exc:
        return {"success": False, "error": f"Failed to write file: {exc.strerror or exc}"}
    finally:
        os.close(fd)

    return {
        "success": True,
        "path": str(real),
        "size": len(data),
        "message": f"Successfully wrote {len(content or '')} characters to {path}",
    }


# credential_headers: ['X-Allowed-Paths']
@server.tool(
    name="list_directory",
    description=(
        "List contents of a directory. Returns files and subdirectories with metadata. "
        "Symlinks are reported with type 'symlink' and never followed."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    credential_headers=CREDENTIAL_HEADERS,
    permission_group="read_files",
)
@declare_schema(
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the directory to list",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden files (starting with .)",
                "default": False,
            },
            "recursive": {
                "type": "boolean",
                "description": "List recursively (up to 2 levels deep)",
                "default": False,
            },
        },
        "required": ["path"],
    }
)
async def list_directory(
    credentials: dict[str, str],
    path: str,
    include_hidden: bool = False,
    recursive: bool = False,
) -> dict:
    """List a directory inside the allowed directories without following symlinks."""
    try:
        roots = allowed_roots(credentials)
        real = resolve(path, roots)
    except FilesystemRefusal as exc:
        return {"success": False, "error": str(exc)}

    if not real.exists():
        return {"success": False, "error": f"Directory not found: {path}"}
    if not real.is_dir():
        return {"success": False, "error": f"Path is not a directory: {path}"}

    entries: list[dict[str, Any]] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for child in children:
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = True
                return
            if not include_hidden and child.name.startswith("."):
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            else:
                kind = "file"
            entries.append(
                {
                    "name": child.name,
                    "path": str(directory / child.name),
                    "type": kind,
                    "size": info.st_size if kind == "file" else None,
                    "modified": info.st_mtime,
                }
            )
            if recursive and kind == "directory" and depth < MAX_LIST_DEPTH:
                walk(directory / child.name, depth + 1)
                if truncated:
                    return

    walk(real, 0)
    result: dict[str, Any] = {
        "success": True,
        "path": str(real),
        "entries": entries,
        "count": len(entries),
    }
    if truncated:
        result["truncated"] = True
        result["message"] = f"Listing stopped at {MAX_LIST_ENTRIES} entries"
    return result
