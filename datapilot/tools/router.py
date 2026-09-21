"""Bounded Query Task routing across registered tools and SQL fallback."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from datapilot.tools.contracts import ToolRegistry


class RoutingModel(Protocol):
    """Optional model boundary used only when deterministic routing has no match."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str: ...


ROUTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["route", "tool_name", "arguments", "reason"],
    "properties": {
        "route": {"type": "string", "enum": ["tool", "sql"]},
        "tool_name": {"type": ["string", "null"]},
        "arguments": {"type": "object"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 240},
    },
}

ROUTER_SYSTEM_PROMPT = """You are a bounded query capability router.
Choose one registered read-only tool only when its contract directly satisfies
the Query Task. Otherwise choose the existing SQL Agent fallback. Do not analyze
data, generate SQL, answer the user, invent a tool, or add unsupported arguments.
Return exactly one JSON object matching the supplied schema.
"""


@dataclass(frozen=True, slots=True)
class ToolRouteDecision:
    """One observable routing decision; it never executes the selected route."""

    route: str
    tool_name: str | None
    reason: str
    arguments: dict[str, Any] = field(default_factory=dict)
    deterministic: bool = True
    model_attempts: int = 0
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "tool_name": self.tool_name,
            "reason": self.reason,
            "arguments": dict(self.arguments),
            "deterministic": self.deterministic,
            "model_attempts": self.model_attempts,
            "latency_ms": self.latency_ms,
        }


class ToolRouter:
    """Prefer deterministic domain routing, then one bounded model repair path."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        capability_context: Mapping[str, Any] | None = None,
        model_client: RoutingModel | None = None,
        max_output_retries: int = 1,
    ) -> None:
        if max_output_retries not in {0, 1}:
            raise ValueError("max_output_retries must be 0 or 1")
        self.registry = registry
        self.capability_context = dict(capability_context or {})
        self.model_client = model_client
        self.max_output_retries = max_output_retries

    def route(
        self,
        task_description: str,
        *,
        metric_binding: Mapping[str, Any] | None = None,
        requested_dimensions: list[str] | None = None,
    ) -> ToolRouteDecision:
        started_at = perf_counter()
        description = task_description.strip()
        if not description:
            raise ValueError("task_description must not be empty")
        lowered = description.lower()
        write_terms = (
            "delete",
            "drop",
            "truncate",
            "update",
            "insert",
            "删除",
            "清空",
            "修改",
            "写入",
        )
        if any(term in lowered for term in write_terms):
            return ToolRouteDecision(
                route="sql",
                tool_name=None,
                reason="Mutation intent is outside every read-only tool contract.",
                latency_ms=(perf_counter() - started_at) * 1000,
            )

        scored = sorted(
            (
                (tool.routing_score(description), index, tool)
                for index, name in enumerate(self.registry.names())
                if (tool := self.registry.get(name)) is not None
            ),
            key=lambda item: (-item[0], item[1]),
        )
        matched = [item for item in scored if item[0] > 0]
        if len(matched) > 1:
            return ToolRouteDecision(
                route="sql",
                tool_name=None,
                reason=(
                    "One Query Task spans multiple tool contracts; use the "
                    "general SQL fallback without changing the task plan."
                ),
                latency_ms=(perf_counter() - started_at) * 1000,
            )
        if matched:
            score, _, tool = scored[0]
            arguments = tool.infer_arguments(description)
            primary_metric = (
                metric_binding.get("primary_metric")
                if isinstance(metric_binding, Mapping)
                else None
            )
            input_properties = tool.input_model.model_json_schema().get(
                "properties", {}
            )
            if (
                isinstance(primary_metric, str)
                and primary_metric.strip()
                and "primary_metric" in input_properties
            ):
                arguments["primary_metric"] = primary_metric.strip()
            if requested_dimensions and "group_by" in input_properties:
                arguments["group_by"] = list(requested_dimensions)
            return ToolRouteDecision(
                route="tool",
                tool_name=tool.name,
                reason=f"Deterministic capability match (score={score}).",
                arguments=arguments,
                deterministic=True,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

        if self.model_client is None:
            return ToolRouteDecision(
                route="sql",
                tool_name=None,
                reason="No deterministic registered tool match; use SQL fallback.",
                latency_ms=(perf_counter() - started_at) * 1000,
            )

        last_error = ""
        attempts = self.max_output_retries + 1
        for attempt in range(1, attempts + 1):
            prompt = self._model_prompt(description, last_error=last_error)
            try:
                raw = self.model_client.complete(
                    system_prompt=ROUTER_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    response_schema=ROUTING_SCHEMA,
                )
                route, tool_name, arguments, reason = self._parse_model_route(raw)
            except Exception as exc:
                last_error = f"invalid structured routing output: {type(exc).__name__}"
                continue
            return ToolRouteDecision(
                route=route,
                tool_name=tool_name,
                reason=reason,
                arguments=arguments,
                deterministic=False,
                model_attempts=attempt,
                latency_ms=(perf_counter() - started_at) * 1000,
            )

        return ToolRouteDecision(
            route="sql",
            tool_name=None,
            reason="Bounded model routing failed; use SQL fallback.",
            deterministic=False,
            model_attempts=attempts,
            latency_ms=(perf_counter() - started_at) * 1000,
        )

    def _model_prompt(self, description: str, *, last_error: str) -> str:
        catalog = [
            {
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["input_schema"],
            }
            for item in self.registry.catalog()
        ]
        context = {
            "entities": self.capability_context.get("entities", []),
            "views": self.capability_context.get("views", []),
        }
        prompt = (
            f"Query Task:\n{description}\n\n"
            f"Registered tool catalog:\n{json.dumps(catalog, ensure_ascii=False, default=str)}\n\n"
            f"Schema capability context:\n{json.dumps(context, ensure_ascii=False, default=str)}"
        )
        if last_error:
            prompt += f"\n\nPrior output error: {last_error}. Return corrected JSON once."
        return prompt

    def _parse_model_route(
        self, response_text: str
    ) -> tuple[str, str | None, dict[str, Any], str]:
        raw = json.loads(response_text)
        if not isinstance(raw, dict) or set(raw) != set(ROUTING_SCHEMA["required"]):
            raise ValueError("routing fields are invalid")
        route = raw["route"]
        tool_name = raw["tool_name"]
        arguments = raw["arguments"]
        reason = raw["reason"]
        if route not in {"tool", "sql"}:
            raise ValueError("route is invalid")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        if route == "sql":
            if tool_name is not None or arguments:
                raise ValueError("SQL route cannot include a tool or arguments")
            return route, None, {}, reason.strip()[:240]
        if not isinstance(tool_name, str) or self.registry.get(tool_name) is None:
            raise ValueError("tool_name is not registered")
        return route, tool_name, arguments, reason.strip()[:240]
