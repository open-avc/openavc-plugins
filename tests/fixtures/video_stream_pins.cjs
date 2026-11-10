// Scenarios for the Video Stream tile, each run on a fresh tile and reported as
// the ordered log video_stream_env.cjs keeps (every call the tile makes, and
// what the viewer sees after each step). tests/test_video_stream_pins.py holds
// the log each one must produce.
//
// Usage: node video_stream_pins.cjs <video_stream.js> <video_stream.html> [name ...]
// NODE_PATH must point at a node_modules holding jsdom.

const { newTile } = require('./video_stream_env.cjs');

const [, , JS_PATH, HTML_PATH, ...ONLY] = process.argv;

const LIST = 'plugin.video_panel.stream_ids';
const KEY = (id) => 'plugin.video_panel.streams.' + id;
const SEL = (channel) => 'plugin.video_panel.selection.' + channel;
const row = (id, extra) => ({ value: id, label: id.toUpperCase(), mode: 'webrtc', ...(extra || {}) });
const list = (...rows) => JSON.stringify(rows);

const isDelivery = (method, url) => url.includes('/delivery/');
const isWhepPost = (method, url) => method === 'POST' && url.includes('/whep/');

// An answer table: the first matching entry wins, the rest fall to defaults.
function answers(table) {
  return (method, url) => {
    for (const [test, spec] of table) {
      if (test(method, url)) return typeof spec === 'function' ? spec() : spec;
    }
    if (url.includes('/delivery/')) return { status: 200, json: { mode: 'webrtc' } };
    if (method === 'POST' && url.includes('/whep/')) {
      return { status: 201, location: '/sidecar/cam/whep/sess-1', text: 'answer-sdp' };
    }
    if (method === 'PATCH') return { status: 204 };
    return { status: 200, json: {} };
  };
}
const mode = (m, extra) => [isDelivery, { status: 200, json: { mode: m, ...(extra || {}) } }];

async function initCam(t, extra) {
  const e = extra || {};
  await t.init({
    config: { stream_id: 'cam', ...(e.config || {}) },
    ext_token: e.token,
    theme: e.theme,
    state: e.state === undefined ? { [LIST]: list(row('cam')), [KEY('cam')]: 'streaming' } : e.state,
  });
}

// A WebRTC tile with its session up and a picture on screen.
async function webrtcPlaying(t, extra) {
  await initCam(t, extra);
  await t.track('video', 'cam-stream');
  await t.connState('connected');
}

