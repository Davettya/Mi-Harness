import pytest
from harness.plugins import PluginManifest, PluginRegistry, ExtensionRegistry
from test_skills import Repo


def manifest(version="1.0.0", **kwargs):
    return PluginManifest(id="fixture", version=version, harness_api=">=1,<2", **kwargs)


class Instance:
    def __init__(self): self.disposed = False
    def initialize(self): pass
    def dispose(self): self.disposed = True


def test_s02_atomic_registry_factory_allowlist_and_rollback():
    repo = Repo()
    instance = Instance()
    registry = PluginRegistry(repo, {"builtin": lambda _: instance}, ExtensionRegistry(supported=["tools"]))
    with pytest.raises(PermissionError): registry.load(manifest(entrypoint="arbitrary.module:factory"))
    with pytest.raises(ValueError):
        registry.load(manifest(entrypoint="builtin", contributes={"tools": [{"name": "safe"}], "runtimes": [{"name": "unknown"}]}))
    assert not registry.extensions.entries
    assert instance.disposed
    assert not repo.list("plugins")


def test_s02_drain_waiting_run_and_restart():
    repo, instance = Repo(), Instance()
    registry = PluginRegistry(repo, {"builtin": lambda _: instance})
    registry.load(manifest(entrypoint="builtin"))
    registry.bind_run("fixture", "1.0.0", "waiting-approval")
    assert registry.drain("fixture", "1.0.0")["status"] == "draining"
    with pytest.raises(ValueError): registry.bind_run("fixture", "1.0.0", "new-run")
    with registry.invocation("fixture", "1.0.0", "waiting-approval"):
        assert not instance.disposed
    restarted_instance = Instance()
    restarted = PluginRegistry(repo, {"builtin": lambda _: restarted_instance})
    restarted.recover()
    with restarted.invocation("fixture", "1.0.0", "waiting-approval"):
        assert not restarted_instance.disposed
    restarted.release_run("waiting-approval")
    assert restarted_instance.disposed
    assert repo.get("plugins", "fixture@1.0.0")["status"] == "disposed"


def test_s02_dependencies_and_manifest_are_not_grants():
    registry = PluginRegistry(Repo())
    with pytest.raises(ValueError): registry.load(manifest(requires={"plugins": {"missing": ">=1"}}))
    one = PluginManifest(id="one", version="1", harness_api=">=1", requires={"plugins": {"two": ">=1"}})
    two = PluginManifest(id="two", version="1", harness_api=">=1", requires={"plugins": {"one": ">=1"}})
    with pytest.raises(ValueError): registry.resolve_dependencies([one, two])
    result = registry.load(manifest(requires={"network": ["*"], "filesystem": ["*"]}))
    assert "grants" not in result
    with pytest.raises(ValueError):
        registry.load({"id": "bad", "version": "1", "harness_api": ">=1", "contributes": {"unknown": []}})


def test_s02_restart_retains_old_and_new_versions_together():
    repo = Repo()
    registry = PluginRegistry(repo)
    registry.load(manifest("1.0.0"))
    registry.bind_run("fixture", "1.0.0", "old-waiting")
    registry.load(manifest("2.0.0"))
    registry.bind_run("fixture", "2.0.0", "new-running")
    registry.drain("fixture", "1.0.0")
    restarted = PluginRegistry(repo)
    restarted.recover()
    assert set(restarted.instances) == {"fixture@1.0.0", "fixture@2.0.0"}
    with restarted.invocation("fixture", "1.0.0", "old-waiting"):
        pass
    with restarted.invocation("fixture", "2.0.0", "new-running"):
        pass
