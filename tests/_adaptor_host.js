// A fake of the engine's synchronous `host` (docs/CUSTOM_ADAPTOR.md, `ascend adaptor spec`), for
// running a generated adaptor on this machine before the engine sees it. Reads one JSON scenario
// on stdin, runs the adaptor in a fresh V8 context exactly as a plain script, prints what came back
// and every host call it made. No network: every response is canned in the scenario.
//
//   {"source": "<adaptor js>", "config": {"url": ..., "api_key": ...}, "turn": {...},
//    "entry": "sendTurn" | "checkReachability",
//    "scenario": {"http": [{"url_contains", "method", "body_contains", "status", "body", "once", "throw"}],
//                 "ws": {"frames": [...], "fail": "..."}}}
"use strict";
const vm = require("vm");
const fs = require("fs");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const calls = [];
const rules = (input.scenario && input.scenario.http) || [];
const state = new Map();
let clock = 1700000000000;
let uuids = 0;

function matchRule(url, opts) {
  for (const r of rules) {
    if (r.used) continue;
    if (r.url_contains && !String(url).includes(r.url_contains)) continue;
    if (r.method && r.method !== (opts.method || "GET")) continue;
    if (r.body_contains && !JSON.stringify(opts.body === undefined ? "" : opts.body).includes(r.body_contains)) continue;
    if (r.once) r.used = true;
    return r;
  }
  return null;
}

const host = {
  log: (level, msg, extra) => { calls.push({ fn: "log", level, msg }); },
  sleep: (ms) => { clock += ms; calls.push({ fn: "sleep", ms }); },
  now: () => clock,
  uuid: () => "00000000-0000-4000-8000-0000000000" + String(++uuids).padStart(2, "0"),
  state: {
    get: (slot) => (state.has(slot) ? state.get(slot) : null),
    set: (slot, value) => { state.set(slot, value); },
    merge: (slot, value) => { const cur = Object.assign({}, state.get(slot) || {}, value); state.set(slot, cur); return cur; },
    getOrMint: (slot, opts, mint) => {
      if (state.has(slot) && state.get(slot) !== null) return state.get(slot);
      const v = mint();
      if (v !== null && v !== undefined) state.set(slot, v);
      return v;
    },
  },
  http: {
    request: (url, opts) => {
      opts = opts || {};
      calls.push({ fn: "http.request", url, method: opts.method || "GET", body: opts.body === undefined ? null : opts.body,
                   headers: opts.headers || {}, timeoutMs: opts.timeoutMs });
      const r = matchRule(url, opts);
      if (!r) throw new Error("no canned response for " + (opts.method || "GET") + " " + url);
      if (r.throw) throw new Error(r.throw);
      return { status: r.status || 200, headers: r.headers || {},
               body: typeof r.body === "string" ? r.body : JSON.stringify(r.body === undefined ? {} : r.body),
               url, location: null };
    },
    client: () => ({ request: (u, o) => host.http.request(u, o) }),
  },
  ws: {
    connect: (url, opts) => {
      calls.push({ fn: "ws.connect", url, headers: (opts && opts.headers) || {}, recvTimeoutMs: opts && opts.recvTimeoutMs });
      const wsc = (input.scenario && input.scenario.ws) || {};
      if (wsc.fail) throw new Error(wsc.fail);
      const frames = (wsc.frames || []).slice();
      return {
        handle: "h1", reused: false,
        send: (frame) => { calls.push({ fn: "ws.send", frame }); },
        recv: (o) => {
          calls.push({ fn: "ws.recv", timeoutMs: o && o.timeoutMs });
          const f = frames.shift();
          if (f === undefined) return { type: "timeout", json: null };
          if (typeof f === "string") return { type: "text", json: null, raw: f };
          if (f.type && f.type !== "text") return f;
          return { type: "text", json: f.json === undefined ? null : f.json,
                   raw: f.raw || (f.json !== undefined ? JSON.stringify(f.json) : "") };
        },
        close: () => { calls.push({ fn: "ws.close" }); },
      };
    },
  },
  lock: { acquire: () => ({ acquired: true }), release: () => {} },
  config: {
    get: (field) => (input.config || {})[field],
    endpoint: () => (input.config || {}).url || "",
    apiKey: () => (input.config || {}).api_key || null,
  },
  parallel: (ops) => ops.map((op) => {
    try {
      if (op.fn === "sleep") { host.sleep(op.args.ms); return { ok: true, v: null }; }
      return { ok: true, v: host.http.request(op.args.url, op.args) };
    } catch (e) { return { ok: false, err: String(e) }; }
  }),
  parseHtml: () => null,
  stripHtml: (html) => String(html).replace(/<[^>]+>/g, ""),
  regex: (pattern, flags, subject) => { const m = new RegExp(pattern, flags).exec(subject); return m ? { match: m[0], groups: m.slice(1) } : null; },
  b64decode: (s) => Buffer.from(s, "base64").toString("utf8"),
};

const sandbox = {};
vm.createContext(sandbox);
let reply = null;
let error = null;
try {
  vm.runInContext(input.source, sandbox, { filename: "adaptor.js" });
  const entry = input.entry === "checkReachability" ? sandbox.checkReachability : sandbox.sendTurn;
  if (typeof entry !== "function") throw new Error("no " + (input.entry || "sendTurn") + " in the adaptor");
  reply = entry(input.turn || { payload: {}, headers: {}, params: {} }, host);
} catch (e) {
  error = String((e && e.stack) || e);
}
process.stdout.write(JSON.stringify({ reply, error, calls, state: Object.fromEntries(state),
                                      defines_preflight: typeof sandbox.checkReachability === "function" }));
