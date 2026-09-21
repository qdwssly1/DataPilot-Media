from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from datapilot.tools.contracts import (
    ReadOnlyTool,
    ToolArguments,
    ToolRegistry,
    ToolResult,
)


class EchoArguments(ToolArguments):
    value: str = Field(min_length=1, max_length=20)


class EchoTool(ReadOnlyTool):
    name = "echo_tool"
    description = "Return a validated value."
    domain = "test"
    input_model = EchoArguments

    def __init__(self) -> None:
        self.calls = 0

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        self.calls += 1
        value = arguments.model_dump()["value"]
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=[{"value": value}],
            summary="validated",
            metadata={},
            error=None,
            execution_time_ms=0.0,
        )


class WriteTool(EchoTool):
    name = "write_tool"
    read_only: ClassVar[bool] = False


class FailingTool(EchoTool):
    name = "failing_tool"

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        del arguments
        raise RuntimeError(
            "api_key=synthetic-secret at C:\\private\\credentials.txt"
        )


def test_contract_exposes_strict_input_and_output_schema() -> None:
    tool = EchoTool()

    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.output_schema["required"]) >= {
        "tool_name",
        "success",
        "data",
        "summary",
        "metadata",
        "error",
    }


def test_invalid_arguments_are_normalized_without_execution() -> None:
    tool = EchoTool()
    result = tool.execute({"value": "ok", "unexpected": "blocked"})

    assert result.success is False
    assert result.error == "invalid tool arguments"
    assert result.metadata["error_type"] == "invalid_arguments"
    assert tool.calls == 0


def test_non_mapping_arguments_are_normalized() -> None:
    result = EchoTool().execute([("value", "not-an-object")])  # type: ignore[arg-type]

    assert result.success is False
    assert result.metadata == {"error_type": "invalid_arguments"}


def test_registry_enforces_read_only_tools() -> None:
    registry = ToolRegistry([EchoTool()])

    assert registry.names() == ("echo_tool",)
    assert registry.catalog()[0]["read_only"] is True

    try:
        registry.register(WriteTool())
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("write-capable tool was accepted")


def test_unknown_tool_and_runtime_errors_are_safe() -> None:
    registry = ToolRegistry([FailingTool()])

    unknown = registry.execute("missing/tool", {})
    failed = registry.execute("failing_tool", {"value": "ok"})

    assert unknown.success is False
    assert unknown.metadata["error_type"] == "unknown_tool"
    assert failed.success is False
    assert "synthetic-secret" not in (failed.error or "")
    assert "credentials.txt" not in (failed.error or "")
