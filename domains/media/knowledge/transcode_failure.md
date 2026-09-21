---
document_id: media-transcode-failure
title: Transcoding Failure Diagnosis
category: codec_transcoding
domain: media
tags: [transcoding-failure, encoding-failure, codec, container, profile, audio, output-validation]
---
# 转码失败诊断 / Transcoding Failure Diagnosis

## 1. 定义与阶段 / Definition and Stages

转码失败（Transcoding Failure / Encoding Failure）是媒体处理任务在读取输入、解复用、解码、滤镜/缩放、音频处理、编码、封装、分片、写出或输出校验任一阶段未能生成符合要求的结果。任务失败状态只标识结果，排查时必须先定位具体阶段。

转封装（Stream Copy/Remux）与重新编码不同：前者不解码媒体内容，后者通常经历解码、处理和编码，资源成本更高，也可能引入质量损失。

## 2. 常见指标与现象 / Metrics and Symptoms

常见信号包括任务状态、错误码、失败阶段、重试次数、处理时长、队列等待、CPU/GPU/内存、输入探测结果、输出档数量，以及输出文件的容器、编码、Profile、Level、分辨率、帧率、码率、像素格式、音频轨和时长。

“源文件可以播放”不等于转码服务一定支持其容器/编码组合；“任务完成”也不等于输出清单、分片、音视频同步和终端兼容性已经验证。

## 3. 常见候选原因 / Common Candidates

- 输入路径错误、权限不足、对象缺失、上传未完成、文件截断或容器元数据损坏。
- 输入缺少预期视频/音频轨，轨道选择、声道映射、字幕或时间码配置错误。
- 编码/容器不受支持，或 Profile、Level、位深、像素格式、分辨率、帧率组合不兼容。
- 码率、分辨率、滤镜图、GOP、硬件编码器或输出封装参数冲突。
- CPU/GPU/内存不足、驱动/硬件编码器异常、任务超时或临时基础设施故障。
- 输出路径、权限、容量、分片/清单生成或发布验证失败。

## 4. 建议排查步骤 / Troubleshooting Steps

源文件可播放但转码任务失败时，仍应逐项检查输入可读性、轨道、编码/容器支持、配置、资源和输出完整性。

1. 记录任务 ID、失败阶段、错误码、输入/输出 URI、模板版本和首次失败时间。
2. 验证输入存在、权限和完整性；使用 `ffprobe` 等工具检查容器、流、编码、Profile/Level、分辨率、帧率、音频和时长。
3. 核对任务轨道选择、音频声道、字幕/时间码和输入输出路径。
4. 将配置缩减到最小可用输出，判断是输入本身、特定滤镜、编码器还是某个输出档导致失败。
5. 检查软件/硬件编码器支持矩阵、资源饱和、驱动和区域容量；区分确定性失败与瞬时失败。
6. 对输出执行结构验证：文件可读、轨道齐全、时长/时间戳合理、清单引用存在、分片可下载。
7. 仅对明确瞬时故障重试；确定性输入或配置错误应先修复，避免无界重试。
8. 若分析播放影响，必须把失败任务、受影响输出与实际播放会话建立关联。

## 5. DataPilot 可用证据 / Available Evidence

- `get_transcode_status`：查询合成任务状态、错误码和结构化信息。
- `query_qoe_metrics`：验证同窗口是否存在播放端影响，但不能自动把两者关联为同一内容。
- `get_alarm_events` 与 `query_logs`：检查同期平台告警和现有结构化日志。
- SQL / Wren：在当前模型字段范围内进行任务和时间窗口分析。

当前模型没有输入 URI、容器探测、编码参数、资源利用率、输出清单或内容 ID 映射，无法仅凭任务状态确认具体失败阶段或播放影响。

## 6. 结论边界 / Evidence Boundary

云厂商错误码和支持矩阵是产品特定行为，不能跨产品直接套用。任务失败与播放 QoE 同期变化只构成相关性；只有内容/输出映射、请求日志和恢复验证才能证明失败任务影响了哪些播放会话。

## 7. 关联主题 / Related Topics

- `codec_transcoding.md`
- `error_codes.md`
- `playback_failure.md`
- `live_streaming_pipeline.md`
- `live_stream_failure_sop.md`

### 8. References

- [ffmpeg Documentation](https://ffmpeg.org/ffmpeg.html) — FFmpeg Project，访问日期 2026-09-21。支持输入、解复用、解码、滤镜、编码、输出与 stream copy/transcoding 的阶段关系。
- [ffprobe Documentation](https://ffmpeg.org/ffprobe.html) — FFmpeg Project，访问日期 2026-09-21。支持检查容器、流、包、帧、错误和机器可读输出的方法。
- [Troubleshooting Transcoder API](https://docs.cloud.google.com/transcoder/docs/troubleshooting) — Google Cloud，访问日期 2026-09-21。支持缺失轨道、无效映射、不支持/损坏输入和内部错误等产品示例。
- [MediaConvert error codes](https://docs.aws.amazon.com/mediaconvert/latest/ug/mediaconvert_error_codes.html) — Amazon Web Services，访问日期 2026-09-21。支持输入访问、缺少音视频、编码不支持、配置不兼容、资源与输出错误等产品特定分类。
