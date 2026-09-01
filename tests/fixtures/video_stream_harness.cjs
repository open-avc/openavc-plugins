// Runs the real integrations/video_panel/panel/video_stream.js inside a jsdom
// window and reports what the tile did, as JSON on stdout.
//
// The panel element is vanilla browser JS with no build step, so the only
// honest way to test it is to run it. Everything it reaches for outside the
// DOM -- the delivery call, the MJPEG connection -- is stubbed here, because
// what is under test is which overlay the element shows and whether it tries
// to play again, not how it plays.
//
// Usage: node video_stream_harness.cjs <video_stream.js> <video_stream.html>
// NODE_PATH must point at a node_modules holding jsdom.

const fs = require('fs');
const { JSDOM } = require('jsdom');

const [, , JS_PATH, HTML_PATH] = process.argv;
const SOURCE = fs.readFileSync(JS_PATH, 'utf8');
const HTML = fs.readFileSync(HTML_PATH, 'utf8');

const PAGE_URL = 'http://panel.test/api/plugins/video_panel/panel/video_stream.html';
const KEY = 'plugin.video_panel.streams.front_door';
const LIST_KEY = 'plugin.video_panel.stream_ids';
const ROW = { value: 'front_door', label: 'Front Door', mode: 'webrtc' };

// One tile, freshly loaded, with the delivery call answered.
function newTile() {
  const dom = new JSDOM(HTML, { url: PAGE_URL, runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  const calls = [];
  win.fetch = (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    // MJPEG needs no RTCPeerConnection, which jsdom does not have; every
    // branch this harness cares about is upstream of how the picture arrives.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ mode: 'mjpeg' }),
    });
  };
  win.eval(SOURCE);

  const doc = win.document;
  const tile = {
    win,
    calls,
    // Everything the viewer can actually see.
    overlay: () => ({
      hidden: doc.getElementById('status').hidden,
      text: doc.getElementById('statusText').textContent,
      spinner: !doc.getElementById('spinner').hidden,
      retry: !doc.getElementById('retry').hidden,
    }),
    deliveries: () => calls.filter((c) => c.url.includes('/ext/delivery/')).length,
    send: (msg) => {
      // Built by hand rather than via win.postMessage: the element ignores any
      // message whose source is not its parent, and jsdom's postMessage does
      // not set one.
      win.dispatchEvent(new win.MessageEvent('message', { data: msg, source: win.parent }));
    },
    settle: () => new Promise((r) => win.setTimeout(r, 0)),
  };
  return tile;
}

function list(rows) {
  return JSON.stringify(rows);
}

async function playing(extraState) {
  const tile = newTile();
  tile.send({
    type: 'openavc:init',
    config: { stream_id: 'front_door' },
    state: { [LIST_KEY]: list([ROW]), [KEY]: 'streaming', ...(extraState || {}) },
  });
  await tile.settle();
  await tile.settle();
  return tile;
}

// The keys a plugin teardown deletes, and the two orders they can arrive in:
// PluginRegistry.cleanup walks a Python set, and the WS hub batches the whole
// teardown into one state.delete, so the order is whatever the set iterates.
const TEARDOWN_ORDERS = {
  status_key_first: [KEY, LIST_KEY],
  list_key_first: [LIST_KEY, KEY],
};

const scenarios = {};

for (const [name, keys] of Object.entries(TEARDOWN_ORDERS)) {
  scenarios['plugin_restart_' + name] = async () => {
    const tile = await playing();
    const before = { overlay: tile.overlay(), deliveries: tile.deliveries() };

    // Plugin stops: every key it set is deleted, and the panel forwards each
    // deletion to us as value=null.
    for (const key of keys) tile.send({ type: 'openavc:state', key, value: null });
    await tile.settle();
    const stopped = { overlay: tile.overlay(), deliveries: tile.deliveries() };

    // Plugin starts again and republishes: the list first, then the per-stream
    // status key (_publish_streams publishes them in that order).
    tile.send({ type: 'openavc:state', key: LIST_KEY, value: list([ROW]) });
    tile.send({ type: 'openavc:state', key: KEY, value: 'idle' });
    await tile.settle();
    await tile.settle();
    return { before, stopped, back: { overlay: tile.overlay(), deliveries: tile.deliveries() } };
  };
}

scenarios.deleted_stream_stays_gone = async () => {
  const tile = await playing();
  const before = { deliveries: tile.deliveries() };
  // DELETE /streams/<id> sets the status key to None, then republishes a list
  // that no longer mentions the stream.
  tile.send({ type: 'openavc:state', key: KEY, value: null });
  tile.send({ type: 'openavc:state', key: LIST_KEY, value: list([]) });
  await tile.settle();
  await tile.settle();
  return { before, after: { overlay: tile.overlay(), deliveries: tile.deliveries() } };
};

scenarios.retry_after_the_key_goes = async () => {
  const tile = await playing();
  tile.send({ type: 'openavc:state', key: KEY, value: null });
  await tile.settle();
  const blocked = tile.overlay();
  // The button is offered, and pressing it while the plugin is still down must
  // say the same thing rather than spin: a Retry that lies is worse than none.
  tile.win.document.getElementById('retry').click();
  await tile.settle();
  await tile.settle();
  return { blocked, pressed: { overlay: tile.overlay(), deliveries: tile.deliveries() } };
};

scenarios.a_source_that_is_still_offline_keeps_its_own_sentence = async () => {
  // The list-derived reason must outrank the generic one: a republished row
  // that says why it cannot play still says why.
  const tile = await playing();
  tile.send({ type: 'openavc:state', key: KEY, value: null });
  await tile.settle();
  tile.send({
    type: 'openavc:state',
    key: LIST_KEY,
    value: list([{ ...ROW, status: 'offline', detail: 'Front Door is switched off.' }]),
  });
  await tile.settle();
  await tile.settle();
  return { overlay: tile.overlay(), deliveries: tile.deliveries() };
};

(async () => {
  const out = {};
  for (const [name, run] of Object.entries(scenarios)) {
    out[name] = await run();
  }
  process.stdout.write(JSON.stringify(out, null, 2));
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
