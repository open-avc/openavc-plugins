// A Video Stream tile in a jsdom window with everything outside the DOM faked,
// and one ordered log of everything the tile does.
//
// The tile is vanilla browser JS with no build step, so the only honest way to
// test it is to run the real file. What it reaches for beyond the DOM -- fetch,
// RTCPeerConnection, hls.js, Media Source Extensions, the reconnect timer, the
// jitter, the host's postMessage -- is faked here so a scenario decides every
// answer and when it lands. A fetch can be answered at once or held until the
// scenario releases it: the tile's "was this superseded?" checks only run when
// an answer arrives after a teardown or a source switch.
//
// The log has two kinds of line. Calls the tile makes (fetch, postMessage,
// peer, hls, timers, play/load) are logged as they happen. What the viewer can
// see (overlay, label, which surface is showing, what it is playing) is logged
// as one `view` line after each step, only when it changed. A step is one task
// plus every microtask it queued, which is the granularity a viewer sees.
//
// NODE_PATH must point at a node_modules holding jsdom.

const fs = require('fs');
const vm = require('vm');
const { JSDOM, VirtualConsole } = require('jsdom');

const PAGE_URL = 'http://panel.test/api/plugins/video_panel/panel/video_stream.html';
const EXT = '/api/plugins/video_panel/ext';

// An offer with two media sections the way a browser writes one for a recvonly
// video + audio session with a data channel: the trickle fragment is built
// from these lines, so they are what a PATCH body is checked against.
const OFFER_SDP = [
  'v=0',
  'o=- 1 2 IN IP4 127.0.0.1',
  'm=video 9 UDP/TLS/RTP/SAVPF 96',
  'a=ice-ufrag:uf1',
  'a=ice-pwd:pw1',
  'm=audio 9 UDP/TLS/RTP/SAVPF 111',
  'a=ice-ufrag:uf2',
  'a=ice-pwd:pw2',
  'm=application 9 UDP/DTLS/SCTP webrtc-datachannel',
  '',
].join('\r\n');

function shortUrl(url) {
  const s = String(url);
  return s.startsWith(EXT) ? 'ext' + s.slice(EXT.length) : s;
}

function show(value) {
  return value === undefined ? 'undefined' : JSON.stringify(value);
}

// One fake HTTP answer. `spec` is {status, json, jsonFails, text, location}
// or {networkError: 'message'}.
function makeResponse(spec) {
  const status = spec.status;
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => (spec.jsonFails ? Promise.reject(new Error('bad json')) : Promise.resolve(spec.json)),
    text: () => Promise.resolve(spec.text === undefined ? '' : spec.text),
    headers: {
      get: (name) => (String(name).toLowerCase() === 'location' && spec.location !== undefined
        ? spec.location : null),
    },
  };
}

// What each call gets when the scenario has not said otherwise.
function defaultAnswer(method, url) {
  if (url.includes('/delivery/')) return { status: 200, json: { mode: 'webrtc' } };
  if (url.includes('/whep/') && method === 'POST') {
    return { status: 201, location: '/sidecar/front_door/whep/sess-1', text: 'answer-sdp' };
  }
  if (method === 'PATCH') return { status: 204 };
  return { status: 200, json: {} };
}

