// Draws the "native windows" of the emulated Hikvision Local Service
// (localservice.py) inside the NVR page, with go2rtc players.
//
// nginx inserts this script into the NVR's pages and rewrites the NVR's
// jsVideoPlugin to talk to localservice.py (/nvr-ls/ws). This script joins the
// same page (/nvr-ls/bridge) and gets the window layout and what each window
// plays. A window only has a player while it plays and is visible, and players
// stop while the browser tab is hidden, so go2rtc only pulls the cameras that
// are actually on screen. Recordings play through go2rtc's pbN streams; a paused
// one keeps its last frame on screen while its stream is stopped. In single-frame
// mode the video is paused and moved on one frame per step (hikdecrypt.py
// releases one more frame from the NVR for each).
(() => {
  if (/\/login\.asp$/i.test(location.pathname)) return;

  const top = window.top;
  const PAGE = top.__nvrlvPage || (top.__nvrlvPage = Math.random().toString(36).slice(2, 14));
  const HEVC = !!(window.MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L153.B0"'));

  let state = { created: false, split: 1, selected: 0, wnds: {}, sound: -1, volume: 50 };
  let catalog = new Map(); // channel id -> {name, streams: Set}
  let ipChanBase = 0;      // first channel number in the NVR's "SDK" play URLs
  let enlarged = -1;       // window shown alone after a double-click, or -1
  let ws = null;
  let root = null;
  const players = new Map(); // wndIndex -> {name, el}
  const recorders = new Map(); // wndIndex -> {rec, chunks, fileName}
  let VideoEl = null;

  const css = `
#main_plugin { position: relative; }
.nvrls-root { position: absolute; inset: 0; z-index: 1; display: grid; gap: 1px;
  background: #222; overflow: hidden; font: 12px/1.4 system-ui, sans-serif; color: #ccc; }
.nvrls-wnd { position: relative; background: #000; min-width: 0; min-height: 0;
  outline: 1px solid #333; outline-offset: -1px; cursor: default; overflow: hidden; }
.nvrls-wnd.nvrls-sel { outline: 2px solid #f0a020; outline-offset: -2px; z-index: 1; }
.nvrls-wnd.nvrls-hidden { display: none; }
.nvrls-wnd canvas { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; pointer-events: none; }
.nvrls-wnd video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
  pointer-events: none; transform-origin: center; transition: transform .15s; }
.nvrls-wnd.nvrls-zoom video { transform: scale(2); }
.nvrls-label { position: absolute; left: 6px; bottom: 4px; padding: 1px 5px; border-radius: 3px;
  background: rgba(0,0,0,.55); pointer-events: none; }
.nvrls-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 8px; color: #888; pointer-events: none; }
.nvrls-rec { position: absolute; right: 6px; top: 4px; color: #f44; pointer-events: none; }
:fullscreen.nvrls-root { background: #000; }`;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  async function loadCatalog() {
    try {
      const r = await fetch("/nvr-live/streams.json", { cache: "no-cache" });
      const data = await r.json();
      catalog = new Map((data.channels || []).map((c) => [Number(c.id), { name: c.name, streams: new Set(c.streams) }]));
    } catch { /* keep empty: streams fall back to chN_main / chN_sub */ }
  }

  async function loadIpChanBase() {
    try {
      const r = await fetch("/SDK/capabilities", { credentials: "same-origin" });
      const m = /<ipChanBase>(\d+)</.exec(await r.text());
      if (m) ipChanBase = Number(m[1]);
    } catch { /* RTSP numbering only */ }
  }

  // go2rtc channel for a window: chN streams are numbered like the NVR's channel list.
  function channelOf(w) {
    return w.sdk && ipChanBase > 1 && w.ch >= ipChanBase ? w.ch - ipChanBase + 1 : w.ch;
  }

  function streamName(w) {
    if (w.pb) return `pb${w.pb}`;
    const ch = channelOf(w);
    const c = catalog.get(ch);
    if (w.kind === "main") {
      return !HEVC && c?.streams.has("main_h264") ? `ch${ch}_main_h264` : `ch${ch}_main`;
    }
    return `ch${ch}_sub`;
  }

  async function videoClass() {
    if (!VideoEl) {
      const { VideoRTC } = await import("/go2rtc/video-rtc.js");
      VideoEl = class extends VideoRTC {};
      if (!customElements.get("nvrls-video")) customElements.define("nvrls-video", VideoEl);
    }
    return VideoEl;
  }

  function container() {
    let c = document.getElementById("main_plugin");
    if (c) return c;
    for (const f of document.querySelectorAll("iframe")) {
      try { c = f.contentDocument?.getElementById("main_plugin"); } catch { c = null; }
      if (c) return c;
    }
    return null;
  }

  function ensureRoot() {
    const host = container();
    if (!host) return null;
    if (root && root.isConnected && root.parentElement === host) return root;
    const doc = host.ownerDocument;
    if (!doc.getElementById("nvrls-style")) {
      const s = doc.createElement("style");
      s.id = "nvrls-style";
      s.textContent = css;
      doc.head.append(s);
    }
    root = doc.createElement("div");
    root.className = "nvrls-root";
    host.append(root);
    for (let i = 0; i < 16; i++) {
      const w = el("div", "nvrls-wnd");
      w.dataset.wnd = i;
      w.addEventListener("mousedown", () => select(i));
      w.addEventListener("dblclick", () => { enlarged = enlarged === i ? -1 : i; render(); });
      root.append(w);
    }
    players.clear();
    return root;
  }

  function select(i) {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "select", wnd: i }));
    state.selected = i;
    paintSelection();
  }

  function paintSelection() {
    if (!root) return;
    for (const w of root.children) w.classList.toggle("nvrls-sel", Number(w.dataset.wnd) === state.selected);
  }

  function visibleWindows() {
    const n = state.split * state.split;
    if (enlarged >= 0 && enlarged < n) return [enlarged];
    return [...Array(n).keys()];
  }

  async function render() {
    if (!state.created) {
      stopAllPlayers();
      root?.remove();
      root = null;
      return;
    }
    if (!ensureRoot()) return;
    const shown = new Set(visibleWindows());
    const side = shown.size === 1 ? 1 : state.split;
    root.style.gridTemplateColumns = `repeat(${side}, 1fr)`;
    root.style.gridTemplateRows = `repeat(${side}, 1fr)`;
    const Video = await videoClass();
    for (const cell of root.children) {
      const i = Number(cell.dataset.wnd);
      cell.classList.toggle("nvrls-hidden", !shown.has(i));
      const w = state.wnds[i];
      const want = shown.has(i) && w && !document.hidden
        ? (w.zero ? "zero" : w.paused ? `paused:${w.at}` : streamName(w)) : null;
      const cur = players.get(i);
      if (cur && cur.name === want) continue;
      const still = want?.startsWith("paused:") ? (cur?.still || frameOf(i)) : null;
      if (cur) stopPlayer(i);
      cell.replaceChildren();
      if (!w) continue;
      const label = w.zero ? "Zero channel"
        : `${catalog.get(channelOf(w))?.name || `Channel ${channelOf(w)}`} \u00B7 ${w.pb || w.paused ? "playback" : w.kind}`;
      if (still || want?.startsWith("paused:")) {
        if (still) cell.append(still);
        cell.append(el("span", "nvrls-label", `${label} \u00B7 paused`));
        players.set(i, { name: want, el: null, still });
      } else if (want === "zero") {
        cell.append(el("div", "nvrls-msg", "The zero channel isn't available here."));
      } else if (want) {
        const v = new Video();
        v.mode = "mse";
        v.media = "video,audio";
        v.src = `/go2rtc/api/ws?src=${encodeURIComponent(want)}`;
        cell.append(v, el("span", "nvrls-label", label));
        const p = { name: want, el: v, frameDur: 0.04, step: -1 };
        players.set(i, p);
        watchFrames(p);
      }
    }
    applySound();
    applySteps();
    paintSelection();
  }

  // Frame spacing of what plays, for single-frame steps.
  function watchFrames(p) {
    const v = p.el.video;
    if (!v?.requestVideoFrameCallback) return;
    let last = null;
    const cb = (now, meta) => {
      if (last !== null && !v.paused) {
        const d = meta.mediaTime - last;
        if (d > 0.005 && d < 0.5) p.frameDur = d;
      }
      last = meta.mediaTime;
      if (players.get(Number(p.el.closest(".nvrls-wnd")?.dataset.wnd)) === p) v.requestVideoFrameCallback(cb);
    };
    v.requestVideoFrameCallback(cb);
  }

  function applySteps() {
    for (const [i, p] of players) {
      const v = p.el?.video;
      const step = state.wnds[i]?.step ?? -1;
      if (!v || step === p.step) continue;
      if (step < 0) {
        v.play().catch(() => {});
      } else {
        v.pause();
        const n = Math.max(1, step - Math.max(p.step, 0));
        const end = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : v.currentTime;
        v.currentTime = Math.min(end, v.currentTime + n * p.frameDur);
      }
      p.step = step;
    }
  }

  function stopPlayer(i) {
    const p = players.get(i);
    if (!p) return;
    stopRecording(i);
    p.el?.remove(); // disconnects the WebSocket; go2rtc stops pulling when nobody watches
    players.delete(i);
  }

  // The window's current picture, as a canvas (kept on screen while paused).
  function frameOf(i) {
    const v = videoOf(i);
    if (!v || !v.videoWidth) return null;
    const c = document.createElement("canvas");
    c.width = v.videoWidth;
    c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    return c;
  }

  function stopAllPlayers() {
    for (const i of [...players.keys()]) stopPlayer(i);
  }

  function videoOf(i) {
    return players.get(i)?.el?.video || null;
  }

  function applySound() {
    for (const [i, p] of players) {
      if (!p.el?.video) continue;
      p.el.video.muted = i !== state.sound;
      p.el.video.volume = Math.max(0, Math.min(1, state.volume / 100));
    }
  }

  // File name for a download: the UI's name (it may contain dots, e.g. the host
  // name) without any directory, plus the extension of what we actually save.
  function baseName(name, ext) {
    let n = String(name || "").split(/[\\/]/).pop().replace(/[^\w.-]+/g, "_");
    if (!n) n = `nvr_${new Date().toISOString().replace(/[:.]/g, "-")}`;
    return n.toLowerCase().endsWith(`.${ext}`) ? n : `${n}.${ext}`;
  }

  function download(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  function capture(i, fileName) {
    const c = frameOf(i) || players.get(i)?.still;
    if (!c) return;
    c.toBlob((b) => b && download(b, baseName(fileName, "jpg")), "image/jpeg", 0.92);
  }

  function startRecording(i, fileName) {
    const v = videoOf(i);
    if (!v || recorders.has(i) || !v.captureStream) return;
    const type = ["video/mp4", "video/webm;codecs=vp9", "video/webm"].find((t) => MediaRecorder.isTypeSupported(t));
    const rec = new MediaRecorder(v.captureStream(), type ? { mimeType: type } : {});
    const entry = { rec, chunks: [], fileName };
    rec.ondataavailable = (e) => e.data.size && entry.chunks.push(e.data);
    rec.onstop = () => {
      const ext = rec.mimeType.includes("mp4") ? "mp4" : "webm";
      download(new Blob(entry.chunks, { type: rec.mimeType }), baseName(fileName, ext));
    };
    rec.start(1000);
    recorders.set(i, entry);
    root?.children[i]?.append(el("span", "nvrls-rec", "\u25CF REC"));
  }

  function stopRecording(i) {
    const entry = recorders.get(i);
    if (!entry) return;
    recorders.delete(i);
    if (entry.rec.state !== "inactive") entry.rec.stop();
    root?.children[i]?.querySelector(".nvrls-rec")?.remove();
  }

  function onMessage(msg) {
    switch (msg.type) {
      case "state":
        state = msg;
        if (enlarged >= state.split * state.split || !state.wnds[enlarged]) enlarged = -1;
        render();
        break;
      case "capture": capture(msg.wnd, msg.fileName); break;
      case "record": msg.on ? startRecording(msg.wnd, msg.fileName) : stopRecording(msg.wnd); break;
      case "zoom": root?.children[msg.wnd]?.classList.toggle("nvrls-zoom", msg.on); break;
      case "fullscreen":
        if (msg.full && root) root.requestFullscreen?.().catch(() => {});
        else if (!msg.full && document.fullscreenElement) document.exitFullscreen().catch(() => {});
        break;
    }
  }

  let retry = 1000;
  function connect() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(`${proto}${location.host}/nvr-ls/bridge?page=${PAGE}`);
    ws.onopen = () => {
      retry = 1000;
      // For recordings: the UI shows times in the browser's time zone, and may
      // number channels from the NVR's ipChanBase.
      ws.send(JSON.stringify({ type: "hello", ipChanBase, tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "" }));
    };
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch { /* ignore */ } };
    ws.onclose = () => {
      ws = null;
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 30000);
    };
  }

  // The NVR code rewrites #main_plugin's contents at times; put the windows back.
  new MutationObserver(() => {
    if (state.created && (!root || !root.isConnected)) render();
  }).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("visibilitychange", () => render());

  Promise.all([loadCatalog(), loadIpChanBase()]).then(connect);
})();
