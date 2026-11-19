"""resolve_artifact on a real directory tree: what is returned and what is refused."""

import os

import pytest

from connectors.playwright.paths import ArtifactError, resolve_artifact


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_relative_path_resolves_under_root(root):
    target = _write(root / ".playwright-mcp" / "page-2026-10-15.png")
    assert resolve_artifact(".playwright-mcp/page-2026-10-15.png", root) == target


def test_absolute_path_under_root_resolves(root):
    target = _write(root / "video-1.webm")
    assert resolve_artifact(str(target), root) == target


def test_absolute_path_outside_root_is_refused(root, tmp_path):
    outside = _write(tmp_path / "secret.png")
    with pytest.raises(ArtifactError, match="outside"):
        resolve_artifact(str(outside), root)


@pytest.mark.parametrize("raw", ["../secret.png", "a/../../secret.png", "../../../secret.png"])
def test_relative_escape_never_reads_outside(root, tmp_path, raw):
    """A relative path that climbs out falls back to its basename INSIDE the root."""
    _write(tmp_path / "secret.png", b"outside")
    with pytest.raises(ArtifactError, match="No artifact"):
        resolve_artifact(raw, root)


def test_absolute_dotdot_outside_is_refused(root):
    with pytest.raises(ArtifactError, match="outside"):
        resolve_artifact("/etc/../etc/passwd", root)


def test_dotdot_that_stays_inside_root_resolves(root):
    target = _write(root / "a.png")
    (root / "sub").mkdir()
    assert resolve_artifact("sub/../a.png", root) == target


def test_playwright_cwd_relative_video_path(root):
    """Exact form @playwright/mcp 0.0.83 prints from cwd /home/node."""
    target = _write(root / "video-2026-10-15T13-46-38-154Z.webm")
    raw = "../../artifacts/video-2026-10-15T13-46-38-154Z.webm"
    assert resolve_artifact(raw, root) == target


def test_markdown_link_reply_is_unwrapped(root):
    target = _write(root / "video-2026-10-15T13-46-38-154Z.webm")
    raw = "- [Video](../../artifacts/video-2026-10-15T13-46-38-154Z.webm)"
    assert resolve_artifact(raw, root) == target
    assert resolve_artifact("[Screenshot](<video-2026-10-15T13-46-38-154Z.webm>)", root) == target


def test_markdown_link_to_outside_is_refused(root, tmp_path):
    outside = _write(tmp_path / "secret.png")
    with pytest.raises(ArtifactError, match="outside"):
        resolve_artifact(f"[x]({outside})", root)


def test_symlink_to_file_outside_root_is_refused(root, tmp_path):
    outside = _write(tmp_path / "secret.png")
    os.symlink(outside, root / "innocent.png")
    with pytest.raises(ArtifactError, match="outside"):
        resolve_artifact("innocent.png", root)


def test_symlinked_directory_escape_is_refused(root, tmp_path):
    _write(tmp_path / "elsewhere" / "shot.png")
    os.symlink(tmp_path / "elsewhere", root / "linked")
    # Escapes, so it falls back to the basename inside the root, which is absent.
    with pytest.raises(ArtifactError, match="No artifact"):
        resolve_artifact("linked/shot.png", root)


def test_symlink_inside_root_is_allowed(root):
    target = _write(root / "real.png")
    os.symlink(target, root / "alias.png")
    assert resolve_artifact("alias.png", root) == target


def test_root_itself_and_directories_are_refused(root):
    (root / "sub.png").mkdir()
    with pytest.raises(ArtifactError, match="directory"):
        resolve_artifact("sub.png", root)
    with pytest.raises(ArtifactError, match="outside"):
        resolve_artifact(str(root), root)


def test_missing_file_is_refused(root):
    with pytest.raises(ArtifactError, match="list_artifacts"):
        resolve_artifact("nope.png", root)


@pytest.mark.parametrize("name", ["run.sh", "data.json", "noext", "page.PNG.exe"])
def test_disallowed_extension_is_refused(root, name):
    _write(root / name)
    with pytest.raises(ArtifactError, match="not allowed"):
        resolve_artifact(name, root)


@pytest.mark.parametrize(
    "name",
    [
        "a.png",
        "b.JPG",
        "c.jpeg",
        "d.webp",
        "e.webm",
        "f.mp4",
        "trace.zip",
        "g.pdf",
        "snap.yml",
        "snap.yaml",
        "log.txt",
        "notes.md",
    ],
)
def test_allowed_extensions(root, name):
    target = _write(root / name)
    assert resolve_artifact(name, root) == target


@pytest.mark.parametrize("raw", ["", "   ", "a\x00.png"])
def test_empty_or_nul_path_is_refused(root, raw):
    with pytest.raises(ArtifactError):
        resolve_artifact(raw, root)


# A reply captured from the pinned 0.0.85 image, with page text that names files.
LIVE_REPLY = """### Result
- [Screenshot of viewport](page-2026-11-19T08-11-40-598Z.png)
### Ran Playwright code
```js
// Screenshot viewport and save it as page-2026-11-19T08-11-40-598Z.png
await page.screenshot({ path: 'other-in-code.png' });
```
### Page
- Page URL: data:text/html,<h1>login.png page-1.png</h1>
- Page Title: [x](title.png)
### Snapshot
- [Snapshot](page-2026-11-19T08-11-40-503Z.yml)
```yaml
- [inline](fenced.png)
```
"""


def test_reported_paths_reads_only_playwrights_own_report_lines(root):
    from connectors.playwright.paths import reported_paths

    assert reported_paths(LIVE_REPLY, root) == [
        str(root / "page-2026-11-19T08-11-40-598Z.png"),
        str(root / "page-2026-11-19T08-11-40-503Z.yml"),
    ]


def test_reported_paths_ignores_free_text_and_other_sections(root):
    from connectors.playwright.paths import reported_paths

    assert reported_paths("Saved to login.png\n- [x](login.png)", root) == []
    assert reported_paths("### Page\n- [x](login.png)", root) == []
    assert reported_paths("### Result\n- [x](../../etc/passwd.png)", root) == [
        str(root / "passwd.png")  # the basename inside the root, never outside it
    ]
    assert reported_paths("### Result\n- [x](notes.exe)", root) == []
