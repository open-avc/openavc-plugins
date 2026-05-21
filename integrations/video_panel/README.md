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
