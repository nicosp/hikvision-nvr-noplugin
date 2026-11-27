# Hikvision NVR live view without the plugin

Older Hikvision NVR web interfaces only show live video through Hikvision's
browser plugin or its Windows/macOS "Local Service". This project makes the
NVR's own web interface play live video in any modern browser, on any OS, with
nothing installed on the viewing machine.

You open the NVR's usual web UI through a small nginx site on a Linux box. The
UI believes its plugin is installed and drives it as usual: its own channel
list, window splits (1/4/9/16), start/stop, snapshot, recording and full
screen all work on the Live View page, and recordings play on the Playback
page (play, pause, stop, jump to a time, slow/fast forward from 1/4x to 4x
and single frame, with the timeline following along).

Tested with a DS-7604NI-K1(B) (web UI V4.0.1 build 201029), with Hikvision
"Video Encryption" turned on.

## How it works

```
browser ──https──▶ nginx ──▶ NVR web UI (unchanged, apart from a few small rewrites)
                     ├─────▶ localservice.py   stand-in for Hikvision's Local Service
                     └─────▶ go2rtc ◀── hikdecrypt.py ◀── NVR RTSP streams
```

- **go2rtc** ([AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc), in a Podman
  container) serves the NVR's RTSP streams to the browser (MSE over a
  WebSocket). It only pulls a camera from the NVR while someone watches it.
  Recordings play through 16 playback streams (`pb1`…`pb16`) that
  localservice.py hands out (see [Playback](#playback)).
- **hikdecrypt.py** undoes Hikvision's stream encryption, if it is on (see
  [Stream encryption](#stream-encryption)).
- **localservice.py** answers the Local Service protocol that the UI's player
  script speaks. The real service draws native video windows over the browser.
  Here, **web/nvr-ls.js**, which nginx adds to the NVR's pages, draws those
  windows inside the page and plays the matching go2rtc stream in each.
- **nginx** proxies the NVR and points the UI's player script at
  localservice.py instead of `127.0.0.1`. It also hides the "Download Plug-in"
  link.

## Requirements

- A Linux machine on the same network as the NVR, running systemd (written for
  Debian/Ubuntu)
- Podman 4.4 or newer (for Quadlet), nginx, Python 3, OpenSSL and curl:
  `sudo apt install podman nginx python3 openssl curl`
- A host name for the site (for example `nvr.example.lan`) pointing at this
  machine in your DNS
- A TLS certificate for that name from a CA your browsers trust (for example
  your own internal CA). The installer creates the key and the signing request.
- The NVR's admin login

## Setup

1. Configure:

   ```sh
   cp .env.example .env
   nano .env    # NVR_HOST, NVR_USER, NVR_PASS, PROXY_DOMAIN
   ```

2. Install go2rtc and the page script:

   ```sh
   sudo deploy/install.sh
   ```

   It asks the NVR for its cameras. Check <http://127.0.0.1:1984> on this
   machine: every camera has a `chN_main` and a `chN_sub` stream.

3. Install the nginx site and the Local Service stand-in:

   ```sh
   sudo deploy/install-nginx.sh
   ```

   The first run creates `/etc/ssl/private/<PROXY_DOMAIN>.key` and a
   certificate request next to it, prints how to sign it, and stops. Sign the
   request with your CA, which must keep the `subjectAltName`. Put the
   certificate (followed by any intermediate CA certificates) in
   `/etc/ssl/certs/<PROXY_DOMAIN>.pem`, then run the script again.

4. Open `https://<PROXY_DOMAIN>/`, log in as usual and go to Live View.

Run `sudo deploy/install.sh` again after changing `.env` or adding or removing
cameras, and both scripts again after updating this repository.

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `NVR_HOST`, `NVR_USER`, `NVR_PASS` | The NVR's address and login |
| `PROXY_DOMAIN` | The host name of the nginx site |
| `NVR_ENCRYPTED` | `1` (default): pass streams through hikdecrypt.py. Safe with encryption off too |
| `NVR_KEY` | The encryption key (verification code), if it isn't `NVR_PASS` |
| `NVR_CHANNELS` | Number of cameras, only used if auto-detection fails |
| `NVR_TRANSCODE` | `1`: also offer H.264 copies of the main streams, for browsers that can't play H.265. Costs CPU |

The UI's own Local settings (Configuration > Local) are stored on the server in
`/var/lib/nvr-localservice/localconfig.json`, shared by everyone using the
site. The defaults are sub stream and "start all live view" on.

## Security

- go2rtc has no login of its own. It listens on `127.0.0.1` only, and nginx
  lets a request through only if it carries a valid NVR session cookie (it
  asks the NVR with `/ISAPI/Security/userCheck`). Only the player files and the
  stream WebSocket are exposed, GET only. go2rtc's API (stream list, config,
  adding streams) is not reachable through nginx. The same check guards
  localservice.py. Logging out of the NVR cuts access.
- The NVR login is stored only in `.env` (git-ignored) and
  `/etc/go2rtc/go2rtc.env` (mode 600, readable by the `go2rtc` user only).
  `go2rtc.yaml` holds only `${VAR}` references.
- go2rtc runs as its own system user with no capabilities and a read-only root
  filesystem. localservice.py runs as a throwaway system user (`DynamicUser`)
  in a locked-down systemd sandbox; it doesn't store or pass on any login.
- nginx talks to the NVR over HTTPS without checking its self-signed
  certificate. That hop stays on your LAN.
- Through the proxy the NVR sees every client as this machine's address.
  Hikvision locks out an address after repeated failed logins, so one
  person's typos lock everyone out of the proxy for a while. The NVR's own
  address still works then.

## Limitations

- **Playback**: reverse playback is reported as failed to the UI. At most 16
  recordings can play at once, across all viewers. Recordings play without
  sound once the speed has been changed.
- **Two-way audio** and features that need the viewer's own disk (firmware
  upgrade, file upload/download, export) are reported as failed to the UI.
- **Drawing tools** on configuration pages (motion grid, regions) can't be
  edited. Saving those pages keeps what the NVR already has.
- The zero channel isn't available.
- Snapshots (JPEG) and recordings (MP4 or WebM) are saved through the
  browser's downloads; the paths in Local settings don't apply.
- **H.265**: most Hikvision cameras send H.265. Recent Chrome and Firefox play
  it with hardware decoding. If yours can't, set `NVR_TRANSCODE=1` or switch
  the camera to H.264.
- Video plays at exactly 1x with a small buffer (typically 0.1–0.3 s behind).
  go2rtc's player would otherwise keep shifting the playback rate around 1x
  (0.88x, 1.01x, ...) to hold about a second of buffer; the page script turns
  that off and jumps ahead instead if more than 2 s pile up. Browser add-ons
  that change the playback speed have no effect on these videos.

## Troubleshooting

- Logs: `journalctl -u go2rtc.service` and
  `journalctl -u nvr-localservice.service` (which window plays which camera).
- No video in Live View: check that the streams play at
  <http://127.0.0.1:1984> on the server. If they don't, check `NVR_HOST`, the
  login, and `NVR_KEY` if encryption is on.
- After updating, reload the NVR page with Ctrl+Shift+R.
- Each stream can also be opened directly, for example in VLC:
  `rtsp://NVR:554/Streaming/Channels/101` (camera 1 main), `…102` (sub).
  With encryption on, VLC shows garbled video.

## Stream encryption

With Hikvision "Video Encryption" on, the RTSP video can't be decoded as it is.
`hikdecrypt.py` undoes it. go2rtc starts one copy per stream that someone is
watching. Each copy pulls the stream from the NVR, decrypts the video in its
RTP packets and publishes it to go2rtc with its original timestamps. It adds
well under a millisecond of delay, and each stream costs about 1–2% of one CPU
core.

The scheme, worked out by analysing the streams:

- Only some NAL units are encrypted: parameter sets, key frames and about every
  fourth P frame. Their RTP packets carry a header extension; plain ones don't.
- Encryption is AES-128-ECB. The key is the device's verification code, padded
  with zero bytes to 16.
- It covers each encrypted NAL unit after its header: the first 4096 bytes at
  most, in whole 16-byte blocks.

To test it without go2rtc, run `hikdecrypt.py 102 --seconds 5 > x.h265`, with
`NVR_HOST`, `NVR_USER_URL` and `NVR_PASS_URL` (URL-encoded) in the environment.

## Playback

When the Playback page plays a recording, localservice.py gives the window a
free playback stream (`pbN`). The page script plays that go2rtc stream, and
go2rtc starts `hikdecrypt.py --slot N`, which asks localservice.py
(`GET /slot/N`, on localhost only) which recording to play and pulls it from
the NVR (`/Streaming/tracks/<channel>01?starttime=…`). The NVR login stays
in go2rtc.

The NVR skips gaps between recordings, so the timeline can't simply count
seconds from the start. Hikvision puts the recording time in a private RTP
header extension, and hikdecrypt.py reports it to localservice.py (UDP,
localhost), which answers the UI's `getOSDTime` with it. Pause stops the
stream and keeps the last frame on screen; resume and jumping to a time start
a new stream from that time.

The replies to those UDP reports carry what the stream should do, and
hikdecrypt.py applies it to the NVR session:

- **Speed** (1/4x to 4x): `PLAY` with `Scale`. The NVR then sends every frame,
  faster or slower. The browser always gets the normal frame rate: when
  slower, hikdecrypt.py stretches the timestamps; when faster, it passes on
  only the first 1/speed of each keyframe group (2 s of video), so fast
  forward plays short stretches at normal pace and jumps ahead, and the
  browser never has to decode more than at normal speed.
- **Single frame**: the NVR is paused (`PAUSE`), hikdecrypt.py keeps a few
  frames in hand and releases one per step, and the page pauses the video and
  moves it on one frame. Play goes on from there.

Recordings may carry G.722.1 audio, which browsers can't play. It is left out.

## The Local Service emulation

The UI's player script (`jsVideoPlugin-1.0.0.min.js`) probes
`http://127.0.0.1:<33686-33695>/imghttp/local` and then talks JSON over
`ws://127.0.0.1:<port>`. nginx rewrites three spots in that script, so that:

- the probe and the WebSocket go to this site (`/nvr-ls/…`) instead, and
- every browser uses the WebSocket (Firefox would otherwise poll with images).

localservice.py speaks the protocol: requests `{sequence, cmd, …}` are answered
with `{sequence, errorCode, …}`, and window events are sent as
`{cmd: "window.onGetSelectWndInfo", …}`. The commands were taken from the UI's
own player script. The page script joins the same "page" (a random id shared by
both) and gets the window layout and what each window plays.

Streams are only fetched when needed: a window has a player only while it plays
and is visible in the current split, players stop while the browser tab is
hidden, and go2rtc only pulls a camera while someone watches it.

## Removing it

```sh
sudo rm /etc/nginx/sites-enabled/<PROXY_DOMAIN> && sudo systemctl reload nginx
sudo systemctl disable --now nvr-localservice.service
sudo rm /etc/systemd/system/nvr-localservice.service
sudo systemctl stop go2rtc.service
sudo rm /etc/containers/systemd/go2rtc.container
sudo systemctl daemon-reload
sudo rm -r /etc/go2rtc /usr/local/share/nvr-live /usr/local/lib/nvr-live
sudo userdel go2rtc
sudo groupdel go2rtc   # if userdel didn't already remove it
```

To generate the go2rtc config without installing anything:
`./setup.py --out build`.

## License

MIT, see [LICENSE](LICENSE). go2rtc is a separate project with its own license;
this repository only runs its container image.
