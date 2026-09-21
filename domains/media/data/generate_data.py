"""Generate deterministic CSV and DuckDB fixtures for DataPilot-Media."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).resolve().parent
STREAM_CSV_PATH = DATA_DIR / "stream_sessions.csv"
ALARM_CSV_PATH = DATA_DIR / "alarm_events.csv"
LOG_CSV_PATH = DATA_DIR / "log_events.csv"
TRANSCODE_CSV_PATH = DATA_DIR / "transcode_jobs.csv"
DATABASE_PATH = DATA_DIR / "media.duckdb"

WINDOW_STARTS = {
    "prior_year": datetime(2025, 9, 1, 11, 0),
    "previous_day": datetime(2026, 8, 31, 11, 0),
    "previous_window": datetime(2026, 9, 1, 10, 0),
    "current_window": datetime(2026, 9, 1, 11, 0),
}

# Each target expands to 20 sessions. The current 华南/CDN-B target is the
# deliberate anomaly used by both Phase 1.1 demo questions.
SUCCESS_TARGETS = {
    "prior_year": {
        ("华东", "CDN-A"): 19,
        ("华东", "CDN-B"): 18,
        ("华东", "CDN-C"): 18,
        ("华南", "CDN-A"): 19,
        ("华南", "CDN-B"): 18,
        ("华南", "CDN-C"): 18,
        ("华北", "CDN-A"): 18,
        ("华北", "CDN-B"): 18,
        ("华北", "CDN-C"): 19,
    },
    "previous_day": {
        ("华东", "CDN-A"): 19,
        ("华东", "CDN-B"): 19,
        ("华东", "CDN-C"): 18,
        ("华南", "CDN-A"): 19,
        ("华南", "CDN-B"): 18,
        ("华南", "CDN-C"): 18,
        ("华北", "CDN-A"): 19,
        ("华北", "CDN-B"): 18,
        ("华北", "CDN-C"): 19,
    },
    "previous_window": {
        ("华东", "CDN-A"): 19,
        ("华东", "CDN-B"): 19,
        ("华东", "CDN-C"): 18,
        ("华南", "CDN-A"): 19,
        ("华南", "CDN-B"): 19,
        ("华南", "CDN-C"): 18,
        ("华北", "CDN-A"): 19,
        ("华北", "CDN-B"): 18,
        ("华北", "CDN-C"): 19,
    },
    "current_window": {
        ("华东", "CDN-A"): 19,
        ("华东", "CDN-B"): 18,
        ("华东", "CDN-C"): 18,
        ("华南", "CDN-A"): 17,
        ("华南", "CDN-B"): 10,
        ("华南", "CDN-C"): 16,
        ("华北", "CDN-A"): 18,
        ("华北", "CDN-B"): 18,
        ("华北", "CDN-C"): 19,
    },
}

REGIONS = (("华东", "east"), ("华南", "south"), ("华北", "north"))
CDNS = ("CDN-A", "CDN-B", "CDN-C")
DEVICES = ("mobile", "web", "tv")
FIELD_NAMES = (
    "session_id",
    "timestamp",
    "window_name",
    "region",
    "cdn",
    "device",
    "startup_time",
    "buffer_duration",
    "playback_duration",
    "play_success",
)

ALARM_FIELD_NAMES = (
    "alarm_id",
    "timestamp",
    "window_name",
    "region",
    "cdn",
    "error_code",
    "severity",
    "status",
    "message",
)

LOG_FIELD_NAMES = (
    "timestamp",
    "window_name",
    "region",
    "cdn",
    "service",
    "level",
    "error_code",
    "trace_id",
    "message",
)

TRANSCODE_FIELD_NAMES = (
    "job_id",
    "timestamp",
    "window_name",
    "region",
    "codec",
    "status",
    "error_code",
    "duration",
    "gpu_pool",
)

# Alarm rows are sparse operational signals aligned to the same four fixture
# windows as stream_sessions. Three high-severity E302 alarms overlap the
# deliberate current_window 华南/CDN-B playback degradation.
ALARM_SPECS = (
    (
        "alarm-py-001",
        "prior_year",
        20,
        "华东",
        "CDN-A",
        "I101",
        "low",
        "resolved",
        "Routine edge health notification recovered automatically.",
    ),
    (
        "alarm-pd-001",
        "previous_day",
        42,
        "华南",
        "CDN-C",
        "W201",
        "medium",
        "resolved",
        "Transient edge latency exceeded the warning threshold.",
    ),
    (
        "alarm-prev-001",
        "previous_window",
        28,
        "华南",
        "CDN-B",
        "I101",
        "low",
        "resolved",
        "Routine edge health notification recovered automatically.",
    ),
    (
        "alarm-prev-002",
        "previous_window",
        35,
        "华东",
        "CDN-A",
        "W110",
        "low",
        "resolved",
        "Short-lived origin latency warning cleared within five minutes.",
    ),
    (
        "alarm-cur-001",
        "current_window",
        6,
        "华南",
        "CDN-B",
        "E302",
        "high",
        "open",
        "CDN upstream request timeout rate exceeded the critical threshold.",
    ),
    (
        "alarm-cur-002",
        "current_window",
        18,
        "华南",
        "CDN-B",
        "E302",
        "high",
        "investigating",
        "CDN edge nodes continued reporting upstream request timeouts.",
    ),
    (
        "alarm-cur-003",
        "current_window",
        34,
        "华南",
        "CDN-B",
        "E302",
        "high",
        "resolved",
        "CDN upstream timeout alarm remained above the recovery threshold.",
    ),
    (
        "alarm-cur-004",
        "current_window",
        24,
        "华南",
        "CDN-C",
        "W201",
        "medium",
        "investigating",
        "Edge latency exceeded the warning threshold.",
    ),
    (
        "alarm-cur-005",
        "current_window",
        40,
        "华南",
        "CDN-A",
        "I101",
        "low",
        "resolved",
        "Routine edge health notification recovered automatically.",
    ),
    (
        "alarm-cur-006",
        "current_window",
        16,
        "华东",
        "CDN-B",
        "W110",
        "medium",
        "resolved",
        "Short-lived origin latency warning cleared within five minutes.",
    ),
    (
        "alarm-cur-007",
        "current_window",
        22,
        "华北",
        "CDN-A",
        "I101",
        "low",
        "resolved",
        "Routine edge health notification recovered automatically.",
    ),
)

# Structured logs deliberately overlap the current 华南/CDN-B E302 incident.
# They are synthetic evidence for Tool/MCP validation, not a real log source.
LOG_SPECS = (
    (
        "previous_window",
        12,
        "华南",
        "CDN-B",
        "cdn-gateway",
        "INFO",
        "I000",
        "trace-prev-001",
        "Upstream request completed within the normal latency budget.",
    ),
    (
        "previous_window",
        31,
        "华南",
        "CDN-C",
        "player-api",
        "WARN",
        "W201",
        "trace-prev-002",
        "Playback startup latency briefly exceeded the warning threshold.",
    ),
    (
        "current_window",
        5,
        "华南",
        "CDN-B",
        "origin-proxy",
        "ERROR",
        "E302",
        "trace-e302-001",
        "Origin upstream timeout after 3000 ms while fetching a media segment.",
    ),
    (
        "current_window",
        17,
        "华南",
        "CDN-B",
        "cdn-gateway",
        "ERROR",
        "E302",
        "trace-e302-002",
        "CDN upstream timeout propagated from the origin proxy.",
    ),
    (
        "current_window",
        33,
        "华南",
        "CDN-B",
        "origin-proxy",
        "ERROR",
        "E302",
        "trace-e302-003",
        "Repeated upstream timeout while requesting the next video segment.",
    ),
    (
        "current_window",
        36,
        "华南",
        "CDN-B",
        "cdn-gateway",
        "WARN",
        "E302",
        "trace-e302-004",
        "Upstream timeout rate remained above the warning threshold.",
    ),
    (
        "current_window",
        24,
        "华南",
        "CDN-C",
        "player-api",
        "WARN",
        "W201",
        "trace-w201-001",
        "Edge latency increased but requests continued to succeed.",
    ),
    (
        "current_window",
        15,
        "华东",
        "CDN-B",
        "origin-proxy",
        "INFO",
        "I000",
        "trace-east-001",
        "Origin request completed normally.",
    ),
    (
        "previous_day",
        22,
        "华南",
        "CDN-A",
        "player-api",
        "INFO",
        "I000",
        "trace-pd-001",
        "Playback request completed normally.",
    ),
    (
        "prior_year",
        18,
        "华北",
        "CDN-C",
        "cdn-gateway",
        "INFO",
        "I000",
        "trace-py-001",
        "CDN request completed normally.",
    ),
)

# Transcode rows are an independent deterministic fixture. Failed current-window
# H.265 jobs provide a bounded status-query scenario; no FFmpeg work is run.
TRANSCODE_SPECS = (
    ("job-py-001", "prior_year", 10, "华东", "H.264", "succeeded", "NONE", 82.4, "gpu-a"),
    ("job-pd-001", "previous_day", 15, "华南", "H.265", "succeeded", "NONE", 95.1, "gpu-b"),
    ("job-prev-001", "previous_window", 8, "华南", "H.265", "succeeded", "NONE", 91.3, "gpu-b"),
    ("job-prev-002", "previous_window", 26, "华北", "AV1", "succeeded", "NONE", 140.8, "gpu-c"),
    ("job-cur-001", "current_window", 4, "华南", "H.265", "failed", "T501", 34.7, "gpu-b"),
    ("job-cur-002", "current_window", 14, "华南", "H.265", "failed", "T501", 37.2, "gpu-b"),
    ("job-cur-003", "current_window", 21, "华东", "H.264", "succeeded", "NONE", 78.5, "gpu-a"),
    ("job-cur-004", "current_window", 29, "华北", "AV1", "running", "NONE", 42.0, "gpu-c"),
    ("job-cur-005", "current_window", 37, "华南", "H.264", "queued", "NONE", 0.0, "gpu-a"),
    ("job-cur-006", "current_window", 43, "华北", "H.265", "succeeded", "NONE", 93.6, "gpu-b"),
)


def build_rows() -> list[dict[str, Any]]:
    """Return the complete four-window fixture in deterministic order."""

    rows: list[dict[str, Any]] = []
    for window_name, window_start in WINDOW_STARTS.items():
        for region_order, (region, region_code) in enumerate(REGIONS, start=1):
            for cdn_order, cdn in enumerate(CDNS, start=1):
                success_count = SUCCESS_TARGETS[window_name][(region, cdn)]
                for session_number in range(1, 21):
                    play_success = session_number <= success_count
                    is_anomaly = (
                        window_name == "current_window"
                        and region == "华南"
                        and cdn == "CDN-B"
                    )
                    rows.append(
                        {
                            "session_id": (
                                f"{window_name}-{region_code}-"
                                f"{cdn.lower().replace('-', '')}-"
                                f"{session_number:02d}"
                            ),
                            "timestamp": window_start
                            + timedelta(
                                minutes=2 * (session_number - 1),
                                seconds=5 * (region_order - 1) + (cdn_order - 1),
                            ),
                            "window_name": window_name,
                            "region": region,
                            "cdn": cdn,
                            "device": DEVICES[session_number % len(DEVICES)],
                            "startup_time": float(
                                400
                                + region_order * 35
                                + cdn_order * 25
                                + (session_number % 5) * 18
                                + (700 if is_anomaly else 0)
                                + (300 if not play_success else 0)
                            ),
                            "buffer_duration": round(
                                0.2
                                + region_order * 0.08
                                + cdn_order * 0.05
                                + (session_number % 4) * 0.1
                                + (4.0 if is_anomaly else 0.0)
                                + (3.0 if not play_success else 0.0),
                                2,
                            ),
                            "playback_duration": float(
                                480
                                + (session_number % 6) * 30
                                + region_order * 12
                                + cdn_order * 8
                            ),
                            "play_success": play_success,
                        }
                    )
    if len(rows) != 720:
        raise RuntimeError(f"expected 720 sessions, generated {len(rows)}")
    return rows


def build_alarm_rows() -> list[dict[str, Any]]:
    """Return deterministic alarms aligned to the session fixture windows."""

    rows = [
        {
            "alarm_id": alarm_id,
            "timestamp": WINDOW_STARTS[window_name] + timedelta(minutes=minute),
            "window_name": window_name,
            "region": region,
            "cdn": cdn,
            "error_code": error_code,
            "severity": severity,
            "status": status,
            "message": message,
        }
        for (
            alarm_id,
            window_name,
            minute,
            region,
            cdn,
            error_code,
            severity,
            status,
            message,
        ) in ALARM_SPECS
    ]
    if len(rows) != 11:
        raise RuntimeError(f"expected 11 alarms, generated {len(rows)}")
    return rows


def build_log_rows() -> list[dict[str, Any]]:
    """Return deterministic structured logs aligned to Media windows."""

    rows = [
        {
            "timestamp": WINDOW_STARTS[window_name] + timedelta(minutes=minute),
            "window_name": window_name,
            "region": region,
            "cdn": cdn,
            "service": service,
            "level": level,
            "error_code": error_code,
            "trace_id": trace_id,
            "message": message,
        }
        for (
            window_name,
            minute,
            region,
            cdn,
            service,
            level,
            error_code,
            trace_id,
            message,
        ) in LOG_SPECS
    ]
    if len(rows) != 10:
        raise RuntimeError(f"expected 10 logs, generated {len(rows)}")
    return rows


def build_transcode_rows() -> list[dict[str, Any]]:
    """Return deterministic transcode job status rows without media processing."""

    rows = [
        {
            "job_id": job_id,
            "timestamp": WINDOW_STARTS[window_name] + timedelta(minutes=minute),
            "window_name": window_name,
            "region": region,
            "codec": codec,
            "status": status,
            "error_code": error_code,
            "duration": duration,
            "gpu_pool": gpu_pool,
        }
        for (
            job_id,
            window_name,
            minute,
            region,
            codec,
            status,
            error_code,
            duration,
            gpu_pool,
        ) in TRANSCODE_SPECS
    ]
    if len(rows) != 10:
        raise RuntimeError(f"expected 10 transcode jobs, generated {len(rows)}")
    return rows


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    field_names: tuple[str, ...],
) -> None:
    """Write one portable, reviewable source fixture."""

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=field_names)
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["timestamp"] = row["timestamp"].isoformat(sep=" ")
            if "play_success" in csv_row:
                csv_row["play_success"] = str(row["play_success"]).lower()
            writer.writerow(csv_row)


def write_duckdb(
    session_rows: list[dict[str, Any]],
    alarm_rows: list[dict[str, Any]],
    log_rows: list[dict[str, Any]],
    transcode_rows: list[dict[str, Any]],
) -> None:
    """Materialize the fixture in the DuckDB file consumed by Wren."""

    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "duckdb is not available; run from the repository's configured "
            "development environment or pass --csv-only"
        ) from exc

    connection = duckdb.connect(str(DATABASE_PATH))
    try:
        connection.execute(
            """
            CREATE OR REPLACE TABLE stream_sessions (
                session_id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                window_name VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                cdn VARCHAR NOT NULL,
                device VARCHAR NOT NULL,
                startup_time DOUBLE NOT NULL,
                buffer_duration DOUBLE NOT NULL,
                playback_duration DOUBLE NOT NULL,
                play_success BOOLEAN NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO stream_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(row[name] for name in FIELD_NAMES) for row in session_rows],
        )
        connection.execute(
            """
            CREATE OR REPLACE TABLE alarm_events (
                alarm_id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                window_name VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                cdn VARCHAR NOT NULL,
                error_code VARCHAR NOT NULL,
                severity VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                message VARCHAR NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO alarm_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(row[name] for name in ALARM_FIELD_NAMES) for row in alarm_rows],
        )
        connection.execute(
            """
            CREATE OR REPLACE TABLE log_events (
                timestamp TIMESTAMP NOT NULL,
                window_name VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                cdn VARCHAR NOT NULL,
                service VARCHAR NOT NULL,
                level VARCHAR NOT NULL,
                error_code VARCHAR NOT NULL,
                trace_id VARCHAR PRIMARY KEY,
                message VARCHAR NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO log_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(row[name] for name in LOG_FIELD_NAMES) for row in log_rows],
        )
        connection.execute(
            """
            CREATE OR REPLACE TABLE transcode_jobs (
                job_id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                window_name VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                codec VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                error_code VARCHAR NOT NULL,
                duration DOUBLE NOT NULL,
                gpu_pool VARCHAR NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO transcode_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                tuple(row[name] for name in TRANSCODE_FIELD_NAMES)
                for row in transcode_rows
            ],
        )
        session_count = connection.execute(
            "SELECT COUNT(*) FROM stream_sessions"
        ).fetchone()[0]
        alarm_count = connection.execute(
            "SELECT COUNT(*) FROM alarm_events"
        ).fetchone()[0]
        log_count = connection.execute("SELECT COUNT(*) FROM log_events").fetchone()[0]
        transcode_count = connection.execute(
            "SELECT COUNT(*) FROM transcode_jobs"
        ).fetchone()[0]
        if session_count != len(session_rows):
            raise RuntimeError(
                f"expected {len(session_rows)} sessions, materialized {session_count}"
            )
        if alarm_count != len(alarm_rows):
            raise RuntimeError(
                f"expected {len(alarm_rows)} alarms, materialized {alarm_count}"
            )
        if log_count != len(log_rows):
            raise RuntimeError(
                f"expected {len(log_rows)} logs, materialized {log_count}"
            )
        if transcode_count != len(transcode_rows):
            raise RuntimeError(
                f"expected {len(transcode_rows)} transcode jobs, "
                f"materialized {transcode_count}"
            )
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv-only",
        action="store_true",
        help="Generate the reviewable CSV without materializing DuckDB.",
    )
    args = parser.parse_args()

    session_rows = build_rows()
    alarm_rows = build_alarm_rows()
    log_rows = build_log_rows()
    transcode_rows = build_transcode_rows()
    write_csv(STREAM_CSV_PATH, session_rows, FIELD_NAMES)
    write_csv(ALARM_CSV_PATH, alarm_rows, ALARM_FIELD_NAMES)
    write_csv(LOG_CSV_PATH, log_rows, LOG_FIELD_NAMES)
    write_csv(TRANSCODE_CSV_PATH, transcode_rows, TRANSCODE_FIELD_NAMES)
    if not args.csv_only:
        write_duckdb(session_rows, alarm_rows, log_rows, transcode_rows)
        print(
            f"Generated {len(session_rows)} sessions, {len(alarm_rows)} alarms, "
            f"{len(log_rows)} logs, and {len(transcode_rows)} transcode jobs "
            f"in {DATABASE_PATH}"
        )
    else:
        print(
            f"Generated {len(session_rows)} sessions, {len(alarm_rows)} alarms, "
            f"{len(log_rows)} logs, and {len(transcode_rows)} transcode jobs"
        )


if __name__ == "__main__":
    main()
