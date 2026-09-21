"""Strict, read-only tool contracts used by DataPilot query execution.

The contract deliberately does not expose SQL, shell, or filesystem primitives.
Domain tools validate a bounded input model and return one normalized result that
can later be adapted into DataPilot's existing reviewable query evidence.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from time import perf_counter
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolArguments(BaseModel):
    """Base model for strict tool arguments."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ToolResult(BaseModel):
    """Framework-neutral, structured result returned by every DataPilot tool."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tool_name: str = Field(min_length=1, max_length=80)
    success: bool
    data: list[dict[str, Any]]
    summary: str = Field(max_length=500)
    metadata: dict[str, Any]
    error: str | None = Field(max_length=300)
    execution_time_ms: float = Field(ge=0.0)


def safe_tool_error(error: BaseException) -> str:
    """Return a bounded error type without credentials, URLs, or local paths."""

    text = " ".join(str(error).split()) or type(error).__name__
    text = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", text)
    text = re.sub(r"(?:[A-Za-z]:\\|/)[^\s,;]+", "[REDACTED_PATH]", text)
    return text[:300]


def _validation_metadata(error: ValidationError) -> dict[str, Any]:
    """Describe invalid fields without echoing potentially sensitive values."""

    issues = []
    for item in error.errors(include_input=False, include_url=False):
        issues.append(
            {
                "field": ".".join(str(part) for part in item.get("loc", ())),
                "type": str(item.get("type", "validation_error")),
            }
        )
    return {"error_type": "invalid_arguments", "validation_issues": issues[:20]}


class ReadOnlyTool(ABC):
    """Template for one strictly validated and normalized read-only tool."""

    name: ClassVar[str]
    description: ClassVar[str]
    domain: ClassVar[str]
    read_only: ClassVar[bool] = True
    input_model: ClassVar[type[ToolArguments]]

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict[str, Any]:
        return ToolResult.model_json_schema()

    def routing_score(self, task_description: str) -> int:
        """Return a deterministic routing score; zero means no match."""

        del task_description
        return 0

    def infer_arguments(self, task_description: str) -> dict[str, Any]:
        """Infer only explicit, schema-valid arguments from a task description."""

        del task_description
        return {}

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        """Validate arguments, execute once, and normalize all failures."""

        started_at = perf_counter()
        try:
            if not isinstance(arguments, Mapping):
                raise TypeError("arguments must be an object")
            validated = self.input_model.model_validate(dict(arguments), strict=True)
        except (ValidationError, TypeError, ValueError) as exc:
            metadata = (
                _validation_metadata(exc)
                if isinstance(exc, ValidationError)
                else {"error_type": "invalid_arguments"}
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=[],
                summary="Tool arguments failed schema validation.",
                metadata=metadata,
                error="invalid tool arguments",
                execution_time_ms=(perf_counter() - started_at) * 1000,
            )
        try:
            result = self._execute(validated)
        except Exception as exc:  # domain/runtime errors are normalized here
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=[],
                summary="Read-only tool execution failed.",
                metadata={"error_type": type(exc).__name__},
                error=safe_tool_error(exc),
                execution_time_ms=(perf_counter() - started_at) * 1000,
            )
        if not isinstance(result, ToolResult) or result.tool_name != self.name:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=[],
                summary="Tool returned an invalid output contract.",
                metadata={"error_type": "tool_name_mismatch"},
                error="invalid tool output",
                execution_time_ms=(perf_counter() - started_at) * 1000,
            )
        return result.model_copy(
            update={"execution_time_ms": (perf_counter() - started_at) * 1000}
        )

    @abstractmethod
    def _execute(self, arguments: ToolArguments) -> ToolResult:
        """Execute validated read-only arguments."""


class ToolRegistry:
    """In-process catalog enforcing unique, read-only DataPilot tools."""

    def __init__(self, tools: list[ReadOnlyTool] | None = None) -> None:
        self._tools: dict[str, ReadOnlyTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ReadOnlyTool) -> None:
        if not isinstance(tool, ReadOnlyTool):
            raise TypeError("tool must implement ReadOnlyTool")
        if not tool.read_only:
            raise ValueError("DataPilot registry accepts read-only tools only")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", tool.name):
            raise ValueError("tool name must be a lowercase identifier")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ReadOnlyTool | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "read_only": tool.read_only,
                "domain": tool.domain,
            }
            for tool in self._tools.values()
        ]

    def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "", str(name))[:80] or "unknown"
            return ToolResult(
                tool_name=safe_name,
                success=False,
                data=[],
                summary="Requested tool is not registered.",
                metadata={"error_type": "unknown_tool"},
                error="unknown tool",
                execution_time_ms=0.0,
            )
        return tool.execute(arguments)
