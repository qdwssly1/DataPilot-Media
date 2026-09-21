---
document_id: media-metric-alarm-log-correlation
title: Metric Alarm and Log Correlation
category: observability
domain: media
tags: [metrics, alarms, logs, correlation, observability, provenance, causality]
---
# 指标、告警与日志联合分析 / Metric, Alarm, and Log Correlation

## 1. 定义与目标 / Definition and Goal

多源观测分析把指标（Metrics）、告警（Alarms）和日志（Logs）放入一致的时间、实体和作用域中，形成可追踪的多源证据（Multi-source Evidence）。指标回答“影响多大、从何时开始、集中在哪里”；告警提示已检测到的条件；日志提供离散请求或组件事件的细节。

联合出现能缩小排查范围，但不会自动证明因果关系。三个信号也可能来自共同原因、监控规则或偶然重合。

## 2. 作用域与关联键 / Scope and Correlation Keys

关联前应固定：指标名称与口径、基线/当前窗口、区域、CDN、设备或内容范围、告警错误码/级别/状态，以及日志的服务、主机、请求 ID、trace ID、流 ID 或对象路径。

最弱关联是“时间接近”；更强关联依次包括共享区域/CDN、共享对象或流、共享请求/trace，以及可重复的依赖链路。ALL alarms 的状态分布与 E302 子集分布属于不同事实，不能混为同一作用域。

## 3. 常见分析错误 / Common Failure Modes

- 用 `MAX(status)` 或一条样本代表全部告警状态。
- 将最多 3 条有界样本泛化为所有事件的消息或状态。
- 把全国指标与 CDN-B 日志拼成同一实体结论，或混用不同时间窗口。
- 只看失败数量，不看总流量和失败率；直接平均预聚合比率。
- 先认定根因，再筛选支持它的日志，忽略反例和其他同时变化。
- 把“同时发生”“高度相关”改写为“导致”“引发”。

## 4. 建议排查步骤 / Troubleshooting Steps

指标、告警和日志的联合排查必须先对齐时间窗口、实体与作用域，再按证据强度形成观察、相关性和待验证假设。

1. 先确定用户可见症状、指标口径和基线/当前窗口，记录总体及分组变化。
2. 找出贡献最大的区域/CDN，同时保留其他组的真实变化，不把轻度下降称为健康。
3. 查询同窗口告警，按错误码、严重等级、状态和作用域聚合，并保留有限原始样本。
4. 查询相同实体/窗口的结构化日志，优先使用 request/trace/stream ID 进行精确关联。
5. 对齐发布、配置、流量切换和依赖服务变化，列出支持与反对每个假设的证据。
6. 给候选影响因素排序，明确还缺哪些 trace、源站、路由或恢复证据。
7. 通过受控变更或恢复验证更新结论；若证据冲突，保持未决而不是强行归因。

## 5. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：提供基线/当前窗口和区域/CDN 分组对比。
- `get_alarm_events`：提供告警原始事件、确定性分布和有界样本。
- `query_logs`：提供结构化日志。
- `get_transcode_status`：在任务实际需要时补充转码证据。
- SQL / Wren：提供工具无法完整表达时的有界只读纠错结果。

证据必须保留 source ID、来源任务、过滤条件、窗口和作用域；Knowledge Evidence 只能解释含义，不能生成当前事件数量。

## 6. 结论边界 / Evidence Boundary

- **Observation**：数据库/工具直接返回且作用域明确的事实。
- **Correlation**：两个或多个兼容数据证据在时间和实体上共同变化。
- **Hypothesis**：至少有数据证据，并由领域知识或限制项支持的待验证解释。
- **Causal Claim**：需要请求级链路、明确依赖、受控实验或恢复验证等充分证据。

Google SRE 的排障方法同样强调 correlation is not causation。应同时记录“已知事实、未知项、下一步如何证伪或验证”。

## 7. 关联主题 / Related Topics

- `troubleshooting_sop.md`
- `origin_timeout.md`
- `dns_routing_failover.md`
- `playback_failure.md`
- `live_stream_failure_sop.md`

### 8. References

- [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) — Google SRE，访问日期 2026-09-21。支持症状与原因、黑盒/白盒监控、时间对比和分层系统中的诊断边界。
- [Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) — Google SRE，访问日期 2026-09-21。支持系统化假设、日志/指标/trace 使用，以及相关性不等于因果关系。
- [OpenTelemetry Logging](https://opentelemetry.io/docs/specs/otel/logs/) — OpenTelemetry，访问日期 2026-09-21。支持按时间、TraceId/SpanId 与资源上下文关联日志、指标和 trace。
- [Media CDN request logging](https://docs.cloud.google.com/media-cdn/docs/logging) — Google Cloud，访问日期 2026-09-21。支持请求 ID、缓存状态、区域、源站和延迟字段的产品级日志示例。
