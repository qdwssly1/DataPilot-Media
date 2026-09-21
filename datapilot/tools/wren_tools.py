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


_MAX_PLANNING_ENTITIES = 12
_MAX_PLANNING_FIELDS = 24
_MAX_PLANNING_OBJECTS = 16
_MAX_PLANNING_RELATIONSHIPS = 24
_MAX_PLANNING_DESCRIPTION_LENGTH = 280


class WrenConfigurationError(RuntimeError):
    """Raised when a Wren project cannot be configured for DataPilot."""


@dataclass(frozen=True, slots=True)
class WrenQueryResult:
    """Small, framework-neutral view of a Wren query result."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


def _manifest_items(manifest: Mapping[str, Any], key: str) -> list[Any]:
    value = manifest.get(key, [])
    return value if isinstance(value, list) else []


def _bounded_description(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    properties = value.get("properties")
    description = (
        properties.get("description")
        if isinstance(properties, Mapping)
        else value.get("description")
    )
    if not isinstance(description, str) or not description.strip():
        return None
    return " ".join(description.split())[:_MAX_PLANNING_DESCRIPTION_LENGTH]


def _named_type(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    item = {"name": name.strip()}
    data_type = value.get("type")
    if isinstance(data_type, str) and data_type.strip():
        item["type"] = data_type.strip()
    description = _bounded_description(value)
    if description is not None:
        item["description"] = description
    return item


def _view_output_fields(value: Mapping[str, Any]) -> tuple[list[str], bool]:
    statement = value.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return ([], False)
    try:
        from sqlglot import parse_one  # noqa: PLC0415

        names = parse_one(statement, read="duckdb").named_selects
    except Exception:
        return ([], False)
    fields = [
        name.strip()
        for name in names
        if isinstance(name, str) and name.strip() and name != "*"
    ]
    return (fields[:_MAX_PLANNING_FIELDS], len(fields) > _MAX_PLANNING_FIELDS)


def _build_planning_context(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a bounded, SQL-free planning view from a Wren manifest."""

    raw_models = _manifest_items(manifest, "models")
    raw_views = _manifest_items(manifest, "views")
    raw_cubes = _manifest_items(manifest, "cubes")
    raw_relationships = _manifest_items(manifest, "relationships")
    truncated = any(
        (
            len(raw_models) > _MAX_PLANNING_ENTITIES,
            len(raw_views) > _MAX_PLANNING_OBJECTS,
            len(raw_cubes) > _MAX_PLANNING_OBJECTS,
            len(raw_relationships) > _MAX_PLANNING_RELATIONSHIPS,
        )
    )

    entities: list[dict[str, Any]] = []
    for raw_model in raw_models[:_MAX_PLANNING_ENTITIES]:
        if not isinstance(raw_model, Mapping):
            continue
        name = raw_model.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_columns = raw_model.get("columns", [])
        columns = raw_columns if isinstance(raw_columns, list) else []
        if len(columns) > _MAX_PLANNING_FIELDS:
            truncated = True
        fields = [
            field
            for raw_column in columns[:_MAX_PLANNING_FIELDS]
            if (field := _named_type(raw_column)) is not None
        ]
        entity: dict[str, Any] = {
            "name": name.strip(),
            "important_fields": fields,
        }
        description = _bounded_description(raw_model)
        if description is not None:
            entity["description"] = description
        entities.append(entity)

    views: list[dict[str, Any]] = []
    for raw_view in raw_views[:_MAX_PLANNING_OBJECTS]:
        if not isinstance(raw_view, Mapping):
            continue
        name = raw_view.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        available_fields, fields_truncated = _view_output_fields(raw_view)
        truncated = truncated or fields_truncated
        view: dict[str, Any] = {
            "name": name.strip(),
            "available_fields": available_fields,
        }
        description = _bounded_description(raw_view)
        if description is not None:
            view["description"] = description
        views.append(view)

    dimensions: list[dict[str, str]] = []
    time_dimensions: list[dict[str, str]] = []
    metrics: list[dict[str, str]] = []
    semantic_objects: list[dict[str, Any]] = []
    for raw_cube in raw_cubes[:_MAX_PLANNING_OBJECTS]:
        if not isinstance(raw_cube, Mapping):
            continue
        name = raw_cube.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        source = name.strip()
        semantic_object: dict[str, Any] = {
            "name": source,
            "queryable_with_sql": False,
        }
        base_object = raw_cube.get("baseObject")
        if isinstance(base_object, str) and base_object.strip():
            base_object = base_object.strip()
            semantic_object["base_entity"] = base_object
        else:
            base_object = None
        description = _bounded_description(raw_cube)
        if description is not None:
            semantic_object["description"] = description
        semantic_objects.append(semantic_object)

        for key, target in (
            ("dimensions", dimensions),
            ("timeDimensions", time_dimensions),
            ("measures", metrics),
        ):
            raw_items = raw_cube.get(key, [])
            cube_items = raw_items if isinstance(raw_items, list) else []
            if len(cube_items) > _MAX_PLANNING_FIELDS:
                truncated = True
            for raw_item in cube_items[:_MAX_PLANNING_FIELDS]:
                item = _named_type(raw_item)
                if item is not None:
                    item["semantic_object"] = source
                    if base_object is not None:
                        item["source_entity"] = base_object
                    target.append(item)

    relationships: list[dict[str, Any]] = []
    for raw_relationship in raw_relationships[:_MAX_PLANNING_RELATIONSHIPS]:
        if not isinstance(raw_relationship, Mapping):
            continue
        name = raw_relationship.get("name")
        models = raw_relationship.get("models")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(models, list) or not all(
            isinstance(model, str) for model in models
        ):
            continue
        relationship: dict[str, Any] = {
            "name": name.strip(),
            "entities": list(models),
        }
        join_type = raw_relationship.get("joinType")
        if isinstance(join_type, str) and join_type.strip():
            relationship["join_type"] = join_type.strip()
        relationships.append(relationship)

    return {
        "version": 1,
        "entities": entities,
        "semantic_objects": semantic_objects,
        "dimensions": dimensions,
        "time_dimensions": time_dimensions,
        "metrics": metrics,
        "views": views,
        "relationships": relationships,
        "truncated": truncated,
    }


def try_fetch_planning_context(provider: Any) -> dict[str, Any] | None:
    """Return compact domain context, or use the legacy planning fallback.

    Older Wren-like test doubles and adapters do not expose this optional API.
    A missing, failing, or malformed provider therefore degrades once to the
    original schema-unaware Planner call instead of blocking the workflow.
    """

    fetch = getattr(provider, "fetch_planning_context", None)
    if not callable(fetch):
        return None
    try:
        context = fetch()
    except Exception:
        return None
    if not isinstance(context, Mapping):
        return None
    return dict(context)


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

    def fetch_planning_context(self) -> dict[str, Any]:
        """Return a bounded capability projection without querying data."""

        manifest = self._toolkit._mdl_source.load_manifest()
        if not isinstance(manifest, Mapping):
            raise TypeError("Wren manifest must be an object")
        return _build_planning_context(manifest)

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
