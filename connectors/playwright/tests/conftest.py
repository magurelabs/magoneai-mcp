"""A fresh artifacts root per test, and the connector's gateway pointed at a fake upstream."""

import importlib
import os
from pathlib import Path

import pytest

from .fakes import FakeUpstream

module = importlib.import_module("connectors.playwright.server")
gateway_module = importlib.import_module("connectors.playwright.gateway")


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """An artifacts root, resolved (macOS tmp lives behind a /private symlink)."""
    base = tmp_path / "artifacts"
    base.mkdir()
    monkeypatch.setenv("ARTIFACTS_ROOT", str(base))
    for name in ("ARTIFACTS_MAX_BYTES", "ARTIFACTS_DELETE_AFTER_PUBLISH", "MCP_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    return Path(os.path.realpath(base))


@pytest.fixture
def upstream():
    """The module's own gateway, relaying to a FakeUpstream, reset afterwards.

    The real `server` object is used rather than a copy, so what is tested is
    the wiring a deployment runs: the same tool table, the same list and call
    wrappers, the same framework controls.
    """
    fake = FakeUpstream()
    browser = module.browser

    def reset(expose_unsafe: bool = False) -> None:
        browser.apply_manifest([])
        browser.pool._sessions.clear()
        browser.pool._locks.clear()
        browser._next_fetch = 0.0
        browser._backoff = gateway_module.FIRST_RETRY_SECONDS
        browser.expose_unsafe = expose_unsafe

    saved = browser.pool.rpc.transport
    browser.pool.rpc.transport = fake.transport
    reset()
    fake.reset = reset
    yield fake
    browser.pool.rpc.transport = saved
    reset()
