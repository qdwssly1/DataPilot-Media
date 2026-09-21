---
document_id: media-live-stream-failure-sop
title: Live Stream Failure Troubleshooting SOP
category: troubleshooting_sop
domain: media
tags: [live-stream-failure, push-stream, ingest, manifest, segment, playback-failure, sop]
---
# 直播故障排查 SOP / Live Stream Failure Troubleshooting SOP

## 1. 适用场景 / Scope

本 SOP 适用于“直播推流正常但观众播放失败”“部分区域直播黑屏”“直播突然卡住”“只有某个清晰度不可播放”等问题。首先明确频道/流 ID、协议、开始时间、影响区域/CDN、受影响输出档和用户现象。

“推流正常”必须说明验证层级：仅连接存在、持续收到包、音视频时间戳连续，还是内容已被解码验证。不同层级不能互相替代。

## 2. 第一步：确认影响范围 / Confirm Scope

对比基线与当前窗口的播放成功率、首帧、缓冲和会话量，并按区域、CDN、设备及实际可用维度分组。分别测试主清单、各媒体清单和多个分片，确认是全流失败、单档失败、单区域/CDN 失败还是终端兼容问题。

如果其他 CDN 也下降，只能说某 CDN 降幅更大，不能把下降较少的 CDN 称为健康对照组。

## 3. 第二步：逐段验证链路 / Validate Each Stage

1. **推流与接入**：检查连接、输入码率/帧率、时间戳、丢包、鉴权和主备输入。
2. **处理与转码**：检查频道和每个输出档状态、资源、编码错误、音视频轨与关键帧。
3. **打包与发布**：检查清单是否持续更新、最新分片是否存在且可完整下载、序列与时间线是否连续。
4. **源站与 CDN**：从源站和不同区域/CDN 请求同一对象，检查缓存、回源、状态码、延迟和告警。
5. **播放器**：检查状态转换、错误分类、直播边缘位置、缓冲水位和解码兼容性。

## 4. 第三步：关联告警与变更 / Correlate Signals

把指标、告警、结构化日志、转码状态和发布/配置变更对齐到同一频道、区域、CDN 和时间窗口。告警状态必须按 `open`、`investigating`、`resolved` 等真实分布保留。检查故障开始前后的推流切换、编码模板、清单配置、CDN 路由和源站发布。

没有共同流 ID 或请求 ID 时，只能进行较弱的时间/作用域相关分析。

## 5. 第四步：缓解与恢复验证 / Mitigation and Recovery

根据已定位阶段选择最小缓解：切换备输入、禁用异常输出档、重启失败管线、修复清单/分片发布、切换源站/CDN、回滚配置或降低码率。每次只改变可观测的有限变量，并记录时间。

恢复验证应同时观察输入/输出健康、清单与分片、CDN 错误、播放成功率、首帧和卡顿；只看到告警消失或单个测试播放成功并不足够。

## 6. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：提供播放端窗口和区域/CDN 对比。
- `get_alarm_events`：提供告警分布与有界样本。
- `query_logs`：提供现有 structured logs。
- `get_transcode_status`：提供合成转码状态。
- SQL / Wren：补充当前数据模型可表达的聚合。

推流、频道、清单、分片和请求 trace 不在当前模型中，必须作为下一步检查项而不是虚构结果。

## 7. 结论边界与关联主题 / Boundary and Related Topics

输出应分别说明数据事实、知识依据、推断和限制。时间重合可支持相关性；“切换后恢复”会增强候选假设，但若同时发生多个变化，仍不能唯一归因。相关文档：`live_streaming_pipeline.md`、`playback_failure.md`、`transcode_failure.md`、`cdn_cache_origin_flow.md`、`metric_alarm_log_correlation.md`。

### 8. References

- [How MediaLive works](https://docs.aws.amazon.com/medialive/latest/ug/how-medialive-works-channels.html) — Amazon Web Services，访问日期 2026-09-21。支持输入、转码管线、输出组和下游系统的分阶段排查思路。
- [Overview of the Live Stream API](https://docs.cloud.google.com/livestream/docs/overview) — Google Cloud，访问日期 2026-09-21。支持输入端点、频道、主备输入、HLS/DASH 输出和 CDN 衔接。
- [RFC 8216: HTTP Live Streaming](https://www.rfc-editor.org/rfc/rfc8216.html) — IETF / RFC Editor，访问日期 2026-09-21。支持 HLS 清单刷新、媒体分片与客户端职责。
- [DASH-IF implementation guidelines: restricted timing model](https://dashif.org/Guidelines-TimingModel/) — DASH Industry Forum，访问日期 2026-09-21。支持动态清单、分片可用窗口、发布延迟和客户端回退边界。
