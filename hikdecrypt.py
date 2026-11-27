#!/usr/bin/env python3
"""Decrypt a Hikvision "stream encryption" (Video Encryption) stream for go2rtc.

Pulls a channel from the NVR over RTSP (TCP), decrypts the video in place in its
RTP packets and publishes the stream (video and audio, original timestamps) to
an RTSP server: go2rtc runs it as

    exec:python3 -I /config/hikdecrypt.py 101 {output}

Without an output URL it writes the decrypted video as a raw Annex-B bitstream
to stdout instead, for testing:  hikdecrypt.py 102 --seconds 5 | ffplay -

Recordings: --start/--end play /Streaming/tracks/<channel> from the NVR. With
--slot N (go2rtc's playback streams pb1, pb2, ...) it asks localservice.py
what to play, and reports the recording time back to it: Hikvision puts the
absolute time in a private RTP header extension on key frames. localservice.py
also sets the speed (RTSP Scale). The NVR sends every frame at that speed and
the timestamps are scaled to match, so the browser plays it faster or slower
(at 4x it decodes 4 times the frame rate: every P frame needs the one before).
Single-frame stepping: the NVR is paused and frames are released one at a
time.

The scheme, worked out from a DS-7604NI-K1(B)'s streams:
  - only NAL units whose RTP packets carry a header extension are encrypted
    (parameter sets, key frames and about every fourth P frame);
  - AES-128-ECB, key = the device's verification code (NVR_KEY, default:
    the NVR password) padded with zero bytes to 16;
  - applied to the NAL unit after its header, to the first 4096 bytes at most,
    in whole 16-byte blocks; a shorter tail is not encrypted.
Unmarked packets pass through, so it also works with encryption turned off.

env: NVR_HOST, NVR_USER_URL, NVR_PASS_URL (URL-encoded, as in go2rtc.env)
     NVR_KEY_URL  optional, if the encryption key isn't the password
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import os
import re
import select
import socket
import sys
import time
import json
from collections import deque
import urllib.parse
import urllib.request

KEEPALIVE = 25          # seconds between keepalives to the NVR
ENCRYPTED_BYTES = 4096  # at most this much of a NAL unit is encrypted
AUDIO_CODECS = {"PCMA", "PCMU", "MPEG4-GENERIC", "OPUS", "L16"}  # audio go2rtc can serve


class AES128ECB:
    """AES-128-ECB decryption (no padding) via the system's libcrypto."""

    def __init__(self, key: bytes):
        lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_aes_128_ecb.restype = ctypes.c_void_p
        lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p] * 5
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.EVP_DecryptUpdate.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p, ctypes.c_int]
        self.lib = lib
        self.ctx = lib.EVP_CIPHER_CTX_new()
        self._key = ctypes.create_string_buffer(key, 16)
        if not lib.EVP_DecryptInit_ex(self.ctx, lib.EVP_aes_128_ecb(), None, self._key, None):
            raise RuntimeError("EVP_DecryptInit_ex failed")
        lib.EVP_CIPHER_CTX_set_padding(self.ctx, 0)
        self._out = ctypes.create_string_buffer(ENCRYPTED_BYTES + 16)
        self._n = ctypes.c_int()

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt data, a multiple of 16 bytes, at most ENCRYPTED_BYTES long."""
        self.lib.EVP_DecryptUpdate(self.ctx, self._out, ctypes.byref(self._n), data, len(data))
        return self._out.raw[:self._n.value]


class RTSPConn:
    """One RTSP/TCP connection: requests with digest auth, interleaved frames."""

    def __init__(self, host, port, user=None, password=None):
        self.sock = socket.create_connection((host, port), 10)
        self.sock.settimeout(30)
        self.user, self.password = user, password
        self.buf = b""
        self.cseq = 0
        self.auth = None
        self.session = None

    def _read(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(262144)
            if not d:
                raise EOFError("connection closed")
            self.buf += d

    def read_response(self):
        while b"\r\n\r\n" not in self.buf:
            self._read(len(self.buf) + 1)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        head = head.decode(errors="replace")
        m = re.search(r"^Content-Length:\s*(\d+)", head, re.I | re.M)
        n = int(m.group(1)) if m else 0
        self._read(n)
        body, self.buf = self.buf[:n], self.buf[n:]
        return head, body.decode(errors="replace")

    def send(self, method, uri, extra="", body=""):
        self.cseq += 1
        h = f"{method} {uri} RTSP/1.0\r\nCSeq: {self.cseq}\r\nUser-Agent: hikdecrypt\r\n"
        if self.auth:
            realm, nonce = self.auth
            ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            h += (f'Authorization: Digest username="{self.user}", realm="{realm}", '
                  f'nonce="{nonce}", uri="{uri}", response="{resp}"\r\n')
        if self.session:
            h += f"Session: {self.session}\r\n"
        if body:
            h += f"Content-Length: {len(body.encode())}\r\n"
        self.sock.sendall((h + extra + "\r\n" + body).encode())

    def request(self, method, uri, extra="", body=""):
        for _ in range(2):
            self.send(method, uri, extra, body)
            head, resp_body = self.read_response()
            status = head.split("\r\n", 1)[0]
            if " 401 " in status and self.auth is None and self.user is not None:
                m = re.search(r'Digest\s+realm="([^"]+)",\s*nonce="([^"]+)"', head)
                if not m:
                    raise RuntimeError("no digest auth offered")
                self.auth = (m.group(1), m.group(2))
                continue
            if " 200 " not in status:
                raise RuntimeError(f"{method}: {status}")
            m = re.search(r"^Session:\s*([^;\r\n]+)", head, re.I | re.M)
            if m:
                self.session = m.group(1).strip()
            return head, resp_body
        raise RuntimeError("authentication failed (check NVR_USER/NVR_PASS)")

    def _take(self):
        """The next interleaved (channel, packet) in the buffer, or None if
        it isn't complete yet. RTSP replies in between are skipped."""
        while self.buf:
            if self.buf[0] == 0x24:
                if len(self.buf) < 4:
                    return None
                n = int.from_bytes(self.buf[2:4], "big")
                if len(self.buf) < 4 + n:
                    return None
                ch, pkt, self.buf = self.buf[1], self.buf[4:4 + n], self.buf[4 + n:]
                return ch, pkt
            if self.buf.startswith(b"RTSP/"):
                end = self.buf.find(b"\r\n\r\n")
                if end < 0:
                    return None
                m = re.search(rb"Content-Length:\s*(\d+)", self.buf[:end], re.I)
                total = end + 4 + (int(m.group(1)) if m else 0)
                if len(self.buf) < total:
                    return None
                self.buf = self.buf[total:]
                continue
            if len(self.buf) < 5 and b"RTSP/".startswith(self.buf):
                return None
            self.buf = self.buf[1:]
        return None

    def next_frame(self, timeout=None):
        """(channel, packet), or None if none is complete within timeout seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while (item := self._take()) is None:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                    return None
            d = self.sock.recv(262144)
            if not d:
                raise EOFError("connection closed")
            self.buf += d
        return item

    def frames(self):
        """Yield (channel, packet) for interleaved data; skip RTSP replies."""
        while True:
            yield self.next_frame()


class Control:
    """UDP link to localservice.py for one playback slot: recording times and
    heartbeats go out, the wanted state comes back ("nvrls-state <gen> <speed>
    <step>", step = frames asked for since single-frame mode began, or -1)."""

    def __init__(self, service, slot, gen, speed):
        host, port = service.rsplit(":", 1)
        self.addr = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.slot, self.gen = slot, gen
        self.speed, self.step = speed, -1
        self.last_hello = 0.0

    def _send(self, msg):
        try:
            self.sock.sendto(msg.encode(), self.addr)
        except OSError:
            pass

    def report(self, t):
        self._send(f"nvrls-time {self.slot} {self.gen} {t}")

    def poll(self):
        now = time.monotonic()
        if now - self.last_hello > 1:
            self.last_hello = now
            self._send(f"nvrls-hello {self.slot} {self.gen} -")
        while True:
            try:
                data = self.sock.recv(512)
            except OSError:  # nothing waiting (or localservice.py not there)
                return
            try:
                tag, gen, speed, step = data.decode("ascii").split()
                if tag == "nvrls-state" and int(gen) == self.gen:
                    self.speed, self.step = min(max(float(speed), 1 / 16), 16.0), int(step)
            except ValueError:
                pass


def parse_sdp(sdp):
    """Return [(kind, lines, control)] for the audio/video media sections."""
    media, cur = [], None
    for line in sdp.splitlines():
        if line.startswith("m="):
            cur = [line.split()[0][2:], [line], None]
            media.append(cur)
        elif cur is not None:
            if line.startswith("a=control:"):
                cur[2] = line.split(":", 1)[1]
            elif line.startswith(("a=rtpmap:", "a=fmtp:")):
                cur[1].append(line)
    return [tuple(m) for m in media if m[0] in ("video", "audio") and m[2]]


def video_codec(lines):
    for line in lines:
        if line.startswith("a=rtpmap:"):
            return line.split()[1].split("/")[0].upper()
    return ""


def recording_time(b):
    """Recording time in a Hikvision RTP header extension ("HK" record, type 0x40),
    as YYYYMMDDTHHMMSS, or None. Bits: year-2000 (8), month (4), day (5),
    hour (5), minute (6), second (6), then sub-second bits."""
    if not b[0] & 0x10:
        return None
    off = 12 + 4 * (b[0] & 0x0F)
    ext = b[off + 4:off + 4 + 4 * int.from_bytes(b[off + 2:off + 4], "big")]
    i = 0
    while i + 2 <= len(ext):
        typ, ln = ext[i], ext[i + 1]
        val = ext[i + 2:i + 2 + ln]
        if typ == 0x40 and len(val) >= 10 and val[:2] == b"HK":
            v = int.from_bytes(val[4:10], "big")
            y, mo, d = 2000 + (v >> 40), (v >> 36) & 0xF, (v >> 31) & 0x1F
            h, mi, se = (v >> 26) & 0x1F, (v >> 20) & 0x3F, (v >> 14) & 0x3F
            if 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and se < 60:
                return f"{y:04d}{mo:02d}{d:02d}T{h:02d}{mi:02d}{se:02d}"
            return None
        if ln == 0 and typ == 0:
            break
        i += 2 + ln
    return None


def rtp_payload(b):
    """(start, end) of the RTP payload in packet b."""
    off = 12 + 4 * (b[0] & 0x0F)
    if b[0] & 0x10:
        off += 4 + 4 * int.from_bytes(b[off + 2:off + 4], "big")
    end = len(b) - (b[-1] if b[0] & 0x20 else 0)
    return off, end


class VideoDecryptor:
    """Decrypts video RTP packets in place. A fragmented NAL unit's packets are
    held back until its encrypted part (up to 4096 bytes) has arrived."""

    def __init__(self, aes, hevc):
        self.aes = aes
        self.hevc = hevc
        self.hdr = 2 if hevc else 1      # NAL unit header
        self.fu_hdr = 3 if hevc else 2   # FU indicator + FU header
        self.pending = None              # {"pkts": [...], "spans": [...], "size": n}

    def _decrypt(self, spans):
        """spans: [(bytearray, start, length)] holding the start of one NAL body."""
        total = sum(ln for _, _, ln in spans)
        n = min(total // 16 * 16, ENCRYPTED_BYTES)
        if n <= 0:
            return
        data = b"".join(bytes(b[s:s + ln]) for b, s, ln in spans)[:n]
        plain = self.aes.decrypt(data)
        pos = 0
        for b, s, ln in spans:
            take = min(ln, n - pos)
            if take <= 0:
                break
            b[s:s + take] = plain[pos:pos + take]
            pos += take

    def _flush(self):
        p, self.pending = self.pending, None
        self._decrypt(p["spans"])
        return [bytes(b) for b in p["pkts"]]

    def process(self, pkt):
        """Take one RTP packet; return the packets ready to send, in order."""
        if len(pkt) < 13:
            return [pkt]
        b = bytearray(pkt)
        encrypted = bool(b[0] & 0x10)
        if encrypted:  # strip the extension that marks encryption
            off = 12 + 4 * (b[0] & 0x0F)
            del b[off:off + 4 + 4 * int.from_bytes(b[off + 2:off + 4], "big")]
            b[0] &= 0xEF
        p0, end = rtp_payload(b)
        if end <= p0:
            return [bytes(b)]
        t = (b[p0] >> 1) & 0x3F if self.hevc else b[p0] & 0x1F
        is_fu = t == (49 if self.hevc else 28)
        is_ap = t == (48 if self.hevc else 24)
        fh = b[p0 + self.fu_hdr - 1] if is_fu and end - p0 >= self.fu_hdr else 0

        if self.pending is not None:
            if not is_fu or fh & 0x80:  # fragments interrupted: flush first
                return self._flush() + self.process(pkt)
            span = (b, p0 + self.fu_hdr, end - p0 - self.fu_hdr)
            self.pending["pkts"].append(b)
            self.pending["spans"].append(span)
            self.pending["size"] += span[2]
            if fh & 0x40 or self.pending["size"] >= ENCRYPTED_BYTES:
                return self._flush()
            return []

        if is_fu:
            if not (encrypted and fh & 0x80):
                return [bytes(b)]
            span = (b, p0 + self.fu_hdr, end - p0 - self.fu_hdr)
            self.pending = {"pkts": [b], "spans": [span], "size": span[2]}
            if fh & 0x40 or span[2] >= ENCRYPTED_BYTES:
                return self._flush()
            return []
        if not encrypted:
            return [bytes(b)]
        if is_ap:
            i = p0 + self.hdr
            while i + 2 <= end:
                n = int.from_bytes(b[i:i + 2], "big")
                self._decrypt([(b, i + 2 + self.hdr, n - self.hdr)])
                i += 2 + n
        else:
            self._decrypt([(b, p0 + self.hdr, end - p0 - self.hdr)])
        return [bytes(b)]


def annexb(packets, hevc, state):
    """Depacketize (decrypted) video RTP packets into NAL units."""
    for pkt in packets:
        s, e = rtp_payload(pkt)
        p = pkt[s:e]
        if not p:
            continue
        t = (p[0] >> 1) & 0x3F if hevc else p[0] & 0x1F
        if t == (48 if hevc else 24):
            i = 2 if hevc else 1
            while i + 2 <= len(p):
                n = int.from_bytes(p[i:i + 2], "big")
                yield p[i + 2:i + 2 + n]
                i += 2 + n
        elif t == (49 if hevc else 28):
            fh = p[2] if hevc else p[1]
            if fh & 0x80:
                state["fu"] = bytearray([(p[0] & 0x81) | ((fh & 0x3F) << 1), p[1]] if hevc
                                        else [(p[0] & 0xE0) | (fh & 0x1F)])
            if state.get("fu") is not None:
                state["fu"] += p[3:] if hevc else p[2:]
                if fh & 0x40:
                    yield bytes(state["fu"])
                    state["fu"] = None
        else:
            yield p


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channel", nargs="?", help="e.g. 101 (channel 1 main) or 102 (sub)")
    ap.add_argument("output", nargs="?", help="RTSP URL to publish to (go2rtc's {output})")
    ap.add_argument("--start", help="play a recording from this time (YYYYMMDDTHHMMSSZ, NVR clock)")
    ap.add_argument("--end", help="... up to this time (default: open-ended)")
    ap.add_argument("--slot", type=int, help="playback slot: ask localservice.py what to play")
    ap.add_argument("--service", default="127.0.0.1:1985", help="localservice.py address (--slot)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (testing)")
    args = ap.parse_args()

    ctl = None
    if args.slot is not None:  # what this playback stream plays: {"track", "start", "end", "gen"}
        if args.output is None:  # "--slot N {output}": the only positional is the output
            args.output, args.channel = args.channel, None
        try:
            with urllib.request.urlopen(f"http://{args.service}/slot/{args.slot}", timeout=5) as r:
                job = json.load(r)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"slot {args.slot}: nothing to play ({e})")
        args.channel, args.start, args.end = str(int(job["track"])), job["start"], job.get("end")
        ctl = Control(args.service, args.slot, int(job["gen"]), float(job.get("speed") or 1))
    if not args.channel:
        ap.error("channel or --slot is required")

    uq = urllib.parse.unquote
    host = os.environ["NVR_HOST"]
    user, password = uq(os.environ["NVR_USER_URL"]), uq(os.environ["NVR_PASS_URL"])
    key = uq(os.environ.get("NVR_KEY_URL") or os.environ["NVR_PASS_URL"]).encode()
    aes = AES128ECB(key[:16].ljust(16, b"\0"))

    # Source: the NVR.
    if args.start:  # a recording: /Streaming/tracks/<channel>?starttime=...
        times = [t for t in (args.start, args.end) if t]
        if not all(re.fullmatch(r"\d{8}T\d{6}Z", t) for t in times):
            sys.exit("--start/--end must look like 20260101T120000Z")
        src_url = f"rtsp://{host}:554/Streaming/tracks/{int(args.channel)}?starttime={args.start}"
        if args.end:
            src_url += f"&endtime={args.end}"
    else:
        src_url = f"rtsp://{host}:554/Streaming/Channels/{int(args.channel)}"
    nvr = RTSPConn(host, 554, user, password)
    head, sdp = nvr.request("DESCRIBE", src_url, "Accept: application/sdp\r\n")
    m = re.search(r"^Content-Base:\s*(\S+)", head, re.I | re.M)
    base = (m.group(1) if m else src_url.split("?")[0]).rstrip("/")
    media = parse_sdp(sdp)
    if not args.output:  # stdout mode: video only
        media = [x for x in media if x[0] == "video"]
    # Recordings may carry audio go2rtc can't use (e.g. G.722.1): leave it out.
    media = [x for x in media if x[0] == "video" or video_codec(x[1]) in AUDIO_CODECS]
    video_ch, hevc = None, True
    payload_types = {}  # interleaved channel -> RTP payload types its media declares
    for i, (kind, lines, ctrl) in enumerate(media):
        payload_types[2 * i] = {int(pt) for pt in lines[0].split()[3:] if pt.isdigit()}
        track = ctrl if ctrl.startswith("rtsp://") else f"{base}/{ctrl}"
        nvr.request("SETUP", track,
                    f"Transport: RTP/AVP/TCP;unicast;interleaved={2 * i}-{2 * i + 1}\r\n")
        if kind == "video" and video_ch is None:
            video_ch = 2 * i
            hevc = video_codec(lines) in ("H265", "HEVC")
    if video_ch is None:
        sys.exit("no video track")

    # Sink: go2rtc (RTSP publish), or stdout.
    sink = None
    if args.output:
        o = urllib.parse.urlsplit(args.output)
        sink = RTSPConn(o.hostname, o.port or 554)
        out_sdp = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=hikdecrypt", "c=IN IP4 0.0.0.0", "t=0 0"]
        for i, (_, lines, _) in enumerate(media):
            out_sdp += lines + [f"a=control:trackID={i}"]
        sink.request("ANNOUNCE", args.output, "Content-Type: application/sdp\r\n",
                     "\r\n".join(out_sdp) + "\r\n")
        for i in range(len(media)):
            sink.request("SETUP", f"{args.output}/trackID={i}",
                         f"Transport: RTP/AVP/TCP;unicast;interleaved={2 * i}-{2 * i + 1};mode=record\r\n")
        sink.request("RECORD", args.output)

    play_url = src_url.split("?")[0]  # PAUSE/PLAY (speed) during a recording
    speed = ctl.speed if ctl else 1.0
    nvr.request("PLAY", src_url, "Range: npt=0.000-\r\n" + (f"Scale: {speed:g}\r\n" if speed != 1 else ""))
    dec = VideoDecryptor(aes, hevc)
    end = time.monotonic() + args.seconds if args.seconds else None
    last_keepalive = time.monotonic()
    out = sys.stdout.buffer
    started = False
    fu_state = {}
    last_time = None
    retime = {}            # channel -> [last RTP timestamp in, timestamp out]
    audio_ok = True        # audio stops for good once the speed changes
    held = deque()         # single-frame mode: packets read but not yet released
    held_frames = 0
    stepping, done, nvr_paused = False, 0, False

    def is_frame_end(ch, pkt):
        return ch == video_ch and pkt[1] & 0x80 and pkt[1] & 0x7F in payload_types[ch]

    def forward(ch, pkt):
        nonlocal last_time, started
        if ctl and ch == video_ch and (t := recording_time(pkt)) and t != last_time:
            last_time = t
            ctl.report(t)
        if len(pkt) < 12 or pkt[1] & 0x7F not in payload_types.get(ch, ()):
            return  # e.g. the NVR's empty PT 112 packets that only carry the time
        if ctl:
            if ch != video_ch:
                if not audio_ok:
                    return
            else:  # timestamps at the browser's pace
                ts = int.from_bytes(pkt[4:8], "big")
                st = retime.setdefault(ch, [ts, float(ts)])
                d = (ts - st[0]) & 0xFFFFFFFF
                if d >= 1 << 31 or d > 90000 * 5:  # backwards or a jump: one frame on
                    d = 3600
                st[0] = ts
                st[1] += d / speed
                pkt = pkt[:4] + (int(st[1]) & 0xFFFFFFFF).to_bytes(4, "big") + pkt[8:]
        pkts = dec.process(pkt) if ch == video_ch else [pkt]
        if sink:
            for p in pkts:
                sink.sock.sendall(b"$" + bytes([ch]) + len(p).to_bytes(2, "big") + p)
        elif ch == video_ch:
            for nal in annexb(pkts, hevc, fu_state):
                if not started:  # start at the first parameter set
                    started = ((nal[0] >> 1) & 0x3F) == 32 if hevc else (nal[0] & 0x1F) == 7
                    if not started:
                        continue
                out.write(b"\x00\x00\x00\x01" + nal)
            out.flush()

    def nvr_play(scale):
        nvr.send("PLAY", play_url, f"Scale: {scale:g}\r\n")

    try:
        while True:
            if time.monotonic() - last_keepalive > KEEPALIVE:
                nvr.send("GET_PARAMETER", src_url)
                last_keepalive = time.monotonic()
            if ctl:
                ctl.poll()
                if ctl.step >= 0 and not stepping:  # single-frame mode: hold the stream
                    stepping, done, audio_ok, speed = True, 0, False, 1.0
                    nvr.send("PAUSE", play_url)
                    nvr_paused = True
                elif stepping and ctl.step < 0:  # play again, from the frames held back
                    stepping = False
                    while held:
                        forward(*held.popleft())
                    held_frames = 0
                    speed = ctl.speed
                    nvr_play(speed)
                    nvr_paused = False
                elif not stepping and ctl.speed != speed:
                    speed, audio_ok = ctl.speed, False
                    nvr_play(speed)
                if stepping:
                    while done < ctl.step and held_frames:
                        while held:
                            ch, pkt = held.popleft()
                            forward(ch, pkt)
                            if is_frame_end(ch, pkt):
                                held_frames -= 1
                                break
                        done += 1
                    if held_frames < 4 and nvr_paused:  # running low: fetch a few more
                        nvr_play(1)
                        nvr_paused = False
                    elif held_frames >= 12 and not nvr_paused:
                        nvr.send("PAUSE", play_url)
                        nvr_paused = True
            item = nvr.next_frame(0.1 if ctl else None)
            if item is None:
                continue
            ch, pkt = item
            if ch % 2:  # RTCP: not forwarded
                continue
            if stepping:
                held.append((ch, pkt))
                if is_frame_end(ch, pkt):
                    held_frames += 1
            else:
                forward(ch, pkt)
            if end and time.monotonic() > end:
                break
    except (BrokenPipeError, ConnectionResetError, KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
