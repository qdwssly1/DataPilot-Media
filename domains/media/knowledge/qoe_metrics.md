---
document_id: media-qoe-metrics
title: Media QoE Metric Definitions
category: qoe_metric
domain: media
tags: [qoe, metric, playback, aggregation]
---
# Media QoE Metric Definitions

## Playback Success Rate / 播放成功率

播放成功率表示有效播放尝试中成功起播的会话占比。在 DataPilot-Media 合成模型中，`play_success = true` 表示成功会话；指标口径应同时说明分子、分母、单位和排除规则。

跨区域、CDN、设备或时间窗口聚合时，必须先汇总成功会话数与总尝试数，再重新计算比率；不能直接平均分母不同的预聚合播放成功率。

### Definition

The share of attempted playback sessions that start successfully. In the Media synthetic model, a successful session has `play_success = true`.

### Formula and unit

`successful sessions / total attempted sessions`, reported as a ratio or percentage.

### Aggregation rule

Aggregate from counts: `SUM(successful_sessions) / SUM(total_sessions)`. Across regions, CDNs, devices, or windows, recompute the ratio from the underlying numerators and denominators. Do not directly average already aggregated ratios unless every ratio has the same denominator.

### Direction and pitfalls

Higher is better. Exclude only sessions that the metric contract explicitly marks ineligible. A falling rate identifies an outcome, not a root cause; use dimensions and concurrent evidence before proposing a cause.

## Rebuffer Ratio / 卡顿率

### Definition

The fraction of playback time spent rebuffering after playback starts.

### Formula and unit

`SUM(rebuffer_duration) / SUM(playback_duration)`, reported as a ratio or percentage. When only session-level `buffer_duration` is available, use it as a diagnostic proxy and state that it is not the full ratio denominator.

### Aggregation rule

Sum durations before division. Never average pre-aggregated rebuffer ratios with unequal playback-duration denominators.

### Direction and pitfalls

Lower is better. Separate startup waiting from mid-play rebuffering, and compare like-for-like device, region, CDN, and content populations.

## Startup Time and Startup Time P95 / 首帧耗时

### Definition

Startup time, also called time to first frame (TTFF), is elapsed time from a valid play request until the first decodable video or audio frame is rendered. P95 is the 95th percentile across eligible sessions.

### Formula and unit

Session value is measured in milliseconds. P95 is the percentile of session-level `startup_time`; it is not `AVG(startup_time) * 0.95`.

### Aggregation rule

Compute percentiles from session-level observations, or merge a compatible percentile sketch. Do not average regional or hourly P95 values to obtain a global P95.

### Direction and pitfalls

Lower is better. Segment by device, region, CDN, protocol, and player version only when those fields exist. Confirm whether failures are excluded because conditioning on successful starts can hide severe incidents.

## Failed Session Count / 失败会话数

### Definition

The number of attempted playback sessions that did not start successfully.

### Formula and unit

`COUNT(*) FILTER (WHERE play_success = false)`, measured in sessions.

### Aggregation rule

Counts are additive across disjoint groups and windows. Pair the count with total attempts or playback success rate because traffic volume changes can move the count without changing reliability.

### Direction and pitfalls

Lower is better for comparable traffic. A count alone cannot compare differently sized regions or CDNs fairly.
