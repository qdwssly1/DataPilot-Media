---
document_id: media-error-codes
title: Media Alarm and Error Code Reference
category: error_code
domain: media
tags: [alarm, error-code, cdn, timeout]
---
# Media Alarm and Error Code Reference

## E302 — CDN Upstream Timeout

E302 means that a CDN edge or intermediate node timed out while waiting for an upstream response. Common triggers include origin overload, an unhealthy origin route, packet loss between CDN and origin, an overly short upstream timeout, or slow object generation.

Check alarm start/end time, affected region and CDN, request volume, upstream latency and timeout rate, origin health, and how other CDNs changed in the same window. Treat another CDN as unaffected only when its measured QoE is stable; a smaller decline is still a decline. E302 is evidence of an upstream-timeout symptom. E302 alone does not prove the root cause, nor does temporal overlap prove that the alarm caused a QoE decline.

## E401 — Token or Playback Authorization Rejected

E401 indicates that the playback request was rejected by an authorization or token check. Common triggers include an expired token, clock skew, an incorrect signing key, or a malformed authorization parameter.

Validate rejection rate, token expiry distribution, server clocks, signing-key rollout, and affected applications. Do not infer an authentication defect from an isolated E401 event without request-level or aggregate evidence.

## E503 — Origin or Service Unavailable

E503 indicates that an origin or dependent service was unavailable or had no healthy capacity. Common triggers include an unhealthy origin pool, overload, failed deployment, or dependency outage.

Check origin health, capacity, deploy events, retries, and cross-CDN behavior. It is distinct from E302: E503 reports unavailability while E302 reports waiting beyond a timeout threshold.

## T100 — Transcode Job Input Unreadable

T100 indicates that a transcode worker could not read or demux its input. Possible triggers include a missing object, damaged container metadata, unsupported input, or storage permission failure.

Confirm input existence, checksum, container probe output, and storage access before retrying. This job error is not direct proof of a playback incident unless affected output and session evidence are linked.
