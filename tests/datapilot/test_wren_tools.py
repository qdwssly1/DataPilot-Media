from __future__ import annotations

from typing import Any

from datapilot.tools.wren_tools import (
    WrenConfigurationError,
    WrenToolAdapter,
    try_fetch_planning_context,
)


class FakeManifestSource:
    def load_manifest(self) -> dict[str, Any]:
        return {
            "models": [
                {
                    "name": "orders",
                    "properties": {"description": "Order facts."},
                    "tableReference": {"table": "private_orders_table"},
                    "columns": [
                        {
                            "name": "region",
                            "type": "VARCHAR",
                            "properties": {"description": "Sales region."},
                        }
                    ],
                }
            ],
            "views": [
                {
                    "name": "regional_orders",
                    "statement": (
                        "SELECT region, SUM(amount) AS revenue "
                        "FROM private_orders_table GROUP BY region"
                    ),
                    "properties": {"description": "Regional rollup."},
                }
            ],
            "cubes": [
                {
                    "name": "order_metrics",
                    "baseObject": "orders",
                    "dimensions": [
                        {
                            "name": "region",
                            "type": "VARCHAR",
                            "expression": "region",
                        }
                    ],
                    "timeDimensions": [],
                    "measures": [
                        {
                            "name": "order_count",
                            "type": "BIGINT",
                            "expression": "COUNT(*)",
                        }
                    ],
                }
            ],
            "relationships": [
                {
                    "name": "orders_customer",
                    "models": ["orders", "customers"],
                    "joinType": "MANY_TO_ONE",
                    "condition": "orders.customer_id = customers.id",
                }
            ],
        }


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


def test_wren_adapter_projects_lightweight_planning_context() -> None:
    adapter = WrenToolAdapter(FakeToolkit())

    context = adapter.fetch_planning_context()

    assert context["entities"] == [
        {
            "name": "orders",
            "description": "Order facts.",
            "important_fields": [
                {
                    "name": "region",
                    "type": "VARCHAR",
                    "description": "Sales region.",
                }
            ],
        }
    ]
    assert context["dimensions"] == [
        {
            "name": "region",
            "type": "VARCHAR",
            "semantic_object": "order_metrics",
            "source_entity": "orders",
        }
    ]
    assert context["metrics"] == [
        {
            "name": "order_count",
            "type": "BIGINT",
            "semantic_object": "order_metrics",
            "source_entity": "orders",
        }
    ]
    assert context["semantic_objects"] == [
        {
            "name": "order_metrics",
            "queryable_with_sql": False,
            "base_entity": "orders",
        }
    ]
    assert context["views"] == [
        {
            "name": "regional_orders",
            "available_fields": ["region", "revenue"],
            "description": "Regional rollup.",
        }
    ]
    assert context["relationships"] == [
        {
            "name": "orders_customer",
            "entities": ["orders", "customers"],
            "join_type": "MANY_TO_ONE",
        }
    ]
    assert context["truncated"] is False
    assert "private_orders_table" not in str(context)
    assert "SELECT" not in str(context)
    assert "COUNT(*)" not in str(context)
    assert "customer_id" not in str(context)


def test_planning_context_failure_degrades_to_none() -> None:
    class FailingProvider:
        def fetch_planning_context(self) -> dict[str, Any]:
            raise RuntimeError("manifest unavailable")

    assert try_fetch_planning_context(FailingProvider()) is None
    assert try_fetch_planning_context(object()) is None


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
