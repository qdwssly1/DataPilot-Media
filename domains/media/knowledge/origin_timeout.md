---
document_id: media-origin-timeout
title: Origin Timeout and Upstream Diagnosis
category: cdn_troubleshooting
domain: media
tags: [origin-timeout, upstream-timeout, origin-fetch, e302, latency, saturation]
---
# 回源超时与上游诊断 / Origin Timeout and Upstream Diagnosis

## 1. 定义与范围 / Definition and Scope

回源超时（Origin Timeout）或上游超时（Upstream Timeout）表示 CDN、反向代理或中间服务在规定时间内未能建立上游连接、取得可用响应头、持续读取响应或完成响应。连接超时、首包/响应超时和相邻读取超时是不同阶段；字段名称与计时边界取决于具体产品。

E302 是 **DataPilot-Media 合成域定义的错误码语义**，表示 CDN 上游等待超时。它不是 CDN 行业统一标准错误码，也不能映射为任一云厂商的通用 E302。

## 2. 常见指标与现象 / Metrics and Symptoms

常见现象包括边缘返回 502/504、首字节时间上升、回源连接失败、读取中断、重试或故障转移增多，以及播放成功率下降、首帧变慢或分片卡顿。需要同时观察请求量、超时率、源站延迟分布、源站容量、重试次数和受影响对象类型。

三条告警中有 `open`、`investigating`、`resolved` 时，必须保留真实状态分布，不能用 `MAX(status)` 或任一代表值声称所有告警均已解决。

## 3. 常见候选原因 / Common Candidates

- 源站 CPU、线程、连接池、磁盘或依赖服务饱和，无法及时返回数据。
- 动态对象生成、鉴权、数据库或上游 API 延迟增加。
- CDN 到源站的 DNS、TCP/QUIC、TLS、路由、丢包或防火墙问题。
- 源站协议、Host 重写、SNI、证书或端口配置不匹配。
- 超时阈值相对业务响应特征过短；也可能阈值过长导致用户等待和故障转移变慢。
- 重试放大源站负载，或主/备源站共享同一故障依赖。

## 4. 建议排查步骤 / Troubleshooting Steps

回源连接超时、上游等待超时或读取超时应按阶段排查，并把 CDN、源站和 QoE 证据限制在一致作用域。

1. 固定告警、日志和 QoE 的同一时间窗口，确认区域、CDN、对象路径和错误类型作用域。
2. 区分 DNS/连接/TLS、等待响应头、响应读取和总尝试时限，检查对应阶段日志。
3. 查看源站延迟分布、5xx、连接数、饱和度、队列、依赖服务和最近发布/配置变更。
4. 验证源站域名解析、目标 IP、协议、端口、证书、SNI 与 Host 重写。
5. 检查 CDN 重试、重定向、主备源站和故障转移是否消耗共同的总时限。
6. 比较命中与未命中对象、清单与分片、不同区域/CDN 的变化，判断是否集中在回源请求。
7. 通过限流、扩容、路由切换或修复上游后验证超时率和 QoE 同时恢复，并保留请求级关联。

## 5. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：定位播放成功率、启动耗时和缓冲代理的窗口/区域/CDN 变化。
- `get_alarm_events`：获取 E302 的数量、严重等级、状态分布与有界样本。
- `query_logs`：查询同作用域的 upstream-timeout 结构化日志。
- SQL / Wren：执行当前模型允许的补充聚合。

当前模型缺少源站资源、DNS/TLS、连接池、HTTP 状态、对象路径、重试和请求 trace，不能从 E302 数量直接判定具体源站故障。

## 6. 结论边界 / Evidence Boundary

QoE 下降、E302 和 upstream-timeout 日志在同一 CDN/区域/窗口重合，可以形成较强相关性，并把上游超时列为优先候选影响因素。它仍不能证明根因是源站饱和、网络路径或阈值配置；这些假设需要相应阶段指标、请求 trace、变更记录和恢复验证。

## 7. 关联主题 / Related Topics

- `error_codes.md`
- `cdn_cache_origin_flow.md`
- `dns_routing_failover.md`
- `metric_alarm_log_correlation.md`
- `startup_latency_diagnostics.md`

### 8. References

- [Origins overview](https://docs.cloud.google.com/media-cdn/docs/origins) — Google Cloud，访问日期 2026-09-21。支持连接、总尝试、读取和响应超时的产品特定阶段，以及重试/故障转移关系。
- [Troubleshoot origins](https://docs.cloud.google.com/media-cdn/docs/troubleshooting-origins) — Google Cloud，访问日期 2026-09-21。支持 DNS、协议、源站 IP、TLS、Host 重写和故障转移检查项。
- [Request and response behavior for Amazon S3 origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/RequestAndResponseBehaviorS3Origin.html) — Amazon Web Services，访问日期 2026-09-21。支持连接尝试与响应/读取超时的产品示例；默认值不外推到其他 CDN。
- [Origin settings](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html) — Amazon Web Services，访问日期 2026-09-21。支持源站连接、响应和持久连接配置的产品边界。
