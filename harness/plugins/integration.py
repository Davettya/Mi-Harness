"""Implemented declarative extension ports for the production composition root."""
from __future__ import annotations

from pathlib import Path
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from harness.core import HarnessError, content_hash
from harness.mcp.models import ConnectionProfile
from harness.runtime.models import AgentSpec
from .registry import ExtensionRegistry, PluginRegistry


class HostExtensionRegistry(ExtensionRegistry):
    """Static declarations only; manifest fields never create grants or import code."""
    def __init__(self, store):
        super().__init__(supported={"skill_sources", "skills", "mcp_connectors", "agent_templates"})
        self.store = store

    def _contributions(self, manifest):
        for category, items in manifest.contributes.model_dump().items():
            if items and category not in self.supported:
                raise ValueError(f"Host extension implementation unavailable: {category}")
            for item in items:
                name = item.get("id") or item.get("name")
                if not name:
                    raise ValueError("plugin contribution requires id")
                if category in {"skills", "skill_sources"}:
                    unknown = set(item) - {"id", "name", "root", "scope", "enabled"}
                    if unknown:
                        raise ValueError("skill contribution cannot set trust or execute install scripts")
                    path = Path(item["root"]).expanduser().resolve(strict=True)
                    if not path.is_dir():
                        raise ValueError("skill source root must be a directory")
                    config = {"id": name, "root": str(path), "scope": item.get("scope", "user"), "enabled": item.get("enabled", True), "trusted": False}
                    if config["scope"] not in {"user", "project"}:
                        raise ValueError("plugin sources cannot impersonate built-in trust")
                    namespace = "config/skill_sources"
                elif category == "mcp_connectors":
                    config = ConnectionProfile.model_validate({key: value for key, value in item.items() if key not in {"id", "name"}} | {"server_id": name}).model_dump(mode="json")
                    namespace = "config/mcp"
                else:
                    agent = AgentSpec.model_validate({key: value for key, value in item.items() if key != "name"} | {"id": name})
                    if agent.context_policy != "default" or self.store.get("config/policies",agent.policy) is None:
                        raise ValueError("plugin Agent references an unavailable policy")
                    config = agent.model_dump(mode="json")
                    namespace = "config/agents"
                yield category, name, namespace, config

    def register_atomic(self, manifest):
        contributions = list(self._contributions(manifest))
        with self._lock, self.store.transaction():
            for category, name, namespace, config in contributions:
                owner_key = content_hash([namespace, name])
                owner = self.store.get("plugin_contribution_owners", owner_key)
                existing = self.store.get(namespace, name)
                if existing is not None and (not owner or owner["plugin_id"] != manifest.id):
                    raise ValueError(f"plugin cannot overwrite another owner: {namespace}/{name}")
            super().register_atomic(manifest)
            try:
                for category, name, namespace, config in contributions:
                    owner_key = content_hash([namespace, name])
                    owner = self.store.get("plugin_contribution_owners", owner_key)
                    # Rehydrating old draining versions must never replace the newest default.
                    publish = not owner or Version(owner["version"]) < Version(manifest.version)
                    entry = {"plugin_id": manifest.id, "version": manifest.version, "namespace": namespace,
                             "name": name, "config": config, "category": category}
                    self.store.put("plugin_contributions", content_hash([manifest.id, manifest.version, namespace, name]), entry)
                    if publish:
                        existing = self.store.get(namespace, name) or {}
                        config = dict(config, revision=existing.get("revision", 0) + 1)
                        self.store.put(namespace, name, config)
                        self.store.put("plugin_contribution_owners", owner_key, {k: v for k, v in entry.items() if k != "config"})
            except Exception:
                super().unregister(manifest.id, manifest.version)
                raise

    def unregister(self, plugin_id, version):
        with self._lock, self.store.transaction():
            for entry in self.store.list("plugin_contributions"):
                if (entry["plugin_id"], entry["version"]) != (plugin_id, version):
                    continue
                namespace, name = entry["namespace"], entry["name"]
                self.store.delete("plugin_contributions", content_hash([plugin_id, version, namespace, name]))
                owner_key = content_hash([namespace, name])
                owner = self.store.get("plugin_contribution_owners", owner_key)
                if owner and (owner["plugin_id"], owner["version"]) == (plugin_id, version):
                    alternatives = [item for item in self.store.list("plugin_contributions")
                                    if item["namespace"] == namespace and item["name"] == name]
                    if alternatives:
                        chosen = max(alternatives, key=lambda item: Version(item["version"]))
                        self.store.put(namespace, name, chosen["config"])
                        self.store.put("plugin_contribution_owners", owner_key, {k: v for k, v in chosen.items() if k != "config"})
                    else:
                        self.store.delete(namespace, name)
                        self.store.delete("plugin_contribution_owners", owner_key)
            super().unregister(plugin_id, version)


