import json

import pytest

from harness.core import HarnessError
from harness.mcp.config_file import McpConfigFileService, digest
from test_integration import setup_services


def document(**overrides):
    return (
        json.dumps(
            dict(
                schema_version=1,
                mcpServers={
                    "local": dict(
                        enabled=False,
                        display_name="Fixture",
                        transport="stdio",
                        command="python",
                        **overrides,
                    )
                },
            ),
            indent=2,
        )
        + "\n"
    )


def test_file_original_text_reload_versions_and_conflict(tmp_path):
    services, _, _ = setup_services(tmp_path)
    config = services.mcp_file
    first = config.view()
    text = document()
    saved = config.save(text, first["hash"])
    assert config.path.read_bytes() == text.encode() and saved["effective_hash"] == digest(text)
    assert services.resolve_mcp_profile("local").config_revision == 1
    with pytest.raises(HarnessError) as exc:
        config.save(document(args=["new"]), first["hash"])
    assert exc.value.status == 412 and config.path.read_text() == text
    external = document(args=["-V"])
    config.path.write_bytes(external.encode())
    config.reload()
    assert services.resolve_mcp_profile("local").config_revision == 2
    assert config.view()["text"] == external
    assert config.reload()["revision"] == 3  # initial empty + two valid publications
    with pytest.raises(HarnessError) as exc:
        services.save_config("mcp", "local", {})
    assert exc.value.code == "MCP_FILE_REQUIRED"


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":1,"schema_version":1,"mcpServers":{}}',
        '{"schema_version":1,"mcpServers":{},"extra":true}',
        '{"schema_version":1,"mcpServers":',
        document(trusted=True),
        document(env={"API_KEY": "forbidden"}),
        document(apiKey="forbidden"),
        document(config_revision=0),
    ],
)
def test_invalid_file_keeps_last_projection_and_never_spawns(tmp_path, text, monkeypatch):
    services, _, _ = setup_services(tmp_path)
    config = services.mcp_file
    good = config.save(document(), config.view()["hash"])
    before = services.store.list("config/mcp")
    config.path.write_bytes(text.encode())
    result = config.reload()
    assert result["load_error"] and result["effective_hash"] == good["effective_hash"]
    assert services.store.list("config/mcp") == before
    assert result["text"] == text
    assert not services.store.list("process_sessions")


def test_replace_before_projection_crash_recovery_does_not_overwrite_newer_file(tmp_path, monkeypatch):
    services, _, _ = setup_services(tmp_path)
    config = services.mcp_file
    original = config._publish

    def crash(*args, **kwargs):
        raise RuntimeError("injected after replace")

    monkeypatch.setattr(config, "_publish", crash)
    with pytest.raises(RuntimeError):
        config.save(document(), config.view()["hash"])
    assert not services.store.get("config/mcp", "local")
    newer = document(args=["newer"])
    config.path.write_bytes(newer.encode())
    monkeypatch.setattr(config, "_publish", original)
    recovered = McpConfigFileService(services.store, services.data_dir)
    recovered.initialize()
    assert recovered.view()["text"] == newer
    assert services.resolve_mcp_profile("local").args == ["newer"]
    assert recovered.view()["effective_hash"] == digest(newer)


def test_initial_migration_only_when_file_absent_and_backup_contains_file(tmp_path):
    from harness.storage.backup import backup, restore

    services, _, _ = setup_services(tmp_path)
    config = services.mcp_file
    config.save(document(), config.view()["hash"])
    destination = tmp_path / "backup"
    manifest = backup(services.data_dir, destination, writers_stopped=True)
    assert "mcp.json" in manifest["files"]
    restored = tmp_path / "restored"
    restore(destination, restored)
    assert (restored / "mcp.json").read_bytes() == config.path.read_bytes()
    # Migration is one-time: losing the desired file must not re-export retained
    # historical server records and silently resurrect deleted configuration.
    config.path.unlink()
    config.initialize()
    assert not config.path.exists()
    assert config.view()["load_error"]


def test_file_symlink_refused(tmp_path):
    services, _, _ = setup_services(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text(document())
    services.mcp_file.path.unlink()
    try:
        services.mcp_file.path.symlink_to(target)
    except OSError:
        pytest.skip("Host does not allow unprivileged symlink creation")
    with pytest.raises(HarnessError) as exc:
        services.mcp_file.view()
    assert exc.value.code == "MCP_CONFIG_PATH"
