// Homestead (issue #143): tiles.json -> placards. Adding a module is one
// config entry, not a code change -- an unknown icon key falls back to the
// signpost, so even a brand-new tile needs nothing from this file.
//
// tiles.json shape: { tiles: [{ name, icon, color, url, description,
// status: "live" | "soon" }] }. Order in the file is the order on the porch.

const ICON_FALLBACK = "signpost";

function knownIcon(key) {
  return document.getElementById(`icon-${key}`) ? key : ICON_FALLBACK;
}

function makeTile(tile) {
  const live = tile.status === "live" && typeof tile.url === "string";
  const el = document.createElement(live ? "a" : "div");
  el.className = live ? "tile" : "tile tile-soon";
  if (live) el.href = tile.url;
  if (tile.color) el.style.setProperty("--blaze", tile.color);

  const top = document.createElement("div");
  top.className = "tile-top";
  const blaze = document.createElement("span");
  blaze.className = "blaze";
  blaze.setAttribute("aria-hidden", "true");
  const status = document.createElement("span");
  status.className = "tile-status stamp";
  status.textContent = live ? "open trail" : "coming soon";
  top.append(blaze, status);

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
  } catch {
    const msg = document.createElement("p");
    msg.className = "empty stamp";
    msg.textContent = "tiles.json didn't load — the porch is empty";
    grid.replaceChildren(msg);
  }
}

lay();
