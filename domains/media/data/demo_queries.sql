-- Demo 1: compare the previous and current hour by region.
WITH regional_rates AS (
    SELECT
        region,
        AVG(CASE WHEN window_name = 'previous_window' THEN CAST(play_success AS INTEGER) END)
            AS previous_success_rate,
        AVG(CASE WHEN window_name = 'current_window' THEN CAST(play_success AS INTEGER) END)
            AS current_success_rate
    FROM stream_sessions
    WHERE timestamp >= TIMESTAMP '2026-09-01 10:00:00'
      AND timestamp < TIMESTAMP '2026-09-01 12:00:00'
    GROUP BY region
)
SELECT
    region,
    previous_success_rate,
    current_success_rate,
    current_success_rate - previous_success_rate AS success_rate_change
FROM regional_rates
ORDER BY success_rate_change ASC;

-- Demo 2: attribute the decrease in successful sessions to each CDN.
-- Every CDN has the same session volume in both fixture windows, so the lost
-- success count is an exact additive decomposition of the overall decline.
WITH cdn_counts AS (
    SELECT
        cdn,
        SUM(CASE WHEN window_name = 'previous_window' AND play_success THEN 1 ELSE 0 END)
            AS previous_successes,
        SUM(CASE WHEN window_name = 'current_window' AND play_success THEN 1 ELSE 0 END)
            AS current_successes
    FROM stream_sessions
    WHERE timestamp >= TIMESTAMP '2026-09-01 10:00:00'
      AND timestamp < TIMESTAMP '2026-09-01 12:00:00'
    GROUP BY cdn
),
contributions AS (
    SELECT
        cdn,
        previous_successes,
        current_successes,
        previous_successes - current_successes AS lost_successes
    FROM cdn_counts
)
SELECT
    cdn,
    previous_successes,
    current_successes,
    lost_successes,
    CAST(lost_successes AS DOUBLE)
        / NULLIF(SUM(lost_successes) OVER (), 0) AS decline_contribution
FROM contributions
ORDER BY lost_successes DESC;

-- Phase 1.2 Demo 1: compare both windows and attach current-window alarms.
-- hourly_alarm_correlation aggregates both fact tables before joining, so
-- multiple alarms cannot duplicate stream sessions or distort QoE rates.
WITH correlated_windows AS (
    SELECT
        region,
        cdn,
        MAX(CASE WHEN window_name = 'previous_window'
            THEN playback_success_rate END) AS previous_success_rate,
        MAX(CASE WHEN window_name = 'current_window'
            THEN playback_success_rate END) AS current_success_rate,
        MAX(CASE WHEN window_name = 'current_window'
            THEN alarm_count ELSE 0 END) AS current_alarm_count,
        MAX(CASE WHEN window_name = 'current_window'
            THEN high_severity_alarm_count ELSE 0 END)
            AS current_high_severity_alarm_count,
        MAX(CASE WHEN window_name = 'current_window'
            THEN representative_high_error_code END) AS correlated_error_code,
        MAX(CASE WHEN window_name = 'current_window'
            THEN representative_high_alarm_message END) AS correlated_alarm_message
    FROM hourly_alarm_correlation
    WHERE window_start >= TIMESTAMP '2026-09-01 10:00:00'
      AND window_start < TIMESTAMP '2026-09-01 12:00:00'
    GROUP BY region, cdn
)
SELECT
    region,
    cdn,
    previous_success_rate,
    current_success_rate,
    current_success_rate - previous_success_rate AS success_rate_change,
    current_alarm_count,
    current_high_severity_alarm_count,
    correlated_error_code,
    correlated_alarm_message
FROM correlated_windows
ORDER BY success_rate_change ASC, current_high_severity_alarm_count DESC;

-- Phase 1.2 Demo 2: strongest correlated signal for the 华南 degradation.
WITH south_correlated_windows AS (
    SELECT
        cdn,
        MAX(CASE WHEN window_name = 'previous_window'
            THEN playback_success_rate END) AS previous_success_rate,
        MAX(CASE WHEN window_name = 'current_window'
            THEN playback_success_rate END) AS current_success_rate,
        MAX(CASE WHEN window_name = 'current_window'
            THEN alarm_count ELSE 0 END) AS current_alarm_count,
        MAX(CASE WHEN window_name = 'current_window'
            THEN high_severity_alarm_count ELSE 0 END)
            AS current_high_severity_alarm_count,
        MAX(CASE WHEN window_name = 'current_window'
            THEN representative_high_error_code END) AS correlated_error_code,
        MAX(CASE WHEN window_name = 'current_window'
            THEN representative_high_alarm_message END) AS correlated_alarm_message
    FROM hourly_alarm_correlation
    WHERE region = '华南'
      AND window_start >= TIMESTAMP '2026-09-01 10:00:00'
      AND window_start < TIMESTAMP '2026-09-01 12:00:00'
    GROUP BY cdn
)
SELECT
    cdn,
    previous_success_rate,
    current_success_rate,
    current_success_rate - previous_success_rate AS success_rate_change,
    current_alarm_count,
    current_high_severity_alarm_count,
    correlated_error_code,
    correlated_alarm_message
FROM south_correlated_windows
ORDER BY success_rate_change ASC, current_high_severity_alarm_count DESC
LIMIT 1;
