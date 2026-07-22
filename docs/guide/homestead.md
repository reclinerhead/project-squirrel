# The front door & Homestead

> Spoke of the [Merle Technical Guide](../../TechnicalGuide.md) — read the hub first for the machine roster, quick start, and cross-cutting conventions.
>
> **Covers:** the Caddy front door on pearl and `launchpad/` — Homestead, the house launchpad
> **Runs on:** pearl
> **Related:** epic #110; issues #141 #143

## The front door (epic #110 Phase 1 — issue #141)

Everything on pearl used to be reached by memorized `IP:port` pairs. Since
#141 there is **one front door: Caddy on pearl:80**, and the house speaks
names — `pearl/mole` (Pi-hole), `mcc.lan` (:3000), `music.lan` (:3001).
The names are Pi-hole Local DNS records, and they use the **`.lan` suffix**
because that's the search domain pearl's DHCP already hands out — so a
desktop can type `mcc/` and the resolver fills in the rest. (The epic
proposed `.home`; `.lan` won because it was already deployed to every
client's resolver config, not chosen.) The Caddyfile lists each site under
both spellings (`http://mcc, http://mcc.lan`) because the browser's Host
header carries what was typed, not what DNS resolved.

Pi-hole's own web server (v6: embedded in `pihole-FTL`) moved off 80/443 to
**loopback:8081** to release the ports — Caddy proxies `/mole` *and* `/api`
(the v6 admin is a shell over its API; proxy one without the other and you
get a login page that can't log in). The `/mole` path is not a Caddy
rewrite: the UI's own home moved there (`webserver.paths.webhome =
"/mole/"`, renamed from the stock `/admin/` in #143 to match the Mole
tile), so the app generates `/mole/...` links itself and the proxy stays
dumb — plus the `/var/www/html/mole → admin` symlink, because FTL serves UI
files from `webroot + webhome` (see Pearl.md § Pi-hole for the trap).
`/api` ignores webhome and stays put. DNS (:53) and DHCP (:67) were untouched.
Plain HTTP, `auto_https off`, nothing on 443: TLS/auth is the epic's
deferred single-choke-point payoff, not a current feature. The broker's
WebSocket (:9001) is deliberately *not* proxied — browsers speak MQTT to it
directly (`NEXT_PUBLIC_MERLE_MQTT_WS` is absolute). **Phase 4 decided this
stays so** (#147): the launchpad's lamps dial `ws://192.168.1.64:9001`
straight (the `bus` key in `tiles.json`), keeping one idiom with the MCC; a
`ws://pearl/bus` Caddy proxy is deferred until something actually needs it —
TLS would be that something.

Canonical Caddyfile: `Servers/Caddyfile` in the repo; live copy
`/etc/caddy/Caddyfile`, synced **manually on purpose** (a reverse-proxy
config that deploys itself on merge could take every web surface down at
once — see Pearl.md § The front door for the two-line sync).

## Homestead — the launchpad (epic #110 Phase 3 — issue #143)

The front door's face: `pearl/` 302s to `/home/`, and that redirect is the
one bookmark the whole system needs — every module is a tile from there.
**Homestead** is the app's settled name (it was the epic's working title;
"Trailhead" was the working name it replaces).

`launchpad/` is a top-level peer of `mcc/` and `music/` and is deliberately
**not a Next app**: four hand-rolled files (`index.html`, `styles.css`,
`app.js`, `tiles.json`) plus two self-hosted font subsets, served by Caddy's
`file_server` straight from pearl's checkout. No framework, no build — which
means **a merge deploys it by `git pull` alone**; `merle-autodeploy` needs
no gate for it and its journal shows nothing, and that's correct, not
broken. **Adding a module is one `tiles.json` entry, not a code change**
(`{name, icon, color, url, description, status: live|soon}`; file order is
porch order; an unknown/omitted icon key falls back to the signpost glyph,
so config-alone stays true even for a tile with no bespoke icon; `soon`
tiles render dimmed and inert). The page fetches `tiles.json` with
`cache: no-store` so edit-and-refresh never serves a stale porch.

Design language is the MCC's "Ranger Station, Night Watch" one room over —
same tokens/topo background/type pairing, **copied, not abstracted** (the
music app's precedent). The tiles are trail placards, not a Card-Card-Card
grid: each wears a painted **blaze** in the module's own color (hue carries
identity, the narrators' voice-color idiom — Merle squirrel-orange, Weather
rain-blue, Music hi-res gold, Mole earth-brown, Hearth ember, Security
watchtower steel-blue since Frigate went live in epic #243 — any
coming-soon tile dims to a whisper). Fonts are the same Fraunces/Sometype
Mono subsets next/font self-hosts for the MCC, with its fallback metrics
copied so the swap can't shift layout; the grid reserves two placard rows
before `tiles.json` lands (house rule #1).

**The lamps (epic #110 Phase 4 — issue #147).** Tiles whose config carries a
`presence: "<topic>"` wear a live status dot fed by the bus's retained
`"online"`/`"offline"` strings — the house Last-Will idiom generalized, not
health checks invented: a publisher dying (crash, Ctrl+C, SIGTERM) flips its
lamp within seconds with no polling and no cleanup code, verified live by
hard-killing a publisher. Three states, one class swap on a fixed 8px slot
every tile reserves (no layout shift): online pulses `--led`, offline is a
static faint dot, and *unknown* — no verdict yet, or the bus itself
unreachable — is fainter still, because a dead broker is not a verdict about
the services. The broker URL is the top-level `bus` key in `tiles.json`
(explicit, per the epic's note; the same-host fallback idea only works
because broker and Caddy share pearl, and explicit survives that changing).
The MQTT client is a **vendored** `mqtt.esm.js` (5.15.1, `launchpad/vendor/`
— the *browser* build, honoring the MCC's hard-won default-entry gotcha;
vendoring is not a build step, the no-build rule holds). Current wiring:
Merle → `services/merle-daemon/status` (the new house-wide namespace from
#147 — `bus.py` grew `service_status_topic()`, and the perception daemon
passes it as `status_topic`, so the lamp answers "is the driveway watch on
right now?"; dark is its *normal* state), Music → `music/status` (the
engine, not the GUI — pearl's always-on Next apps are boring, the engine is
the news), Weather Post → `weather/status`. Mole/Hearth/coming-soon carry no
topic and keep their slot invisible.
