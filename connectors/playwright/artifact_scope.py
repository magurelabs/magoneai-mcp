"""Which artifact files each caller run has produced, so one run cannot read another's.

The browser writes screenshots, recordings and traces to one shared volume
(``ARTIFACTS_ROOT``), and the local tools read, list and delete files there.
Keyed only by path, ``publish_artifact`` and ``list_artifacts`` would let one
run fetch — and, because delete-after-publish is on by default, remove —
another run's files.

So the gateway records, per caller session, the real paths that showed up in
that session's own relayed browser results, and the local tools restrict
themselves to that set. A session that has produced nothing sees nothing. The
set is capped, and the gateway drops a session's entry when its browser session
is evicted.
"""

from __future__ import annotations

from collections.abc import Iterable

SessionKey = tuple[str, str]


class ArtifactScope:
    def __init__(self, max_paths_per_session: int = 4000) -> None:
        self._by_key: dict[SessionKey, set[str]] = {}
        self.max_paths = max_paths_per_session

    def record(self, key: SessionKey, real_paths: Iterable[str]) -> None:
        bucket = self._by_key.setdefault(key, set())
        for path in real_paths:
            if len(bucket) >= self.max_paths:
                break
            bucket.add(path)

    def allowed(self, key: SessionKey) -> set[str]:
        """The real paths this session may read, list or delete (a copy)."""
        return set(self._by_key.get(key, ()))

    def forget(self, key: SessionKey) -> None:
        self._by_key.pop(key, None)

    def retain(self, keys: Iterable[SessionKey]) -> None:
        """Drop every session's record except the ones still active."""
        live = set(keys)
        for key in list(self._by_key):
            if key not in live:
                self._by_key.pop(key, None)
