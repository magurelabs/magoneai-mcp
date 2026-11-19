"""Which artifact files each caller run produced, so one run cannot read another's.

The browser writes screenshots, recordings, PDFs and snapshots to one shared
volume (``ARTIFACTS_ROOT``), and the local tools read, list and delete files
there. Keyed only by path, ``publish_artifact`` and ``list_artifacts`` would let
one run fetch (and, because delete-after-publish is on by default, remove)
another run's files.

So a run's scope holds only files its own browser session produced, proved
three ways before a file is recorded (see ``Gateway._record_artifacts``):

- the path came from a structured line of the run's own relayed result (a
  ``- [Screenshot](path)`` link in the ``### Result`` or ``### Snapshot``
  section), never from free text such as a page's URL, title or snapshot;
- the file exists and is a regular returnable file inside the root; and
- it was written during that call (its mtime is no older than the call's start,
  less a small allowance for filesystem timestamp granularity).

A recorded file is pinned by its inode and mtime. If anything replaces or
rewrites it afterwards, it no longer counts as this run's, so a name another
run reuses is never served from a stale record. When a later run's call
reports a file a previous run had recorded (the model asked Playwright for the
same ``filename`` and Playwright overwrote it), ownership moves to that later
run, whose bytes the file now holds.

## What this costs on a restart or a second replica

The record lives in this process only, as does the browser session pool. So:

- **Restart or redeploy mid-run.** The run's next ``publish_artifact`` or
  ``list_artifacts`` is refused with an explicit "this server has no record of
  this run" error (``RUN_UNKNOWN``), never a silent empty list. The files stay
  on the volume, but this server can no longer prove which run made them, so
  they cannot be returned; the browser session (pages, cookies, logins) is gone
  too and the next browser call starts a fresh one.
- **Two replicas without sticky routing on ``X-Execution-Id``.** A call that
  lands on the other replica gets the same refusal and a fresh browser. Run one
  replica, or route every call of a run to the same one.
- **A run that stays idle** longer than ``PLAYWRIGHT_SESSION_IDLE_MINUTES``
  loses its record the same way, and (unless ``ARTIFACTS_DELETE_AFTER_PUBLISH``
  is off) the files it produced but never fetched are deleted from the volume,
  since no one can fetch them any more. Files orphaned by a restart belong to
  no record and are not deleted; clean the volume on a schedule if that matters
  (see the connector README).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

SessionKey = tuple[str, str]

RUN_UNKNOWN = (
    "This server has no record of this run, so it cannot tell which files are yours. "
    "Either no browser tool has run yet in this run, or the record was lost: the "
    "connector restarted, the run was idle longer than PLAYWRIGHT_SESSION_IDLE_MINUTES, "
    "or this request reached a different replica. Files saved before that cannot be "
    "returned, and the browser session (pages, cookies, logins) starts fresh."
)


@dataclass(frozen=True)
class Produced:
    """One file a run's browser produced, pinned by identity so a rewrite is noticed."""

    real_path: str
    inode: int
    mtime_ns: int

    @classmethod
    def of(cls, real_path: str, info: os.stat_result) -> Produced:
        return cls(real_path, info.st_ino, info.st_mtime_ns)

    def still_current(self) -> bool:
        """The file is still the one recorded: same inode, not rewritten since."""
        try:
            info = os.stat(self.real_path)
        except OSError:
            return False
        return info.st_ino == self.inode and info.st_mtime_ns == self.mtime_ns


@dataclass
class _Run:
    last_used: float
    files: dict[str, Produced] = field(default_factory=dict)


class ArtifactScope:
    def __init__(
        self,
        idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_paths_per_run: int = 4000,
    ) -> None:
        self.idle_seconds, self.clock, self.max_paths = idle_seconds, clock, max_paths_per_run
        self._runs: dict[SessionKey, _Run] = {}
        self._owner: dict[str, SessionKey] = {}

    def open(self, key: SessionKey) -> None:
        """Start (or refresh) a run's record; called on each of its browser calls."""
        run = self._runs.get(key)
        if run is None:
            self._runs[key] = _Run(self.clock())
        else:
            run.last_used = self.clock()

    def known(self, key: SessionKey) -> bool:
        """Whether this process holds a record for the run (see the module docstring)."""
        return key in self._runs

    def touch(self, key: SessionKey) -> None:
        run = self._runs.get(key)
        if run is not None:
            run.last_used = self.clock()

    def record(self, key: SessionKey, produced: Iterable[Produced]) -> None:
        """Add verified files to a run; a file another run held moves to this one."""
        run = self._runs.setdefault(key, _Run(self.clock()))
        for item in produced:
            if item.real_path not in run.files and len(run.files) >= self.max_paths:
                continue
            previous = self._owner.get(item.real_path)
            if previous is not None and previous != key and previous in self._runs:
                self._runs[previous].files.pop(item.real_path, None)
            run.files[item.real_path] = item
            self._owner[item.real_path] = key

    def allowed(self, key: SessionKey) -> set[str]:
        """The real paths this run may read, list or delete: its files still as recorded."""
        run = self._runs.get(key)
        if run is None:
            return set()
        stale = [path for path, item in run.files.items() if not item.still_current()]
        for path in stale:
            run.files.pop(path, None)
            if self._owner.get(path) == key:
                self._owner.pop(path, None)
        return set(run.files)

    def expire(self) -> list[Produced]:
        """End every run idle past the limit; return the files they left behind."""
        now = self.clock()
        idle = [k for k, run in self._runs.items() if now - run.last_used > self.idle_seconds]
        left: list[Produced] = []
        for key in idle:
            run = self._runs.pop(key)
            for path, item in run.files.items():
                if self._owner.get(path) == key:
                    self._owner.pop(path, None)
                    left.append(item)
        return left
