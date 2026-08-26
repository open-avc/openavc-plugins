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
//
// HOW to play is not ours to decide. WebRTC's media is UDP straight to a LAN
// address, so it reaches a panel in the room and never reaches somebody on the
// far side of the cloud tunnel -- who gets HLS instead, over plain HTTP the
// tunnel already relays. That answer is per viewer, and the stream list is one
// value for everybody, so it cannot live there: we ask /ext/delivery on every
// playback and dispatch on what comes back. hls.js is fetched only if that
// answer is 'hls', so a panel in the room never pays for 385 KB it will not
// use.

(() => {
  const videoEl = document.getElementById('video');
  const imgEl = document.getElementById('mjpeg');
  const labelEl = document.getElementById('label');
  const overlayEl = document.getElementById('status');
  const spinnerEl = document.getElementById('spinner');
  const statusTextEl = document.getElementById('statusText');
  const retryEl = document.getElementById('retry');

  const STATE_PREFIX = 'plugin.video_panel.';
  const RECONNECT_BASE_MS = 1000;
  const RECONNECT_CAP_MS = 10000;
  // Said only when a source is unplayable and nobody supplied a sentence. Every
  // driver that uses the status convention supplies one, so this is the floor
  // rather than the norm.
  const NO_STREAM_TEXT = 'This source has no stream right now.';
  const SOURCE_GONE_TEXT = 'This source is no longer available.';
  const PLAYOUT_DELAY_HINT = 0.1; // seconds; small jitter buffer for LAN latency

  // The iframe is served at <base>/api/plugins/video_panel/panel/video_stream.html.
  // The plugin's HTTP routes live at the sibling .../ext/. Deriving the base
  // from our own location keeps the WHEP calls correct under any path prefix.
  const EXT_BASE = location.pathname.replace(/\/panel\/[^/]*$/, '/ext');

  let config = {};
  let token; // undefined on an open instance
  let streamId = '';
  let streamLabel = '';
  // Set from the server's delivery answer at each start, never guessed here.
  let streamMode = '';
  // The kind the stream list last advertised. NOT what we dispatch on -- only a
  // change-detector, so a source that changes format under a stable id still
  // restarts onto the right path.
  let listedKind = '';
  let hlsPlayer = null;
  let hlsLibPromise = null;
  // When the element is bound to a channel it follows the plugin-namespaced
  // selection key instead of its static stream_id, so a macro/script/API can
  // switch the source at runtime. streamListRaw caches the last stream list so
  // a switch can re-resolve the new source's label + render mode.
  let channel = '';
  let selectionKey = ''; // plugin.video_panel.selection.<channel>, '' when unbound
  let streamListRaw = null;
  // Why our source cannot be played, as the stream list last said it -- or null
  // when it can. A source that has lost its stream is not a connection failure
  // and must not be retried like one: the driver has already said what is
  // missing and what to do about it, and a spinner throws that away.
  let blockReason = null;
  // Whether our id has ever appeared in a published list. Until it has, a list
  // that does not mention it is "we haven't been told yet", not "it is gone" --
  // the plugin publishes the configured streams a moment before the discovered
  // ones, so a panel connecting in that gap must not draw a verdict from it.
  let seenRow = false;

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
    const cover = config.fit === 'cover';
    videoEl.classList.toggle('fit-cover', cover);
    imgEl.classList.toggle('fit-cover', cover);

    channel = (config.channel || '').trim();
    selectionKey = channel ? STATE_PREFIX + 'selection.' + channel : '';

    const snapshot = msg.state || {};
    if (snapshot[STATE_PREFIX + 'stream_ids']) {
      streamListRaw = snapshot[STATE_PREFIX + 'stream_ids'];
    }

    // A channel selection (when set and non-empty) overrides the static stream;
    // otherwise the element shows its configured stream_id.
    let newId = (config.stream_id || '').trim();
    if (selectionKey && typeof snapshot[selectionKey] === 'string' && snapshot[selectionKey].trim()) {
      newId = snapshot[selectionKey].trim();
    }
    if (newId !== streamId) {
      streamId = newId;
      seenRow = false;
      stop();
    }
    streamMode = resolveMeta();

    if (!streamId) {
      showOverlay({ spinner: false, text: 'No stream selected' });
      return;
    }
    if (blockReason) {
      active = false;
      showOverlay({ spinner: false, text: blockReason, retry: true });
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
    if (selectionKey && key === selectionKey) {
      // A macro / script / API changed this channel's selection. An empty value
      // falls back to the element's configured stream_id (or "no stream").
      const next = (typeof value === 'string' && value.trim())
        ? value.trim()
        : (config.stream_id || '').trim();
      selectStream(next);
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

  // Our source's row in the cached list: the entry object, `null` when the list
  // arrived and does not mention us, `undefined` when there is no usable list.
  //
  // Matched on `id` as well as `value`, because a source that cannot be played
  // is published WITHOUT a `value` -- that is how the plugin keeps it out of
  // pickers while still saying it exists. Matching on `value` alone is exactly
  // why a tile whose source went away had nothing to read.
  function listRow() {
    if (!streamListRaw) return undefined;
    let list;
    try {
      list = JSON.parse(streamListRaw);
    } catch {
      return undefined;
    }
    if (!Array.isArray(list)) return undefined;
    return list.find((e) => e && (e.value === streamId || e.id === streamId)) || null;
  }

  // Resolve the current stream's label from the cached list and put it on
  // screen, and set blockReason from what the list says about it. Returns the
  // kind the list advertises, which is used only to notice that something
  // changed — the delivery route decides how we actually play.
  function resolveMeta() {
    let kind = listedKind;
    streamLabel = streamId;
    const row = listRow();
    if (row) {
      seenRow = true;
      streamLabel = row.label || streamId;
      if (row.mode) kind = row.mode;
      const status = typeof row.status === 'string' ? row.status.trim() : '';
      // No `value` means there is no stream behind the row. A status means the
      // source is offline, half-configured, or switched off. Either way it
      // cannot be drawn, and the row carries the sentence that says why.
      const playable = row.value !== undefined && row.value !== null
        && (status === '' || status === 'ready');
      blockReason = playable
        ? null
        : ((typeof row.detail === 'string' && row.detail.trim()) || NO_STREAM_TEXT);
    } else if (row === null && seenRow) {
      blockReason = SOURCE_GONE_TEXT;
    } else {
      blockReason = null;
    }
    labelEl.textContent = streamLabel || '';
    labelEl.hidden = !(config.show_label && streamLabel);
    return kind;
  }

  // Stop playing and say why. Not a failure state: no backoff, no retry timer,
  // and `active` goes false so nothing schedules one. The list is republished
  // on every change, so when the missing piece is supplied the tile comes back
  // on its own without anyone touching the panel.
  function blockPlayback() {
    teardown();
    active = false;
    showOverlay({ spinner: false, text: blockReason, retry: true });
  }

  function updateLabelFromList(raw) {
    streamListRaw = raw;
    const wasBlocked = blockReason;
    const nextKind = resolveMeta();
    const kindChanged = nextKind !== listedKind;
    listedKind = nextKind;

    if (blockReason) {
      if (blockReason !== wasBlocked || active) blockPlayback();
      return;
    }
    if (wasBlocked && streamId) {
      // Whatever was missing has been supplied. Come back by ourselves.
      active = true;
      reconnectAttempts = 0;
      start();
      return;
    }
    // The source changed kind under a stable id (a driver republished it with a
    // different preview format). We'd be playing the wrong way, so restart and
    // let the server say how.
    if (kindChanged && active && streamId) {
      teardown();
      reconnectAttempts = 0;
      start();
    }
  }

  // Switch to a different source at runtime (the channel selection changed).
  // Tears down the current playback, re-resolves the new source's label + mode,
  // and starts it — or shows "no stream" when the selection is cleared.
  function selectStream(newId) {
    newId = (newId || '').trim();
    if (newId === streamId) return;
    teardown();
    streamId = newId;
    seenRow = false;
    listedKind = resolveMeta();
    streamMode = '';
    if (!streamId) {
      active = false;
      showOverlay({ spinner: false, text: 'No stream selected' });
      return;
    }
    if (blockReason) {
      active = false;
      showOverlay({ spinner: false, text: blockReason, retry: true });
      return;
    }
    active = true;
    reconnectAttempts = 0;
    start();
  }

  // ──── Playback dispatch ────

  function deliveryUrl() {
    return EXT_BASE + '/delivery/' + encodeURIComponent(streamId);
  }

  // Ask the server how THIS viewer should play THIS stream, then dispatch.
  // Asked on every start, not cached: an entitlement can be revoked mid-session
  // and a reconnect is exactly when we should find out.
  async function start() {
    if (!active || !streamId) return;
    const wanted = streamId;
    showOverlay({ spinner: true, text: reconnectAttempts > 0 ? 'Reconnecting…' : 'Connecting…' });

    let delivery;
    try {
      const res = await fetch(deliveryUrl(), { headers: authHeaders() });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      delivery = await res.json();
    } catch {
      // We could not even ask. That is a connection problem like any other, so
      // it takes the normal backoff rather than a special message.
      if (active && streamId === wanted) scheduleReconnect();
      return;
    }
    // The source was switched, or we were stopped, while that was in flight.
    if (!active || streamId !== wanted) return;

    streamMode = delivery && delivery.mode ? delivery.mode : 'webrtc';
    if (streamMode === 'blocked') {
      // Not a failure and not a retry storm: the plan does not include remote
      // video. Say so, stop the spinner, and leave Retry there for the case
      // where it gets bought while the panel is open.
      teardown();
      showOverlay({
        spinner: false,
        text: (delivery && delivery.detail) || 'Remote video is not included in this plan.',
        retry: true,
      });
      return;
    }
    if (streamMode === 'mjpeg') startMjpeg();
    else if (streamMode === 'hls') startHls();
    else startWhep();
  }

  // ──── MJPEG client (<img> multipart over HTTP) ────

  function mjpegUrl() {
    let url = EXT_BASE + '/mjpeg/' + encodeURIComponent(streamId);
    // An <img> can't set headers, so the plugin token rides the query string;
    // the platform's ext-auth accepts it there.
    if (token) url += '?_plugin_token=' + encodeURIComponent(token);
    return url;
  }

  function startMjpeg() {
    if (!active || !streamId) return;
    videoEl.hidden = true;
    imgEl.hidden = false;
    showOverlay({ spinner: true, text: reconnectAttempts > 0 ? 'Reconnecting…' : 'Connecting…' });
    // Setting src opens the multipart connection; `load` fires on the first
    // frame, `error` if the encoder or the AV LAN is unreachable.
    imgEl.src = mjpegUrl();
  }

  // ──── HLS client (tunnelled viewers) ────

  // Only Safari plays HLS in a bare <video>; Chrome, Edge and the Android
  // WebView need Media Source Extensions driven by a library. hls.js is loaded
  // from our own panel folder rather than a CDN, because the rooms this runs in
  // routinely have no route to the internet.
  //
  // WHICH of those a browser is cannot be asked with canPlayType. Chrome, Edge
  // and Firefox all answer 'maybe' for the HLS MIME types and then cannot play
  // one -- so trusting it sent every desktop browser down the native path,
  // where it fetched playlists and segments, decoded nothing, and left a
  // spinner up forever. Media Source Extensions are the honest test: where they
  // exist, hls.js works, and where they do not the browser is an iPhone or iPad,
  // which plays HLS natively and better. Checked before the library is fetched
  // so a phone never downloads 385 KB to be told it cannot use it.
  function mseAvailable() {
    return typeof window.MediaSource !== 'undefined'
      && typeof window.MediaSource.isTypeSupported === 'function';
  }

  function loadHlsLib() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (hlsLibPromise) return hlsLibPromise;
    hlsLibPromise = new Promise((resolve, reject) => {
      const tag = document.createElement('script');
      tag.src = 'hls.light.min.js';
      tag.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error('hls.js did not define Hls')));
      tag.onerror = () => reject(new Error('hls.js failed to load'));
      document.head.appendChild(tag);
    });
    // A failed load must not be cached as a permanent no: the next reconnect
    // should be free to try again.
    hlsLibPromise.catch(() => { hlsLibPromise = null; });
    return hlsLibPromise;
  }

  function hlsUrl() {
    let url = EXT_BASE + '/hls/' + encodeURIComponent(streamId) + '/index.m3u8';
    // hls.js sets its own headers, but the segment requests it derives from the
    // playlist inherit only the query string, so the token rides there for both.
    if (token) url += '?_plugin_token=' + encodeURIComponent(token);
    return url;
  }

  async function startHls() {
    if (starting || !active || !streamId) return;
    starting = true;
    videoEl.hidden = false;
    imgEl.hidden = true;
    const wanted = streamId;
    try {
      // No Media Source Extensions: an iPhone or iPad, where the native player
      // is both the only option and the better one — lower power, hardware
      // pipeline.
      if (!mseAvailable()) {
        if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
          videoEl.src = hlsUrl();
          videoEl.play().catch(() => { /* autoplay policy; muted should allow it */ });
        } else {
          showOverlay({ spinner: false, text: 'This browser cannot play remote video.', retry: false });
        }
        return;
      }
      const Hls = await loadHlsLib();
      if (!active || streamId !== wanted) return;
      if (!Hls.isSupported()) {
        showOverlay({ spinner: false, text: 'This browser cannot play remote video.', retry: false });
        return;
      }
      const player = new Hls({
        lowLatencyMode: true,
        // The tunnel is a relay, not a pipe: it is worth waiting a moment
        // longer for a part than tearing the session down over one late fetch.
        manifestLoadingTimeOut: 20000,
        fragLoadingTimeOut: 20000,
        backBufferLength: 10,
      });
      hlsPlayer = player;
      player.on(Hls.Events.ERROR, (_evt, data) => {
        if (hlsPlayer !== player || !data || !data.fatal) return;
        if (active) scheduleReconnect();
      });
      player.loadSource(hlsUrl());
      player.attachMedia(videoEl);
      videoEl.play().catch(() => { /* autoplay policy; muted should allow it */ });
    } catch {
      if (active && streamId === wanted) scheduleReconnect();
    } finally {
      starting = false;
    }
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

  async function startWhep() {
    if (starting || !active || !streamId || pc) return;
    starting = true;
    videoEl.hidden = false;
    imgEl.hidden = true;
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
    // HLS: destroy() stops the playlist polling and the segment fetches. Left
    // running it keeps pulling video across the tunnel with nothing on screen,
    // which is the one leak that costs money rather than memory.
    if (hlsPlayer) {
      const player = hlsPlayer;
      hlsPlayer = null;
      try { player.destroy(); } catch { /* already destroyed */ }
    }
    // The native-HLS path (Safari) sets src rather than srcObject.
    if (videoEl.getAttribute('src')) {
      videoEl.removeAttribute('src');
      try { videoEl.load(); } catch { /* nothing loaded */ }
    }
    // MJPEG: drop the src to close the multipart connection. removeAttribute
    // (not src = '') so the browser doesn't refetch the iframe's own URL.
    if (imgEl.getAttribute('src')) imgEl.removeAttribute('src');
    imgEl.hidden = true;
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
    // Re-read the list first. A source that is still missing its stream should
    // say so again rather than spin, so the button never becomes a lie.
    resolveMeta();
    if (blockReason) {
      showOverlay({ spinner: false, text: blockReason, retry: true });
      return;
    }
    active = true;
    pausedByVisibility = false;
    reconnectAttempts = 0;
    start();
  });

  // MJPEG playback feedback: the first frame clears the overlay; a load error
  // (encoder offline / no route to the AV LAN) drives the same reconnect path
  // as WebRTC. The src-present guard ignores the error a teardown clear emits.
  imgEl.addEventListener('load', () => {
    if (streamMode === 'mjpeg' && active) {
      reconnectAttempts = 0;
      hideOverlay();
    }
  });
  imgEl.addEventListener('error', () => {
    if (streamMode === 'mjpeg' && active && imgEl.getAttribute('src')) {
      scheduleReconnect();
    }
  });

  // HLS playback feedback. WebRTC clears its overlay off the peer connection
  // state and MJPEG off the first frame; HLS has neither, so the <video>
  // element's own events are the signal. `playing` rather than `loadeddata`:
  // through the tunnel the first segment can arrive well before it decodes,
  // and clearing the overlay early shows a black rectangle.
  videoEl.addEventListener('playing', () => {
    if (streamMode === 'hls' && active) {
      reconnectAttempts = 0;
      hideOverlay();
    }
  });
  videoEl.addEventListener('error', () => {
    // Only the native-HLS path reports here; hls.js swallows media errors and
    // reports them on its own ERROR event, handled at the player.
    if (streamMode === 'hls' && active && videoEl.getAttribute('src')) {
      scheduleReconnect();
    }
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
      if (blockReason) {
        showOverlay({ spinner: false, text: blockReason, retry: true });
      } else if (config.reconnect_on_idle !== false) {
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
