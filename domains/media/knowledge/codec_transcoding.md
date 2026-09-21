---
document_id: media-codec-transcoding
title: Codec and Transcoding Basics
category: codec_transcoding
domain: media
tags: [codec, transcoding, h264, h265, av1]
---
# Codec and Transcoding Basics

## H.264, H.265, and AV1 Trade-offs

H.264/AVC has broad device support and moderate encoding cost. H.265/HEVC and AV1 can reduce bitrate at similar visual quality, but encode cost and device compatibility differ. Codec choice should consider playback population, resolution, latency target, licensing, and available hardware acceleration.

A codec name alone does not explain QoE degradation. Validate device compatibility, decode errors, rendition selection, bitrate, and delivery telemetry.

## Transcoding Pipeline Stages

A minimal on-demand pipeline reads and demuxes the input, decodes it, filters or scales frames, encodes renditions, packages segments and manifests, validates outputs, and publishes artifacts. A live pipeline performs similar stages under tighter latency and backpressure constraints.

Failures should be localized to a stage using job status, error code, worker logs, input metadata, and output validation. Retry is appropriate only after classifying whether the failure is transient or deterministic.

## GOP, Keyframes, and Segment Alignment

A group of pictures (GOP) begins with an independently decodable keyframe. Aligned keyframes across bitrate renditions allow a player to switch renditions at segment boundaries. Poor alignment can delay switching or cause playback discontinuity.

GOP length trades compression efficiency against random access, seek behavior, and latency. Diagnose with encoded-output metadata rather than assuming that every startup or rebuffer issue is caused by GOP configuration.

## Bitrate Ladder and Quality Validation

A bitrate ladder supplies multiple resolution/bitrate renditions for adaptive playback. Rungs should match source quality, device population, network distribution, and codec efficiency; adding rungs does not automatically improve quality.

Validate resolution, bitrate, frame rate, codec profile, audio, segment duration, manifest references, and objective or perceptual quality where available. Structural validation confirms deliverability, not business QoE impact.
