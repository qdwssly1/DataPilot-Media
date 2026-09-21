---
document_id: media-troubleshooting-sop
title: Media QoE Troubleshooting SOP
category: troubleshooting_sop
domain: media
tags: [sop, qoe, cdn, startup, rebuffer, alarm]
---
# Media QoE Troubleshooting SOP

## CDN Playback Success Degradation SOP

CDN 播放成功率下降时，应先用等长基线/当前窗口重算整体与区域、CDN 分组指标，再关联同作用域告警、日志、发布和流量切换；降幅较小的 CDN 不能自动称为健康对照组。

1. Define equal baseline and current windows and recompute playback success rate from successful and total sessions; do not average ratios.
2. Confirm the overall change and attempt volume, then break down only by available dimensions such as region, CDN, and device.
3. Identify the region/CDN with the largest weighted contribution using failed-session or denominator-aware changes.
4. Retrieve alarms in the same window and match region, CDN, and time. For E302, inspect upstream latency, timeout rate, origin health, and routing.
5. Compare the affected CDN with other CDNs in the same region and check deploy, traffic-shift, and origin events. Call a comparison CDN unaffected only when its measured QoE is stable; a smaller decline is not a healthy result.

Report SQL results as data facts, SOP steps as knowledge, and any diagnosis as an inference. Concurrent alarms are correlation; without request traces, origin telemetry, routing data, or a controlled mitigation, causality remains unproven.

## Startup Latency Increase SOP

1. Compare average and P95 startup time over matched baseline/current windows, together with playback success rate and session count.
2. Segment by available region, CDN, and device dimensions. Do not request OS, player version, protocol, or content type unless the schema exposes them.
3. Check DNS/connect/TLS, manifest and first-segment latency, CDN cache miss, upstream latency, player initialization, and decode readiness in their owning systems.
4. Correlate alarms and deployment or traffic-shift events by time and scope.

An aggregate `startup_time` increase locates a symptom. It cannot by itself distinguish network, CDN, origin, player, or decoder causes. P95 must be computed from session observations or mergeable sketches, not averaged from subgroup P95 values.

## Rebuffer Ratio Increase SOP

1. Recompute rebuffer ratio from summed rebuffer duration and summed playback duration over comparable windows. If only `buffer_duration` exists, label it a diagnostic proxy.
2. Check playback traffic and success, then compare region, CDN, and device slices that exist in the schema.
3. Inspect throughput, segment-download latency, cache hit, upstream latency, bitrate ladder selection, and player buffer health using the appropriate telemetry source.
4. Correlate alarms by region/CDN/time and compare unaffected cohorts.

Do not average pre-aggregated ratios. Do not claim a CDN or codec root cause without the corresponding telemetry or an intervention that changes the symptom.

## Alarm Correlation and Evidence Rules

同期告警与 QoE 下降只能在时间、区域和 CDN 作用域一致时形成相关性证据。告警状态应保留真实分布，不能用单一聚合值代替全部事件，也不能把时间重合表述为已证明的因果关系。

Match an alarm to a QoE window by overlapping time and the shared region/CDN scope. Include severity and status, and distinguish an active alarm from a historical or resolved alarm.

When the schema exposes alarm status, retrieve individual alarms or counts grouped by status. An active-alarm count or a representative status cannot describe the full lifecycle-state distribution.

Temporal and dimensional overlap increases diagnostic relevance but is not causal proof. Preserve the difference between query results, documented error-code meaning, and analyst inference. State missing evidence explicitly.
