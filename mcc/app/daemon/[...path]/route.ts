// Explicit proxy for all daemon traffic (issue #35). This was a next.config.ts
// rewrite, but Next's internal proxy logs every failed upstream attempt -- and
// with the MCC running 24/7 on pearl while the daemon on bluejay only runs
// during test sessions, daemon-down is the NORMAL state, not an error. At one
// poll per second per open tab, that was ~86k journal lines a day.
//
// This handler owns the failure path instead: an unreachable daemon is a quiet
// 503 (the client already treats any non-OK /daemon response as "Merle is
// asleep"), and the journal gets one line per state transition -- one when the
// daemon drops, one when it returns -- never one per request.

import type { NextRequest } from "next/server";

// 127.0.0.1, not "localhost": uvicorn binds IPv4 only, but Node resolves
// "localhost" to IPv6 (::1) first on Windows -- so a "localhost" target makes a
// wasted ::1 attempt (and a doubled ECONNREFUSED when the daemon is down)
// before falling back to IPv4. Targeting IPv4 directly avoids the detour.
const DAEMON = process.env.MERLE_DAEMON_URL ?? "http://127.0.0.1:8000";

// Transition-logging state. Module-level is fine: one server process, and a
// wrong guess after a restart costs one log line, not correctness. Starts true
// so a server booting with the daemon already down logs the transition once.
let daemonWasUp = true;

async function proxy(
  req: NextRequest,
  params: Promise<{ path: string[] }>,
): Promise<Response> {
  const { path } = await params;
  const url = `${DAEMON}/${path.join("/")}${req.nextUrl.search}`;

  // Only the content type crosses to the daemon: there's no auth or cookies on
  // this path, and forwarding host/connection headers wholesale confuses
  // uvicorn more than it helps. Body/duplex only when a body can exist --
  // Node's fetch rejects a duplex option on GET.
  const init: RequestInit & { duplex?: "half" } = {
    method: req.method,
    cache: "no-store",
    signal: req.signal,
  };
  const contentType = req.headers.get("content-type");
  if (contentType) init.headers = { "content-type": contentType };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half"; // required by Node fetch to stream a request body
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch {
    // A tab that navigated away aborts our fetch -- that says nothing about
    // the daemon, so it neither logs nor flips the transition state.
    if (req.signal.aborted) return new Response(null, { status: 499 });
    if (daemonWasUp) {
      daemonWasUp = false;
      console.log(
        `[daemon] unreachable at ${DAEMON} -- Merle is asleep (staying quiet until he's back)`,
      );
    }
    return Response.json({ error: "daemon unreachable" }, { status: 503 });
  }

  if (!daemonWasUp) {
    daemonWasUp = true;
    console.log("[daemon] reachable again -- Merle is awake");
  }

  // Pass the body through untouched as a stream: /daemon/stream is an infinite
  // MJPEG response, so buffering would kill it, and its content-type carries
  // the multipart boundary. Length/encoding stay behind -- fetch may have
  // decompressed the body, and Next frames the response itself.
  const headers = new Headers();
  const ct = upstream.headers.get("content-type");
  if (ct) headers.set("content-type", ct);
  const cd = upstream.headers.get("content-disposition");
  if (cd) headers.set("content-disposition", cd);
  return new Response(quietBody(upstream.body), {
    status: upstream.status,
    headers,
  });
}

// The daemon dying mid-stream (every test session ends by stopping it under a
// live MJPEG stream) would otherwise surface as an unhandled "failed to pipe
// response" ECONNRESET stack trace from Next. Convert the mid-stream error to
// a clean end-of-stream: the <img>'s onError retry and the next /state poll
// (which owns the transition logging) take it from there.
function quietBody(
  body: ReadableStream<Uint8Array> | null,
): ReadableStream<Uint8Array> | null {
  if (!body) return body;
  const reader = body.getReader();
  return new ReadableStream({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) controller.close();
        else controller.enqueue(value);
      } catch {
        controller.close();
      }
    },
    cancel(reason) {
      // Client gone (closed tab): release the upstream connection too.
      return reader.cancel(reason);
    },
  });
}

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  return proxy(req, ctx.params);
}

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  return proxy(req, ctx.params);
}