function newTile(jsPath, htmlPath, opts) {
  const options = opts || {};
  const log = [];
  const say = (line) => log.push(line);
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (err) => say('uncaught ' + (err && err.message)));
  virtualConsole.on('error', (...args) => say('console.error ' + args.join(' ')));
  const dom = new JSDOM(fs.readFileSync(htmlPath, 'utf8'), {
    url: PAGE_URL,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const win = dom.window;
  const doc = win.document;
  const byId = (id) => doc.getElementById(id);
  const video = byId('video');
  const img = byId('mjpeg');

  // ── fetch ──
  const pending = [];
  let answer = options.answer || defaultAnswer;
  let holdMatching = null; // a predicate: matching calls wait for release()
  win.fetch = (url, init) => {
    const method = (init && init.method) || 'GET';
    const u = String(url);
    let line = 'fetch ' + method + ' ' + shortUrl(u);
    if (init && init.headers) line += ' headers=' + JSON.stringify(init.headers);
    // The offer is the fake peer's own; a body that is anything else is shown.
    if (init && init.body !== undefined) line += init.body === OFFER_SDP ? ' body=<the offer>' : ' body=' + JSON.stringify(init.body);
    say(line);
    return new Promise((resolve, reject) => {
      const call = { method, url: u, resolve, reject };
      const settleWith = (spec) => {
        if (spec.networkError) reject(new TypeError(spec.networkError));
        else resolve(makeResponse(spec));
      };
      call.settleWith = settleWith;
      if (holdMatching && holdMatching(method, u)) pending.push(call);
      else settleWith(answer(method, u));
    });
  };

  // ── the host ──
  win.postMessage = (msg, target) => say('post ' + JSON.stringify(msg) + ' to ' + show(target));

  // ── timers and jitter ──
  let timerSeq = 0;
  const timers = new Map();
  win.setTimeout = (fn, delay) => {
    timerSeq += 1;
    timers.set(timerSeq, fn);
    say('timer set #' + timerSeq + ' ' + delay);
    return timerSeq;
  };
  win.clearTimeout = (id) => {
    say('timer clear #' + id + (timers.has(id) ? '' : ' (not pending)'));
    timers.delete(id);
  };
  win.Math.random = options.random || (() => 0.5);

  // ── the media elements ──
  let playRefused = false;
  let nativeHls = options.nativeHls === undefined ? '' : options.nativeHls;
  let videoError = null;
  video.play = () => {
    say('video play');
    return playRefused ? Promise.reject(new Error('autoplay refused')) : Promise.resolve();
  };
  let loadThrows = false;
  video.load = () => {
    say('video load');
    if (loadThrows) throw new Error('nothing loaded');
  };
  video.canPlayType = (type) => {
    say('video canPlayType ' + type);
    return nativeHls;
  };
  let srcObject = null;
  Object.defineProperty(video, 'srcObject', {
    get: () => srcObject,
    set: (v) => { srcObject = v; },
    configurable: true,
  });
  Object.defineProperty(video, 'error', { get: () => videoError, configurable: true });

  // ── Media Source Extensions ──
  if (options.mse === 'no-isTypeSupported') {
    win.MediaSource = function MediaSource() {};
  } else if (options.mse !== false) {
    win.MediaSource = function MediaSource() {};
    win.MediaSource.isTypeSupported = () => true;
  }

  // ── hls.js, loaded by a <script> the tile appends ──
  const scripts = [];
  const head = doc.head;
  const realAppend = head.appendChild.bind(head);
  head.appendChild = (el) => {
    say('script append ' + el.getAttribute('src'));
    scripts.push(el);
    return realAppend(el);
  };
  let hlsSupported = true;
  let hlsDestroyThrows = false;
  const players = [];
  class FakeHls {
    static isSupported() {
      say('Hls.isSupported');
      return hlsSupported;
    }

    constructor(cfg) {
      this.n = players.length + 1;
      this.handlers = {};
      players.push(this);
      say('hls#' + this.n + ' new ' + JSON.stringify(cfg));
    }

    on(evt, cb) {
      say('hls#' + this.n + ' on ' + evt);
      (this.handlers[evt] = this.handlers[evt] || []).push(cb);
    }

    loadSource(url) { say('hls#' + this.n + ' loadSource ' + shortUrl(url)); }

    attachMedia(el) { say('hls#' + this.n + ' attachMedia #' + el.id); }

    destroy() {
      say('hls#' + this.n + ' destroy');
      if (hlsDestroyThrows) throw new Error('already destroyed');
    }
  }
  FakeHls.Events = { ERROR: 'hlsError' };

  // ── RTCPeerConnection ──
  const peers = [];
  const peerFails = {};
  let hintThrows = false;
  class FakePeer {
    constructor(cfg) {
      this.n = peers.length + 1;
      this.connectionState = 'new';
      this.closed = false;
      peers.push(this);
      say('peer#' + this.n + ' new ' + JSON.stringify(cfg));
    }

    addTransceiver(kind, init) { say('peer#' + this.n + ' addTransceiver ' + kind + ' ' + JSON.stringify(init)); }

    createDataChannel(label) { say('peer#' + this.n + ' createDataChannel ' + JSON.stringify(label)); }

    createOffer() {
      say('peer#' + this.n + ' createOffer');
      if (peerFails.createOffer) return Promise.reject(new Error(peerFails.createOffer));
      return Promise.resolve({ type: 'offer', sdp: OFFER_SDP });
    }

    setLocalDescription(desc) {
      say('peer#' + this.n + ' setLocalDescription ' + desc.type);
      if (peerFails.setLocalDescription) return Promise.reject(new Error(peerFails.setLocalDescription));
      return Promise.resolve();
    }

    setRemoteDescription(desc) {
      say('peer#' + this.n + ' setRemoteDescription ' + JSON.stringify(desc));
      if (peerFails.setRemoteDescription) return Promise.reject(new Error(peerFails.setRemoteDescription));
      return Promise.resolve();
    }

    close() {
      say('peer#' + this.n + ' close');
      this.closed = true;
      this.connectionState = 'closed';
      if (peerFails.close) throw new Error(peerFails.close);
    }
  }
  win.RTCPeerConnection = FakePeer;

  // jsdom's media elements fetch nothing and an unframed window is its own
  // parent, so the tile's window.parent.postMessage lands on win.postMessage.
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), dom.getInternalVMContext(), { filename: jsPath });

  // ── what the viewer sees ──
  const view = () => {
    const overlay = byId('status');
    const label = byId('label');
    const parts = [
      overlay.hidden ? 'overlay hidden' : 'overlay ' + JSON.stringify(byId('statusText').textContent)
        + (byId('spinner').hidden ? '' : ' +spinner') + (byId('retry').hidden ? '' : ' +retry'),
      label.hidden ? 'label hidden(' + JSON.stringify(label.textContent) + ')' : 'label ' + JSON.stringify(label.textContent),
      'video ' + (video.hidden ? 'hidden' : 'shown')
        + (video.getAttribute('src') ? ' src=' + shortUrl(video.getAttribute('src')) : '')
        + (srcObject ? ' obj=' + srcObject.name : '')
        + (video.className ? ' .' + video.className : ''),
      'img ' + (img.hidden ? 'hidden' : 'shown')
        + (img.getAttribute('src') ? ' src=' + shortUrl(img.getAttribute('src')) : '')
        + (img.className ? ' .' + img.className : ''),
    ];
    const css = doc.documentElement.style.cssText;
    if (css) parts.push('theme ' + css);
    return parts.join(' | ');
  };
  let lastView = '';

  const settle = async () => {
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    const now = view();
    if (now !== lastView) {
      say('view ' + now);
      lastView = now;
    }
  };

  const currentPeer = () => peers[peers.length - 1];
  const livePeer = (n) => {
    const peer = n === undefined ? currentPeer() : peers[n - 1];
    // A closed RTCPeerConnection fires no further events in a browser.
    return peer && !peer.closed ? peer : null;
  };

  const tile = {
    win,
    log,
    say,
    settle,
    // ── what the scenario controls ──
    answerWith: (fn) => { answer = fn; },
    hold: (predicate) => { holdMatching = predicate; },
    pendingCount: () => pending.length,
    // Answer the oldest held call matching `predicate` (or the oldest of all).
    // `spec` is an answer, or a function of (method, url) that returns one.
    release: async (spec, predicate) => {
      const i = pending.findIndex((c) => !predicate || predicate(c.method, c.url));
      if (i < 0) {
        say('release: nothing held');
      } else {
        const [call] = pending.splice(i, 1);
        say('release ' + call.method + ' ' + shortUrl(call.url));
        call.settleWith(typeof spec === 'function' ? spec(call.method, call.url) : spec);
      }
      await settle();
    },
    refusePlay: (v) => { playRefused = v; },
    setNativeHls: (v) => { nativeHls = v; },
    setVideoError: (v) => { videoError = v; },
    setLoadThrows: (v) => { loadThrows = v; },
    setHlsSupported: (v) => { hlsSupported = v; },
    setHlsDestroyThrows: (v) => { hlsDestroyThrows = v; },
    failPeer: (method, message) => { peerFails[method] = message; },
    setHintThrows: (v) => { hintThrows = v; },
    peerCount: () => peers.length,
    hlsCount: () => players.length,
    timersPending: () => [...timers.keys()],
    // ── things that happen to the tile ──
    init: async (msg) => {
      win.dispatchEvent(new win.MessageEvent('message', { data: { type: 'openavc:init', ...msg }, source: win }));
      await settle();
    },
    state: async (key, value) => {
      win.dispatchEvent(new win.MessageEvent('message', { data: { type: 'openavc:state', key, value }, source: win }));
      await settle();
    },
    message: async (data, source) => {
      win.dispatchEvent(new win.MessageEvent('message', { data, source: source === undefined ? win : source }));
      await settle();
    },
    retry: async () => { byId('retry').click(); await settle(); },
    hide: async () => {
      Object.defineProperty(doc, 'hidden', { get: () => true, configurable: true });
      doc.dispatchEvent(new win.Event('visibilitychange'));
      await settle();
    },
    reveal: async () => {
      Object.defineProperty(doc, 'hidden', { get: () => false, configurable: true });
      doc.dispatchEvent(new win.Event('visibilitychange'));
      await settle();
    },
    pagehide: async () => { win.dispatchEvent(new win.Event('pagehide')); await settle(); },
    fireTimer: async (id) => {
      const which = id === undefined ? [...timers.keys()][0] : id;
      const fn = timers.get(which);
      if (!fn) {
        say('fire: no timer');
      } else {
        timers.delete(which);
        say('timer fire #' + which);
        fn();
      }
      await settle();
    },
    hlsLib: async (outcome) => {
      const tag = scripts[scripts.length - 1];
      if (!tag) {
        say('hlsLib: no script');
      } else if (outcome === 'error') {
        tag.onerror();
      } else {
        if (outcome === 'ok') win.Hls = FakeHls;
        tag.onload();
      }
      await settle();
    },
    hlsError: async (data, n) => {
      const player = players[(n === undefined ? players.length : n) - 1];
      const handlers = (player && player.handlers.hlsError) || [];
      for (const cb of handlers) cb('hlsError', data);
      await settle();
    },
    candidate: async (mline, text, n) => {
      const peer = livePeer(n);
      if (peer && peer.onicecandidate) {
        peer.onicecandidate({ candidate: { candidate: text || 'candidate:' + mline + ' 1 udp 1 10.0.0.9 5000 typ host', sdpMLineIndex: mline } });
      }
      await settle();
    },
    endOfCandidates: async (n) => {
      const peer = livePeer(n);
      if (peer && peer.onicecandidate) peer.onicecandidate({ candidate: null });
      await settle();
    },
    track: async (kind, streamName, n) => {
      const peer = livePeer(n);
      if (peer && peer.ontrack) {
        const receiver = {};
        Object.defineProperty(receiver, 'playoutDelayHint', {
          set: (v) => {
            say('playoutDelayHint ' + v);
            if (hintThrows) throw new Error('unsupported');
          },
        });
        const streams = streamName === undefined ? [] : [{ name: streamName }];
        peer.ontrack({ track: { kind }, receiver, streams });
      }
      await settle();
    },
    // Re-sends a track event with the same stream object the video already
    // holds, which is how a second track of one stream arrives.
    sameStreamTrack: async (kind, n) => {
      const peer = livePeer(n);
      if (peer && peer.ontrack && srcObject) {
        peer.ontrack({ track: { kind }, receiver: {}, streams: [srcObject] });
      }
      await settle();
    },
    connState: async (state, n) => {
      const peer = livePeer(n);
      if (peer && peer.onconnectionstatechange) {
        peer.connectionState = state;
        peer.onconnectionstatechange();
      }
      await settle();
    },
    imgLoad: async () => { img.dispatchEvent(new win.Event('load')); await settle(); },
    imgError: async () => { img.dispatchEvent(new win.Event('error')); await settle(); },
    videoPlaying: async () => { video.dispatchEvent(new win.Event('playing')); await settle(); },
    videoError: async () => { video.dispatchEvent(new win.Event('error')); await settle(); },
    close: () => win.close(),
  };
  return tile;
}

module.exports = { newTile, OFFER_SDP, EXT };
