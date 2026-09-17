from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager, nullcontext
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from .manifest import EXTENSIONS, PluginManifest


class ExtensionRegistry:
    """Only host-registered extension points can accept staged contributions."""
    def __init__(self, supported=EXTENSIONS):
        self.supported = set(supported)
        self.entries: dict[tuple[str, str, str, str], dict] = {}
        self._lock = threading.RLock()

    def register_atomic(self, manifest: PluginManifest):
        staged = {}
        with self._lock:
            for category, contributions in manifest.contributes.model_dump().items():
                if contributions and category not in self.supported:
                    raise ValueError(f"extension point not implemented: {category}")
                for entry in contributions:
                    name = entry.get("id") or entry.get("name")
                    if not isinstance(name, str) or not name:
                        raise ValueError("contribution needs a stable id or name")
                    key = (manifest.id, manifest.version, category, name)
                    if key in staged:
                        raise ValueError("duplicate contribution in manifest")
                    if any(k[2:] == key[2:] and k[0] != manifest.id for k in self.entries):
                        raise ValueError(f"extension name conflict: {category}/{name}")
                    staged[key] = entry
            self.entries.update(staged)

    def unregister(self, plugin_id, version):
        with self._lock:
            self.entries = {k: v for k, v in self.entries.items() if k[:2] != (plugin_id, version)}


