# Telemetry and crash reports

StreamStack can expose numbers about the rig to your own Prometheus, and it writes a structured report when something
crashes. Both stay on your computer. Nothing is sent anywhere, and there is no outbound telemetry.

## Prometheus metrics

Off by default. Turn it on under System, Telemetry and crash reports, or set `STREAMSTACK_TELEMETRY=true` in
`~/.config/streamstack/streamstack.env`. While it is off, `/metrics` does not exist (404 for a caller with a token,
401 for one without) and nothing is measured.

`GET /metrics` on the Console answers in the Prometheus text format (0.0.4). It needs a token, sent as
`X-StreamStack-Token`, `Authorization: Bearer ...` or `?token=`. Two tokens work:

- the Console's own token (it changes at every launch), and
- a stable scrape token, created when you switch telemetry on and kept in `auto.env` (mode 600) as
  `STREAMSTACK_TELEMETRY_TOKEN`. It opens `/metrics` and nothing else. It is never part of the Console state and
  never shown on the page. "Copy scrape config" puts a ready `scrape_configs` block, token included, on the
  clipboard; "New token" replaces it and locks the old one out.

The Console listens on 127.0.0.1 only. To scrape from another machine, use a tunnel (the Ziti ZTNA path in a fleet).

```yaml
scrape_configs:
  - job_name: streamstack
    metrics_path: /metrics
    authorization: {type: Bearer, credentials: <scrape token>}
    static_configs:
      - targets: ['127.0.0.1:<console port>']
```

### What is exposed

No video, no audio, no names, no file paths, no meeting titles. A light is labelled by a short hash of its id.

| Metric | Type | Meaning |
|---|---|---|
| `streamstack_service_up{service}` | gauge | 1 when running and healthy: `console`, `studio`, `voice_chain`, `canon_bridge`, `lights_service`, `mic_watch`, `auto_director`, and `stream` (when the Console reports streaming) and `engine` (when it reports one) |
| `streamstack_health_check_state{check,group}` | gauge | pre-flight check: 0 ok, 1 warn, 2 fail |
| `streamstack_health_checks{state}` | gauge | pre-flight checks by result |
| `streamstack_studio_busy` | gauge | studio starting or stopping |
| `streamstack_virtual_camera`, `streamstack_privacy_scene` | gauge | video state flags |
| `streamstack_canon_fps_in`, `_fps_out`, `_grab_milliseconds`, `_cpu_percent`, `_decode_on_gpu` | gauge | Canon bridge health |
| `streamstack_canon_restarts_total`, `streamstack_canon_stalls_total` | counter | bridge restarts and stalls |
| `streamstack_stream_live`, `_stream_degraded`, `_stream_bitrate_kbps`, `_stream_dropped_frames`, `_stream_latency_milliseconds`, `_stream_uptime_seconds`, `streamstack_recording_active` | gauge | recording and streaming |
| `streamstack_stream_destination_up`, `_destination_health`, `_destination_restarts{destination,kind}` | gauge | each destination, labelled by kind and a hash of its id (never its name, URL or key) |
| `streamstack_mic_present`, `_stalled`, `_stalled_seconds`, `_muted`, `_gain_db` | gauge | microphone and its stall watchdog |
| `streamstack_audio_peak_dbfs{source}`, `_rms_dbfs`, `_loudness_lufs` | gauge | levels, taken from the Console's meter stream |
| `streamstack_audio_clip_latched{source}` | gauge | clip lamp lit |
| `streamstack_audio_clip_events_total{source}` | counter | clip events counted since telemetry was switched on |
| `streamstack_light_power`, `_brightness_percent`, `_temperature_kelvin`, `_connected`, `_held{light}` | gauge | each light |
| `streamstack_process_cpu_seconds_total{role}`, `streamstack_process_resident_memory_bytes{role}`, `streamstack_process_count{role}` | counter/gauge | StreamStack's own processes (`console`, `lights`, `canon-bridge`, `auto`, `tray`, `cli`), read from `/proc` without command lines |
| `streamstack_host_cpu_percent`, `streamstack_host_load1`, `streamstack_host_cores` | gauge | the computer |
| `streamstack_event_loop_lag_seconds` | histogram | how late the Console's sampler thread wakes (the Console is threaded, so this is scheduling delay) |
| `streamstack_event_loop_lag_max_seconds` | gauge | worst delay since the last scrape |
| `streamstack_actions_total{action,outcome}`, `streamstack_action_errors_total{action,kind}` | counter | Console actions and their failures |
| `streamstack_action_duration_seconds{action}` | histogram | action latency |
| `streamstack_http_errors_total{status}` | counter | HTTP errors answered |
| `streamstack_build_info{version}`, `streamstack_uptime_seconds` | gauge | version and uptime |
| `streamstack_crash_reports`, `streamstack_last_crash_timestamp_seconds` | gauge | crash reports on disk |
| `streamstack_store_warnings` | gauge | repaired files the Console still reports |
| `streamstack_telemetry_scrapes_total` | counter | scrapes of this endpoint |

Label sets are bounded: a counter or histogram that would exceed its series cap folds the overflow into `other`.

### Not yet exposed (follow-ups)

- OTLP export. It is optional and would be the first outbound path, so it stays out until it is wanted. When it is,
  it should be a second opt-in that names its endpoint explicitly.
- GPU, network and calendar figures, the pre-flight score, and encoder dropped frames: the Console does not measure
  them yet. Add the metric where the measurement is made.
- The `engine` service appears as soon as the engine publishes `state.engine`.

## Crash reports

`streamstack`, `streamstack-auto` and the tray install hooks for uncaught exceptions (main thread and other
threads), unraisable exceptions and fatal signals (`faulthandler`). Each crash writes one JSON file to
`~/.local/state/streamstack/crashes/` (the newest 20 are kept): version, Python, system, component, thread, error,
cause, stack and a small allow-listed summary of the Console state. Errors the Console survives but should not
ignore (a failed action, a handler that broke) are filed too, at most one per kind and place every ten seconds.

A fatal signal cannot run Python, so `faulthandler` writes its stack to `native-<component>-<pid>.log`; the next
start of any StreamStack program turns the log of a dead process into a report of kind `native`.

Nothing secret is written. Tokens in URLs or headers, `key=`, `token=` and `password=` pairs, passwords inside URLs,
stream keys at the end of an RTMP, SRT, RIST or WHIP address, and your home directory (shortened to `~`) are
replaced before a report is stored. The state summary is built from an allow-list, never from the whole state.

System lists the newest reports with a link to each, and "Clear" removes them. The same list is `state.diagnostics`.
