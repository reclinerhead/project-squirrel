// The player proxy (issue #129): the browser's one door to the playback
// daemon on pearl. Exists for the same reason the MCC has a /daemon route --
// the daemon's address is server config (MERLE_MUSIC_DAEMON, no default),
// not something to bake into client bundles, and the browser talking to
// another origin would drag CORS into a two-box LAN app.
//
// GET  /api/player/state          -> daemon GET /state
// POST /api/player/{play|pause|stop|seek|rate} -> daemon POST, body piped through
//
// The verb allowlist is the security boundary: this proxies five named verbs
// to one configured host, not arbitrary paths anywhere. A daemon that's down
// or unconfigured answers 503 with a reason -- the player bar treats that as
// "controls do nothing", never as a crash.
//
// `rate` is a write and still belongs here (issue #135): the catalog is
// pearl's, this app's own DB handle is readOnly by construction, and the
// daemon is the writer. It rides the player proxy rather than a route of its
// own because the door and its rules are already exactly right.

// `queue` (issue #139) is the playlist engine's door: it generates a track
// list on pearl and starts nothing -- the daemon stays one-track-at-a-time
// on the transport verbs, and PlayerProvider owns the queue it fetches.
const GET_VERBS = new Set(["state"]);
const POST_VERBS = new Set(["play", "pause", "stop", "seek", "rate", "queue"]);

function daemonBase(): string | null {
  const base = process.env.MERLE_MUSIC_DAEMON?.trim();
  return base ? base.replace(/\/+$/, "") : null;
}

async function pipe(url: string, init?: RequestInit): Promise<Response> {
  try {
    const res = await fetch(url, { ...init, cache: "no-store" });
    return Response.json(await res.json(), { status: res.status });
  } catch {
    return Response.json({ error: "music daemon unreachable" }, { status: 503 });
  }
}

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ verb: string }> },
) {
  const { verb } = await params;
  if (!GET_VERBS.has(verb)) {
    return Response.json({ error: "unknown verb" }, { status: 404 });
  }
  const base = daemonBase();
  if (!base) {
    return Response.json({ error: "MERLE_MUSIC_DAEMON not configured" }, { status: 503 });
  }
  return pipe(`${base}/${verb}`);
}

export async function POST(
  req: Request,
  { params }: { params: Promise<{ verb: string }> },
) {
  const { verb } = await params;
  if (!POST_VERBS.has(verb)) {
    return Response.json({ error: "unknown verb" }, { status: 404 });
  }
  const base = daemonBase();
  if (!base) {
    return Response.json({ error: "MERLE_MUSIC_DAEMON not configured" }, { status: 503 });
  }
  const body = await req.text();
  return pipe(`${base}/${verb}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body || "{}",
  });
}
