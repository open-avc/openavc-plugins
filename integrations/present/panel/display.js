'use strict';

// Present Display page — one display's output surface.
//
// Served by the plugin's guest route at .../guest/display/<display>?key=<display
// key>, so it runs in any plain browser with no OpenAVC login: a mini PC at
// the projector, a stick PC behind a TV, a spare tablet. It shows the idle
// "connect" card (space name, server address, join code) and cuts to the
// routed presenter's screen when the space's routing sends one here, then
// back.
//
// Idle/live switching is driven by polling the display status route every two
// seconds — the route answers with what THIS display should show, so routing
// (auto or pinned) is resolved server-side. Playback is a focused WHEP client
// (create a recvonly PeerConnection, POST the SDP offer to the plugin's
// reverse-proxied WHEP route, set the answer, trickle ICE by PATCH, DELETE on
// teardown). MediaMTX is the upstream. The display key rides every call as a
// query parameter.

(() => {
  const videoEl = document.getElementById('video');
  const idleEl = document.getElementById('idle');
  const spaceNameEl = document.getElementById('spaceName');
  const displayLabelEl = document.getElementById('displayLabel');
  const joinHostEl = document.getElementById('joinHost');
  const joinCodeEl = document.getElementById('joinCode');
  const idleStatusEl = document.getElementById('idleStatus');
  const presenterBadgeEl = document.getElementById('presenterBadge');
  const audioHintEl = document.getElementById('audioHint');

  const POLL_MS = 2000;
  const PLAYOUT_DELAY_HINT = 0.1; // seconds; small jitter buffer for LAN latency

  // The page lives at <base>/api/plugins/present/guest/display/<display>. The
  // other guest routes are siblings under .../guest/. Deriving the base from
  // our own location keeps every call correct under any path prefix.
  const path = location.pathname.replace(/\/+$/, '');
  const displayId = decodeURIComponent(path.split('/').pop());
  const GUEST_BASE = path.replace(/\/display\/[^/]*$/, '');
  const KEY = new URLSearchParams(location.search).get('key') || '';

  let currentPresenter = ''; // the ingest we're playing ('' when idle)
  let pc = null;
  let resourceUrl = null; // the WHEP session resource (PATCH/DELETE target)
  let offerData = null; // parsed ice-ufrag/pwd + media lines, for trickle frags
  let queuedCandidates = [];
  let starting = false;
  let linkDead = false; // the key was rejected; stop polling for good

  // The join line comes from the status payload (the plugin decides the
  // address guests should type); our own location.host is only the
  // placeholder until the first poll lands — it is wrong on the server host
  // and on multi-network installs.
  joinHostEl.textContent = location.host;

  // ──── Status poll (drives idle <-> live) ────

  function withKey(url) {
    return url + (url.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(KEY);
  }

  async function poll() {
    if (linkDead) return;
    let status;
    try {
      const res = await fetch(withKey(GUEST_BASE + '/displays/' + encodeURIComponent(displayId) + '/status'));
      if (res.status === 401) {
        // The display key was regenerated (or the display removed). Terminal:
        // a human has to paste the new link.
        linkDead = true;
        stopLive();
        showIdle();
        idleStatusEl.textContent =
          'This display link is no longer valid. Copy the display’s link again from the Present plugin page in the OpenAVC Programmer.';
        return;
      }
      if (!res.ok) throw new Error('status ' + res.status);
      status = await res.json();
      idleStatusEl.textContent = '';
    } catch {
      // Server restarting or network blip: keep the card up, keep trying.
      idleStatusEl.textContent = 'Reconnecting to OpenAVC…';
      stopLive();
      showIdle();
      return;
    }

    spaceNameEl.textContent = status.space_name || '';
    displayLabelEl.textContent = status.label || displayId;
    if (status.join_url) joinHostEl.textContent = status.join_url;
    joinCodeEl.textContent = (status.code || '').split('').join(' ');
    document.title = (status.label || displayId) + ' — Present';

    if (status.state === 'live' && status.presenter) {
      presenterBadgeEl.textContent = status.presenter_label || status.presenter;
      if (status.presenter !== currentPresenter) {
        stopLive();
        currentPresenter = status.presenter;
        startWhep();
      }
    } else if (currentPresenter) {
      stopLive();
      showIdle();
    }
  }

  // ──── WHEP client ────

  function whepUrl(secret) {
    let url = GUEST_BASE + '/whep/' + encodeURIComponent(displayId) + '/' + encodeURIComponent(currentPresenter);
    if (secret) url += '/' + encodeURIComponent(secret);
    return withKey(url);
  }

  async function startWhep() {
    if (starting || !currentPresenter || pc) return;
    starting = true;

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
        playWithAudioFallback();
      }
    };
    peer.onconnectionstatechange = () => {
      if (pc !== peer) return; // a newer attempt superseded this one
      const s = peer.connectionState;
      if (s === 'connected') {
        showLive();
      } else if (s === 'failed' || s === 'closed' || s === 'disconnected') {
        // Drop back to the card; the next poll restarts playback if this
        // display is still routed a live presenter.
        stopLive();
        showIdle();
      }
    };

    try {
      const offer = await peer.createOffer();
      offerData = parseOffer(offer.sdp);
      await peer.setLocalDescription(offer);

      const res = await fetch(whepUrl(), {
        method: 'POST',
        body: offer.sdp,
        headers: { 'Content-Type': 'application/sdp' },
      });
      if (res.status !== 201) throw new Error('WHEP POST returned ' + res.status);
      const location = res.headers.get('location');
      if (!location) throw new Error('WHEP response missing Location header');
      // Anchor the resource URL to our own guest base; take only the session
      // id (last path segment) from the upstream Location so a path prefix on
      // the proxy side can't desync the follow-up PATCH/DELETE.
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
      if (pc === peer) {
        stopLive();
        showIdle();
      }
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
        headers: {
          'Content-Type': 'application/trickle-ice-sdpfrag',
          'If-Match': '*',
        },
      });
    } catch {
      // Trickle is best-effort; ICE can still complete with the candidates
      // already exchanged in the offer/answer.
    }
  }

  function stopLive() {
    const peer = pc;
    const url = resourceUrl;
    pc = null;
    resourceUrl = null;
    currentPresenter = '';
    if (videoEl.srcObject) videoEl.srcObject = null;
    if (peer) {
      try { peer.close(); } catch { /* already closed */ }
    }
    if (url) {
      // Best-effort session delete; the server also cleans up when the peer
      // connection drops, so failure here is harmless.
      fetch(url, { method: 'DELETE' }).catch(() => {});
    }
  }

  // ──── Idle card / live video ────

  function showIdle() {
    idleEl.hidden = false;
    presenterBadgeEl.hidden = true;
    audioHintEl.hidden = true;
  }

  function showLive() {
    idleEl.hidden = true;
    presenterBadgeEl.hidden = !presenterBadgeEl.textContent;
  }

  // A display should play sound, but browsers block unmuted autoplay until
  // the page has had a user gesture (kiosk launchers typically disable that
  // policy). Try with audio first; fall back to muted playback plus a
  // "tap for sound" affordance.
  function playWithAudioFallback() {
    videoEl.muted = false;
    videoEl.play().catch(() => {
      videoEl.muted = true;
      videoEl.play().catch(() => { /* still blocked; poll will retry */ });
      audioHintEl.hidden = false;
    });
  }

  audioHintEl.addEventListener('click', () => {
    videoEl.muted = false;
    audioHintEl.hidden = true;
  });

  // Convenience for manual (non-kiosk) setups: double-click toggles fullscreen.
  document.addEventListener('dblclick', () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else document.documentElement.requestFullscreen().catch(() => {});
  });

  window.addEventListener('pagehide', stopLive);

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

  // ──── Go ────

  showIdle();
  poll();
  setInterval(poll, POLL_MS);
})();
