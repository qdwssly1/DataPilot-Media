from __future__ import annotations

from datetime import UTC

from datapilot.tracing.trace import EventType, TraceCollector


def test_trace_add_event() -> None:
    trace = TraceCollector("trace-123")

    event = trace.add_event(
        EventType.USER_QUERY,
        component="cli",
        action="accept_input",
        summary="Accepted a user query.",
        metadata={"query_length": 12},
    )

    assert event.trace_id == "trace-123"
    assert event.event_type is EventType.USER_QUERY
    assert event.component == "cli"
    assert event.action == "accept_input"
    assert event.summary == "Accepted a user query."
    assert event.metadata == {"query_length": 12}
    assert event.timestamp.tzinfo is UTC
    assert trace.get_events() == [event]


def test_trace_event_order() -> None:
    trace = TraceCollector()

    trace.add_event(
        EventType.USER_QUERY,
        component="cli",
        action="accept_input",
        summary="Query accepted.",
    )
    trace.add_event(
        EventType.STATE_CREATED,
        component="agent.state",
        action="create_initial_state",
        summary="State created.",
    )

    events = trace.get_events()
    assert [event.event_type for event in events] == [
        EventType.USER_QUERY,
        EventType.STATE_CREATED,
    ]
    assert events[0].timestamp <= events[1].timestamp


def test_get_events_does_not_expose_internal_list() -> None:
    trace = TraceCollector()
    trace.add_event(
        EventType.STATE_CREATED,
        component="agent.state",
        action="create_initial_state",
        summary="State created.",
    )

    returned_events = trace.get_events()
    returned_events.clear()

    assert len(trace) == 1
