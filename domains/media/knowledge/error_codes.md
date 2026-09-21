---
document_id: media-error-codes
title: Media Alarm and Error Code Reference
category: error_code
domain: media
tags: [alarm, error-code, cdn, timeout]
---
# Media Alarm and Error Code Reference

## E302 — CDN Upstream Timeout

E302 是 DataPilot-Media 合成域中的错误码约定，表示 CDN 边缘或中间节点等待上游响应超时，并非 CDN 行业统一标准码。某区域播放成功率下降并同期出现 E302 时，应在相同时间、区域和 CDN 作用域内核对告警状态、上游超时日志、源站与路由证据；时间重合只能支持相关性，不能单独证明因果关系。

E302 is a DataPilot-Media synthetic-domain error-code convention; it is not a CDN industry-standard code. In this synthetic domain, it means that a CDN edge or intermediate node timed out while waiting for an upstream response. Common triggers include origin overload, an unhealthy origin route, packet loss between CDN and origin, an overly short upstream timeout, or slow object generation.

Check alarm start/end time, affected region and CDN, request volume, upstream latency and timeout rate, origin health, and how other CDNs changed in the same window. Treat another CDN as unaffected only when its measured QoE is stable; a smaller decline is still a decline. E302 is evidence of an upstream-timeout symptom. E302 alone does not prove the root cause, nor does temporal overlap prove that the alarm caused a QoE decline.

## E401 — Token or Playback Authorization Rejected

E401 indicates that the playback request was rejected by an authorization or token check. Common triggers include an expired token, clock skew, an incorrect signing key, or a malformed authorization parameter.

Validate rejection rate, token expiry distribution, server clocks, signing-key rollout, and affected applications. Do not infer an authentication defect from an isolated E401 event without request-level or aggregate evidence.

## E503 — Origin or Service Unavailable

E503 indicates that an origin or dependent service was unavailable or had no healthy capacity. Common triggers include an unhealthy origin pool, overload, failed deployment, or dependency outage.

Check origin health, capacity, deploy events, retries, and cross-CDN behavior. It is distinct from E302: E503 reports unavailability while E302 reports waiting beyond a timeout threshold.

## T100 — Transcode Job Input Unreadable

T100 是 DataPilot-Media 合成域中的转码输入不可读错误，表示转码任务无法读取或解封装输入。应先检查输入对象是否存在、权限、校验和、上传完整性、容器探测和编码支持，再决定是否重试。

T100 indicates that a transcode worker could not read or demux its input. Possible triggers include a missing object, damaged container metadata, unsupported input, or storage permission failure.

Confirm input existence, checksum, container probe output, and storage access before retrying. This job error is not direct proof of a playback incident unless affected output and session evidence are linked.
