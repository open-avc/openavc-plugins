'use strict';

// Present Connect page — the presenter's sender surface.
//
// This is the short URL on every connect card (http://<join-address>/present,
// also served at the canonical /api/plugins/present/guest/). A guest types
// the join code shown on the space's displays plus their name; the page
// exchanges them for a guest token (POST /connect), captures the screen with
// getDisplayMedia, and publishes it to the plugin's token-gated WHIP proxy.
//
// The publish is non-trickle: wait for ICE gathering to finish, POST the
// complete offer, apply the answer — one round trip, proven on the LAN (the
// working candidate pair is found peer-reflexively over the sidecar's media
// UDP port). The WHIP resource URL from the Location header is kept only for
// the DELETE teardown.
//
// Screen capture needs a secure context. Plain-HTTP LAN pages load fine but
// can't capture, so that case gets an honest explanation instead of a dead
// button (only the platform's HTTPS support fixes it). Mobile browsers don't
// implement getDisplayMedia at all — also explained rather than hidden.

(() => {
  const joinView = document.getElementById('joinView');
  const liveView = document.getElementById('liveView');
  const joinForm = document.getElementById('joinForm');
  const nameInput = document.getElementById('nameInput');
  const codeInput = document.getElementById('codeInput');
  const joinError = document.getElementById('joinError');
  const shareBtn = document.getElementById('shareBtn');
  const blockedNote = document.getElementById('blockedNote');
  const liveName = document.getElementById('liveName');
  const liveSpace = document.getElementById('liveSpace');
  const liveStatus = document.getElementById('liveStatus');
  const stopBtn = document.getElementById('stopBtn');

  // The page is served at the guest mount root ("/present" or
  // "/api/plugins/present/guest"); the other guest routes are children.
  const GUEST_BASE = location.pathname.replace(/\/+$/, '');

  let token = '';
  let presenter = ''; // path-safe ingest name from the exchange
  let pc = null;
  let stream = null;
  let resourceUrl = null; // WHIP session resource (DELETE target)
  let busy = false;

  // ──── Capability gate (honest, not hidden) ────

  const canCapture = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  if (!window.isSecureContext) {
    blockedNote.textContent =
      'Screen sharing needs a secure (HTTPS) connection, and this OpenAVC ' +
      'system doesn’t have HTTPS enabled. Ask whoever runs it to enable ' +
      'HTTPS in OpenAVC under Settings > Security, then reload this page.';
    blockedNote.hidden = false;
    shareBtn.disabled = true;
  } else if (!canCapture) {
    blockedNote.textContent =
      'This browser can’t share its screen. Open this page in a browser on ' +
      'a laptop or desktop computer — phones and tablets don’t support ' +
      'screen sharing.';
    blockedNote.hidden = false;
    shareBtn.disabled = true;
  }

  // ──── Join flow ────

  function showError(message) {
    joinError.textContent = message;
    joinError.hidden = !message;
  }

  // FastAPI errors arrive as {"detail": "..."} — surface the human part.
  async function errorDetail(res, fallback) {
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') return body.detail;
    } catch { /* not JSON */ }
    return fallback;
  }

  joinForm.addEventListener('submit', async (evt) => {
    evt.preventDefault();
    if (busy || shareBtn.disabled) return;
    showError('');
    busy = true;
    shareBtn.textContent = 'Connecting…';
    try {
      const res = await fetch(GUEST_BASE + '/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nameInput.value, code: codeInput.value }),
      });
      if (!res.ok) {
        showError(await errorDetail(res, 'Could not connect (' + res.status + ').'));
        return;
      }
      const data = await res.json();
      token = data.token;
      presenter = data.presenter;
      liveName.textContent = data.label;
      liveSpace.textContent = data.space_name ? 'Presenting to ' + data.space_name : '';

      // Ask for the screen only after the code checks out, so a wrong code
      // never triggers the browser's share picker.
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: { frameRate: { ideal: 30 } },
          audio: true, // best-effort; Chrome/Edge offer it, others ignore it
        });
      } catch (err) {
        showError(err && err.name === 'NotAllowedError'
          ? 'Screen share was cancelled.'
          : 'Could not start screen capture.');
        return;
      }

      await publish();
    } catch {
      stopSharing('Could not reach the server. Check the address and try again.');
    } finally {
      busy = false;
      shareBtn.textContent = 'Share my screen';
    }
  });

  // ──── WHIP publish ────

  function whipUrl(secret) {
    let url = GUEST_BASE + '/whip/' + encodeURIComponent(presenter);
    if (secret) url += '/' + encodeURIComponent(secret);
    return url + '?token=' + encodeURIComponent(token);
  }

  // Non-trickle: the offer must carry every candidate. Gathering is quick on
  // a LAN (no STUN configured); the timeout just guards a pathological stack.
  function waitForIceGathering(peer) {
    if (peer.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, 2000);
      peer.addEventListener('icegatheringstatechange', () => {
        if (peer.iceGatheringState === 'complete') done();
      });
    });
  }

  async function publish() {
    const peer = new RTCPeerConnection({ iceServers: [] });
    pc = peer;
    for (const track of stream.getTracks()) peer.addTrack(track, stream);

    // The browser's own "Stop sharing" bar ends the track; treat it as Stop.
    const videoTrack = stream.getVideoTracks()[0];
    if (videoTrack) videoTrack.addEventListener('ended', () => stopSharing());

    peer.onconnectionstatechange = () => {
      if (pc !== peer) return;
      const s = peer.connectionState;
      if (s === 'connected') {
        liveStatus.textContent = 'Your screen is live.';
      } else if (s === 'failed' || s === 'closed') {
        stopSharing('Connection lost. Enter the code to share again.');
      }
    };

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await waitForIceGathering(peer);

    const res = await fetch(whipUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: peer.localDescription.sdp,
    });
    if (res.status === 401) {
      stopSharing('Your session expired. Enter the code shown on the display again.');
      return;
    }
    if (res.status !== 201) {
      stopSharing('Sharing failed (' + res.status + '). Try again.');
      return;
    }
    const loc = res.headers.get('location');
    if (loc) {
      const secret = loc.split('?')[0].replace(/\/+$/, '').split('/').pop();
      resourceUrl = whipUrl(secret);
    }
    const answer = await res.text();
    if (pc !== peer) return; // stopped while awaiting the answer
    await peer.setRemoteDescription({ type: 'answer', sdp: answer });

    liveStatus.textContent = 'Connecting…';
    joinView.hidden = true;
    liveView.hidden = false;
  }

  // ──── Teardown ────

  function stopTracks() {
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
      stream = null;
    }
  }

  function stopSharing(message) {
    const peer = pc;
    const url = resourceUrl;
    pc = null;
    resourceUrl = null;
    stopTracks();
    if (peer) {
      try { peer.close(); } catch { /* already closed */ }
    }
    if (url) {
      // Best-effort; the server also drops the session when the peer
      // connection disappears.
      fetch(url, { method: 'DELETE', keepalive: true }).catch(() => {});
    }
    liveView.hidden = true;
    joinView.hidden = false;
    // The code likely rotated when the session ended; make the guest read
    // the display again rather than resubmitting a dead one.
    codeInput.value = '';
    showError(message || '');
  }

  stopBtn.addEventListener('click', () => stopSharing());
  window.addEventListener('pagehide', () => {
    if (resourceUrl) fetch(resourceUrl, { method: 'DELETE', keepalive: true }).catch(() => {});
  });
})();
