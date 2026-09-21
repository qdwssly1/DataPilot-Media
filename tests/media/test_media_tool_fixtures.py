from __future__ import annotations

from domains.media.data.generate_data import (
    build_alarm_rows,
    build_log_rows,
    build_rows,
    build_transcode_rows,
)


def test_tool_fixtures_are_deterministic_and_window_aligned() -> None:
    sessions = build_rows()
    alarms = build_alarm_rows()
    logs = build_log_rows()
    jobs = build_transcode_rows()

    assert len(sessions) == 720
    assert len(alarms) == 11
    assert len(logs) == 10
    assert len(jobs) == 10
    assert {row["window_name"] for row in logs} <= {
        "prior_year",
        "previous_day",
        "previous_window",
        "current_window",
    }
    assert all(row["playback_duration"] > 0 for row in sessions)


def test_e302_logs_and_failed_h265_jobs_are_explicit_synthetic_cases() -> None:
    e302_logs = [
        row
        for row in build_log_rows()
        if row["window_name"] == "current_window"
        and row["region"] == "华南"
        and row["cdn"] == "CDN-B"
        and row["error_code"] == "E302"
    ]
    failed_h265 = [
        row
        for row in build_transcode_rows()
        if row["window_name"] == "current_window"
        and row["codec"] == "H.265"
        and row["status"] == "failed"
    ]

    assert len(e302_logs) == 4
    assert {row["service"] for row in e302_logs} == {
        "origin-proxy",
        "cdn-gateway",
    }
    assert len(failed_h265) == 2
    assert {row["error_code"] for row in failed_h265} == {"T501"}
