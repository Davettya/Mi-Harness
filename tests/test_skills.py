from types import SimpleNamespace
from pathlib import Path
import copy
import pytest
from harness.skills import SkillRegistry, SkillActivator, SnapshotStore, LocalSkillSource, RemoteSkillSource
from harness.skills.parser import parse_skill


class Repo:
    def __init__(self): self.rows = {}
    def get(self, ns, key): return copy.deepcopy(self.rows.get((ns, key)))
    def put(self, ns, key, value): self.rows[ns, key] = copy.deepcopy(value)
    def list(self, ns): return copy.deepcopy([v for (n, _), v in self.rows.items() if n == ns])
    def delete(self, ns, key): self.rows.pop((ns, key), None)
    def compare_and_set(self, ns, key, revision, value):
        if self.get(ns, key)["revision"] != revision: raise ValueError("revision conflict")
        self.put(ns, key, value)


def context(run="run"):
    return SimpleNamespace(owner_id="owner", workspace_id="workspace", session_id="session", run_id=run)


def write_skill(root: Path, body="Original rules", name="fixture"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: useful test fixture\n---\n{body}\n", "utf-8")
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "guide.txt").write_text("Evidence", "utf-8")


def test_s01_fixed_snapshot_conflicts_and_escape(tmp_path):
    write_skill(tmp_path / "one")
    write_skill(tmp_path / "two")
    registry = SkillRegistry([LocalSkillSource("local", tmp_path, trusted=True)], SnapshotStore(tmp_path.parent / (tmp_path.name + "-snap")), Repo())
    report = registry.refresh()
    assert len(report.name_conflicts["fixture"]) == 2
    skill = next(r for r in report.refreshed if r.relative_path == "one")
    activator = SkillActivator(registry)
    activated = activator.activate(skill.skill_id, context())
    write_skill(tmp_path / "one", "New rules")
    registry.refresh()
    assert activator.activate(skill.skill_id, context()).body == activated.body
    assert activator.activate(skill.skill_id, context("new")).body != activated.body
    resource = activator.read_resource(activated.activation.activation_ref, "references/guide.txt", context())
    assert resource.content == b"Evidence"
    for illegal in ("../SKILL.md", "C:/secret", "references\\guide.txt", "//host/share", "references/../../secret"):
        with pytest.raises(PermissionError):
            activator.read_resource(activated.activation.activation_ref, illegal, context())
    with pytest.raises(PermissionError):
        activator.read_resource(activated.activation.activation_ref, "SKILL.md", context("other"))
    assert "New rules" in registry.diff(skill.skill_id, skill.content_hash, registry.get(skill.skill_id).content_hash, context())


def test_s01_invalid_safe_yaml_and_policy(tmp_path):
    write_skill(tmp_path / "valid")
    (tmp_path / "invalid").mkdir()
    (tmp_path / "invalid" / "SKILL.md").write_text("---\nname: !!python/object/apply:os.system ['whoami']\n---\n", "utf-8")
    registry = SkillRegistry([LocalSkillSource("source", tmp_path)], SnapshotStore(tmp_path.parent / (tmp_path.name + "-snap")), Repo())
    records = registry.refresh().refreshed
    assert len(records) == 2
    assert sum(bool(r.validation_diagnostics) for r in records) == 1
    assert registry.search("", context()) == []
    valid = next(r for r in records if r.enabled)
    with pytest.raises(PermissionError): SkillActivator(registry).activate(valid.skill_id, context())
    trusted = registry.configure(valid.skill_id, expected_revision=valid.revision, trust_state="trusted")
    assert len(registry.search("", context())) == 1
    with pytest.raises(ValueError): registry.configure(valid.skill_id, expected_revision=valid.revision, enabled=False)
    registry.configure(valid.skill_id, expected_revision=trusted.revision, enabled=False)
    with pytest.raises(PermissionError): SkillActivator(registry).activate(valid.skill_id, context())


def test_s01_snapshot_tamper_and_limits(tmp_path):
    root = tmp_path / "source"
    write_skill(root)
    source = LocalSkillSource("source", root, max_file_bytes=8)
    with pytest.raises(ValueError): source.snapshot(next(source.discover()))
    snapshots = SnapshotStore(tmp_path / "snapshots")
    digest = snapshots.put({"SKILL.md": b"data"})
    (tmp_path / "snapshots" / digest / "files" / "SKILL.md").write_bytes(b"corrupt")
    with pytest.raises(ValueError): snapshots.read(digest, "SKILL.md")


def test_remote_skill_requires_verified_extension():
    with pytest.raises(ValueError):
        RemoteSkillSource("remote", capability={}, discover=lambda: [], fetch_bundle=lambda _: {}, expected_revision="v1")
