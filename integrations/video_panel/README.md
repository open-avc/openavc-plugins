# Video Panel

Show live IP camera streams on the OpenAVC touch panel. The plugin bundles
[MediaMTX](https://github.com/bluenviron/mediamtx) as a local helper that pulls
each camera's RTSP feed and republishes it as WebRTC, which any modern browser
can play in a plain `<video>` element. The helper listens on localhost only;
the panel reaches camera video through the OpenAVC server, so it is covered by
the same login as the rest of the system.

## Requirements

- One or more IP cameras that provide an RTSP stream.
- For the widest browser support, set cameras to **H.264 (Constrained Baseline)**.
  H.265 / HEVC cameras are re-encoded to H.264 automatically (this uses more CPU).
- **Network:** WebRTC video travels over **UDP port 8189** directly between the
  browser and the server. On a normal LAN this works as-is; if the server has a
  firewall, allow inbound UDP 8189 from the panel's network.

The MediaMTX and FFmpeg helpers are downloaded automatically when the plugin is
installed. No manual setup is required on Windows or Linux.

## Adding cameras

Cameras are stored with the project. Each camera has:

| Field | Description |
|-------|-------------|
| Name | Friendly name shown in the UI |
| Stream ID | Short unique identifier (lowercase, no spaces) |
| RTSP URL | The camera's stream address, e.g. `rtsp://192.168.1.50:554/stream1` |
| Username / Password | Camera login, if the stream is protected |

Credentials are sent to the camera as part of the stream address; you do not
need to embed them in the URL yourself.

## State keys

| Key | Type | Description |
|-----|------|-------------|
| `plugin.video_panel.running` | boolean | Helper is up and responding |
| `plugin.video_panel.sidecar` | string | Helper process state: `starting`, `running`, `restarting`, `failed` |
| `plugin.video_panel.error` | string | Last fatal error message (empty when healthy) |
| `plugin.video_panel.stream_ids` | string | JSON list of `{value, label}` for configured cameras |
| `plugin.video_panel.streams.<stream_id>` | string | Per-camera state: `idle` or `streaming` |

## Events

| Event | Payload | Description |
|-------|---------|-------------|
| `plugin.video_panel.error` | `{reason}` | The helper failed repeatedly and stopped restarting |

## Troubleshooting

- **Plugin shows Error on start:** The MediaMTX helper could not start. Check the
  System Log. If the binary is missing, reinstall the plugin so its components
  download again.
- **Camera not showing video:** Verify the RTSP URL plays in a tool like VLC, and
  that the camera username and password are correct. Check
  `plugin.video_panel.streams.<stream_id>` in the State view.
- **Video works locally but not from another device:** Make sure UDP port 8189 is
  open between the panel and the server.
- **High CPU use:** An H.265 camera is being re-encoded. Switch the camera to
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
