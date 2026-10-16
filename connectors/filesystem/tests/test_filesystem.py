"""The filesystem connector, called through a real MCP client.

Each escape a path-taking tool can suffer is tried end to end: `..`, an
absolute path outside the allowed directory, a symlink inside it pointing out,
an allowed directory that is itself a symlink, a dangling symlink used as a
write target, and a symlink swapped in after the check. None of them may read
or write outside the ceiling.
"""

import importlib
import json
import os

import pytest
from mcp import Client

module = importlib.import_module("connectors.filesystem.server")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A ceiling with one allowed directory inside it and a secret outside it."""
    ceiling = tmp_path / "ceiling"
    allowed = ceiling / "work"
    allowed.mkdir(parents=True)
    (allowed / "hello.txt").write_text("hello world", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")

    monkeypatch.setenv(module.ROOT_ENV, str(ceiling))
    monkeypatch.delenv("MCP_READ_ONLY", raising=False)
    headers = {"x-allowed-paths": str(allowed)}
    monkeypatch.setattr("magone_mcp_core.server._request_headers", lambda ctx: dict(headers))
    return {"ceiling": ceiling, "allowed": allowed, "outside": outside, "headers": headers}


async def call(tool: str, args: dict) -> tuple[bool, dict]:
    async with Client(module.server.mcp) as client:
        result = await client.call_tool(tool, args)
    return result.is_error, json.loads(result.content[0].text)


async def test_the_three_v1_tools_are_listed_with_their_annotations():
    async with Client(module.server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"read_file", "write_file", "list_directory"}
    assert tools["read_file"].annotations.read_only_hint is True
    assert tools["list_directory"].annotations.read_only_hint is True
    assert tools["write_file"].annotations.read_only_hint is False
    assert tools["write_file"].annotations.destructive_hint is True


async def test_read_write_and_list_inside_the_allowed_directory(tree):
    failed, body = await call("read_file", {"path": str(tree["allowed"] / "hello.txt")})
    assert not failed and body["content"] == "hello world"

    failed, body = await call(
        "write_file", {"path": str(tree["allowed"] / "sub" / "new.txt"), "content": "abc"}
    )
    assert not failed and body["size"] == 3
    assert (tree["allowed"] / "sub" / "new.txt").read_text() == "abc"

    failed, body = await call("list_directory", {"path": str(tree["allowed"]), "recursive": True})
    assert not failed
    names = {e["name"]: e["type"] for e in body["entries"]}
    assert names == {"hello.txt": "file", "sub": "directory", "new.txt": "file"}


async def test_a_relative_path_resolves_against_the_first_allowed_directory(tree):
    failed, body = await call("read_file", {"path": "hello.txt"})
    assert not failed and body["content"] == "hello world"


async def test_dot_dot_cannot_climb_out(tree):
    failed, body = await call("read_file", {"path": "../../outside/secret.txt"})
    assert failed and "not within the allowed directories" in body["error"]
    failed, body = await call(
        "read_file", {"path": str(tree["allowed"] / ".." / ".." / "outside" / "secret.txt")}
    )
    assert failed and "top secret" not in json.dumps(body)


async def test_an_absolute_path_outside_the_allowed_directory_is_refused(tree):
    failed, body = await call("read_file", {"path": str(tree["outside"] / "secret.txt")})
    assert failed and "not within the allowed directories" in body["error"]
    failed, _ = await call("write_file", {"path": str(tree["outside"] / "x.txt"), "content": "x"})
    assert failed and not (tree["outside"] / "x.txt").exists()
    failed, _ = await call("list_directory", {"path": str(tree["outside"])})
    assert failed


async def test_a_symlink_inside_pointing_outside_is_not_followed(tree):
    (tree["allowed"] / "escape.txt").symlink_to(tree["outside"] / "secret.txt")
    (tree["allowed"] / "escape_dir").symlink_to(tree["outside"])

    failed, body = await call("read_file", {"path": "escape.txt"})
    assert failed and "top secret" not in json.dumps(body)
    failed, body = await call("read_file", {"path": "escape_dir/secret.txt"})
    assert failed and "top secret" not in json.dumps(body)
    failed, _ = await call("write_file", {"path": "escape_dir/planted.txt", "content": "x"})
    assert failed and not (tree["outside"] / "planted.txt").exists()

    failed, body = await call("list_directory", {"path": str(tree["allowed"]), "recursive": True})
    assert not failed
    kinds = {e["name"]: e["type"] for e in body["entries"]}
    assert kinds["escape.txt"] == "symlink" and kinds["escape_dir"] == "symlink"
    assert "secret.txt" not in kinds, "the listing descended into a symlinked directory"


async def test_a_dangling_symlink_cannot_be_used_to_create_a_file_outside(tree):
    (tree["allowed"] / "dangling.txt").symlink_to(tree["outside"] / "created.txt")
    failed, _ = await call("write_file", {"path": "dangling.txt", "content": "x"})
    assert failed and not (tree["outside"] / "created.txt").exists()


async def test_an_allowed_directory_that_is_a_symlink_to_outside_is_refused(tree):
    link = tree["ceiling"] / "link_out"
    link.symlink_to(tree["outside"])
    tree["headers"]["x-allowed-paths"] = str(link)
    failed, body = await call("read_file", {"path": "secret.txt"})
    assert failed and "resolves outside" in body["error"]


async def test_an_allowed_directory_that_is_a_symlink_inside_is_resolved_and_works(tree):
    link = tree["ceiling"] / "link_in"
    link.symlink_to(tree["allowed"])
    tree["headers"]["x-allowed-paths"] = str(link)
    failed, body = await call("read_file", {"path": str(link / "hello.txt")})
    assert not failed and body["content"] == "hello world"


async def test_an_allowed_path_outside_the_ceiling_is_refused(tree):
    tree["headers"]["x-allowed-paths"] = str(tree["outside"])
    failed, body = await call("read_file", {"path": str(tree["outside"] / "secret.txt")})
    assert failed and "resolves outside" in body["error"]


async def test_no_ceiling_means_nothing_is_exposed(tree, monkeypatch):
    monkeypatch.delenv(module.ROOT_ENV)
    failed, body = await call("read_file", {"path": "hello.txt"})
    assert failed and module.ROOT_ENV in body["error"]


@pytest.mark.parametrize("root", ["/", "/etc", "/proc", "/tmp", "/var"])  # noqa: S108
async def test_a_system_directory_is_refused_as_a_ceiling(tree, monkeypatch, root):
    if not os.path.isdir(root):
        pytest.skip(f"{root} does not exist here")
    monkeypatch.setenv(module.ROOT_ENV, root)
    tree["headers"]["x-allowed-paths"] = root
    failed, body = await call("list_directory", {"path": root})
    assert failed
    assert "system directory" in body["error"] or "never exposed" in body["error"]


async def test_the_connection_must_name_its_directories(tree):
    tree["headers"].pop("x-allowed-paths")
    failed, body = await call("read_file", {"path": "hello.txt"})
    assert failed and "X-Allowed-Paths" in body["error"]


async def test_a_symlink_swapped_in_after_the_check_is_refused(tree, monkeypatch):
    """Resolve before AND after: the race the open-time checks exist for."""
    target = tree["allowed"] / "hello.txt"
    real_resolve = module.resolve

    def resolve_then_swap(path, roots):
        resolved = real_resolve(path, roots)
        target.unlink()
        target.symlink_to(tree["outside"] / "secret.txt")
        return resolved

    monkeypatch.setattr(module, "resolve", resolve_then_swap)
    failed, body = await call("read_file", {"path": "hello.txt"})
    assert failed and "top secret" not in json.dumps(body)


async def test_reads_and_writes_are_bounded_in_size(tree, monkeypatch):
    monkeypatch.setenv(module.MAX_READ_ENV, "5")
    monkeypatch.setenv(module.MAX_WRITE_ENV, "5")
    failed, body = await call("read_file", {"path": "hello.txt"})
    assert failed and "at most 5" in body["error"]
    failed, body = await call("write_file", {"path": "big.txt", "content": "123456"})
    assert failed and "at most 5" in body["error"]
    assert not (tree["allowed"] / "big.txt").exists()


async def test_a_binary_file_is_refused_not_returned(tree):
    (tree["allowed"] / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    failed, body = await call("read_file", {"path": "blob.bin"})
    assert failed and "binary" in body["error"]


async def test_a_fifo_is_refused_without_blocking(tree):
    fifo = tree["allowed"] / "pipe"
    os.mkfifo(fifo)
    failed, body = await call("read_file", {"path": "pipe"})
    assert failed and "not a file" in body["error"]


async def test_a_non_text_codec_is_refused(tree):
    failed, body = await call("read_file", {"path": "hello.txt", "encoding": "hex"})
    assert failed and "text encoding" in body["error"]


async def test_writing_over_an_allowed_directory_itself_is_refused(tree):
    failed, body = await call("write_file", {"path": str(tree["allowed"]), "content": "x"})
    assert failed


async def test_write_is_refused_and_hidden_in_read_only_mode(tree, monkeypatch):
    monkeypatch.setenv("MCP_READ_ONLY", "1")
    async with Client(module.server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("write_file", {"path": "ro.txt", "content": "x"})
    assert "write_file" not in names
    assert result.is_error
    assert not (tree["allowed"] / "ro.txt").exists()
