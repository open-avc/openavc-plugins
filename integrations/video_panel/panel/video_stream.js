'use strict';

// Video Stream panel element — plays one stream over WHEP/WebRTC.
//
// The OpenAVC panel runtime hosts this file in a sandboxed same-origin iframe
// and sends an `openavc:init` message (config + theme + state snapshot +
// ext_token) once the iframe loads, then `openavc:state` messages as plugin
// state changes. We never carry the operator's credentials, so when the
// instance has auth configured the panel mints a plugin-scoped token and we
// present it on every /ext/* call via the X-OpenAVC-Plugin-Token header.
//
// This is a focused WHEP client (no third-party lib): create a recvonly
// PeerConnection, POST the SDP offer to the plugin's reverse-proxied WHEP
// route, set the answer, trickle ICE candidates by PATCH, and DELETE on
// teardown. MediaMTX is the upstream; its flow is mirrored from the server's
// own reference reader, plus low-latency tuning and reconnect logic the panel
// needs and the reference lacks.

(() => {
  const videoEl = document.getElementById('video');
  const labelEl = document.getElementById('label');
  const overlayEl = document.getElementById('status');
  const spinnerEl = document.getElementById('spinner');
  const statusTextEl = document.getElementById('statusText');
  const retryEl = document.getElementById('retry');

  const STATE_PREFIX = 'plugin.video_panel.';
  const RECONNECT_BASE_MS = 1000;
  const RECONNECT_CAP_MS = 10000;
  const PLAYOUT_DELAY_HINT = 0.1; // seconds; small jitter buffer for LAN latency

  // The iframe is served at <base>/api/plugins/video_panel/panel/video_stream.html.
  // The plugin's HTTP routes live at the sibling .../ext/. Deriving the base
  // from our own location keeps the WHEP calls correct under any path prefix.
  const EXT_BASE = location.pathname.replace(/\/panel\/[^/]*$/, '/ext');

  let config = {};
  let token; // undefined on an open instance
  let streamId = '';
  let streamLabel = '';

  let pc = null;
  let resourceUrl = null; // the WHEP session resource (PATCH/DELETE target)
  let offerData = null; // parsed ice-ufrag/pwd + media lines, for trickle frags
  let queuedCandidates = [];
  let starting = false;
  let active = false; // we want a live connection (false while paused/stopped)
  let pausedByVisibility = false;
  let reconnectAttempts = 0;
  let reconnectTimer = null;

  // ──── Panel host messaging ────

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    const msg = event.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'openavc:init') onInit(msg);
    else if (msg.type === 'openavc:state') onState(msg.key, msg.value);
  });

  function onInit(msg) {
    config = msg.config || {};
    token = msg.ext_token || undefined;
    applyTheme(msg.theme || {});
    videoEl.classList.toggle('fit-cover', config.fit === 'cover');

    const newId = (config.stream_id || '').trim();
    if (newId !== streamId) {
      streamId = newId;
      stop();
    }

    const snapshot = msg.state || {};
    if (snapshot[STATE_PREFIX + 'stream_ids']) {
      updateLabelFromList(snapshot[STATE_PREFIX + 'stream_ids']);
    }

    if (!streamId) {
      showOverlay({ spinner: false, text: 'No stream selected' });
      return;
    }
    active = true;
    reconnectAttempts = 0;
    start();
  }

  function onState(key, value) {
    if (key === STATE_PREFIX + 'stream_ids') {
      updateLabelFromList(value);
      return;
    }
    if (streamId && key === STATE_PREFIX + 'streams.' + streamId && value === null) {
      // The stream was deleted from the project while this panel is open.
      stop();
      showOverlay({ spinner: false, text: 'Stream removed' });
    }
  }

  function applyTheme(theme) {
    const root = document.documentElement;
    for (const [k, v] of Object.entries(theme)) {
      if (v) root.style.setProperty(k, v);
    }
  }

  function updateLabelFromList(raw) {
    try {
      const list = JSON.parse(raw);
      const found = Array.isArray(list) && list.find((e) => e && e.value === streamId);
      streamLabel = found ? found.label || streamId : streamId;
    } catch {
      streamLabel = streamId;
    }
    labelEl.textContent = streamLabel || '';
    labelEl.hidden = !(config.show_label && streamLabel);
  }

  // ──── WHEP client ────

  function whepUrl(secret) {
    const url = EXT_BASE + '/whep/' + encodeURIComponent(streamId);
    return secret ? url + '/' + encodeURIComponent(secret) : url;
  }

  function authHeaders(base) {
    const headers = base || {};
    if (token) headers['X-OpenAVC-Plugin-Token'] = token;
    return headers;
  }

  async function start() {
    if (starting || !active || !streamId || pc) return;
    starting = true;
    showOverlay({ spinner: true, text: reconnectAttempts > 0 ? 'Reconnecting…' : 'Connecting…' });

    const peer = new RTCPeerConnection({ iceServers: [] });
    pc = peer;
    resourceUrl = null;
    queuedCandidates = [];

    peer.addTransceiver('video', { direction: 'recvonly' });
    peer.addTransceiver('audio', { direction: 'recvonly' });
    // MediaMTX's reference client opens a data channel; mirror it so the
    // negotiated session matches what the server expects.
    peer.createDataChannel('');

    peer.onicecandidate = (evt) => {
      if (!evt.candidate) return;
      if (!resourceUrl) queuedCandidates.push(evt.candidate);
      else sendCandidates([evt.candidate]);
    };
    peer.ontrack = (evt) => {
      if (evt.track.kind === 'video') {
        try { evt.receiver.playoutDelayHint = PLAYOUT_DELAY_HINT; } catch { /* unsupported (e.g. Firefox) */ }
      }
      if (evt.streams && evt.streams[0] && videoEl.srcObject !== evt.streams[0]) {
        videoEl.srcObject = evt.streams[0];
        videoEl.play().catch(() => { /* autoplay policy; muted should allow it */ });
      }
    };
    peer.onconnectionstatechange = () => {
      if (pc !== peer) return; // a newer attempt superseded this one
      const s = peer.connectionState;
      if (s === 'connected') {
        reconnectAttempts = 0;
        hideOverlay();
      } else if (s === 'failed' || s === 'closed' || s === 'disconnected') {
        if (active) scheduleReconnect();
      }
    };

    try {
      const offer = await peer.createOffer();
      offerData = parseOffer(offer.sdp);
      await peer.setLocalDescription(offer);

      const res = await fetch(whepUrl(), {
        method: 'POST',
        body: offer.sdp,
        headers: authHeaders({ 'Content-Type': 'application/sdp' }),
      });
      if (res.status !== 201) throw new Error('WHEP POST returned ' + res.status);
      const location = res.headers.get('location');
      if (!location) throw new Error('WHEP response missing Location header');
      // Anchor the resource URL to our own ext base; take only the session id
      // (last path segment) from the upstream Location so a path prefix on the
      // proxy side can't desync the follow-up PATCH/DELETE.
      const secret = location.split('?')[0].replace(/\/+$/, '').split('/').pop();
      resourceUrl = whepUrl(secret);

      const answer = await res.text();
      if (pc !== peer) return; // superseded while awaiting the answer
      await peer.setRemoteDescription({ type: 'answer', sdp: answer });

      if (queuedCandidates.length) {
        sendCandidates(queuedCandidates);
        queuedCandidates = [];
      }
    } catch (err) {
      if (pc === peer && active) scheduleReconnect();
    } finally {
      starting = false;
    }
  }

  async function sendCandidates(candidates) {
    if (!resourceUrl) return;
    try {
      await fetch(resourceUrl, {
        method: 'PATCH',
        body: generateSdpFragment(offerData, candidates),
        headers: authHeaders({
          'Content-Type': 'application/trickle-ice-sdpfrag',
          'If-Match': '*',
        }),
      });
    } catch {
      // Trickle is best-effort; ICE can still complete with the candidates
      // already exchanged in the offer/answer.
    }
  }

  function scheduleReconnect() {
    teardown();
    if (!active || reconnectTimer) return;
    const exp = Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** reconnectAttempts);
    // Full jitter in [exp/2, exp] so repeated failures across many panels don't
    // hammer the server in lockstep.
    const delay = exp / 2 + Math.random() * (exp / 2);
    reconnectAttempts += 1;
    showOverlay({ spinner: true, text: 'Reconnecting…', retry: true });
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      start();
    }, delay);
  }

  function teardown() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    const peer = pc;
    const url = resourceUrl;
    pc = null;
    resourceUrl = null;
    if (videoEl.srcObject) videoEl.srcObject = null;
    if (peer) {
      try { peer.close(); } catch { /* already closed */ }
    }
    if (url) {
      // Best-effort session delete; the server also cleans up when the peer
      // connection drops, so failure here is harmless.
      fetch(url, { method: 'DELETE', headers: authHeaders() }).catch(() => {});
    }
  }

  function stop() {
    active = false;
    teardown();
  }

  // ──── SDP helpers (offer parse + trickle fragment) ────

  function parseOffer(sdp) {
    const out = { iceUfrag: '', icePwd: '', medias: [] };
    for (const line of sdp.split('\r\n')) {
      if (line.startsWith('m=')) out.medias.push(line.slice(2));
      else if (!out.iceUfrag && line.startsWith('a=ice-ufrag:')) out.iceUfrag = line.slice('a=ice-ufrag:'.length);
      else if (!out.icePwd && line.startsWith('a=ice-pwd:')) out.icePwd = line.slice('a=ice-pwd:'.length);
    }
    return out;
  }

  function generateSdpFragment(od, candidates) {
    const byMedia = {};
    for (const candidate of candidates) {
      const mid = candidate.sdpMLineIndex;
      (byMedia[mid] = byMedia[mid] || []).push(candidate);
    }
    let frag = 'a=ice-ufrag:' + od.iceUfrag + '\r\n' + 'a=ice-pwd:' + od.icePwd + '\r\n';
    let mid = 0;
    for (const media of od.medias) {
      if (byMedia[mid]) {
        frag += 'm=' + media + '\r\n' + 'a=mid:' + mid + '\r\n';
        for (const candidate of byMedia[mid]) frag += 'a=' + candidate.candidate + '\r\n';
      }
      mid += 1;
    }
    return frag;
  }

  // ──── Overlay UI ────

  function showOverlay({ spinner = false, text = '', retry = false } = {}) {
    overlayEl.hidden = false;
    spinnerEl.hidden = !spinner;
    statusTextEl.textContent = text;
    retryEl.hidden = !retry;
  }

  function hideOverlay() {
    overlayEl.hidden = true;
  }

  retryEl.addEventListener('click', () => {
    if (!streamId) return;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    active = true;
    pausedByVisibility = false;
    reconnectAttempts = 0;
    start();
  });

  // ──── Visibility: free the decoder when hidden, resume when shown ────

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (active) {
        pausedByVisibility = true;
        stop();
        showOverlay({ spinner: false, text: 'Paused' });
      }
    } else if (pausedByVisibility && streamId) {
      pausedByVisibility = false;
      if (config.reconnect_on_idle !== false) {
        active = true;
        reconnectAttempts = 0;
        start();
      } else {
        showOverlay({ spinner: false, text: 'Paused', retry: true });
      }
    }
  });

  window.addEventListener('pagehide', stop);
})();
