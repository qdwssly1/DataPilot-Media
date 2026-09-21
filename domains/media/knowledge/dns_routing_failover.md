---
document_id: media-dns-routing-failover
title: DNS Routing and CDN Failover
category: cdn_troubleshooting
domain: media
tags: [dns, routing, failover, regional-degradation, edge-node, origin]
---
# DNS、路由与 CDN 故障转移 / DNS, Routing, and CDN Failover

## 1. 定义与链路 / Definition and Path

音视频分发至少包含两类解析与路由：客户端域名到 CDN 边缘的调度，以及 CDN/中间层到源站域名或地址的上游连接。故障转移（Failover）可能发生在 CDN、源站组、全局流量调度或业务层，触发条件、支持的 HTTP 方法和状态码均可能是产品特定配置。

区域退化（Regional Degradation）表示问题在某些地理区域、网络运营商路径、边缘节点或路由上更突出，不等于整个 CDN 全局故障。

## 2. 常见指标与现象 / Metrics and Symptoms

可观察信号包括 DNS 解析错误/延迟、边缘节点或区域分布、连接/TLS 失败、HTTP 502/503/504、回源 IP、路由变更、故障转移次数、主备源站响应，以及区域/CDN QoE 差异。

“同一 CDN 只有华南下降”“部分用户无法打开而其他区域正常”“切换 CDN 后恢复”是路由候选线索，但也可能由区域源站、配置、流量结构或终端差异造成。

## 3. 常见候选原因 / Common Candidates

- DNS 记录错误、TTL 尚未过期、权威 DNS 异常或解析到非预期地址。
- 调度或 Anycast/BGP 路径变化，使用户到达性能较差或异常的边缘位置。
- 边缘到源站的 DNS、路由、端口、防火墙、TLS 证书、SNI 或 Host 配置错误。
- 路由规则、缓存行为或配置发布只影响部分主机、路径、区域或节点。
- 主源站超时，但故障转移条件、总尝试时限或备源站 Host 配置不正确。
- 主备源站共享依赖，形式上切换但未隔离真实故障域。

## 4. 建议排查步骤 / Troubleshooting Steps

1. 以区域、CDN、时间窗口和请求主机明确影响范围，并确认其他组是否真的稳定。
2. 从受影响与正常网络分别验证 DNS 答案、TTL、CNAME/别名链和传播状态。
3. 检查请求实际到达的边缘位置、回源 IP、协议、端口、TLS SNI/证书与 Host 重写。
4. 对比区域路由、配置版本、流量切换和最近部署，检查是否只影响特定路径。
5. 验证故障转移触发条件、重试次数、总时限、备源站可达性和数据一致性。
6. 检查主备是否共享 DNS、负载均衡、存储或上游服务，避免伪冗余。
7. 受控切换后同时验证 QoE、错误率、路由和源站指标；不能只凭切换成功一次宣称根因确定。

## 5. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：识别区域/CDN 退化范围并比较基线与当前窗口。
- `get_alarm_events`：查询同作用域告警及其真实状态分布。
- `query_logs`：获取当前合成数据中与 CDN、区域和上游超时一致的日志。
- SQL / Wren：进行数据模型允许的区域/CDN 分组分析。

当前模型没有 DNS 答案、运营商、边缘节点 ID、BGP/Anycast、回源 IP、Host/SNI 或故障转移事件；这些应作为证据缺口，而不是由知识库补造。

## 6. 结论边界 / Evidence Boundary

区域性 QoE 差异可支持“问题具有区域作用域”的观察，但不能自动推导为 DNS、运营商或边缘节点故障。切换 CDN 后恢复能增强某条路径相关的假设；若要形成因果结论，还需路由/配置差异、请求级路径或可重复的受控实验。

## 7. 关联主题 / Related Topics

- `cdn_cache_origin_flow.md`
- `origin_timeout.md`
- `playback_failure.md`
- `troubleshooting_sop.md`
- `metric_alarm_log_correlation.md`

### 8. References

- [How CloudFront delivers content](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/HowCloudFrontWorks.html) — Amazon Web Services，访问日期 2026-09-21。支持 DNS 将用户请求引导至边缘、缓存未命中后回源的产品流程示例。
- [Optimize high availability with CloudFront origin failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html) — Amazon Web Services，访问日期 2026-09-21。支持主备源站、状态码触发和连接/响应超时对故障转移的产品特定行为。
- [Troubleshoot origins](https://docs.cloud.google.com/media-cdn/docs/troubleshooting-origins) — Google Cloud，访问日期 2026-09-21。支持 DNS、协议、源站地址、TLS 和 Host 重写排查。
- [Origins overview](https://docs.cloud.google.com/media-cdn/docs/origins) — Google Cloud，访问日期 2026-09-21。支持源站连接、重试、故障转移和超时配置的产品边界。
