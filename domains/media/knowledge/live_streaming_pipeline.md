---
document_id: media-live-streaming-pipeline
title: Live Streaming Pipeline
category: streaming_architecture
domain: media
tags: [live-streaming, ingest, push-stream, pull-stream, transcoding, packaging, cdn]
---
# 直播链路 / Live Streaming Pipeline

## 1. 定义与通用链路 / Definition and Pipeline

典型直播链路为：推流端或上游源 → 接入/采集（Ingest）→ 处理与转码 → 打包（Packaging）→ 源站或对象存储 → CDN → 播放器。上游可以主动推流（Push），也可以由处理系统拉流（Pull）；输出可能是 HLS、MPEG-DASH、WebRTC 或厂商特定协议。

不同协议和产品会合并、拆分或旁路其中某些阶段。例如低延迟方案可能以更小分片、分块传输或实时媒体通道降低等待，但对时钟、可用性和网络抖动更敏感。

## 2. 各阶段信号 / Stage Signals

- **推流/接入**：输入连接状态、输入码率、帧率、时间戳连续性、丢包和主备输入状态。
- **处理/转码**：通道状态、处理延迟、输入/输出帧率、编码错误、资源饱和和各档输出健康度。
- **打包/发布**：清单更新时间、分片生成/上传延迟、序列号、时间戳、音视频同步和对象可用性。
- **CDN**：缓存状态、边缘/回源延迟、HTTP 错误、区域节点和流量分布。
- **播放端**：播放成功率、首帧、卡顿、直播延迟、所选码率、播放器错误和设备兼容性。

端到端问题需要把这些信号按事件时间和流 ID 对齐，而不是只看一个环节“进程正常”。

## 3. 常见候选原因 / Common Candidates

直播清单不更新或最新分片发布晚时，应优先检查打包输出、发布时间线、对象可用性与播放器刷新行为，再沿相邻阶段扩展排查。

- 推流端断流、码率/帧率剧烈抖动、时间戳回退或网络上行不稳。
- 接入鉴权、IP 白名单、协议、端口或主备输入切换错误。
- 转码资源不足、单档编码失败、音频轨缺失或输出参数不兼容。
- 打包器清单未更新、分片生成晚、序列/时间线不连续或发布不原子。
- 源站上传/读取慢、CDN 缓存与回源异常、区域路由退化。
- 播放端未及时刷新清单、请求过于接近直播边缘、缓冲目标不足或解码不兼容。

## 4. 建议排查步骤 / Troubleshooting Steps

1. 建立单个频道/流 ID 的端到端时间线，明确第一个异常阶段和影响开始时间。
2. 验证推流输入持续存在，检查输入协议、码率、帧率、时间戳和主备源状态。
3. 检查转码通道及每个输出档，不以“通道运行中”替代输出内容验证。
4. 检查清单刷新、最新分片序列、分片可下载性、音视频同步和发布时间。
5. 从源站与多个 CDN/区域请求同一清单和分片，区分发布、缓存、回源和边缘问题。
6. 检查播放器状态、直播边缘位置、缓冲和终端兼容性。
7. 使用 request/stream trace 或共同标识关联各阶段；切换输入、输出或 CDN 后验证恢复。

## 5. DataPilot 可用证据 / Available Evidence

- `query_qoe_metrics`：观察播放端结果及区域/CDN 分布。
- `get_alarm_events`：查询 CDN 和业务告警。
- `query_logs`：检查当前结构化上游超时日志。
- `get_transcode_status`：查询合成转码任务状态。
- SQL / Wren：进行现有 Media 模型支持的窗口与维度分析。

当前模型不含流 ID、推流状态、频道、清单版本、分片序列、直播延迟或端到端 trace，不能凭播放端数据判断首个故障阶段。

## 6. 结论边界 / Evidence Boundary

“推流端仍在线”只证明输入连接的某个层面存在，不证明输入内容、转码输出、清单发布、CDN 分发和终端播放均健康。多个阶段同时告警属于多源证据；只有共同流 ID、时间线和依赖路径才能增强阶段间因果判断。

## 7. 关联主题 / Related Topics

- `live_stream_failure_sop.md`
- `transcode_failure.md`
- `cdn_cache_origin_flow.md`
- `buffering_rebuffering.md`
- `metric_alarm_log_correlation.md`

### 8. References

- [How MediaLive works](https://docs.aws.amazon.com/medialive/latest/ug/how-medialive-works-channels.html) — Amazon Web Services，访问日期 2026-09-21。支持输入、频道、转码、输出组、下游打包/CDN 和双管线的产品流程示例。
- [Overview of the Live Stream API](https://docs.cloud.google.com/livestream/docs/overview) — Google Cloud，访问日期 2026-09-21。支持 RTMP/SRT 输入、转码、HLS/DASH 输出、存储和 CDN 衔接的产品流程示例。
- [RFC 8216: HTTP Live Streaming](https://www.rfc-editor.org/rfc/rfc8216.html) — IETF / RFC Editor，访问日期 2026-09-21。支持 HLS 清单、变体和媒体分片的协议结构。
- [DASH-IF Live Media Ingest Protocol](https://dashif.org/Ingest/) — DASH Industry Forum，访问日期 2026-09-21。支持直播接入接口、同步、冗余和故障转移的开放技术规范。
