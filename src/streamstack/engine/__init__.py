"""The native video engine: StreamStack's own camera pipeline, straight to the hardware (docs/native-video-engine.md).

    source -> GPU upload (or VA-API JPEG decode) -> libplacebo/Vulkan (crop, scale, colour) -> sinks

`graph` is pure (looks, windows and sources in; an ffmpeg filter graph and runtime commands out), `process`
supervises the one ffmpeg child that runs it, `encode` picks the hardware encoder for the codec-agnostic encode tap.
"""
