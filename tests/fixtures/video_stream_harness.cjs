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

// Every window this run opened, closed before we exit (see newTile).
const windows = [];

// One tile, freshly loaded, with the delivery call answered.
function newTile() {
  const dom = new JSDOM(HTML, { url: PAGE_URL, runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  // A scenario that ends mid-reconnect leaves the element's backoff timer
  // armed, and a live jsdom timer keeps node's event loop from draining -- the
  // harness then produces its JSON and hangs forever instead of exiting.
  windows.push(win);
  const calls = [];
  // What the delivery call answers with. Mutable so a scenario can expire the
  // token mid-session, which is the whole point of the ext-token tests: the
  // panel keeps running and the credential stops working underneath it.
  let deliveryStatus = 200;
  win.fetch = (url, opts) => {
    const headers = (opts && opts.headers) || {};
    calls.push({ url: String(url), method: (opts && opts.method) || 'GET', headers });
    if (deliveryStatus !== 200) {
      return Promise.resolve({
        ok: false,
        status: deliveryStatus,
        json: () => Promise.reject(new Error('no body')),
      });
    }
    // MJPEG needs no RTCPeerConnection, which jsdom does not have; every
    // branch this harness cares about is upstream of how the picture arrives.
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ mode: 'mjpeg' }),
    });
  };
  // What the element says back to the panel. jsdom gives an unframed window
  // `parent === window`, so the element's window.parent.postMessage lands here.
  const posted = [];
  win.postMessage = (msg) => { posted.push(msg); };
  win.eval(SOURCE);

  const doc = win.document;
  const tile = {
    win,
    calls,
    posted,
    setDeliveryStatus: (code) => { deliveryStatus = code; },
    asks: () => posted.filter((m) => m && m.type === 'openavc:request-init').length,
    lastDeliveryToken: () => {
      const delivery = calls.filter((c) => c.url.includes('/ext/delivery/'));
      const last = delivery[delivery.length - 1];
      return last ? last.headers['X-OpenAVC-Plugin-Token'] : undefined;
    },
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

// A tile on a claimed instance: the panel minted an ext token for it. Its TTL
// is hours and a wall panel runs for days, so this is the normal case, not an
// edge one.
async function playingWithToken(extToken) {
  const tile = newTile();
  tile.send({
    type: 'openavc:init',
    config: { stream_id: 'front_door' },
    ext_token: extToken,
    state: { [LIST_KEY]: list([ROW]), [KEY]: 'streaming' },
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

scenarios.an_expired_token_is_replaced_without_a_reload = async () => {
  const tile = await playingWithToken('first-token');
  const before = { deliveries: tile.deliveries(), token: tile.lastDeliveryToken() };

  // Hours pass. The token's TTL runs out and every /ext/ call starts refusing.
  tile.setDeliveryStatus(401);
  tile.win.document.getElementById('retry').click();
  await tile.settle();
  await tile.settle();
  const asked = tile.asks();

  // The panel answers request-init by re-running its own sendInit, which mints
  // a fresh token and re-sends the whole init message.
  tile.setDeliveryStatus(200);
  tile.send({
    type: 'openavc:init',
    config: { stream_id: 'front_door' },
    ext_token: 'second-token',
    state: { [LIST_KEY]: list([ROW]), [KEY]: 'streaming' },
  });
  await tile.settle();
  await tile.settle();

  return {
    before,
    asked,
    after: {
      deliveries: tile.deliveries(),
      token: tile.lastDeliveryToken(),
      overlay: tile.overlay(),
    },
  };
};

scenarios.a_token_is_only_ever_asked_about_once = async () => {
  const tile = await playingWithToken('first-token');
  tile.setDeliveryStatus(401);
  // Several failures against the same dead token: the backoff re-fires, and a
  // request per failure would be a loop against the host.
  for (let i = 0; i < 4; i += 1) {
    tile.win.document.getElementById('retry').click();
    await tile.settle();
    await tile.settle();
  }
  return { asks: tile.asks() };
};

scenarios.an_open_instance_never_asks = async () => {
  // No token was ever issued, so a 401 is not about one. Asking would put a
  // refresh loop in front of whatever the real fault is.
  const tile = await playing();
  tile.setDeliveryStatus(401);
  tile.win.document.getElementById('retry').click();
  await tile.settle();
  await tile.settle();
  return { asks: tile.asks() };
};

scenarios.a_dead_mjpeg_connection_asks_once = async () => {
  // An <img> reports no status code, so an expired token looks exactly like an
  // unplugged encoder. Ask once per token; the answer settles which it was.
  const tile = await playingWithToken('first-token');
  const img = tile.win.document.getElementById('mjpeg');
  for (let i = 0; i < 3; i += 1) {
    img.dispatchEvent(new tile.win.Event('error'));
    await tile.settle();
  }
  return { asks: tile.asks() };
};

(async () => {
  const out = {};
  for (const [name, run] of Object.entries(scenarios)) {
    out[name] = await run();
  }
  for (const win of windows) win.close();
  process.stdout.write(JSON.stringify(out, null, 2));
})().catch((err) => {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
