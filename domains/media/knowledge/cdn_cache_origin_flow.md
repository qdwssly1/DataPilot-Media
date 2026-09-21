---
document_id: media-cdn-cache-origin-flow
title: CDN Delivery Cache and Origin Flow
category: cdn_architecture
domain: media
tags: [cdn, cache-hit-ratio, cache-miss, origin-fetch, edge-node, cdn-degradation]
---
# CDN 分发、缓存与回源链路 / CDN Delivery, Cache, and Origin Flow

## 1. 定义与请求路径 / Definition and Request Path

CDN 从边缘缓存到回源的请求路径包括调度、缓存键匹配、命中返回、未命中回源和可缓存响应写入。

典型 CDN 请求先经 DNS 或调度系统到达边缘节点（Edge Node）。边缘根据主机、路径、查询参数、请求头等组成缓存键：命中（Cache Hit）时直接返回对象；未命中（Cache Miss）时，经上层缓存或缓存填充组件向源站发起回源请求（Origin Fetch），再把结果返回客户端并在符合规则时缓存。

厂商的缓存层级、回源协议、请求合并、Origin Shield 和日志字段并不相同。本文只总结通用链路，具体默认值必须以实际 CDN 产品配置为准。

## 2. 常见指标与现象 / Metrics and Symptoms

常见信号包括缓存命中率（Cache Hit Ratio）、未命中率、回源请求量/带宽、缓存填充延迟、边缘首字节时间、源站首字节时间、HTTP 4xx/5xx、缓存键基数和不同区域/节点的响应差异。

“CDN 一直去源站拿内容”“边缘节点一直找源站”“回源请求突然变多”通常对应缓存命中率下降或内容不可缓存，但也可能来自新内容冷启动、直播分片快速更新、主动失效或流量结构变化。

## 3. 常见候选原因 / Common Candidates

- 缓存键包含高基数或无关查询参数、Cookie、Header，导致同一对象被拆成大量变体。
- `Cache-Control`、`Expires`、TTL 或缓存模式配置使对象频繁过期、重验证或不可缓存。
- 大量新分片、低复用长尾内容、预热不足或主动失效造成合理的冷缓存。
- 清单和分片路径规则不一致，错误路由到不同源站或绕过缓存。
- 对象缺少产品要求的长度、范围或校验头，厂商实现可能降低可缓存性。
- 边缘节点、区域路由或上层缓存异常，导致请求集中回源。

## 4. 建议排查步骤 / Troubleshooting Steps

1. 确认问题是命中率变化、回源量变化、边缘延迟变化还是用户 QoE 变化，避免把不同症状混为一类。
2. 按区域、CDN、主机、路径、对象类型和缓存状态对比基线/当前窗口。
3. 检查缓存键指纹或基数，验证查询参数、Cookie、Header 是否造成碎片化。
4. 检查清单与媒体分片的 TTL、缓存控制头、缓存模式、失效操作和发布时间。
5. 对未命中请求检查回源连接、源站首字节、状态码、范围请求和对象完整性。
6. 检查节点/区域集中度、路由或配置发布，必要时比较另一 CDN，但只有指标稳定时才能称其为未受影响对照。
7. 调整缓存键、TTL、Origin Shield、预热或路由后，同时验证命中率、源站负载和 QoE 是否恢复。

## 5. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：确认播放成功率、启动耗时或缓冲代理是否在区域/CDN 维度发生变化。
- `get_alarm_events`：查询同窗口 CDN 和上游告警。
- `query_logs`：检查现有结构化 CDN/源站日志和超时线索。
- SQL / Wren：按当前模型可用维度做窗口对比。

当前合成模型没有缓存命中率、缓存键、TTL、边缘节点 ID、回源请求量或源站延迟，因此知识库只能建议补充这些证据，不能声称当前发生缓存退化。

## 6. 结论边界 / Evidence Boundary

缓存命中率下降与源站负载上升可能相关，但二者也可能同时由流量切换、新内容发布或缓存配置变化引起。只有请求日志、缓存状态、回源链路和变更记录能够建立更具体的解释。厂商文档中的默认值和限制属于产品特定行为，不应写成行业统一规则。

## 7. 关联主题 / Related Topics

- `origin_timeout.md`
- `dns_routing_failover.md`
- `playback_failure.md`
- `startup_latency_diagnostics.md`
- `troubleshooting_sop.md`

### 8. References

- [Media CDN overview](https://docs.cloud.google.com/media-cdn/docs/overview) — Google Cloud，访问日期 2026-09-21。支持路由器、缓存、缓存填充、缓存键和回源请求的产品链路示例。
- [Configure caching behavior](https://docs.cloud.google.com/media-cdn/docs/caching) — Google Cloud，访问日期 2026-09-21。支持缓存模式、TTL、重验证、范围缓存和回源负载关系；具体数值仅适用于该产品。
- [Caching and availability](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/ConfiguringCaching.html) — Amazon Web Services，访问日期 2026-09-21。支持缓存命中率、边缘缓存和降低源站请求的一般关系。
- [How CloudFront delivers content](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/HowCloudFrontWorks.html) — Amazon Web Services，访问日期 2026-09-21。支持 DNS 调度、边缘命中与未命中后回源的产品流程示例。
