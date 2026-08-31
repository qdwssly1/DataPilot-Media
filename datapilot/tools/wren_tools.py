"""Thin DataPilot adapter over the real Wren-LangChain Python API.

The adapter normalizes Wren return values but does not reproduce context
retrieval, memory, semantic planning, connectors, or SQL execution.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WrenConfigurationError(RuntimeError):
    """Raised when a Wren project cannot be configured for DataPilot."""


@dataclass(frozen=True, slots=True)
class WrenQueryResult:
    """Small, framework-neutral view of a Wren query result."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


class WrenToolAdapter:
    """Expose the WrenToolkit direct API in DataPilot-friendly shapes."""

    def __init__(self, toolkit: Any) -> None:
        self._toolkit = toolkit

    @classmethod
    def from_project(
        cls,
        project_path: str | Path,
        *,
        profile: str | None = None,
    ) -> WrenToolAdapter:
        """Create the real SDK toolkit without importing it during fake tests."""

        try:
            from wren_langchain import WrenToolkit  # noqa: PLC0415
        except ImportError as exc:
            raise WrenConfigurationError(
                "wren-langchain is not installed"
            ) from exc

        try:
            toolkit = WrenToolkit.from_project(project_path, profile=profile)
        except Exception as exc:
            raise WrenConfigurationError(
                f"Wren project initialization failed: {type(exc).__name__}"
            ) from exc
        return cls(toolkit)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> WrenToolAdapter:
        """Create an adapter from WREN_PROJECT_PATH and optional profile."""

        values = os.environ if environ is None else environ
        project_path = values.get("WREN_PROJECT_PATH", "").strip()
        if not project_path:
            raise WrenConfigurationError("WREN_PROJECT_PATH is required")
        profile = values.get("WREN_PROFILE", "").strip() or None
        return cls.from_project(project_path, profile=profile)

    def list_models(self) -> list[dict[str, Any]]:
        """Return models using the same manifest source as wren_list_models."""

        manifest = self._toolkit._mdl_source.load_manifest()
        return list(manifest.get("models", []) or [])

    def fetch_context(
        self,
        question: str,
        *,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Use Wren Memory search, or Wren's full-schema fallback."""

        if self._toolkit._memory.enabled:
            return self._toolkit.memory.fetch(question, limit=limit)

        from wren.memory import WrenMemory  # noqa: PLC0415

        manifest = self._toolkit._mdl_source.load_manifest()
        return {
            "strategy": "full",
            "schema": WrenMemory.describe_schema(manifest),
            "note": "Wren SQL memory is not enabled; full schema context used.",
        }

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Recall verified NL-to-SQL examples when Wren Memory is enabled."""

        if not self._toolkit._memory.enabled:
            return []
        return list(self._toolkit.memory.recall(question, limit=limit))

    def dry_plan(self, sql: str) -> str:
        """Delegate semantic SQL planning to Wren Engine."""

        return self._toolkit.dry_plan(sql)

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        """Execute through Wren and normalize the returned PyArrow table."""

        table = self._toolkit.query(sql, limit=limit)
        return WrenQueryResult(
            columns=list(table.column_names),
            rows=list(table.to_pylist()),
            row_count=table.num_rows,
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        """Expose Wren storage for a later verified-SQL phase.

        SQLAgent deliberately never calls this method. Reviewer approval is
        required before DataPilot may persist a generated query.
        """

        self._toolkit.memory.store(nl=nl, sql=sql, tags=tags)
