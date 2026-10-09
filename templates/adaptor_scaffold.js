// @ts-check
// THE ONBOARDING ADAPTOR. Its job is to get the Application created, and then be replaced.
//
// Ascend disables Save until "Test connection" goes green, and for an adaptor-driven app
// that test runs THROUGH the adaptor. So you cannot create the app without an adaptor, and
// you cannot iterate on an adaptor without an app to pin the target to - `ascend adaptor test`
// takes the address from an application you own, never from the request. This file is the way
// out of that circle, and it is also the file you start editing.
//
//     ascend adaptor scaffold --out my_adaptor.js     # this file
//     ascend adaptor spec --out host.d.ts             # the host surface it is typed against
//
// WHAT IT REPORTS, and why it is not a rubber stamp. It POSTs the rendered template to the
// endpoint once and reports 200 when the target ANSWERED - whatever it answered. A 401, a
// 404, an HTML login page: all 200 here, all with the body in the reply, because at
// onboarding "your target is reachable and here is exactly what it said" is both true and
// the most useful thing anyone can tell you. That reply is the spec for the adaptor you are
// about to write. The only failure is a target that could not be reached at all, which is a
// wrong URL or a wrong network - the one thing worth blocking an app over.
//
// It does NOT claim the integration works. Nothing can claim that yet. The check that does
// is `ascend adaptor verify`, which runs what is STORED on the app.
//
// CONFIGURATION, in the Application's request_template:
//   _adaptor_endpoint     the target, only for an app with no URL of its own (an app's URL
//                         is its adaptor's address; inline source reads this bare key only)
//   _adaptor_timeout_ms   per-request deadline (default 30000)

/** @typedef {import("./host").Host} Host */
/** @typedef {import("./host").Turn} Turn */

/**
 * @param {unknown} body
 * @returns {string}
 */
function preview(body) {
  const s = typeof body === "string" ? body : JSON.stringify(body);
  if (!s) return "";
  return s.length > 600 ? s.slice(0, 600) + "... [truncated]" : s;
}

/**
 * @param {Turn} turn
 * @param {Host} host
 * @returns {import("./host").Reply}
 */
function sendTurn(turn, host) {
  const url = host.config.endpoint();
  // `|| {}` on both: a turn built by hand (the debugger, a test) may carry neither, and
  // an adaptor that assumes they exist dies with a TypeError instead of running.
  const params = turn.params || {};
  const timeoutMs = Number(params.timeout_ms) || 30000;

  const headers = { "Content-Type": "application/json" };
  for (const k in turn.headers || {}) headers[k] = turn.headers[k];

  const key = host.config.apiKey();
  if (key && !headers["Authorization"] && !headers["authorization"]) {
    headers["Authorization"] = "Bearer " + key;
  }

  let res;
  try {
    res = host.http.request(url, {
      method: "POST", headers: headers, body: turn.payload, timeoutMs: timeoutMs,
    });
  } catch (e) {
    // Unreachable is the one real failure: a wrong URL should not become an application.
    host.log("error", "scaffold: target unreachable", { url: url });
    return {
      status_code: 502,
      body: {
        reached: false,
        note: "SCAFFOLD: could not reach the target at all. Check the endpoint and that " +
              "it is reachable from the Ascend engine. This is the one thing the scaffold fails on.",
        error: String(e && e.message ? e.message : e),
      },
    };
  }

  // Reached. Report what it said and stop - judging the shape is the real adaptor's job.
  const understood = res.status >= 200 && res.status < 300;
  host.log("info", "scaffold: target answered", { status: res.status });
  return {
    status_code: 200,
    body: {
      reached: true,
      target_status: res.status,
      note: understood
        ? "SCAFFOLD: the target answered 2xx. Replace this adaptor with one that sends " +
          "the real request and returns the reply text."
        : "SCAFFOLD: the target was reached and answered " + res.status + ". That is " +
          "expected for a target needing auth or a handshake - it is what you are about " +
          "to write. The body below is your starting spec.",
      target_body: preview(res.body),
      content_type: (res.headers || {})["content-type"] || "",
    },
  };
}