const scenarios = {
  // ── the host's messages ──
  async messages_not_from_the_host_are_ignored(t) {
    await t.message({ type: 'openavc:init', config: { stream_id: 'cam' } }, null);
    await t.message('openavc:init');
    await t.message(null);
    await t.message({ type: 'openavc:other' });
  },
  async init_with_nothing_to_show(t) {
    await t.init({});
  },
  async init_applies_theme_fit_and_label(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, {
      config: { fit: 'cover', show_label: true },
      theme: { '--panel-bg': '#101010', '--panel-text': '', '--panel-accent': 'red' },
    });
  },
  async init_label_stays_hidden_without_show_label(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { fit: 'contain' } });
  },
  async init_with_a_blank_stream_id_and_a_label_wanted(t) {
    await t.init({ config: { stream_id: '   ', show_label: true } });
  },

  // ── WebRTC ──
  async webrtc_session_end_to_end(t) {
    await initCam(t, { token: 'tok', config: { show_label: true } });
    await t.candidate(0);
    await t.candidate(1);
    await t.endOfCandidates();
    await t.track('video', 'cam-stream');
    await t.connState('connected');
    await t.pagehide();
  },
  async webrtc_candidates_before_the_answer_are_queued(t) {
    t.hold(isWhepPost);
    await initCam(t);
    await t.candidate(0);
    await t.candidate(2);
    await t.candidate(0, 'candidate:second 1 udp 2 10.0.0.9 5001 typ host');
    await t.release({ status: 201, location: '/sidecar/cam/whep/sess-2', text: 'answer-2' });
    await t.candidate(1);
  },
  async webrtc_location_with_a_query_and_trailing_slash(t) {
    t.answerWith(answers([[isWhepPost, { status: 201, location: 'http://x/a/b/sess-9/?q=1', text: 'a' }]]));
    await initCam(t);
    await t.candidate(0);
    await t.pagehide();
  },
  async webrtc_post_refused_unauthorized_asks_for_a_token(t) {
    t.answerWith(answers([[isWhepPost, { status: 401 }]]));
    await initCam(t, { token: 'tok' });
    await t.fireTimer();
  },
  async webrtc_post_refused_without_a_token(t) {
    t.answerWith(answers([[isWhepPost, { status: 503 }]]));
    await initCam(t);
  },
  async webrtc_post_without_a_location(t) {
    t.answerWith(answers([[isWhepPost, { status: 201, text: 'a' }]]));
    await initCam(t);
  },
  async webrtc_post_network_error(t) {
    t.answerWith(answers([[isWhepPost, { networkError: 'Failed to fetch' }]]));
    await initCam(t);
  },
  async webrtc_offer_fails(t) {
    t.failPeer('createOffer', 'no codecs');
    await initCam(t);
  },
  async webrtc_answer_rejected(t) {
    t.failPeer('setRemoteDescription', 'bad answer');
    await initCam(t);
  },
  async webrtc_post_lands_after_a_source_switch(t) {
    t.hold(isWhepPost);
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')), [KEY('cam')]: 'idle' } });
    await t.state(SEL('front'), 'door');
    await t.release({ status: 201, location: '/sidecar/cam/whep/sess-old', text: 'old' });
    await t.fireTimer();
    await t.hide();
  },
  async webrtc_post_lands_after_the_tile_is_hidden(t) {
    t.hold(isWhepPost);
    await initCam(t);
    await t.hide();
    await t.release({ status: 401 });
    await t.reveal();
  },
  async webrtc_connection_failed_then_reconnects(t) {
    await webrtcPlaying(t);
    await t.connState('failed');
    await t.fireTimer();
    await t.track('video', 'cam-stream-2');
    await t.connState('connected');
  },
  async webrtc_connection_disconnected_and_closed(t) {
    await webrtcPlaying(t);
    await t.connState('disconnected');
    await t.fireTimer();
    await t.connState('closed');
  },
  async webrtc_connected_resets_the_backoff(t) {
    await webrtcPlaying(t);
    await t.connState('failed');
    await t.fireTimer();
    await t.connState('connected');
    await t.connState('failed');
  },
  async webrtc_post_answered_200_is_refused(t) {
    t.answerWith(answers([[isWhepPost, { status: 200, location: '/sidecar/cam/whep/sess-1', text: 'a' }]]));
    await initCam(t);
  },
  async webrtc_offer_refused_after_a_source_switch(t) {
    t.hold(isWhepPost);
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.state(SEL('front'), 'door');
    await t.release({ status: 503 });
    await t.release({ networkError: 'gone' });
  },
  async webrtc_connecting_state_does_nothing(t) {
    await initCam(t);
    await t.connState('connecting');
    await t.connState('checking');
  },
  async webrtc_tracks_audio_first_then_video(t) {
    await initCam(t);
    await t.track('audio', 'cam-stream');
    await t.sameStreamTrack('video');
    await t.track('video');
    await t.track('video', 'other-stream');
  },
  async webrtc_delay_hint_unsupported_and_autoplay_refused(t) {
    t.setHintThrows(true);
    t.refusePlay(true);
    await initCam(t);
    await t.track('video', 'cam-stream');
  },
  async webrtc_trickle_failure_is_ignored(t) {
    t.answerWith(answers([[(m) => m === 'PATCH', { networkError: 'reset' }]]));
    await initCam(t);
    await t.candidate(0);
    await t.connState('connected');
  },
  async webrtc_retry_while_a_session_is_up(t) {
    await webrtcPlaying(t);
    await t.retry();
  },
  async webrtc_reinit_with_a_block_while_playing(t) {
    await webrtcPlaying(t);
    await t.init({
      config: { stream_id: 'cam' },
      state: { [LIST]: list(row('cam', { status: 'offline', detail: 'Cam is off.' })) },
    });
    await t.connState('connected');
    await t.connState('failed');
  },

  // ── asking how to play ──
  async delivery_refused(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t);
  },
  async delivery_network_error(t) {
    t.answerWith(answers([[isDelivery, { networkError: 'Failed to fetch' }]]));
    await initCam(t);
  },
  async delivery_body_unreadable(t) {
    t.answerWith(answers([[isDelivery, { status: 200, jsonFails: true }]]));
    await initCam(t);
  },
  async delivery_body_null_plays_webrtc(t) {
    t.answerWith(answers([[isDelivery, { status: 200, json: null }]]));
    await initCam(t);
  },
  async delivery_unknown_mode_plays_webrtc(t) {
    t.answerWith(answers([mode('carrier-pigeon')]));
    await initCam(t);
  },
  async delivery_blocked_with_its_sentence(t) {
    t.answerWith(answers([mode('blocked', { detail: 'Ask your integrator.' })]));
    await initCam(t);
    await t.retry();
  },
  async delivery_blocked_without_a_sentence(t) {
    t.answerWith(answers([mode('blocked')]));
    await initCam(t);
  },
  async delivery_lands_after_a_source_switch(t) {
    t.hold(isDelivery);
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.state(SEL('front'), 'door');
    await t.release({ status: 200, json: { mode: 'mjpeg' } }, (m, u) => u.endsWith('/cam'));
    await t.release({ status: 200, json: { mode: 'mjpeg' } });
  },
  async delivery_fails_after_a_source_switch(t) {
    t.hold(isDelivery);
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.state(SEL('front'), 'door');
    await t.release({ status: 500 }, (m, u) => u.endsWith('/cam'));
  },
  async delivery_lands_after_a_stop(t) {
    t.hold(isDelivery);
    await initCam(t);
    await t.pagehide();
    await t.release({ status: 200, json: { mode: 'mjpeg' } });
  },
  async delivery_unauthorized_asks_once_per_token(t) {
    t.answerWith(answers([[isDelivery, { status: 401 }]]));
    await initCam(t, { token: 'tok-1' });
    await t.fireTimer();
    await t.retry();
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { token: 'tok-2' });
    await t.imgLoad();
  },
  async delivery_unauthorized_on_an_open_instance(t) {
    t.answerWith(answers([[isDelivery, { status: 401 }]]));
    await initCam(t);
  },

  // ── MJPEG ──
  async mjpeg_plays_fails_and_recovers(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { token: 'a b&c' });
    await t.imgLoad();
    await t.imgError();
    await t.fireTimer();
    await t.imgLoad();
  },
  async mjpeg_without_a_token(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.imgError();
    await t.imgError();
  },
  async mjpeg_events_after_teardown_or_in_another_mode(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.pagehide();
    await t.imgError();
    await t.imgLoad();
    t.answerWith(answers([]));
    await t.retry();
    await t.imgLoad();
    await t.imgError();
  },

  // ── HLS ──
  async hls_through_hls_js(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { token: 'tok' });
    await t.hlsLib('ok');
    await t.videoPlaying();
    await t.pagehide();
  },
  async hls_library_fails_then_loads_on_the_retry(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('error');
    await t.fireTimer();
    await t.hlsLib('ok');
    await t.fireTimer();
  },
  async hls_library_without_hls(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('loaded-without-Hls');
  },
  async hls_library_already_loaded(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('ok');
    await t.hlsError({ fatal: true, details: 'manifestLoadError' });
    await t.fireTimer();
  },
  async hls_not_supported(t) {
    t.setHlsSupported(false);
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('ok');
    await t.retry();
  },
  async hls_fatal_unauthorized_asks_for_a_token(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { token: 'tok' });
    await t.hlsLib('ok');
    await t.hlsError({ fatal: true, details: 'fragLoadError', response: { code: 401 } });
  },
  async hls_errors_that_do_not_reconnect(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { token: 'tok' });
    await t.hlsLib('ok');
    await t.hlsError(null);
    await t.hlsError({ fatal: false, details: 'bufferStalledError', response: { code: 401 } });
    await t.videoError();
  },
  async hls_fatal_error_names(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('ok');
    await t.hlsError({ fatal: true, type: 'networkError', response: { code: 404 } });
    await t.fireTimer();
    await t.hlsError({ fatal: true });
    await t.fireTimer();
    await t.hlsError({ fatal: true, details: 'x' }, 1);
  },
  async hls_fatal_error_while_stopped(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { token: 'tok' });
    await t.hlsLib('ok');
    await t.init({ config: { stream_id: 'cam' }, ext_token: 'tok', state: { [LIST]: list(row('cam', { status: 'offline' })) } });
    await t.hlsError({ fatal: true, details: 'late', response: { code: 401 } });
  },
  async hls_switch_while_the_library_loads(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.state(SEL('front'), 'door');
    await t.hlsLib('ok');
    await t.retry();
  },
  async hls_destroy_throws(t) {
    t.setHlsDestroyThrows(true);
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('ok');
    await t.pagehide();
  },
  async hls_native_on_a_phone(t) {
    t.answerWith(answers([mode('hls')]));
    const t2 = t; // the tile was built with no Media Source Extensions
    t2.setNativeHls('maybe');
    t2.refusePlay(true);
    await initCam(t2, { token: 'tok' });
    await t2.videoPlaying();
    t2.setVideoError({ code: 4 });
    await t2.videoError();
    await t2.fireTimer();
    t2.setVideoError(null);
    await t2.videoError();
  },
  async hls_native_cannot_play(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
  },
  async hls_media_source_without_is_type_supported(t) {
    t.answerWith(answers([mode('hls')]));
    t.setNativeHls('probably');
    await initCam(t);
  },
  async video_events_outside_hls(t) {
    await webrtcPlaying(t);
    await t.videoPlaying();
    await t.videoError();
  },

  // ── reconnecting and reporting ──
  async reconnect_backoff_and_one_report_per_burst(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t, { token: 'tok' });
    for (let i = 0; i < 6; i += 1) await t.fireTimer();
  },
  async a_picture_lets_the_same_fault_be_reported_again(t) {
    let refuse = true;
    t.answerWith(answers([[isDelivery, () => (refuse ? { status: 502 } : { status: 200, json: { mode: 'mjpeg' } })]]));
    await initCam(t);
    refuse = false;
    await t.fireTimer();
    await t.imgLoad();
    refuse = true;
    await t.retry();
  },
  async a_report_that_fails_is_ignored(t) {
    t.answerWith(answers([
      [isDelivery, { status: 500 }],
      [(m, u) => u.endsWith('/report'), { networkError: 'down' }],
    ]));
    await initCam(t);
  },
  async a_reinit_cancels_the_pending_retry(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t);
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { token: 'new' });
    await t.fireTimer();
  },

  async a_switch_during_the_backoff_cancels_the_retry(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    t.answerWith(answers([mode('mjpeg')]));
    await t.state(SEL('front'), 'door');
    await t.fireTimer();
  },
  async hidden_during_the_backoff(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t);
    await t.hide();
    await t.fireTimer();
  },
  async blocked_during_the_backoff(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t);
    await t.state(LIST, list(row('cam', { status: 'offline', detail: 'Off.' })));
    await t.fireTimer();
  },
  async teardown_survives_a_peer_and_a_video_that_throw(t) {
    t.failPeer('close', 'already closed');
    t.setLoadThrows(true);
    await initCam(t);
    await t.pagehide();
    t.answerWith(answers([mode('hls')]));
    t.setNativeHls('maybe');
    await t.retry();
    await t.pagehide();
  },

  // ── Retry ──
  async retry_with_no_stream(t) {
    await t.init({});
    await t.retry();
  },
  async retry_cancels_the_timer_and_starts_over(t) {
    t.answerWith(answers([[isDelivery, { status: 500 }]]));
    await initCam(t);
    t.answerWith(answers([mode('mjpeg')]));
    await t.retry();
  },
  async retry_on_a_blocked_source_says_why_again(t) {
    await initCam(t, { state: { [LIST]: list(row('cam', { status: 'offline', detail: 'Cam is off.' })) } });
    await t.retry();
  },
  async retry_after_a_pause_clears_it(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.hide();
    await t.retry();
    await t.reveal();
  },

  // ── visibility ──
  async hidden_then_shown(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.imgLoad();
    await t.hide();
    await t.reveal();
  },
  async hidden_when_not_playing(t) {
    await t.init({});
    await t.hide();
    await t.reveal();
  },
  async shown_without_reconnect_on_idle(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { reconnect_on_idle: false } });
    await t.hide();
    await t.reveal();
  },
  async shown_on_a_source_that_became_blocked(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.hide();
    await t.state(LIST, list(row('cam', { status: 'disabled', detail: 'Turned off.' })));
    await t.reveal();
  },
  async shown_twice(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.hide();
    await t.hide();
    await t.reveal();
    await t.reveal();
  },

  // ── what the stream list says about the source ──
  async list_rows_that_cannot_play(t) {
    await initCam(t, { state: { [LIST]: list({ id: 'cam', label: 'Cam', status: 'needs_setup', detail: '  Set it up.  ' }) } });
    await t.state(LIST, list({ id: 'cam', label: 'Cam' }));
    await t.state(LIST, list(row('cam', { status: 'offline', detail: '   ' })));
    await t.state(LIST, list(row('cam', { status: 5, detail: 7 })));
    await t.state(LIST, list(row('cam', { value: null })));
    await t.state(LIST, list(row('cam', { status: ' ready ' })));
    await t.state(LIST, list({ id: 'cam', status: 'ready' }));
    await t.state(LIST, list({ id: 'cam', value: null }));
  },
  async list_unreadable_or_not_a_list(t) {
    await initCam(t, { state: { [LIST]: '{not json' } });
    await t.state(LIST, JSON.stringify({ cam: 1 }));
    await t.state(LIST, list(null, 3, row('cam', { label: '' })));
    await t.state(LIST, null);
  },
  async list_that_never_named_the_source(t) {
    await initCam(t, { state: { [LIST]: list(row('other')) } });
    await t.state(LIST, list(row('cam')));
    await t.state(LIST, list(row('other')));
    await t.state(LIST, list(row('cam')));
  },
  async list_changes_the_kind_under_a_stable_id(t) {
    await initCam(t);
    t.answerWith(answers([mode('mjpeg')]));
    await t.state(LIST, list(row('cam', { mode: 'mjpeg' })));
    await t.state(LIST, list(row('cam', { mode: 'mjpeg', label: 'Renamed' })));
    await t.state(LIST, list(row('cam', { mode: '' })));
  },
  async list_kind_change_while_not_playing(t) {
    await initCam(t, { state: { [LIST]: list(row('cam', { status: 'offline' })) } });
    await t.state(LIST, list(row('cam', { status: 'offline', mode: 'mjpeg' })));
  },
  async list_block_reason_changes_and_clears(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.state(LIST, list(row('cam', { status: 'offline', detail: 'Off.' })));
    await t.state(LIST, list(row('cam', { status: 'offline', detail: 'Off.' })));
    await t.state(LIST, list(row('cam', { status: 'offline', detail: 'Unplugged.' })));
    await t.state(LIST, list(row('cam')));
  },
  async a_row_with_an_empty_id_while_no_stream_is_chosen(t) {
    await t.init({ config: {} });
    await t.state(LIST, list({ id: '', label: 'Blank', status: 'offline', detail: 'Blank is off.' }));
    await t.state(LIST, list({ id: '', value: '', label: 'Blank' }));
    await t.hide();
    await t.reveal();
  },
  async list_before_any_stream_is_chosen(t) {
    await t.init({ config: {} });
    await t.state(LIST, list(row('cam')));
    await t.state(LIST, list(row('cam', { status: 'offline' })));
  },

  // ── the plugin's own key for the stream ──
  async state_keys_that_are_not_ours(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.state(KEY('other'), null);
    await t.state(KEY('cam'), 'idle');
    await t.state(KEY('cam'), '');
    await t.state(KEY('cam'), 0);
    await t.state(KEY('cam'), false);
    await t.state('plugin.video_panel.unrelated', null);
  },
  async our_key_goes_while_no_stream_is_chosen(t) {
    await t.init({});
    await t.state(KEY(''), null);
  },
  async a_new_stream_after_our_key_went_is_not_blocked(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.state(KEY('cam'), null);
    await t.init({ config: { stream_id: 'door' }, state: { [LIST]: list(row('door')) } });
  },
  async retry_re_reads_the_reason_from_the_list(t) {
    await initCam(t, { state: { [LIST]: list(row('cam', { status: 'offline', detail: 'Cam is off.' })) } });
    await t.state(KEY('cam'), null);
    await t.retry();
  },
  async our_key_goes_and_the_list_is_unreadable(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.state(KEY('cam'), null);
    await t.state(LIST, '{broken');
    await t.state(LIST, list(row('cam')));
  },

  // ── channels ──
  async channel_selection_in_the_snapshot_wins(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({
      config: { stream_id: 'cam', channel: ' front ', show_label: true },
      state: { [LIST]: list(row('cam'), row('door')), [SEL('front')]: ' door ' },
    });
  },
  async channel_selection_blank_or_not_text(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({
      config: { stream_id: 'cam', channel: 'front' },
      state: { [LIST]: list(row('cam'), row('door')), [SEL('front')]: '  ' },
    });
    await t.init({
      config: { stream_id: 'cam', channel: 'front' },
      state: { [LIST]: list(row('cam'), row('door')), [SEL('front')]: 7 },
    });
  },
  async channel_selection_changes(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({
      config: { stream_id: 'cam', channel: 'front', show_label: true },
      state: { [LIST]: list(row('cam'), row('door'), row('yard', { status: 'offline', detail: 'Yard is off.' })) },
    });
    await t.state(SEL('front'), ' door ');
    await t.state(SEL('front'), 'door');
    await t.state(SEL('front'), 'yard');
    await t.state(SEL('front'), '');
    await t.state(SEL('front'), 42);
    await t.state(SEL('back'), 'door');
  },
  async a_switch_to_another_kind_then_the_same_list(t) {
    const rows = list(row('cam'), row('door', { mode: 'mjpeg' }));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: rows } });
    t.answerWith(answers([mode('mjpeg')]));
    await t.state(SEL('front'), 'door');
    await t.state(LIST, rows);
  },
  async channel_cleared_with_no_fallback(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({ config: { channel: 'front' }, state: { [LIST]: list(row('door')), [SEL('front')]: 'door' } });
    await t.state(SEL('front'), null);
    await t.retry();
  },
  async unbound_tile_ignores_selection_keys(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.state(SEL(''), 'door');
    await t.state('plugin.video_panel.selection.', 'door');
  },

  async reinit_while_hls_plays(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t);
    await t.hlsLib('ok');
    await t.videoPlaying();
    t.hold(isDelivery);
    await initCam(t);
    await t.videoPlaying();
    await t.release({ status: 200, json: { mode: 'hls' } });
    await t.pagehide();
  },
  async a_selection_key_named_by_no_channel(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({ config: { stream_id: 'cam' }, state: { [LIST]: list(row('cam'), row('door')), '': 'door' } });
  },
  async init_to_no_stream_after_playing(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.init({ config: {} });
    await t.hide();
  },
  async a_switch_to_a_source_the_list_has_not_named(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { channel: 'front' } });
    await t.state(SEL('front'), 'door');
  },
  async a_switch_forgets_the_last_mode(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.imgLoad();
    t.hold(isDelivery);
    await t.state(SEL('front'), 'door');
    await t.imgLoad();
  },
  async a_blocked_plan_answered_after_a_stop(t) {
    t.hold(isDelivery);
    await initCam(t);
    await t.pagehide();
    await t.release({ status: 200, json: { mode: 'blocked' } });
  },
  async a_retry_while_playing_finds_the_plan_blocked(t) {
    await webrtcPlaying(t);
    t.answerWith(answers([mode('blocked')]));
    await t.retry();
  },
  async hls_switch_while_the_library_fails(t) {
    t.answerWith(answers([mode('hls')]));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')) } });
    await t.state(SEL('front'), 'door');
    await t.hlsLib('error');
  },
  async a_playable_list_while_hidden(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t);
    await t.hide();
    await t.state(LIST, list(row('cam', { label: 'Again' })));
    await t.reveal();
  },
  async a_selection_cleared_while_hidden(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({ config: { channel: 'front' }, state: { [LIST]: list(row('door')), [SEL('front')]: 'door' } });
    await t.hide();
    await t.state(SEL('front'), '');
    await t.reveal();
    await t.hide();
  },
  async a_selection_change_while_hidden(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { channel: 'front' }, state: { [LIST]: list(row('cam'), row('door')), [SEL('front')]: 'door' } });
    await t.hide();
    await t.state(SEL('front'), '');
    await t.reveal();
    await t.hide();
  },
  async webrtc_session_ids_that_need_escaping(t) {
    t.answerWith(answers([[isWhepPost, { status: 201, location: 'http://x/a/sess 1?x=1', text: 'a' }]]));
    await initCam(t);
    await t.candidate(0);
    await t.pagehide();
    t.answerWith(answers([[isWhepPost, { status: 201, location: '/', text: 'a' }]]));
    await t.retry();
    await t.candidate(1);
    await t.pagehide();
  },

  // ── re-init ──
  async reinit_with_another_stream_stops_the_first(t) {
    await webrtcPlaying(t, { token: 'tok' });
    t.answerWith(answers([mode('mjpeg')]));
    await t.init({ config: { stream_id: 'door' }, ext_token: 'tok', state: { [LIST]: list(row('door')) } });
  },
  async reinit_same_stream_keeps_its_session(t) {
    await webrtcPlaying(t);
    await initCam(t);
  },
  async reinit_without_a_list_keeps_the_cached_one(t) {
    t.answerWith(answers([mode('mjpeg')]));
    await initCam(t, { config: { show_label: true }, state: { [LIST]: list(row('cam', { label: 'Front' })) } });
    await t.init({ config: { stream_id: 'cam', show_label: true }, state: {} });
    await t.init({ config: { stream_id: 'cam', show_label: true } });
  },
};

(async () => {
  const out = {};
  for (const [name, run] of Object.entries(scenarios)) {
    if (ONLY.length && !ONLY.includes(name)) continue;
    const options = {};
    // These play HLS the way a phone does: no Media Source Extensions.
    if (name.startsWith('hls_native') || name.startsWith('teardown_survives')) options.mse = false;
    if (name === 'hls_media_source_without_is_type_supported') options.mse = 'no-isTypeSupported';
    const t = newTile(JS_PATH, HTML_PATH, options);
    try {
      await run(t);
    } catch (err) {
      t.say('scenario threw ' + (err && err.stack));
    }
    out[name] = t.log;
    t.close();
  }
  process.stdout.write(JSON.stringify(out, null, 1));
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
