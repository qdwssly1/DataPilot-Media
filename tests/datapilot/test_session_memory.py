from __future__ import annotations

from datapilot.agent.state import SessionContext, TimeRangeContext
from datapilot.memory.session_memory import SessionMemoryStore


def _context(session_id: str, metric: str) -> SessionContext:
    return SessionContext(
        session_id=session_id,
        turn_index=1,
        metrics=[metric],
        dimensions=["category"],
        time_range=TimeRangeContext(labels=["Q2", "Q3"]),
        filters={"region": ["华南"]},
        entities={"product": ["A"]},
        analysis_goal="Compare periods.",
    )


def test_session_memory_create() -> None:
    store = SessionMemoryStore()

    created = store.create("session-a")

    assert created.session_id == "session-a"
    assert created.turn_index == 0
    assert store.get("session-a") == created


def test_session_memory_save_and_load() -> None:
    store = SessionMemoryStore()
    context = _context("session-a", "GMV")

    store.save(context)

    loaded = store.get("session-a")
    assert loaded is not None
    assert loaded.metrics == ["GMV"]
    assert loaded.time_range.labels == ["Q2", "Q3"]


def test_session_memory_clear() -> None:
    store = SessionMemoryStore()
    store.save(_context("session-a", "GMV"))

    assert store.clear("session-a") is True
    assert store.get("session-a") is None
    assert store.clear("session-a") is False


def test_session_memory_sessions_are_isolated() -> None:
    store = SessionMemoryStore()
    store.save(_context("session-a", "GMV"))
    store.save(_context("session-b", "customer_count"))

    assert store.get("session-a").metrics == ["GMV"]
    assert store.get("session-b").metrics == ["customer_count"]


def test_session_mutable_fields_are_isolated() -> None:
    store = SessionMemoryStore()
    source = _context("session-a", "GMV")
    store.save(source)

    source.metrics.append("revenue")
    first_load = store.get("session-a")
    first_load.filters["region"].append("华东")
    second_load = store.get("session-a")

    assert second_load.metrics == ["GMV"]
    assert second_load.filters == {"region": ["华南"]}
