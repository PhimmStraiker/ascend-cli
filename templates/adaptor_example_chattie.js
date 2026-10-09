// @ts-check
// The SIMPLE END of the range, worked end to end.
//
// chattie is a plain JSON POST - one request, no login, no session - and Ascend can drive
// it with configuration alone (`request_template` IS the body; the demo app points at this
// same target with no code at all). That is exactly why it is worth having as an example:
// an adaptor has to cover the simple case too. Once an application carries an adaptor, the
// adaptor is what drives it, and "the target is easy" is not a reason for the file to be
// absent or clever.
//
// So this is what thin looks like, and what to write when `ascend adaptor har` shows no
// session chains: read the prompt, make the call, return the reply text, and fail loudly on
// anything else. Do not add steps a HAR did not show.
//
//     ascend adaptor scaffold --example --out chattie.js
//
// THE OTHER END, for contrast - shapes real targets have, none expressible as configuration:
//
//   - mint a token, open a conversation, send, then POLL a separate endpoint until the
//     bot's reply appears
//   - POST the question and read an SSE stream CONCURRENTLY - the stream only carries the
//     answer while the question is in flight, so they must overlap
//   - a WebSocket whose handshake is mandatory, where "complete" ends the turn but not the
//     connection, and the socket carries the whole conversation
//
// CONFIGURATION, in the Application's request_template:
//   _adaptor_endpoint     the target, only for an app with no URL of its own
//   _adaptor_user_role    optional; chattie varies its answers by role (default public)
//
// UNPREFIXED, and that is not a style choice. `_adaptor_<slug>_endpoint` is read only when
// the adaptor is a v0 ALIAS, because the slug comes from the alias; inline source has no
// slug, so it reads the bare keys and a namespaced one is silently ignored - the adaptor
// gets no address and fails far from the typo. The app carrying the source is the
// namespace.

/** @typedef {import("./host").Host} Host */
/** @typedef {import("./host").Turn} Turn */

/**
 * The prompt, wherever this app's template happens to put it. An adaptor outlives the
 * template it was written against, so it should not assume one key.
 * @param {Turn} turn
 * @returns {string}
 */
function promptOf(turn) {
  const p = turn.payload || {};
  for (const k of ["message", "prompt", "text", "q", "query", "input"]) {
    if (p[k]) return typeof p[k] === "string" ? p[k] : String(p[k]);
  }
  return JSON.stringify(p);
}

/**
 * @param {Turn} turn
 * @param {Host} host
 * @returns {import("./host").Reply}
 */
function sendTurn(turn, host) {
  const params = turn.params || {};
  const prompt = promptOf(turn);

  // Conversation-scoped, so it survives the isolate being destroyed between turns and is
  // gone when the conversation ends. A module variable here would be silently empty.
  const n = Number(host.state.get("turn_index") || 0) + 1;
  host.state.set("turn_index", String(n));

  const headers = { "Content-Type": "application/json" };
  const key = host.config.apiKey();
  if (key) headers["Authorization"] = "Bearer " + key;

  const res = host.http.request(host.config.endpoint(), {
    method: "POST",
    headers: headers,
    body: {
      prompt: prompt,
      user_name: "straiker",
      user_role: String(params.user_role || "public"),
    },
    timeoutMs: Number(params.timeout_ms) || 60000,
  });

  if (res.status >= 400) {
    return { status_code: res.status, body: { error: "target returned " + res.status,
                                              detail: String(res.body).slice(0, 400) } };
  }

  let parsed;
  try {
    parsed = JSON.parse(res.body);
  } catch (e) {
    // A target that stops returning JSON is a real failure, and the first 300 characters
    // are what tells you whether it is an HTML error page or a truncated stream.
    return { status_code: 502, body: { error: "target did not return JSON",
                                       preview: String(res.body).slice(0, 300) } };
  }

  const data = parsed.data || {};
  const text = data.response;
  if (typeof text !== "string" || !text) {
    // Scored by a detector downstream, so an empty answer must not look like a quiet pass.
    return { status_code: 502, body: { error: "no reply text at data.response",
                                       keys: Object.keys(data).join(",") } };
  }

  host.log("info", "chattie turn complete", { turn: n, chars: text.length });
  // The body shape matches this application's response_template, {"response": "{{ RESPONSE }}"},
  // and the Content-Type is what makes Ascend APPLY that template: without it the reply is
  // stringified and the detector scores `{'response': '...'}` instead of the answer.
  // Newer hosts default this for a structured body; setting it is explicit and always works.
  return {
    status_code: 200,
    body: { response: text },
    headers: { "Content-Type": "application/json" },
    _diag: { turn: n },
  };
}
