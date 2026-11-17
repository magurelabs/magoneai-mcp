"""publish_artifact and list_artifacts over a real MCP client and a real directory.

The file goes back in the tool result as an embedded resource, so what is
asserted is the wire shape a client receives: a JSON text item naming the
file, then a `resource` block holding `BlobResourceContents` with an
`artifact://` URI, the MIME type and the base64 bytes.
"""

import base64
import json
import os
import time

import pytest
from mcp import Client
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent

from .conftest import module


async def call(name: str, arguments: dict):
    async with Client(module.server.mcp) as client:
        return await client.call_tool(name, arguments)


def summary(result) -> dict:
    assert isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


def resource(result) -> BlobResourceContents:
    block = result.content[1]
    assert isinstance(block, EmbeddedResource) and block.type == "resource"
    assert isinstance(block.resource, BlobResourceContents)
    return block.resource


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("video.webm", "video/webm"),
        ("page-1.png", "image/png"),
        ("trace.zip", "application/zip"),
        ("page.pdf", "application/pdf"),
        ("snapshot.yml", "application/yaml"),
    ],
)
async def test_the_file_comes_back_as_an_embedded_resource(root, name, mime):
    (root / name).write_bytes(b"BYTES!")
    result = await call("publish_artifact", {"path": str(root / name)})
    assert result.is_error is False
    assert [block.type for block in result.content] == ["text", "resource"]
    blob = resource(result)
    assert str(blob.uri) == f"artifact://{name}"
    assert blob.mime_type == mime
    assert base64.b64decode(blob.blob) == b"BYTES!"
    assert summary(result) == {
        "filename": name,
        "uri": f"artifact://{name}",
        "mime_type": mime,
        "size_bytes": 6,
        "deleted_local_copy": True,
    }
    assert not (root / name).exists()


async def test_playwrights_own_path_forms_are_accepted(root):
    (root / "video-2026-10-15T13-46-38-154Z.webm").write_bytes(b"WEBM")
    link = "- [Video](../../artifacts/video-2026-10-15T13-46-38-154Z.webm)"
    result = await call("publish_artifact", {"path": link})
    assert resource(result).mime_type == "video/webm"


async def test_keep_file_when_delete_disabled(root, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DELETE_AFTER_PUBLISH", "false")
    (root / "shot.png").write_bytes(b"PNG")
    result = await call("publish_artifact", {"path": "shot.png"})
    assert summary(result)["deleted_local_copy"] is False
    assert (root / "shot.png").exists()


async def test_custom_filename_keeps_extension_and_drops_directories(root):
    (root / "trace.zip").write_bytes(b"PK")
    result = await call("publish_artifact", {"path": "trace.zip", "filename": "../../my trace"})
    assert summary(result)["filename"] == "my trace.zip"
    assert str(resource(result).uri) == "artifact://my%20trace.zip"


async def test_too_large_is_refused_and_the_file_kept(root, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "4")
    (root / "big.png").write_bytes(b"12345")
    result = await call("publish_artifact", {"path": "big.png"})
    assert result.is_error is True
    assert "ARTIFACTS_MAX_BYTES" in result.content[0].text
    assert len(result.content) == 1 and (root / "big.png").exists()


def test_the_size_limit_defaults_to_25_mb_and_ignores_junk(monkeypatch):
    from connectors.playwright import config

    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "junk")
    assert config.max_bytes() == 25 * 1024 * 1024
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "-1")
    assert config.max_bytes() == 25 * 1024 * 1024
    monkeypatch.setenv("ARTIFACTS_MAX_BYTES", "2048")
    assert config.max_bytes() == 2048


async def test_empty_file_is_refused(root):
    (root / "video.webm").write_bytes(b"")
    result = await call("publish_artifact", {"path": "video.webm"})
    assert result.is_error is True and "browser_stop_video" in result.content[0].text


async def test_escape_attempt_returns_an_error_and_nothing_else(root, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"s")
    result = await call("publish_artifact", {"path": str(tmp_path / "secret.png")})
    assert result.is_error is True and "outside" in result.content[0].text
    assert len(result.content) == 1
    assert (tmp_path / "secret.png").exists()


async def test_a_symlink_out_of_the_root_is_refused(root, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"s")
    os.symlink(tmp_path / "secret.png", root / "innocent.png")
    result = await call("publish_artifact", {"path": "innocent.png"})
    assert result.is_error is True and "outside" in result.content[0].text


async def test_read_only_mode_refuses_publish_and_allows_listing(root, monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "1")
    (root / "shot.png").write_bytes(b"PNG")
    assert (await call("publish_artifact", {"path": "shot.png"})).is_error is True
    assert (root / "shot.png").exists()
    assert (await call("list_artifacts", {})).is_error is False


def _write(path, age_seconds=0, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


async def test_listing_is_newest_first_within_window_and_returnable_only(root, tmp_path):
    _write(root / "old.png", age_seconds=3 * 3600)
    _write(root / ".playwright-mcp" / "page.png", age_seconds=120)
    _write(root / "video.webm", age_seconds=10)
    _write(root / "script.sh", age_seconds=5)
    _write(tmp_path / "outside.png")
    os.symlink(tmp_path / "outside.png", root / "link.png")

    out = json.loads((await call("list_artifacts", {"since_minutes": 60})).content[0].text)

    assert [a["path"] for a in out["artifacts"]] == ["video.webm", ".playwright-mcp/page.png"]
    assert out["artifacts"][0]["size_bytes"] == 4
    assert out["artifacts"][0]["modified_at"].endswith("+00:00")
    assert out["count"] == 2 and out["truncated"] is False


async def test_listing_limit_truncates(root):
    for i in range(3):
        _write(root / f"shot-{i}.png", age_seconds=i)
    out = json.loads((await call("list_artifacts", {"limit": 2})).content[0].text)
    assert [a["path"] for a in out["artifacts"]] == ["shot-0.png", "shot-1.png"]
    assert out["truncated"] is True


async def test_missing_root_lists_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path / "absent"))
    out = json.loads((await call("list_artifacts", {})).content[0].text)
    assert out == {"artifacts": [], "count": 0, "truncated": False}
