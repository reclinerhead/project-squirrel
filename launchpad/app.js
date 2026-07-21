// Homestead (issue #143): tiles.json -> placards. Adding a module is one
// config entry, not a code change -- an unknown icon key falls back to the
// signpost, so even a brand-new tile needs nothing from this file.
//
// tiles.json shape: { bus: "ws://…:9001", tiles: [{ name, icon, color, url,
// description, status: "live" | "soon", presence: "<topic>" }] }. Order in
// the file is the order on the porch. `bus` and `presence` are optional --
// without them the porch is exactly the Phase 3 porch, no lamps.
//
// Status dots (issue #147, epic #110 Phase 4): tiles whose config names a
// presence topic get a lamp fed by the bus's retained "online"/"offline"
// strings -- the same Last-Will idiom every Merle service already speaks, so
// a publisher dying flips its lamp within seconds with no polling and no
// health checks. The vendored mqtt.esm.js is the BROWSER build (the MCC's
// hard-won gotcha: the default "mqtt" entry is the Node build, whose CONNECT
// never sends in a browser). The broker is dialed directly -- an HTTP proxy
// can't carry WebSockets, and the MCC already speaks to :9001 the same way.

import mqtt from "./vendor/mqtt.esm.js";

const ICON_FALLBACK = "signpost";

function knownIcon(key) {
  return document.getElementById(`icon-${key}`) ? key : ICON_FALLBACK;
}

// presence topic -> the lamp spans wearing it. Filled during render, read by
// the bus callbacks.
const lampsByTopic = new Map();

// Lamp states, the MCC's presence vocabulary: online pulses led-green,
// offline is a static faint dot, unknown (no verdict yet, or the bus itself
// is quiet) is fainter still. One class swap, fixed geometry -- a lamp
// changing state moves nothing (house rule #1).
function setLamp(lamp, state) {
  lamp.className = `tile-lamp tile-lamp-${state}`;
  const words = {
    online: "on the air",
    offline: "dark",
    unknown: "no word from the bus",
  };
  lamp.title = words[state];
  lamp.setAttribute("aria-label", `presence: ${words[state]}`);
}

function makeTile(tile) {
  const live = tile.status === "live" && typeof tile.url === "string";
  const el = document.createElement(live ? "a" : "div");
  el.className = live ? "tile" : "tile tile-soon";
  if (live) {
    el.href = tile.url;
    // Every trail opens in its own tab -- the porch never disappears.
    el.target = "_blank";
    el.rel = "noopener";
  }
  if (tile.color) el.style.setProperty("--blaze", tile.color);

  const top = document.createElement("div");
  top.className = "tile-top";
  const blaze = document.createElement("span");
  blaze.className = "blaze";
  blaze.setAttribute("aria-hidden", "true");

  const right = document.createElement("span");
  right.className = "tile-right";
  // The lamp slot exists on EVERY tile so presence arriving (or a topic
  // added to the config later) never re-flows a row -- tiles without a
  // topic just keep theirs invisible.
  const lamp = document.createElement("span");
  if (typeof tile.presence === "string" && tile.presence) {
    setLamp(lamp, "unknown");
    lampsByTopic.set(tile.presence, lamp);
  } else {
    lamp.className = "tile-lamp tile-lamp-none";
    lamp.setAttribute("aria-hidden", "true");
  }
  const status = document.createElement("span");
  status.className = "tile-status stamp";
  status.textContent = live ? "Online" : "coming soon";
  right.append(lamp, status);
  top.append(blaze, right);

  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "tile-icon");
  icon.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${knownIcon(tile.icon)}`);
  icon.append(use);

  const name = document.createElement("h2");
  name.className = "tile-name";
  name.textContent = tile.name ?? "";

  const desc = document.createElement("p");
  desc.className = "tile-desc";
  desc.textContent = tile.description ?? "";

  el.append(top, icon, name, desc);
  return el;
}

function watchPresence(busUrl) {
  if (!busUrl || lampsByTopic.size === 0) return;
  const client = mqtt.connect(busUrl, { reconnectPeriod: 3000 });
  client.on("connect", () => {
    // All presence topics are RETAINED, so the broker answers each
    // subscription with the latest verdict immediately -- lamps light on
    // arrival, no poll loop anywhere.
    client.subscribe([...lampsByTopic.keys()]);
  });
  client.on("message", (topic, payload) => {
    const lamp = lampsByTopic.get(topic);
    if (!lamp) return;
    // Plain strings, not JSON -- the status topics predate JSON on the bus.
    setLamp(lamp, payload.toString() === "online" ? "online" : "offline");
  });
  // Bus down means the lamps know nothing -- "unknown", never "offline":
  // the broker being unreachable is not a verdict about the services.
  client.on("close", () => {
    for (const lamp of lampsByTopic.values()) setLamp(lamp, "unknown");
  });
  // Mandatory (the MCC gotcha): an unhandled mqtt.js "error" throws and
  // wedges its own reconnect loop.
  client.on("error", () => {});
}

async function lay() {
  const grid = document.getElementById("tiles");
  try {
    // no-store: this file IS the config -- a stale cache would make "edit
    // tiles.json, refresh" quietly lie.
    const res = await fetch("tiles.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`tiles.json answered ${res.status}`);
    const data = await res.json();
    const tiles = Array.isArray(data.tiles) ? data.tiles : [];
    grid.replaceChildren(...tiles.map(makeTile));
    watchPresence(typeof data.bus === "string" ? data.bus : null);
  } catch {
    const msg = document.createElement("p");
    msg.className = "empty stamp";
    msg.textContent = "tiles.json didn't load — the porch is empty";
    grid.replaceChildren(msg);
  }
}

lay();
