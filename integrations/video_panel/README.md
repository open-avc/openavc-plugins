# Video Panel

Show live video streams on the OpenAVC touch panel. The most common source is an
IP camera, but any RTSP, RTMP, or SRT source works (encoders, media servers, and
similar). The plugin bundles [MediaMTX](https://github.com/bluenviron/mediamtx)
as a local helper that pulls each source's feed and republishes it as WebRTC,
which any modern browser can play in a plain `<video>` element. The helper
listens on localhost only; the panel reaches the video through the OpenAVC
server, so it is covered by the same login as the rest of the system.

## Requirements

- One or more video sources that provide an RTSP stream (most often an IP camera).
- For the widest browser support, use **H.264 (Constrained Baseline)**.
  H.265 / HEVC sources are re-encoded to H.264 automatically (this uses more CPU).
- **Network:** WebRTC video travels over **UDP port 8189** directly between the
  browser and the server. On a normal LAN this works as-is; if the server has a
  firewall, allow inbound UDP 8189 from the panel's network.

The MediaMTX and FFmpeg helpers are downloaded automatically when the plugin is
installed. No manual setup is required on Windows or Linux.

## Adding video streams

Streams are stored with the project. Each stream has:

| Field | Description |
|-------|-------------|
| Name | Friendly name shown in the UI |
| Stream ID | Short unique identifier (lowercase, no spaces) |
| Source URL | The stream address, e.g. `rtsp://192.168.1.50:554/stream1` |
| Username / Password | Source login, if the stream is protected |
| Transcode | Whether the source is re-encoded to H.264 (see Transcoding below) |
| Hardware acceleration | Which video chip to use when transcoding (see below) |

Credentials are sent to the source as part of the stream address; you do not
need to embed them in the URL yourself. Use **Test** when adding a stream to
confirm it is reachable and to see whether it needs transcoding.

## Showing a stream on a panel

In the UI Builder, add a **Video Stream** element to a page, then open its
properties and pick the stream from the **Stream** list. Other options:

| Option | Description |
|--------|-------------|
| Fit | `contain` shows the whole picture with letterboxing; `cover` fills the element and crops the edges |
| Show stream name overlay | Draws the stream's name along the bottom of the video |
| Auto-reconnect when tab regains focus | Reconnects after the panel has been in the background (on by default) |

The element shows a spinner while connecting and a Retry button if the stream
goes offline. Playback is muted and starts on its own.

## Transcoding and hardware acceleration

Browsers reliably play H.264 but not H.265 / HEVC, so the plugin re-encodes
non-H.264 sources to H.264 on the server before sending them to the panel. The
**Transcode** setting on each stream controls when this happens:

| Setting | Behavior |
|---------|----------|
| Auto (only when needed) | Default. Plays H.264 sources untouched; transcodes anything not confirmed to be H.264 (including H.265). |
| Always transcode to H.264 | Forces re-encoding even for H.264, useful if a camera sends a profile the browser rejects. |
| Never transcode | Sends the source through as-is. Only for an H.264 source on browsers you control. |

Transcoding adds latency and uses the host's CPU or GPU:

- With hardware acceleration: roughly 200 to 500 ms of added latency.
- Software only: roughly 500 to 1500 ms, and noticeably more CPU.

H.264 passthrough (no transcoding) is lowest latency, usually under half a second
on a wired LAN.

The **Hardware acceleration** setting picks how transcoding runs:

| Option | When to use |
|--------|-------------|
| Auto-detect | Default. Tests the encoders available on this machine and uses the best one that works, falling back to software. |
| Off (software) | Always use the CPU. Most compatible, most CPU-intensive. |
| Intel QuickSync | Intel CPUs with integrated graphics (x86). |
| NVIDIA NVENC | NVIDIA GPUs. |
| VA-API | Linux on a supported Intel or AMD GPU (x86). |
| V4L2 M2M (Raspberry Pi) | The Raspberry Pi hardware encoder. |

Auto-detect is right on nearly every system. Available encoders vary by platform:
QuickSync and VA-API are x86 only; Raspberry Pi uses V4L2 M2M; NVENC works wherever
a supported NVIDIA GPU is present. An option that is not available on the machine
is skipped automatically rather than failing.

**Raspberry Pi 4 and H.265:** A Pi 4 has no hardware H.265 decoder and is too slow
to decode H.265 in software (a few frames per second at 1080p). Use H.264 sources
on a Pi 4, or a Pi 5 or x86 host for H.265.

To confirm the panel is decoding on the GPU rather than the CPU, open
`chrome://media-internals` in Chrome or Edge on the panel while a stream plays and
check the decoder name. Hardware decode reads as `D3D11VideoDecoder` (Windows) or
`VaapiVideoDecoder` (Linux); `FFmpegVideoDecoder` means the browser is decoding in
software.

## State keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.video_panel.running` | boolean | Helper is up and responding |
| `plugin.video_panel.sidecar` | string | Helper process state: `starting`, `running`, `restarting`, `failed` |
| `plugin.video_panel.error` | string | Last fatal error message (empty when healthy) |
| `plugin.video_panel.stream_ids` | string | JSON list of `{value, label}` for configured streams |
| `plugin.video_panel.streams.<stream_id>` | string | Per-stream state: `idle` or `streaming` |

## Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.video_panel.error` | `{reason}` | The helper failed repeatedly and stopped restarting |

## Troubleshooting

- **Plugin shows Error on start:** The MediaMTX helper could not start. Check the
  System Log. If the binary is missing, reinstall the plugin so its components
  download again.
- **Stream not showing video:** Verify the source URL plays in a tool like VLC, and
  that the username and password are correct. Check
  `plugin.video_panel.streams.<stream_id>` in the State view.
- **Video works locally but not from another device:** Make sure UDP port 8189 is
  open between the panel and the server.
- **High CPU use:** An H.265 source is being re-encoded. Switch the source to
  H.264 if possible, or reduce its resolution / frame rate.

## Bundled components

This plugin downloads and runs two third-party programs. They are fetched at
install time and are not redistributed in this repository.

| Component | License | Purpose |
|-----------|---------|---------|
| MediaMTX | MIT | RTSP-to-WebRTC media server |
| FFmpeg (BtbN LGPL build) | LGPL-2.1 | H.265-to-H.264 transcoding |

The FFmpeg build is the LGPL variant (no GPL components) so it remains
compatible with this repository's MIT license.

## License

MIT