class HostPluginManager:
    def __init__(self, store):
        self.store = store
        self.registry = PluginRegistry(store, extensions=HostExtensionRegistry(store))

    def recover(self):
        self.registry.recover()
        # Run snapshots, not process-local invocation counts, are authoritative references.
        active = self.store.runs(statuses=["queued", "running", "waiting_user", "waiting_children", "recovering", "needs_review", "cancelling"])
        live_ids = {run["id"] for run in active}
        for reference in self.store.list("plugin_run_refs"):
            if reference["run_id"] not in live_ids:
                self.registry.release_run(reference["run_id"])
        for run in active:
            snapshot = self.store.get("snapshots", run["snapshot_id"]) or {}
            for reference in snapshot.get("plugins", []):
                key = reference["plugin_id"] + "@" + reference["version"] + ":" + run["id"]
                if not self.store.get("plugin_run_refs", key):
                    record = self.registry._record(reference["plugin_id"], reference["version"])
                    if record["manifest_hash"] != reference["manifest_hash"] or record["status"] not in {"ready", "draining"}:
                        raise RuntimeError("active run plugin lock cannot be restored")
                    self.store.put("plugin_run_refs", key, {**reference, "run_id": run["id"]})

    def snapshot_refs(self, *, agent_id, mcp_servers, skills):
        requested = [("config/agents", agent_id)] + [("config/mcp", server) for server in mcp_servers]
        requested.extend(("config/skill_sources", skill["source_id"]) for skill in skills)
        references = {}
        def include(plugin_id, version):
            key = plugin_id + "@" + version
            if key in references:
                return
            record = self.registry._record(plugin_id, version)
            if record["status"] != "ready":
                raise HarnessError("PLUGIN_DRAINING", "插件版本已停止接受新的任务绑定", 409)
            references[key] = {"plugin_id": plugin_id, "version": version, "manifest_hash": record["manifest_hash"]}
            for dependency, constraint in record["manifest"]["requires"]["plugins"].items():
                candidates = [r for r in self.store.list("plugins") if r["manifest"]["id"] == dependency
                              and r["status"] == "ready" and Version(r["manifest"]["version"]) in SpecifierSet(constraint)]
                if not candidates:
                    raise HarnessError("PLUGIN_DEPENDENCY", "插件依赖未就绪", 409)
                chosen = max(candidates, key=lambda r: Version(r["manifest"]["version"]))
                include(dependency, chosen["manifest"]["version"])
        for namespace, name in requested:
            owner = self.store.get("plugin_contribution_owners", content_hash([namespace, name]))
            if owner:
                include(owner["plugin_id"], owner["version"])
        return list(references.values())

    def bind_run(self, run, snapshot):
        for reference in snapshot.get("plugins", []):
            bound = self.registry.bind_run(reference["plugin_id"], reference["version"], run["id"])
            if bound["manifest_hash"] != reference["manifest_hash"]:
                raise HarnessError("PLUGIN_LOCK_CHANGED", "插件版本锁与快照不一致", 409)

    def validate_run(self, ctx, snapshot):
        for reference in snapshot.get("plugins", []):
            key = reference["plugin_id"] + "@" + reference["version"] + ":" + ctx.run_id
            bound = self.store.get("plugin_run_refs", key)
            if not bound or bound["manifest_hash"] != reference["manifest_hash"]:
                raise HarnessError("PLUGIN_RUN_BINDING", "运行未持有固定插件版本", 409)
            record = self.registry._record(reference["plugin_id"], reference["version"])
            if record["status"] not in {"ready", "draining"}:
                raise HarnessError("PLUGIN_UNAVAILABLE", "固定插件版本不可用", 409)
