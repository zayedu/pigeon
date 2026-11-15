import json
from pathlib import Path

from typer.testing import CliRunner

from pigeon.cli import HOMEBREW_LAUNCHER_ENV, app
from pigeon.config import Config, save


def test_homebrew_setup_registers_stable_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    launcher = tmp_path / "opt" / "pigeon" / "bin" / "pigeon"
    monkeypatch.setenv(HOMEBREW_LAUNCHER_ENV, str(launcher))
    profile = tmp_path / "profile.json"
    save(profile, Config(relay="ws://127.0.0.1:8765", agent="@test/agent", token="a" * 43))

    for _ in range(2):
        result = CliRunner().invoke(app, ["--config", str(profile), "setup", "cursor"])
        assert result.exit_code == 0, result.output

    servers = json.loads((Path.home() / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert servers["pigeon-relay"] == {
        "command": str(launcher),
        "args": ["--config", str(profile), "mcp"],
    }


def test_homebrew_update_uses_brew_instructions(monkeypatch):
    monkeypatch.setenv(HOMEBREW_LAUNCHER_ENV, "/opt/homebrew/opt/pigeon/bin/pigeon")
    monkeypatch.setenv("PATH", "")

    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0
    assert "brew upgrade pigeon" in result.output
