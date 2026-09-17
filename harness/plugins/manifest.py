from __future__ import annotations

from typing import Any
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, field_validator

EXTENSIONS = ("providers", "tools", "skill_sources", "skills", "mcp_connectors", "context_policies",
              "agent_templates", "runtimes", "ui_panels")


class Contributions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    skill_sources: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    mcp_connectors: list[dict[str, Any]] = Field(default_factory=list)
    context_policies: list[dict[str, Any]] = Field(default_factory=list)
    agent_templates: list[dict[str, Any]] = Field(default_factory=list)
    runtimes: list[dict[str, Any]] = Field(default_factory=list)
    ui_panels: list[dict[str, Any]] = Field(default_factory=list)


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugins: dict[str, str] = Field(default_factory=dict)
    network: list[str] = Field(default_factory=list)
    filesystem: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    version: str
    harness_api: str
    entrypoint: str | None = None
    contributes: Contributions = Field(default_factory=Contributions)
    requires: Requirements = Field(default_factory=Requirements)

    @field_validator("version")
    @classmethod
    def valid_version(cls, value):
        Version(value)
        return value

    @field_validator("harness_api")
    @classmethod
    def valid_api(cls, value):
        SpecifierSet(value)
        return value
