from __future__ import annotations

import re
import yaml


def parse_skill(text: str) -> tuple[dict, str, list[str]]:
    """Safe YAML only. Invalid documents remain visible as disabled records."""
    diagnostics: list[str] = []
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized, ["SKILL.md:1: missing YAML frontmatter"]
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return {}, normalized, ["SKILL.md: missing closing frontmatter delimiter"]
    try:
        metadata = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 2 if mark else 1
        return {}, normalized[end + 5:], [f"SKILL.md:{line}: invalid safe YAML"]
    if not isinstance(metadata, dict):
        return {}, normalized[end + 5:], ["SKILL.md: frontmatter must be an object"]
    name = metadata.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        diagnostics.append("SKILL.md: name must be 1-64 lowercase letters, numbers and single hyphens")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        diagnostics.append("SKILL.md: description must contain 1-1024 characters")
    compatibility = metadata.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 500):
        diagnostics.append("SKILL.md: compatibility must be a string of at most 500 characters")
    if "metadata" in metadata and not isinstance(metadata["metadata"], dict):
        diagnostics.append("SKILL.md: metadata must be an object")
    return metadata, normalized[end + 5:], diagnostics
