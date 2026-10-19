"""Find, budget and encode the video and screenshots a test report embeds.

Paths go through ``resolve_artifact``, the same rules as publish_artifact
(markdown link stripped, Playwright cwd-relative form, real-path containment).
A path that does not resolve becomes "unavailable" in the report, never an
error for the whole report.

The finished HTML must fit the budget it is given (what is left of the
report's byte budget once the CSV and the smallest PDF are counted). Base64
grows each file by a third, so the plan drops screenshots first (largest
first), then per-case videos (largest first), then the full-run video, until
the estimate fits. The renderer re-checks the real size and asks for more
drops if the estimate was short.

``include_videos=false`` (the default) leaves every recording out on purpose.
That is not a drop: the recordings are not listed as dropped and are not
deleted, so they stay on the volume for publish_artifact.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

from .config import content_type_for
from .files import read_bounded
from .paths import ArtifactError, resolve_artifact

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
VIDEO_TYPES = {"video/webm", "video/mp4"}
_URI_OVERHEAD = len("data:image/jpeg;base64,") + 2


@dataclass
class Media:
    path: Path
    content_type: str
    size: int
    uses: int = 1
    kept: bool = True

    @property
    def cost(self) -> int:
        """Bytes this file adds to the HTML: base64 length times each place it appears."""
        return (4 * ((self.size + 2) // 3) + _URI_OVERHEAD) * self.uses

    def data_uri(self, limit: int) -> str:
        encoded = base64.b64encode(read_bounded(self.path, limit)).decode("ascii")
        return f"data:{self.content_type};base64,{encoded}"


@dataclass
class MediaSet:
    video: Media | None = None
    video_requested: bool = False
    shots: dict[str, Media | None] = field(default_factory=dict)
    # One entry per test case, by position: its own recording, or None.
    case_videos: list[Media | None] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    videos_excluded: bool = False

    def _unique(self, items) -> list[Media]:
        seen: dict[Path, Media] = {}
        for media in items:
            if media is not None:
                seen.setdefault(media.path, media)
        return list(seen.values())

    def unique_shots(self) -> list[Media]:
        return self._unique(self.shots.values())

    def unique_videos(self) -> list[Media]:
        return self._unique([*self.case_videos, self.video])

    def _kept(self) -> list[Media]:
        everything = [*self.shots.values(), *self.case_videos, self.video]
        return [m for m in self._unique(everything) if m.kept]

    def embedded_paths(self) -> list[Path]:
        return [m.path for m in self._kept()]

    def media_cost(self) -> int:
        return sum(m.cost for m in self._kept())

    def exclude_videos(self) -> None:
        """Leave every recording out by request: not embedded, not dropped, not deleted."""
        self.videos_excluded = True
        for media in self.unique_videos():
            media.kept = False

    def drop_one(self) -> bool:
        """Leave out the largest screenshot, else the largest case video, else the run video."""
        for group in (self.shots.values(), self.case_videos, [self.video]):
            kept = sorted((m for m in self._unique(group) if m.kept), key=lambda m: -m.size)
            if kept:
                kept[0].kept = False
                self.dropped.append(kept[0].path.name)
                return True
        return False


def _locate(raw: str, root: Path, allowed: set[str]) -> Media | None:
    try:
        path = resolve_artifact(raw, root)
    except ArtifactError:
        return None
    content_type = content_type_for(path) or ""
    if content_type not in allowed:
        return None
    size = path.stat().st_size
    return Media(path=path, content_type=content_type, size=size) if size > 0 else None


class _Registry:
    """One Media per real file, however many places reference it."""

    def __init__(self, root: Path) -> None:
        self.root, self.by_path = root, {}

    def find(self, raw: str | None, allowed: set[str]) -> Media | None:
        found = _locate(raw, self.root, allowed) if raw and raw.strip() else None
        if found is None:
            return None
        if found.path in self.by_path:
            self.by_path[found.path].uses += 1
            return self.by_path[found.path]
        self.by_path[found.path] = found
        return found


def collect(
    report_paths: list[str],
    video_path: str | None,
    root: Path,
    case_video_paths: list[str | None] | None = None,
) -> MediaSet:
    """Resolve every referenced file once; count how often each one appears."""
    registry = _Registry(root)
    media = MediaSet(video_requested=bool(video_path and video_path.strip()))
    media.video = registry.find(video_path, VIDEO_TYPES)
    media.case_videos = [registry.find(raw, VIDEO_TYPES) for raw in case_video_paths or []]
    for raw in report_paths:
        if raw in media.shots:
            if media.shots[raw] is not None:
                media.shots[raw].uses += 1
            continue
        media.shots[raw] = registry.find(raw, IMAGE_TYPES)
    return media


def fit_budget(media: MediaSet, skeleton_bytes: int, limit: int) -> None:
    """Drop media until the estimated HTML size fits ``limit``."""
    while skeleton_bytes + media.media_cost() > limit and media.drop_one():
        pass


def recording_names(media: MediaSet) -> list[str]:
    """Per test case: its own recording's file name, else the full run's, else ""."""
    run = media.video.path.name if media.video else ""
    return [video.path.name if video else run for video in media.case_videos]
