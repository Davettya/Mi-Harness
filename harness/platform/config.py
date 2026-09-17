"""Strict configuration precedence with narrowing-only lower-trust permissions."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .credentials import assert_secret_refs


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "LocalAgentHarness"


class Permissions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    network: bool = False
    process: bool = False
    workspace_write: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data_dir: Path = Field(default_factory=default_data_dir)
    instance_id: str | None = None
    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(default=8767, ge=1024, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    startup_timeout: float = Field(default=20, gt=0, le=120)
    drain_timeout: float = Field(default=30, gt=0, le=3600)
    model_profile_ref: str | None = None
    credential_ref: str | None = None
    permissions: Permissions = Field(default_factory=Permissions)

    @field_validator("credential_ref")
    @classmethod
    def credential_reference_only(cls, value):
        if value is not None and not value.startswith("credential:"):
            raise ValueError("credential_ref must reference the OS credential vault")
        return value


Settings = PlatformConfig


ENV_KEYS = {"HARNESS_DATA_DIR": "data_dir", "HARNESS_HOST": "host", "HARNESS_PORT": "port",
            "HARNESS_LOG_LEVEL": "log_level", "HARNESS_STARTUP_TIMEOUT": "startup_timeout",
            "HARNESS_DRAIN_TIMEOUT": "drain_timeout", "HARNESS_MODEL_PROFILE_REF": "model_profile_ref"}


def load_config(*, user_file: Path | None = None, workspace_file: Path | None = None,
                environ: dict | None = None, cli: dict | None = None):
    effective = PlatformConfig().model_dump()
    sources = {key: "builtin" for key in effective}
    rejected = []

    def merge(layer: dict, source: str, base: Path, narrow_permissions: bool):
        nonlocal effective
        assert_secret_refs(layer)
        # Validate a complete candidate, so unknown keys and invalid value types fail.
        if "data_dir" in layer:
            path = Path(layer["data_dir"]).expanduser()
            layer["data_dir"] = str((base / path).resolve() if not path.is_absolute() else path.resolve())
        if "permissions" in layer:
            requested = Permissions.model_validate(effective["permissions"] | layer["permissions"]).model_dump()
            if narrow_permissions:
                prior = effective["permissions"]
                for key in ("network", "process", "workspace_write"):
                    if requested[key] and not prior[key]:
                        rejected.append({"source": source, "field": "permissions." + key, "reason": "lower-trust scope cannot expand permission"})
                    requested[key] = prior[key] and requested[key]
                if prior["allowed_hosts"]:
                    if not requested["allowed_hosts"]:
                        requested["allowed_hosts"] = prior["allowed_hosts"]
                        rejected.append({"source": source, "field": "permissions.allowed_hosts", "reason": "lower-trust scope cannot remove destination restrictions"})
                    else:
                        requested["allowed_hosts"] = [host for host in requested["allowed_hosts"] if host in prior["allowed_hosts"]]
                        if not requested["allowed_hosts"]:
                            requested["network"] = False
            layer["permissions"] = requested
        effective = PlatformConfig.model_validate(effective | layer).model_dump()
        sources.update({key: source for key in layer})

    for source, file, narrowing in (("user", user_file, False), ("workspace", workspace_file, True)):
        if file and Path(file).exists():
            path = Path(file).resolve()
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration file must be an object")
            merge(data, source, path.parent, narrowing)
    env = environ if environ is not None else os.environ
    merge({field: env[key] for key, field in ENV_KEYS.items() if key in env}, "environment", Path.cwd(), True)
    merge({k: v for k, v in (cli or {}).items() if v is not None}, "cli", Path.cwd(), True)
    config = PlatformConfig.model_validate(effective)
    return config, {"effective": config.model_dump(mode="json"), "sources": sources, "rejected_overrides": rejected,
                    "hot_reload_fields": []}


def instance_config(data_dir: Path, instance_id: str, **fallback):
    """Read only the configuration published by this instance's supervisor."""
    from harness.storage import Store
    from harness.core import HarnessError
    record = Store(Path(data_dir) / "app.db").get("system", "effective_config")
    if record:
        if record["instance_id"] != instance_id:
            raise HarnessError("CONFIG_INSTANCE_MISMATCH", "有效配置不属于当前监督实例", 409)
        config = PlatformConfig.model_validate(record["settings"])
        if config.data_dir.resolve() != Path(data_dir).resolve() or config.instance_id != instance_id:
            raise HarnessError("CONFIG_INSTANCE_MISMATCH", "有效配置的数据目录或实例绑定不一致", 409)
        return config
    return PlatformConfig(data_dir=data_dir, instance_id=instance_id, **fallback)
