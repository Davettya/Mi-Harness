from __future__ import annotations

import ipaddress
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from harness.core import AccessContext, DiagnosticContext, ExecutionContext, HarnessError, content_hash
from harness.storage import Store

DEFAULT_POLICY = {
    "revision": 1,
    "egress": "cloud_allowed",
    "domains": [],
    "capabilities": [
        "file_read",
        "file_write",
        "process",
        "network",
        "input",
        "artifact",
        "delegate",
        "memory",
        "skills",
        "mcp",
    ],
    "approval_effects": ["workspace_write", "process", "external_write"],
    "trusted_local_execution": True,
}


@dataclass(frozen=True)
class ModelSetupGrant:
    """In-memory authority minted only by the paired user's model setup service."""

    owner_id: str
    diagnostic_id: str
    profile_ref: str
    endpoint: str


class PolicyEngine:
    def __init__(self, store: Store):
        self.store = store

    def set_environment_cap(self, permissions):
        value = permissions.model_dump() if hasattr(permissions, "model_dump") else dict(permissions)
        with self.store.transaction():
            prior = self.store.get("system", "platform_permissions") or {}
            if prior.get("permissions") != value:
                self.store.put(
                    "system",
                    "platform_permissions",
                    dict(permissions=value, revision=prior.get("revision", 0) + 1),
                )

    def _environment_cap(self, policy):
        record = self.store.get("system", "platform_permissions")
        if not record:
            return policy
        cap = record["permissions"]
        capabilities = set(policy["capabilities"])
        if not cap["network"]:
            capabilities.discard("network")
        if not cap["process"]:
            capabilities.discard("process")
        if not cap["workspace_write"]:
            capabilities.discard("file_write")
        policy = {
            **policy,
            "capabilities": sorted(capabilities),
            "trusted_local_execution": policy["trusted_local_execution"] and cap["process"],
            "platform_network": cap["network"],
            "platform_process": cap["process"],
            "revision": record["revision"] * 1000000000000 + policy["revision"],
        }
        if cap["allowed_hosts"]:
            hosts = set(cap["allowed_hosts"])
            if policy["egress"] == "domain_allowlist":
                hosts &= set(policy["domains"])
            policy["platform_allowed_hosts"] = sorted(hosts)
        return policy

    def current(self, ctx: AccessContext) -> dict:
        global_policy = {**DEFAULT_POLICY, **(self.store.get("config/policies", "default") or {})}
        if ctx.workspace_id:
            workspace = self.store.workspace(ctx.workspace_id, ctx.owner_id)
            local = {**DEFAULT_POLICY, **workspace["policy"]}
            combined = {
                **local,
                "capabilities": sorted(set(local["capabilities"]) & set(global_policy["capabilities"])),
                "approval_effects": sorted(
                    set(local["approval_effects"]) | set(global_policy["approval_effects"])
                ),
                "revision": global_policy["revision"] * 1000000 + local["revision"],
            }
            if "local_only" in {local["egress"], global_policy["egress"]}:
                combined["egress"] = "local_only"
            elif "domain_allowlist" in {local["egress"], global_policy["egress"]}:
                combined["egress"] = "domain_allowlist"
                sets = [
                    set(p["domains"]) for p in (local, global_policy) if p["egress"] == "domain_allowlist"
                ]
                combined["domains"] = sorted(set.intersection(*sets))
            combined["trusted_local_execution"] = (
                local["trusted_local_execution"] and global_policy["trusted_local_execution"]
            )
            return self._environment_cap(combined)
        return self._environment_cap(global_policy)

    def for_agent(self, ctx, policy_ref="default"):
        base = self.current(ctx)
        if policy_ref == "default":
            return base
        named = self.store.get("config/policies", policy_ref)
        if named is None:
            raise HarnessError("POLICY_NOT_FOUND", "Agent 引用的权限策略不存在或已撤销", 403)
        named = {**DEFAULT_POLICY, **named}
        combined = {
            **base,
            "capabilities": sorted(set(base["capabilities"]) & set(named["capabilities"])),
            "approval_effects": sorted(set(base["approval_effects"]) | set(named["approval_effects"])),
            "trusted_local_execution": base["trusted_local_execution"] and named["trusted_local_execution"],
        }
        if "local_only" in {base["egress"], named["egress"]}:
            combined["egress"] = "local_only"
        elif "domain_allowlist" in {base["egress"], named["egress"]}:
            combined["egress"] = "domain_allowlist"
            sets = [set(p["domains"]) for p in (base, named) if p["egress"] == "domain_allowlist"]
            combined["domains"] = sorted(set.intersection(*sets))
        # Full policy content participates; keep the opaque revision exact in JavaScript integers.
        combined["revision"] = int(content_hash([base, named, policy_ref])[:13], 16)
        combined["agent_policy_ref"] = policy_ref
        return combined

    def effective(self, ctx: ExecutionContext) -> dict:
        snapshot = self.store.get("snapshots", ctx.config_snapshot_id) or {}
        current = self.for_agent(ctx, snapshot.get("agent_spec", {}).get("policy", "default"))
        frozen = snapshot.get("policy", current)
        capabilities = set(current["capabilities"]) & set(frozen.get("capabilities", []))
        agent = snapshot.get("agent_spec", {})
        if isinstance(agent.get("policy"), dict) and "capabilities" in agent["policy"]:
            capabilities &= set(agent["policy"]["capabilities"])
        result = {
            **current,
            "capabilities": sorted(capabilities),
            "approval_effects": sorted(
                set(current["approval_effects"]) | set(frozen.get("approval_effects", []))
            ),
        }
        if "local_only" in {current["egress"], frozen.get("egress")}:
            result["egress"] = "local_only"
        elif "domain_allowlist" in {current["egress"], frozen.get("egress")}:
            result["egress"] = "domain_allowlist"
            domains = [
                set(p.get("domains", [])) for p in (current, frozen) if p.get("egress") == "domain_allowlist"
            ]
            result["domains"] = sorted(set.intersection(*domains))
        for key in ("platform_network", "platform_process", "trusted_local_execution"):
            result[key] = current.get(key, True) and frozen.get(key, True)
        host_sets = [
            set(p["platform_allowed_hosts"]) for p in (current, frozen) if "platform_allowed_hosts" in p
        ]
        if host_sets:
            result["platform_allowed_hosts"] = sorted(set.intersection(*host_sets))
        return result

    def decide(self, ctx: ExecutionContext, spec, args: dict) -> dict:
        if not isinstance(ctx, ExecutionContext):
            raise HarnessError("DIAGNOSTIC_TOOL_DENIED", "诊断身份不可执行工具", 403)
        self.store.assert_fence(ctx)
        policy = self.effective(ctx)
        required = set(spec.required_capabilities)
        snapshot = self.store.get("snapshots", ctx.config_snapshot_id) or {}
        enabled_servers = {p.get("server_id") for p in snapshot.get("mcp", [])}
        for capability in list(required):
            if capability.startswith("mcp:") and capability[4:] in enabled_servers:
                required.remove(capability)
                required.add("mcp")
        if not required <= set(policy["capabilities"]):
            return dict(
                decision="deny", reason="工具能力超出当前有效权限", policy_revision=policy["revision"]
            )
        if spec.effect == "process" and not policy.get("trusted_local_execution"):
            return dict(decision="deny", reason="未启用可信本机执行模式", policy_revision=policy["revision"])
        needs_approval = spec.effect in policy["approval_effects"] or spec.id == "request_approval"
        return dict(
            decision="approval" if needs_approval else "allow",
            reason="需要批准具体操作" if needs_approval else "在当前授权范围内",
            policy_revision=policy["revision"],
        )

    def path(self, ctx: ExecutionContext, candidate: str, *, write: bool = False) -> Path:
        workspace = self.store.workspace(ctx.workspace_id, ctx.owner_id)
        roots = [Path(value).resolve() for value in workspace["roots"]]
        root = roots[0]
        if not isinstance(candidate, str) or not candidate or "\x00" in candidate:
            raise HarnessError("PATH_DENIED", "路径无效", 403)
        normalized = candidate.replace("/", "\\")
        if normalized.startswith("\\\\") or normalized.startswith("\\?\\") or normalized.startswith("\\.\\"):
            raise HarnessError("PATH_DENIED", "不允许 UNC 或设备路径", 403)
        given = Path(candidate)
        raw = given if given.is_absolute() else root / given
        # Resolve every ancestor, including reparse points. No lexical prefix authorization.
        resolved = raw.resolve(strict=False)
        matching = [value for value in roots if resolved.is_relative_to(value)]
        if not matching:
            raise HarnessError("PATH_DENIED", "目标不在授权工作区内", 403)
        root = max(matching, key=lambda value: len(value.parts))
        if os.name == "nt" and ":" in str(resolved)[2:]:
            raise HarnessError("PATH_DENIED", "不允许备用数据流", 403)
        for parent in [raw, *raw.parents]:
            if parent.exists() or parent.is_symlink():
                st = parent.lstat()
                if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
                    raise HarnessError("PATH_DENIED", "链接与 reparse point 不可作为文件工具目标", 403)
            if parent == root:
                break
        if write and resolved in roots:
            raise HarnessError("PATH_DENIED", "不可覆盖工作区根目录", 403)
        capability = "file_write" if write else "file_read"
        if capability not in self.effective(ctx)["capabilities"]:
            raise HarnessError("CAPABILITY_DENIED", "工作区文件能力已撤销", 403)
        return resolved

    def endpoint(
        self,
        url: str,
        ctx: AccessContext,
        *,
        purpose: str,
        configured: bool = False,
        model_profile_ref: str | None = None,
        model_setup_grant: ModelSetupGrant | None = None,
    ) -> list[str]:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise HarnessError("ENDPOINT_DENIED", "网络目标格式不允许", 403)
        policy = self.effective(ctx) if isinstance(ctx, ExecutionContext) else self.current(ctx)
        model_granted = False
        if purpose == "model" and configured and model_profile_ref:
            if isinstance(model_setup_grant, ModelSetupGrant) and isinstance(ctx, DiagnosticContext):
                model_granted = (
                    model_setup_grant.owner_id == ctx.owner_id
                    and model_setup_grant.diagnostic_id == ctx.diagnostic_id
                    and model_setup_grant.profile_ref == model_profile_ref
                    and model_setup_grant.endpoint == url
                )
            saved = self.store.get("model_endpoint_grants", model_profile_ref)
            if saved and saved.get("enabled"):
                model_granted = model_granted or (
                    saved.get("owner_id") == ctx.owner_id
                    and saved.get("profile_ref") == model_profile_ref
                    and saved.get("endpoint") == url
                )
        if not policy.get("platform_network", True) and not model_granted:
            raise HarnessError("NETWORK_DISABLED", "当前启动配置禁止网络访问（包括配置端点诊断）", 403)
        if "platform_allowed_hosts" in policy and parts.hostname not in policy["platform_allowed_hosts"]:
            raise HarnessError("EGRESS_DENIED", "目标不在当前启动配置允许的主机中", 403)
        addresses = sorted(
            {
                x[4][0]
                for x in socket.getaddrinfo(
                    parts.hostname,
                    parts.port or (443 if parts.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        )
        if not addresses:
            raise HarnessError("ENDPOINT_DENIED", "网络目标无法解析", 403)
        ips = [ipaddress.ip_address(x) for x in addresses]
        local = all(x.is_loopback for x in ips)
        # TUN proxies synthesize benchmark-range addresses for public hostnames.
        # Only exact, authorized official HTTPS endpoints qualify: provider transports
        # retain hostname certificate verification and never follow redirects.
        fake_ip_model = False
        if model_granted and parts.scheme == "https":
            from harness.model_gateway.presets import PRESETS

            official = {
                p["default_base_url"].rstrip("/")
                for p in PRESETS.values()
                if not p["allow_custom_base_url"] and p["default_base_url"].startswith("https://")
            }
            fake_range = ipaddress.ip_network("198.18.0.0/15")
            fake_ip_model = url.rstrip("/") in official and all(
                ip.is_global or (ip.version == 4 and ip in fake_range) for ip in ips
            )
        if any(not x.is_global for x in ips) and not (
            fake_ip_model or (configured and purpose in {"model", "mcp"} and local)
        ):
            raise HarnessError("SSRF_DENIED", "禁止访问私网、链路本地或元数据地址", 403)
        if policy["egress"] == "local_only" and not local:
            raise HarnessError("EGRESS_DENIED", "此工作区仅允许本地数据处理", 403)
        if policy["egress"] == "domain_allowlist" and parts.hostname not in policy.get("domains", []):
            raise HarnessError("EGRESS_DENIED", "域名不在授权列表中", 403)
        return addresses

    def authorize_skill(self, action: str, ctx: AccessContext, record) -> None:
        if not isinstance(ctx, ExecutionContext) or "skills" not in self.effective(ctx)["capabilities"]:
            raise HarnessError("SKILL_DENIED", "Skill 不在任务授权范围", 403)

    def authorize_mcp(self, profile, ctx: AccessContext, purpose: str) -> None:
        if getattr(profile, "transport", None) != "stdio":
            self.endpoint(profile.endpoint, ctx, purpose="mcp", configured=True)
        elif not getattr(profile, "command", None):
            raise HarnessError("MCP_CONFIG", "未配置 MCP 启动命令", 422)
        elif not self.current(ctx).get("platform_process", True):
            raise HarnessError("PROCESS_DISABLED", "当前启动配置禁止 stdio MCP 进程启动", 403)
        if isinstance(ctx, ExecutionContext) and "mcp" not in self.effective(ctx)["capabilities"]:
            raise HarnessError("MCP_DENIED", "MCP 能力已撤销", 403)

    def fingerprint(self, ctx: ExecutionContext, paths: list[str], write: bool = False) -> str:
        values = []
        for item in paths:
            path = self.path(ctx, item, write=write)
            st = path.stat() if path.exists() else None
            values.append(
                dict(
                    path=os.path.normcase(str(path)),
                    device=st.st_dev if st else None,
                    inode=st.st_ino if st else None,
                    size=st.st_size if st else None,
                    mtime=st.st_mtime_ns if st else None,
                )
            )
        return content_hash(values)
