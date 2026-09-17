from __future__ import annotations
from typing import Callable
import jsonschema
from harness.core import HarnessError
from .contracts import ToolSpec


class ToolRegistry:
    def __init__(self):
        self._items: dict[tuple[str, str], tuple[ToolSpec, Callable]] = {}

    def register(self, spec: ToolSpec, executor: Callable):
        jsonschema.Draft202012Validator.check_schema(spec.input_schema)
        jsonschema.Draft202012Validator.check_schema(spec.output_schema)
        key = (spec.id, spec.version)
        if key in self._items:
            raise HarnessError("TOOL_CONFLICT", "同名同版本工具重复注册")
        self._items[key] = (spec, executor)

    def unregister(self, tool_id: str, version: str):
        self._items.pop((tool_id, version), None)

    def get(self, tool_id: str, version: str = "1") -> tuple[ToolSpec, Callable]:
        if (tool_id, version) not in self._items:
            raise HarnessError("TOOL_UNAVAILABLE", f"工具不可用: {tool_id}", 422)
        return self._items[(tool_id, version)]

    def specs(self) -> list[ToolSpec]:
        return [x[0] for x in self._items.values()]