class PluginRegistry:
    def __init__(self, repository, factories: dict | None = None, extensions=None, *, harness_api="1.0.0"):
        self.repository, self.factories = repository, factories or {}
        self.extensions, self.harness_api = extensions or ExtensionRegistry(), Version(harness_api)
        self.instances = {}
        self.inflight = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(plugin_id, version):
        return plugin_id + "@" + version

    def _record(self, plugin_id, version):
        record = self.repository.get("plugins", self._key(plugin_id, version))
        if not record:
            raise KeyError("plugin version not registered")
        return record

    def resolve_dependencies(self, manifests):
        pending = {}
        for item in manifests:
            pending.setdefault(item.id, []).append(item)
        installed = [r for r in self.repository.list("plugins") if r["status"] == "ready"]
        visiting, visited, order = set(), set(), []

        def visit(manifest):
            identity = self._key(manifest.id, manifest.version)
            if identity in visiting:
                raise ValueError("plugin dependency cycle")
            if identity in visited:
                return
            visiting.add(identity)
            for dependency, constraint in manifest.requires.plugins.items():
                requirement = SpecifierSet(constraint)
                if dependency in pending:
                    candidates = [candidate for candidate in pending[dependency] if Version(candidate.version) in requirement]
                    if not candidates:
                        raise ValueError(f"plugin dependency version conflict: {dependency}")
                    chosen = max(candidates, key=lambda candidate: Version(candidate.version))
                    visit(chosen)
                elif not any(r["manifest"]["id"] == dependency and Version(r["manifest"]["version"]) in requirement for r in installed):
                    raise ValueError(f"missing plugin dependency: {dependency}")
            visiting.remove(identity)
            visited.add(identity)
            order.append(manifest)
        for manifest in manifests:
            visit(manifest)
        return order

    def load(self, manifest_ref, *, rehydrate=False):
        manifest = manifest_ref if isinstance(manifest_ref, PluginManifest) else PluginManifest.model_validate(manifest_ref)
        if self.harness_api not in SpecifierSet(manifest.harness_api):
            raise ValueError("plugin harness API range is incompatible")
        if manifest.entrypoint and manifest.entrypoint not in self.factories:
            raise PermissionError("plugin entrypoint is not a host built-in factory")
        self.resolve_dependencies([manifest])
        key = self._key(manifest.id, manifest.version)
        with self._lock, (self.repository.transaction() if hasattr(self.repository, "transaction") else nullcontext()):
            previous = self.repository.get("plugins", key)
            if rehydrate and (not previous or previous["status"] not in {"ready", "draining"}):
                return previous
            canonical = json.dumps(manifest.model_dump(), sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            if previous and previous["manifest_hash"] != digest:
                raise ValueError("plugin version is immutable; increment its version")
            if key in self.instances:
                return previous
            instance = None
            try:
                if manifest.entrypoint:
                    instance = self.factories[manifest.entrypoint](manifest)
                    instance.initialize()
                self.extensions.register_atomic(manifest)
                if instance and hasattr(instance, "register"):
                    instance.register()
                record = {"manifest": manifest.model_dump(), "manifest_hash": digest, "status": "ready",
                          "revision": (previous or {}).get("revision", 0) + 1,
                          "implementation_version": manifest.version, "diagnostics": [],
                          "lifecycle": ["discover", "validate", "resolve_dependencies", "initialize", "register", "ready"]}
                if previous and previous["status"] == "draining":
                    record["status"] = "draining"
                self.repository.put("plugins", key, record)
                self.instances[key] = instance
                return record
            except Exception:
                self.extensions.unregister(manifest.id, manifest.version)
                if instance:
                    try:
                        instance.dispose()
                    except Exception:
                        # Keep the handle; supervisor can retry cleanup.
                        self.instances[key] = instance
                        self.repository.put("plugins", key, {"manifest": manifest.model_dump(), "manifest_hash": digest,
                            "status": "dispose_failed", "revision": 1, "diagnostics": ["rollback dispose failed"]})
                raise

    def bind_run(self, plugin_id, version, run_id):
        with self._lock:
            record = self._record(plugin_id, version)
            key = self._key(plugin_id, version) + ":" + run_id
            existing = self.repository.get("plugin_run_refs", key)
            if existing:
                return existing
            if record["status"] != "ready":
                raise ValueError("plugin version is draining; new run bindings rejected")
            value = {"plugin_id": plugin_id, "version": version, "run_id": run_id,
                     "manifest_hash": record["manifest_hash"]}
            self.repository.put("plugin_run_refs", key, value)
            return value

    def references(self, plugin_id, version):
        return [r for r in self.repository.list("plugin_run_refs") if (r["plugin_id"], r["version"]) == (plugin_id, version)]

    @contextmanager
    def invocation(self, plugin_id, version, run_id):
        key = self._key(plugin_id, version)
        with self._lock:
            if not self.repository.get("plugin_run_refs", key + ":" + run_id):
                raise PermissionError("run does not hold this plugin version")
            if key not in self.instances:
                raise RuntimeError("plugin implementation not rehydrated after restart")
            if self._record(plugin_id, version)["status"] not in ("ready", "draining"):
                raise PermissionError("plugin unavailable")
            self.inflight[key] = self.inflight.get(key, 0) + 1
        try:
            yield self.instances[key]
        finally:
            with self._lock:
                self.inflight[key] -= 1
                self._dispose_if_unused(plugin_id, version)

    def drain(self, plugin_id, version):
        with self._lock, (self.repository.transaction() if hasattr(self.repository, "transaction") else nullcontext()):
            record = self._record(plugin_id, version)
            if record["status"] == "disposed":
                return record
            record["status"] = "draining"
            record["revision"] += 1
            record.setdefault("lifecycle", []).append("drain")
            self.repository.put("plugins", self._key(plugin_id, version), record)
            return self._dispose_if_unused(plugin_id, version)

    def release_run(self, run_id):
        with self._lock, (self.repository.transaction() if hasattr(self.repository, "transaction") else nullcontext()):
            for reference in self.repository.list("plugin_run_refs"):
                if reference["run_id"] == run_id:
                    plugin_id, version = reference["plugin_id"], reference["version"]
                    self.repository.delete("plugin_run_refs", self._key(plugin_id, version) + ":" + run_id)
                    self._dispose_if_unused(plugin_id, version)

    def _dispose_if_unused(self, plugin_id, version):
        key = self._key(plugin_id, version)
        record = self._record(plugin_id, version)
        if record["status"] != "draining" or self.references(plugin_id, version) or self.inflight.get(key, 0):
            return record
        instance = self.instances.get(key)
        try:
            if instance:
                instance.dispose()
            self.extensions.unregister(plugin_id, version)
            self.instances.pop(key, None)
            record["status"] = "disposed"
            record.setdefault("lifecycle", []).append("dispose")
        except Exception:
            record["status"] = "dispose_failed"
            record.setdefault("diagnostics", []).append("dispose failed; supervisor cleanup required")
        self.repository.put("plugins", key, record)
        return record

    def recover(self):
        """Persistent run refs protect code while waiting runs survive process restarts."""
        records = [r for r in self.repository.list("plugins") if r["status"] in ("ready", "draining")]
        for manifest in self.resolve_dependencies([PluginManifest.model_validate(r["manifest"]) for r in records]):
            self.load(manifest,rehydrate=True)
        return records
