import asyncio
from shutil import which

import pytest

from pigeon.adapters import Codex
from pigeon.models import PigeonError


def test_sandbox_probe_rejects_an_executable_that_only_returns_success(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = Codex(workspace, tmp_path / "state", executable=which("true"))
    with pytest.raises(PigeonError) as exc:
        asyncio.run(runtime.check_sandbox())
    assert exc.value.code == "sandbox_failed"
