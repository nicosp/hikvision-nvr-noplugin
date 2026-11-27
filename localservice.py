#!/usr/bin/env python3
"""Server-side stand-in for Hikvision's "Local Service" (LocalServiceComponents).

The NVR web UI only plays video through that native Windows/macOS helper: its
player script (jsVideoPlugin) talks JSON over ws://127.0.0.1:33686-33695 and the
helper draws video in a native window over the browser. nginx rewrites the
script so it talks to this service instead (wss://<site>/nvr-ls/ws), and this
service answers the protocol like the real helper does.

Video can't be drawn from the server, so the "windows" are rendered in the page
by web/nvr-ls.js. It connects to /nvr-ls/bridge with the same page id and gets
the window layout and what each window plays; it then plays the matching go2rtc
stream in that spot. Window clicks go back to the UI as the native events.

Only what a window shows is streamed: a window that isn't playing or isn't
visible in the current split has no player, and go2rtc only pulls a camera
while someone watches it.

Recordings play through go2rtc's playback streams pb1..pbN: a playback window
gets a free slot, and the hikdecrypt.py that go2rtc starts for that stream asks
this service what to play (GET /slot/N, localhost only) and reports the
recording time back (UDP, same port), which answers the UI's getOSDTime. Pause
and resume restart the stream from where it was.

Commands that need the viewer's local disk (upgrade, upload, file download,
browse) or two-way audio fail with an error, as they would if the helper
couldn't do them. Drawing settings (motion grid, regions, lines) are echoed
back unchanged, so saving a config page keeps what the NVR had.

usage: localservice.py [--listen 127.0.0.1:1985] [--state DIR]
Standard library only.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import struct
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

VERSION = "1.9.9.999"  # newer than any version the UI asks to upgrade from
MAX_MESSAGE = 1 << 20
MAX_PAGES = 200
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PAGE_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")
FAIL = 1  # generic error code for commands that can't be done here
PB_SLOTS = 16  # go2rtc playback streams pb1..pb16 (setup.py), shared by all viewers
PB_LAG = 1.0   # seconds the viewer is behind the time hikdecrypt.py reports
TZ_NAME = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")

log = logging.getLogger("localservice")

# Local settings ("Configuration > Local"), shared by everyone using this site.
DEFAULT_CONFIG = {
    "protocolType": "0", "streamType": "1", "buffNumberType": "1",
    "playWndType": "0", "ivsMode": "0", "captureFileFormat": "0",
    "packgeSize": "1", "realPlayAll": "1", "secretKey": "", "osdPosInfo": "0",
    "recordPath": "Downloads", "downloadPath": "Downloads",
    "capturePath": "Downloads", "playbackPicPath": "Downloads",
    "playbackFilePath": "Downloads",
}

# setXxx commands whose payload is stored and returned by the matching getXxx.
ECHO = {
    "video.setGridInfo": ("video.getGridInfo", "gridInfo"),
    "video.setRectInfo": ("video.getRectInfo", "rectInfo"),
    "video.setPolygonInfo": ("video.getPolygonInfo", "polygonInfo"),
    "video.setLineInfo": ("video.getLineInfo", "lineInfo"),
    "video.setPointInfo": ("video.getPointInfo", "pointInfo"),
    "video.setTextOverlay": ("video.getTextOverlay", "overlayInfo"),
}
ECHO_DEFAULT = {"gridInfo": {}, "rectInfo": [], "polygonInfo": [], "lineInfo": [],
                "pointInfo": [], "overlayInfo": {}, "fishBoxList": []}

# Need the viewer's file system, or audio from the microphone: not possible here.
UNSUPPORTED = {
    "video.browseFilePath", "video.openDirectory", "video.startAsynUpload",
    "video.uploadFile", "video.downloadFile", "video.startAsyncDownload",
    "video.startUpgrade", "video.exportDeviceLog", "video.exportReport",
    "video.startTalk", "video.reversePlay",
    "video.fast", "video.slow", "video.frame",  # playback runs at normal speed only
}


class WebSocket:
    """Server side of RFC 6455, text messages only."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.closed = False
        self.lock = asyncio.Lock()

    async def recv(self):
        """Next text message, or None once the connection is closed."""
        parts = []
        while True:
            try:
                b0, b1 = await self.reader.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    (n,) = struct.unpack("!H", await self.reader.readexactly(2))
                elif n == 127:
                    (n,) = struct.unpack("!Q", await self.reader.readexactly(8))
                if n > MAX_MESSAGE or not b1 & 0x80:  # clients must mask
                    await self.close(1009 if n > MAX_MESSAGE else 1002)
                    return None
                mask = await self.reader.readexactly(4)
                data = await self.reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                self.closed = True
                return None
            data = (int.from_bytes(data, "little") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "little")).to_bytes(n, "little")
            op = b0 & 0x0F
            if op == 0x8:
                await self.close()
                return None
            if op == 0x9:
                await self._frame(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if sum(map(len, parts)) > MAX_MESSAGE:
                await self.close(1009)
                return None
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    async def send(self, obj):
        await self._frame(0x1, json.dumps(obj, separators=(",", ":")).encode())

    async def _frame(self, op, data):
        if self.closed:
            return
        n = len(data)
        head = bytes([0x80 | op])
        head += bytes([n]) if n < 126 else (struct.pack("!BH", 126, n) if n < 65536 else struct.pack("!BQ", 127, n))
        async with self.lock:
            try:
                self.writer.write(head + data)
                await self.writer.drain()
            except ConnectionError:
                self.closed = True

    async def close(self, code=1000):
        if not self.closed:
            await self._frame(0x8, struct.pack("!H", code))
            self.closed = True
        self.writer.close()


class Page:
    """One browser page: its plugin connection(s), bridge(s) and window state."""

    def __init__(self, pid):
        self.pid = pid
        self.plugins, self.bridges = set(), set()
        self.tz = None          # the browser's time zone (IANA name), from the page script
        self.ipchanbase = 0     # first channel number of the NVR's SDK numbering
        self.reset()

    def reset(self):
        self.created = False
        self.split = 1
        self.selected = 0
        self.wnds = {}          # wndIndex -> {"ch", "kind"[, "pb" or "paused"]} or {"zero": True}
        self.sound = -1         # window with sound on
        self.volume = 50
        self.echo = {}
        self.events = []        # events for the plugin, sent after the current reply

    def state(self):
        return {"type": "state", "created": self.created, "split": self.split,
                "selected": self.selected, "sound": self.sound, "volume": self.volume,
                "wnds": {str(k): v for k, v in self.wnds.items()}}

    async def to_bridges(self, msg):
        for b in list(self.bridges):
            await b.send(msg)

    async def push(self):
        await self.to_bridges(self.state())

    async def to_plugins(self, msg):
        for p in list(self.plugins):
            await p.send(msg)


def parse_time(value):
    """The UI's recording times (20260101T120000Z or 2026-01-01T12:00:00Z, NVR
    clock): normalised to 20260101T120000Z, or None."""
    t = str(value or "").replace("-", "").replace(":", "")
    return t if re.fullmatch(r"\d{8}T\d{6}Z", t) else None


def parse_recording_url(url):
    """(channel, stream type) of a recording URL. The UI sends
    /SDK/playback/<id - 1> ("TCP" transport, like live view),
    /ISAPI/streaming/tracks/<id> or .../<id>[/transcoding], id = channel * 100 + type."""
    path = urlsplit(url).path
    m = re.search(r"/SDK/playback/(\d+)", path)
    if m:
        sid = int(m.group(1)) + 1
    else:
        ids = re.findall(r"/(\d{3,5})(?=/|$)", path)
        if not ids:
            return None
        sid = int(ids[-1])
    ch, t = divmod(sid, 100)
    return (ch, t) if ch >= 1 and 1 <= t <= 9 else None


def parse_play_url(url):
    """Stream the UI asked for: {"ch", "kind"}, {"zero"}, {"playback"} or None."""
    path = urlsplit(url).path
    if "/SDK/playback/" in path or "/ISAPI/streaming/tracks/" in path:
        return {"playback": True}
    if "ZeroStreaming" in path:
        return {"zero": True}
    # "TCP" transport: /SDK/play/<id - 1>/004, where the channel part of id is
    # numbered from the NVR's ipChanBase (the page script maps it back).
    # RTSP: /ISAPI/streaming/channels/<id>, numbered like the channel list.
    sdk = re.search(r"/SDK/play/(\d+)/", path)
    m = re.search(r"/ISAPI/streaming/channels/(\d+)", path)
    sid = int(m.group(1)) if m else int(sdk.group(1)) + 1 if sdk else 0
    if sid < 101:
        return None
    ch, t = divmod(sid, 100)
    target = {"ch": ch, "kind": "main" if t == 1 else "sub"}
    if sdk and not m:
        target["sdk"] = True
    return target


class Service:
    def __init__(self, state_dir):
        self.pages = {}
        self.slots = {}         # playback slot -> {"track", "start", "end", "gen", "time", "mono", "page", "wnd"}
        self.free = deque(range(1, PB_SLOTS + 1))
        self.gen = 0
        self.cfg_file = Path(state_dir) / "localconfig.json" if state_dir else None
        self.config = dict(DEFAULT_CONFIG)
        if self.cfg_file and self.cfg_file.exists():
            try:
                self.config.update(json.loads(self.cfg_file.read_text()))
            except (OSError, ValueError) as e:
                log.warning("ignoring %s: %s", self.cfg_file, e)

    def save_config(self, new):
        if not isinstance(new, dict):
            raise ValueError
        for k, v in new.items():
            if isinstance(k, str) and len(k) < 64 and isinstance(v, (str, int, float, bool)) and len(str(v)) < 1024:
                self.config[k] = v
        if self.cfg_file:
            tmp = self.cfg_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.config, indent=1))
            os.replace(tmp, self.cfg_file)

    def page(self, pid):
        p = self.pages.get(pid)
        if p is None:
            if len(self.pages) >= MAX_PAGES:
                return None
            p = self.pages[pid] = Page(pid)
        return p

    # --- playback slots -------------------------------------------------------------

    def take_slot(self, page, wnd, track, start, end):
        if not self.free:
            log.warning("page %s: all %d playback streams are in use", page.pid, PB_SLOTS)
            raise Failed(FAIL)
        n = self.free.popleft()
        self.gen += 1
        self.slots[n] = {"track": track, "start": start, "end": end, "gen": self.gen,
                         "time": None, "mono": None, "page": page, "wnd": wnd}
        return n

    def drop_slot(self, n):
        if self.slots.pop(n, None) is not None:
            self.free.append(n)  # reused last, so go2rtc has stopped the old one by then

    def clear_wnd(self, page, wnd):
        w = page.wnds.pop(wnd, None)
        if w and "pb" in w:
            self.drop_slot(w["pb"])

    def clear_all(self, page):
        for wnd in list(page.wnds):
            self.clear_wnd(page, wnd)

    @staticmethod
    def wall_time(slot):
        """What the viewer sees now (NVR clock, naive datetime)."""
        if slot["time"]:
            seen = datetime.strptime(slot["time"], "%Y%m%dT%H%M%S")
            return seen + timedelta(seconds=time.monotonic() - slot["mono"] - PB_LAG)
        return datetime.strptime(slot["start"], "%Y%m%dT%H%M%SZ")

    @staticmethod
    def epoch(page, wall):
        """The UI shows osdTime as a date in the browser's time zone."""
        if page.tz:
            try:
                return int(wall.replace(tzinfo=ZoneInfo(page.tz)).timestamp())
            except (KeyError, ValueError, OSError):
                pass
        return int(time.mktime(wall.timetuple()))

    def slot_job(self, n):
        slot = self.slots.get(n)
        if slot is None:
            return None
        # If go2rtc restarts the stream (viewer reconnected), go on from where it was.
        start = f"{slot['time']}Z" if slot["time"] else slot["start"]
        if slot["end"] and start >= slot["end"]:
            return None
        return {"track": slot["track"], "start": start, "end": slot["end"], "gen": slot["gen"]}

    def time_report(self, data):
        """UDP from hikdecrypt.py: "nvrls-time <slot> <gen> <YYYYMMDDTHHMMSS>"."""
        try:
            tag, n, gen, t = data.decode("ascii").split()
            slot = self.slots.get(int(n))
        except (UnicodeDecodeError, ValueError):
            return
        if tag == "nvrls-time" and slot and slot["gen"] == int(gen) and re.fullmatch(r"\d{8}T\d{6}", t):
            slot["time"], slot["mono"] = t, time.monotonic()

    def release(self, page):
        if not page.plugins and not page.bridges:
            self.pages.pop(page.pid, None)

    # --- the Local Service protocol -------------------------------------------------

    async def plugin(self, ws, page):
        conn_uuid = str(uuid.uuid4())
        page.plugins.add(ws)
        log.info("page %s: plugin connected", page.pid)
        try:
            while (text := await ws.recv()) is not None:
                try:
                    msg = json.loads(text)
                    if not isinstance(msg, dict):
                        continue
                except ValueError:
                    continue
                cmd = str(msg.get("cmd", ""))
                if cmd == "system.webconnect":
                    await ws.send({"uuid": conn_uuid, "version": VERSION})
                    continue
                try:
                    extra = await self.command(page, cmd, msg)
                    reply = {"errorCode": 0, **(extra or {})}
                except Failed as e:
                    reply = {"errorCode": e.code}
                except (ValueError, TypeError):
                    reply = {"errorCode": FAIL}
                reply["sequence"] = msg.get("sequence")
                await ws.send(reply)
                events, page.events = page.events, []
                for ev in events:
                    await page.to_plugins(ev)
        finally:
            page.plugins.discard(ws)
            if not page.plugins:  # page closed or navigated: nothing plays any more
                self.clear_all(page)
                page.reset()
                await page.push()
            log.info("page %s: plugin disconnected", page.pid)
            self.release(page)

    async def command(self, page, cmd, m):
        wnd = int(m.get("wndIndex") or 0)
        if cmd == "window.createWnd":
            page.created = True
            await page.push()
        elif cmd == "window.destroyWnd":
            page.created = False
            self.clear_all(page)
            await page.push()
        elif cmd == "window.getLocalServiceVersion":
            return {"localServiceVersion": VERSION}
        elif cmd == "video.arrangeWindow":
            page.split = max(1, min(4, int(m.get("type") or 1)))
            page.selected = 0
            await page.push()
            page.events.append({"cmd": "window.onGetSelectWndInfo", "wndInfo": {"wndIndex": 0}})
        elif cmd == "video.startPlay" and parse_time(m.get("startTime")):
            await self.start_playback(page, wnd, m)
        elif cmd == "video.startPlay":
            target = parse_play_url(str(m.get("url", "")))
            if target is None or target.get("playback"):
                raise Failed(FAIL)
            self.clear_wnd(page, wnd)
            page.wnds[wnd] = target
            log.info("page %s: window %d plays %s", page.pid, wnd,
                     "zero channel" if target.get("zero") else
                     f"channel {target['ch']}{' (SDK numbering)' if target.get('sdk') else ''} {target['kind']}")
            await page.push()
        elif cmd == "video.stop":
            self.clear_wnd(page, wnd)
            if page.sound == wnd:
                page.sound = -1
            await page.push()
        elif cmd == "video.stopAll":
            self.clear_all(page)
            page.sound = -1
            await page.push()
        elif cmd == "video.openSound":
            page.sound = wnd
            await page.push()
        elif cmd == "video.closeSound":
            page.sound = -1
            await page.push()
        elif cmd == "video.getVolume":
            return {"volume": page.volume}
        elif cmd == "video.setVolume":
            page.volume = max(0, min(100, int(m.get("volume") or 0)))
            await page.push()
        elif cmd == "video.capture":
            await page.to_bridges({"type": "capture", "wnd": wnd, "fileName": str(m.get("fileName", ""))[:200]})
        elif cmd == "video.startSave":
            await page.to_bridges({"type": "record", "wnd": wnd, "on": True, "fileName": str(m.get("fileName", ""))[:200]})
        elif cmd == "video.stopSave":
            await page.to_bridges({"type": "record", "wnd": wnd, "on": False})
        elif cmd == "video.fullScreen":
            await page.to_bridges({"type": "fullscreen", "full": bool(m.get("full"))})
        elif cmd == "video.enableZoom":
            await page.to_bridges({"type": "zoom", "wnd": wnd, "on": True})
        elif cmd == "video.disableZoom":
            await page.to_bridges({"type": "zoom", "wnd": wnd, "on": False})
        elif cmd == "video.getLocalConfig":
            return {"localConfig": dict(self.config)}
        elif cmd == "video.setLocalConfig":
            self.save_config(m.get("localConfig"))
        elif cmd == "video.getEncryptString":
            return {"encryptString": m.get("originalKey", "")}
        elif cmd == "video.getDecryptString":
            return {"decryptString": m.get("encrypted", "")}
        elif cmd == "video.getOSDTime":
            w = page.wnds.get(wnd) or {}
            if "pb" in w:
                return {"osdTime": self.epoch(page, self.wall_time(self.slots[w["pb"]]))}
            if w.get("paused"):
                return {"osdTime": self.epoch(page, datetime.strptime(w["at"], "%Y%m%dT%H%M%SZ"))}
            return {"osdTime": int(time.time())}
        elif cmd == "video.pause":
            w = page.wnds.get(wnd) or {}
            if "pb" not in w:
                raise Failed(FAIL)
            slot = self.slots[w["pb"]]
            at = self.wall_time(slot).strftime("%Y%m%dT%H%M%SZ")
            self.drop_slot(w.pop("pb"))
            w.update(paused=True, at=at, track=slot["track"], end=slot["end"])
            log.info("page %s: window %d paused at %s", page.pid, wnd, at)
            await page.push()
        elif cmd == "video.resume":
            w = page.wnds.get(wnd) or {}
            if not w.get("paused"):
                raise Failed(FAIL)
            w["pb"] = self.take_slot(page, wnd, w["track"], w["at"], w["end"])
            for k in ("paused", "at", "track", "end"):
                w.pop(k, None)
            log.info("page %s: window %d resumed (stream pb%d)", page.pid, wnd, w["pb"])
            await page.push()
        elif cmd == "video.getPlayInfo":
            return {"playInfo": {"playInfo": {}}}
        elif cmd == "video.getLastError":
            return {"lastError": 0}
        elif cmd == "video.getEngineSupport":
            return {"engineSupport": []}
        elif cmd in ECHO:
            key = ECHO[cmd][1]
            page.echo[key] = m.get(key)
        elif cmd in {g for g, _ in ECHO.values()} or cmd == "video.getFishBoxListInfo":
            key = next((k for g, k in ECHO.values() if g == cmd), "fishBoxList")
            val = page.echo.get(key)
            return {key: ECHO_DEFAULT[key] if val is None else val}
        elif cmd in UNSUPPORTED:
            log.info("page %s: %s is not supported", page.pid, cmd)
            raise Failed(FAIL)
        # Everything else (window geometry, covers, z-order, draw modes, fisheye,
        # play mode, https, reconnect info, secret keys, ...) has no effect here.
        return None

    async def start_playback(self, page, wnd, m):
        rec = parse_recording_url(str(m.get("url", "")))
        if rec is None:
            log.info("page %s: can't play recording URL %s", page.pid, urlsplit(str(m.get("url", ""))).path[:100])
            raise Failed(FAIL)
        ch, t = rec
        if page.ipchanbase > 1 and ch >= page.ipchanbase:  # SDK numbering
            ch = ch - page.ipchanbase + 1
        start, end = parse_time(m.get("startTime")), parse_time(m.get("stopTime"))
        self.clear_wnd(page, wnd)
        n = self.take_slot(page, wnd, ch * 100 + t, start, end)
        page.wnds[wnd] = {"ch": ch, "kind": "main" if t == 1 else "sub", "pb": n}
        log.info("page %s: window %d plays the recording of channel %d from %s (stream pb%d)",
                 page.pid, wnd, ch, start, n)
        await page.push()

    # --- the page script ------------------------------------------------------------

    async def bridge(self, ws, page):
        page.bridges.add(ws)
        try:
            await ws.send(page.state())
            while (text := await ws.recv()) is not None:
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "hello":
                    tz, base = msg.get("tz"), msg.get("ipChanBase")
                    if isinstance(tz, str) and TZ_NAME.match(tz):
                        page.tz = tz
                    if isinstance(base, int) and 0 <= base < 10000:
                        page.ipchanbase = base
                elif isinstance(msg, dict) and msg.get("type") == "select":
                    wnd = int(msg.get("wnd") or 0)
                    if 0 <= wnd < 16:
                        page.selected = wnd
                        await page.to_plugins({"cmd": "window.onGetSelectWndInfo", "wndInfo": {"wndIndex": wnd}})
                        await page.push()
        finally:
            page.bridges.discard(ws)
            self.release(page)

    # --- HTTP / WebSocket upgrade -----------------------------------------------------

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        url = urlsplit(target)
        slot = re.fullmatch(r"/slot/(\d{1,3})", url.path)
        if method == "GET" and slot and "upgrade" not in headers:  # hikdecrypt.py asking what to play
            job = self.slot_job(int(slot.group(1)))
            body = json.dumps(job).encode() if job else b""
            status = b"200 OK" if job else b"404 Not Found"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            return
        pid = (parse_qs(url.query).get("page") or [""])[0]
        key = headers.get("sec-websocket-key", "")
        if (method != "GET" or url.path not in ("/plugin", "/bridge") or not PAGE_ID.match(pid)
                or headers.get("upgrade", "").lower() != "websocket" or not key):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        page = self.page(pid)
        if page is None:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        keepalive = asyncio.create_task(self.keepalive(ws))
        try:
            if url.path == "/plugin":
                await self.plugin(ws, page)
            else:
                await self.bridge(ws, page)
        except Exception:  # noqa: BLE001 - one broken connection must not stop the service
            log.exception("page %s: connection failed", pid)
        finally:
            keepalive.cancel()
            if not ws.closed:
                await ws.close()

    @staticmethod
    async def keepalive(ws):
        """Pings idle connections, so proxies don't time them out."""
        while not ws.closed:
            await asyncio.sleep(30)
            await ws._frame(0x9, b"")  # noqa: SLF001


class Failed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", default="127.0.0.1:1985")
    ap.add_argument("--state", default=os.environ.get("STATE_DIRECTORY"),
                    help="directory for the local settings (default: $STATE_DIRECTORY)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host, port = args.listen.rsplit(":", 1)
    svc = Service(args.state)
    server = await asyncio.start_server(svc.handle, host, int(port), limit=16384)

    class TimeReports(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            svc.time_report(data)

    await asyncio.get_running_loop().create_datagram_endpoint(TimeReports, local_addr=(host, int(port)))
    log.info("listening on %s (version %s)", args.listen, VERSION)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
