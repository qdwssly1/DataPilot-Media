from __future__ import annotations

from typing import Any

from datapilot.tools.wren_tools import (
    WrenConfigurationError,
    WrenToolAdapter,
)


class FakeManifestSource:
    def load_manifest(self) -> dict[str, Any]:
        return {"models": [{"name": "orders", "columns": []}]}


class FakeMemoryProvider:
    enabled = True


class FakeMemoryAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def fetch(self, question: str, *, limit: int) -> dict[str, Any]:
        self.calls.append(("fetch", (question, limit)))
        return {"strategy": "search", "results": [{"name": "orders"}]}

    def recall(self, question: str, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("recall", (question, limit)))
        return [{"nl_query": question, "sql_query": "SELECT 1"}]

    def store(
        self,
        *,
        nl: str,
        sql: str,
        tags: list[str] | None,
    ) -> None:
        self.calls.append(("store", (nl, sql, tags)))


class FakeArrowTable:
    column_names = ["id"]
    num_rows = 1

    def to_pylist(self) -> list[dict[str, Any]]:
        return [{"id": 1}]


class FakeToolkit:
    def __init__(self) -> None:
        self._mdl_source = FakeManifestSource()
        self._memory = FakeMemoryProvider()
        self.memory = FakeMemoryAPI()
        self.calls: list[tuple[str, Any]] = []

    def dry_plan(self, sql: str) -> str:
        self.calls.append(("dry_plan", sql))
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int) -> FakeArrowTable:
        self.calls.append(("query", (sql, limit)))
        return FakeArrowTable()


def test_wren_adapter_uses_real_toolkit_api_shapes() -> None:
    toolkit = FakeToolkit()
    adapter = WrenToolAdapter(toolkit)

    assert adapter.list_models()[0]["name"] == "orders"
    assert adapter.fetch_context("orders")["strategy"] == "search"
    assert adapter.recall_queries("orders")[0]["sql_query"] == "SELECT 1"
    assert adapter.dry_plan("SELECT 1") == "planned: SELECT 1"
    result = adapter.query("SELECT 1", limit=25)

    assert result.columns == ["id"]
    assert result.rows == [{"id": 1}]
    assert result.row_count == 1
    assert toolkit.calls == [
        ("dry_plan", "SELECT 1"),
        ("query", ("SELECT 1", 25)),
    ]


def test_wren_adapter_exposes_store_but_does_not_hide_verification_boundary() -> None:
    toolkit = FakeToolkit()
    adapter = WrenToolAdapter(toolkit)

    adapter.store_query("question", "SELECT 1", tags=["verified"])

    assert toolkit.memory.calls == [
        ("store", ("question", "SELECT 1", ["verified"]))
    ]


def test_wren_adapter_requires_project_path() -> None:
    try:
        WrenToolAdapter.from_env({})
    except WrenConfigurationError as exc:
        assert str(exc) == "WREN_PROJECT_PATH is required"
    else:
        raise AssertionError("missing WREN_PROJECT_PATH must fail")
