"""Artist bios: MusicBrainz identity, Wikipedia prose, Last.fm fallback (#170).

The `artists` table has sat empty since Phase 0 built it and ArtistView has
rendered an empty bio slot since the fixture era -- the landing pad exists on
both ends with nothing in between. This is the fetch that fills it, at a
measured scale of ~720 canonical identities.

IDENTITY RESOLUTION IS THE WHOLE PROBLEM, and the issue's original accept rule
does not survive contact with the API. Measured 2026-07-18: searching
MusicBrainz for the library's band "We" returns "We Are Scientists" with score
100 -- a fuzzy match scoring identically to a perfect one -- so "exact-fold
name match plus a score threshold" would have written a New York indie band's
bio onto that page. Their score is Lucene relevance, not confidence.

So the gate is CORROBORATION: an exact case-folded name match must also share
an album title with the MusicBrainz artist's release groups. The library
already knows which records it holds, and that evidence is free -- the release
groups ride along on the lookup the pass makes anyway for the Wikidata link.
Where MusicBrainz lists no release groups at all, corroboration is impossible
rather than failed, and the pass falls back to name-and-score. Everything else
goes to a report and bio_rules.yaml, never to the page.

Downstream of the MBID nothing is guessed: MusicBrainz -> Wikidata (a URL
relation) -> the enwiki sitelink -> the article's lead extract. Name->MBID is
the only fuzzy step in the chain.

SHAPE: propose_for_artist(name, albums, rules) is the unit of work -- one
artist in, one proposal dict out, no database and no writes. main() is a thin
worklist loop over it. Same reason as music_blurb.py: the planned per-entity
"refresh this artist" button calls this same function and writes only on
approval, so the button and the bulk pass cannot drift.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from jukebox import music_catalog

LOG_EVERY = 25

# MusicBrainz throttles on IP at 1 request/second and returns 503 for the rest
# (verified against their rate-limiting doc, 2026-07-18, and reproduced by an
# unthrottled spike). One pass is ~2 requests per artist over ~720 artists, so
# obeying this costs about 25 minutes ONCE -- the issue's do-not-change list
# forbids parallelising it to save that.
MB_MIN_INTERVAL_S = 1.3

# A lookup's `inc=release-groups` subquery is CAPPED at 25 and cannot be
# paginated -- measured 2026-07-18, when "A Tribe Called Quest" and "Andrew
# Manze" both came back "none of its 25 release groups match this library".
# Exactly 25 therefore means "probably truncated", not "that's all of them",
# and a prolific artist's corroborating album can easily sit outside that
# window. When a corroboration fails at exactly the cap, the browse endpoint
# (which DOES paginate) is asked for the full discography before the artist
# is condemned to the review queue.
MB_SUBQUERY_CAP = 25
MB_BROWSE_LIMIT = 100
# Ceiling on the browse follow-up, so one absurdly prolific compilation
# artist cannot turn a once-ever pass into an afternoon.
MB_BROWSE_MAX_PAGES = 5
FETCH_TIMEOUT_S = 20
# A real contact string is required by both MusicBrainz and Wikimedia; an
# anonymous agent gets the stricter bucket or an outright block.
USER_AGENT = ("MerleMusic/1.0 "
              "( https://github.com/reclinerhead/project-squirrel )")

MB_ROOT = "https://musicbrainz.org/ws/2"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

# The Action API's extracts, deliberately NOT the REST rest_v1/page/summary
# endpoint: rest_v1 entered gradual deprecation in July 2026 with replacement
# routes unannounced until H2. prop=extracts is the long-stable path and takes
# maxlag, which is what a non-interactive job is supposed to send.
WIKI_MAXLAG_S = 5

# Statuses propose_for_artist can conclude with.
OK = "ok"
SKIPPED = "skipped"          # rules-file skip; never fetch, never write
UNRESOLVED = "unresolved"    # no confident identity -- the review queue
NO_PROSE = "no-prose"        # identity found, no article anywhere

TALLY_KEY = {OK: "written", SKIPPED: "skipped", UNRESOLVED: "unresolved",
             NO_PROSE: "no_prose"}

DEFAULT_RULES_PATH = os.path.join(os.path.dirname(__file__), "bio_rules.yaml")
RULES_SECTIONS = ("skip", "pin", "tuning")

# Leading articles are noise when comparing album titles across two catalogs
# that disagree about them ("The Square Root..." vs "Square Root...").
LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

# MusicBrainz writes typographic punctuation as a house style ("People's
# Instinctive Travels", "Jazz (We've Got)"); iTunes writes ASCII. Measured
# 2026-07-18: this alone sank the corroboration for A Tribe Called Quest,
# whose one album here is "We Got It from Here... Thank You 4 Your Service"
# against MusicBrainz's "...Thank You 4 Your service" with a U+2026 ellipsis.
# Folding the two conventions together is normalizing an encoding difference,
# not loosening the match -- the words still have to agree exactly.
PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ",
})
# Last.fm closes every bio with a licence tail. It is boilerplate, not prose.
# The artist name sits BETWEEN "Read more about" and "on Last.fm", so the two
# halves cannot be matched as one contiguous phrase; the bounded `.{0,120}?`
# spans the name without letting the pattern swallow a paragraph that merely
# happens to contain the words.
LASTFM_TAIL = re.compile(
    r"\s*(<a[^>]*>)?\s*Read more\b(on|about)?.{0,120}?Last\.fm.*$",
    re.IGNORECASE | re.DOTALL)
HTML_TAG = re.compile(r"<[^>]+>")
# Wikipedia lead extracts occasionally open with a parenthetical pronunciation
# or script gloss; harmless, left alone. What DOES need removing is the
# leftover section scaffolding a truncated extract can end on.
WIKI_SECTION_STUB = re.compile(r"\n==.*$", re.DOTALL)


class RulesError(ValueError):
    """A bio_rules.yaml that cannot be trusted. Raised before any write --
    music_genre.py's posture: validation fails loudly, never half-applies."""


# --- pure: rules, matching, corroboration, prose cleanup ----------------------

def rules_path():
    """MERLE_MUSIC_BIO_RULES, else the copy that ships in the repo. A repo
    default is legitimate here for genre_rules.yaml's reason: the file IS the
    ruleset and lives in git, unlike an art store whose default would scatter
    images into a checkout."""
    return os.environ.get("MERLE_MUSIC_BIO_RULES", "").strip() or \
        DEFAULT_RULES_PATH


def parse_rules(text):
    """bio_rules.yaml -> {"skip": {folded names}, "pin": {folded: mbid},
    "tuning": {...}}. Unknown sections are an error, not a warning: a typo'd
    section name that silently did nothing would be a rule the owner believes
    is in force."""
    import yaml
    data = yaml.safe_load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RulesError("bio rules must be a mapping, got %s"
                         % type(data).__name__)
    unknown = set(data) - set(RULES_SECTIONS)
    if unknown:
        raise RulesError("unknown bio rules section(s): %s"
                         % ", ".join(sorted(unknown)))
    skip = data.get("skip") or []
    if not isinstance(skip, list):
        raise RulesError("`skip` must be a list of artist names")
    pin = data.get("pin") or {}
    if not isinstance(pin, dict):
        raise RulesError("`pin` must be a mapping of name -> mbid")
    for name, mbid in pin.items():
        if not isinstance(mbid, str) or not mbid.strip():
            raise RulesError("pin for %r must be an MBID string" % name)
    tuning = data.get("tuning") or {}
    if not isinstance(tuning, dict):
        raise RulesError("`tuning` must be a mapping")
    min_score = tuning.get("min_score", 90)
    if not isinstance(min_score, int) or not 0 <= min_score <= 100:
        raise RulesError("tuning.min_score must be an int 0-100, got %r"
                         % (min_score,))
    return {"skip": {fold(s) for s in skip},
            "pin": {fold(k): v.strip() for k, v in pin.items()},
            "tuning": {"min_score": min_score}}


def load_rules(path):
    """Read and validate the rules file. OSError becomes RulesError so the
    caller has one exception type to fail loudly on."""
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_rules(fh.read())
    except OSError as e:
        raise RulesError("cannot read bio rules %s: %s" % (path, e))


def fold(name):
    """The comparison form of a name: case-folded and whitespace-collapsed.
    Not stripped of punctuation -- "AC/DC" must not collide with "ACDC", and
    at this scale a near-miss belongs in the review queue anyway."""
    return " ".join(str(name or "").split()).casefold()


def fold_title(title):
    """The comparison form of an ALBUM title: typographic punctuation folded
    to ASCII, an ellipsis spelled out, a leading article dropped, then cased
    and whitespace-collapsed.

    Every step here is an ENCODING difference between two catalogs, never a
    loosening of the match -- the words themselves still have to agree
    exactly. Two catalogs routinely disagree about "The" (this library holds
    both "Square Root of Minus One" and "The Square Root of Negative One" for
    one band) and about curly-vs-straight punctuation (MusicBrainz house
    style vs iTunes), and neither disagreement is evidence of anything."""
    text = str(title or "").translate(PUNCT_FOLD).replace("…", "...")
    return LEADING_ARTICLE.sub("", " ".join(text.split())).casefold()


def album_overlap(mine, theirs):
    """The corroborating evidence: album titles this library and MusicBrainz
    agree the artist made. A set, so the caller can report WHICH ones matched
    -- "trust me" is not a reviewable answer."""
    return {t for t in (fold_title(x) for x in mine) if t} & \
           {t for t in (fold_title(x) for x in theirs) if t}


def exact_matches(name, candidates, min_score):
    """MusicBrainz search hits whose name IS this artist's, case-folded, and
    which clear the score floor. Everything downstream reasons over this list
    -- notably its LENGTH, since two artists genuinely sharing a name is the
    ambiguity no amount of scoring resolves."""
    return [c for c in candidates
            if fold(c.get("name")) == fold(name)
            and (c.get("score") or 0) >= min_score]


def decide(name, candidates, my_albums, mb_albums_by_id, min_score):
    """THE ACCEPT RULE, pure so it is entirely under test.

    Returns (mbid, reason) with mbid None when nothing is confident enough.
    In order:

    1. No exact case-folded name match -> unresolved. A fuzzy match scoring
       100 is still the wrong band ("We" -> "We Are Scientists", measured).
    2. Exactly one exact match that shares an album title -> accept. This is
       the common, boring case and the strongest evidence available.
    3. Several exact matches -> accept the one (and only one) that
       corroborates. Two bands really do share a name; the albums say which
       one is on the shelf. Two corroborating matches is a genuine ambiguity
       and goes to the queue.
    4. One exact match, MusicBrainz lists NO release groups for it ->
       accept on name and score. Corroboration was impossible here rather
       than failed, and refusing every sparsely-documented artist would empty
       the page for exactly the obscure bands a bio helps most with.
    5. One exact match, release groups exist and none match -> UNRESOLVED.
       This is the "We" case, and the whole reason the rule exists."""
    exact = exact_matches(name, candidates, min_score)
    if not exact:
        return (None, "no exact name match above score %d" % min_score)

    corroborated = []
    for c in exact:
        shared = album_overlap(my_albums, mb_albums_by_id.get(c["id"], []))
        if shared:
            corroborated.append((c, shared))

    if len(corroborated) == 1:
        c, shared = corroborated[0]
        return (c["id"], "album match: %s" % ", ".join(sorted(shared)))
    if len(corroborated) > 1:
        return (None, "%d name matches all corroborate -- ambiguous"
                % len(corroborated))

    if len(exact) == 1:
        if not mb_albums_by_id.get(exact[0]["id"]):
            return (exact[0]["id"],
                    "sole name match, no release groups to corroborate")
        return (None, "sole name match, but none of its %d release groups "
                      "match this library"
                % len(mb_albums_by_id.get(exact[0]["id"], [])))
    return (None, "%d name matches, none corroborated" % len(exact))


def clean_wikipedia(text):
    """A Wikipedia lead extract -> the paragraph a page renders. plaintext
    already, so this only unescapes entities, drops any trailing section
    scaffolding, and collapses the blank-line runs the extractor leaves
    between paragraphs."""
    if not text:
        return None
    out = WIKI_SECTION_STUB.sub("", html.unescape(str(text)))
    out = re.sub(r"\n{2,}", "\n\n", out).strip()
    return out or None


def clean_lastfm(text):
    """A Last.fm bio -> plain prose. Theirs arrives as HTML with a licence
    tail ("Read more on Last.fm"); the tail goes first, because stripping
    tags would turn its anchor into bare words that read like content."""
    if not text:
        return None
    out = LASTFM_TAIL.sub("", str(text))
    out = HTML_TAG.sub("", out)
    out = html.unescape(out)
    out = re.sub(r"\n{2,}", "\n\n", out).strip()
    return out or None


# --- I/O: the throttled fetch chain --------------------------------------------

class Throttle:
    """A minimum interval between calls, as an object rather than a module
    global so tests can drive it and a second caller cannot silently share
    (or defeat) another's timer."""

    def __init__(self, interval_s, sleep=time.sleep, clock=time.monotonic):
        self.interval_s = interval_s
        self._sleep = sleep
        self._clock = clock
        self._last = None

    def wait(self):
        if self._last is not None:
            due = self._last + self.interval_s - self._clock()
            if due > 0:
                self._sleep(due)
        self._last = self._clock()


def get_json(url, throttle=None):
    """One GET, decoded as JSON, or None. weather.py's never-raise posture: a
    flaky network is a skipped artist that the next run picks up, never a
    dead pass. 503 from MusicBrainz means we out-ran the limiter, so it is
    worth one patient retry rather than a lost artist."""
    for attempt in (1, 2):
        if throttle:
            throttle.wait()
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt == 1:
                print("[music] 503 (rate limit) -- backing off")
                time.sleep(3)
                continue
            print("[music] fetch failed: HTTP %s" % e.code)
            return None
        except Exception as e:
            # Never log the URL: the Last.fm variant carries the API key.
            print("[music] fetch failed: %s" % type(e).__name__)
            return None
    return None


def mb_search_artist(name, throttle):
    """MusicBrainz artist search. Returns the raw candidate dicts -- name,
    id, score, disambiguation -- because the report shows all of them."""
    q = urllib.parse.quote('artist:"%s"' % str(name).replace('"', ""))
    d = get_json("%s/artist?query=%s&fmt=json&limit=8" % (MB_ROOT, q),
                 throttle)
    return (d or {}).get("artists", []) or []


def mb_artist_detail(mbid, throttle):
    """One lookup for BOTH halves the resolver needs: the release groups it
    corroborates against and the Wikidata URL relation it follows for prose.
    Deliberately one request -- at 1 req/s, a second would double the pass."""
    d = get_json("%s/artist/%s?inc=url-rels+release-groups&fmt=json"
                 % (MB_ROOT, mbid), throttle)
    if not d:
        return ([], {})
    albums = [rg.get("title") for rg in d.get("release-groups", [])
              if rg.get("title")]
    urls = {}
    for rel in d.get("relations", []):
        target = (rel.get("url") or {}).get("resource")
        if rel.get("type") and target:
            urls[rel["type"]] = target
    return (albums, urls)


def mb_browse_release_groups(mbid, throttle):
    """Every release group for an artist, via the browse endpoint, which
    paginates where the lookup's subquery just truncates at 25. Only called
    when a corroboration failed at exactly the cap -- the common case never
    pays for it."""
    titles, offset = [], 0
    for _ in range(MB_BROWSE_MAX_PAGES):
        d = get_json("%s/release-group?artist=%s&limit=%d&offset=%d&fmt=json"
                     % (MB_ROOT, urllib.parse.quote(mbid), MB_BROWSE_LIMIT,
                        offset), throttle)
        if not d:
            break
        page = [rg.get("title") for rg in d.get("release-groups", [])
                if rg.get("title")]
        titles.extend(page)
        total = d.get("release-group-count", len(titles))
        offset += MB_BROWSE_LIMIT
        if offset >= total or not page:
            break
    return titles


def wikidata_enwiki_title(qid):
    """The English Wikipedia article title for a Wikidata entity, or None.
    Following a link, not guessing a name -- the whole point of routing
    through MusicBrainz."""
    d = get_json("%s?action=wbgetentities&ids=%s&props=sitelinks&format=json"
                 % (WIKIDATA_API, urllib.parse.quote(qid)))
    entity = ((d or {}).get("entities") or {}).get(qid) or {}
    return ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")


def wikipedia_extract(title):
    """The article's lead section as plaintext, plus its canonical URL.
    formatversion=2 so `pages` is a list; redirects=1 so a renamed article
    still resolves; maxlag so a loaded cluster tells us to wait instead of
    being hammered by a non-interactive job."""
    params = urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "exintro": 1,
        "explaintext": 1, "redirects": 1, "format": "json",
        "formatversion": 2, "maxlag": WIKI_MAXLAG_S, "titles": title})
    d = get_json("%s?%s" % (WIKIPEDIA_API, params))
    pages = ((d or {}).get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        return (None, None)
    text = clean_wikipedia(pages[0].get("extract"))
    if not text:
        return (None, None)
    url = "https://en.wikipedia.org/wiki/%s" % urllib.parse.quote(
        str(pages[0].get("title", title)).replace(" ", "_"))
    return (text, url)


def lastfm_key():
    """MERLE_LASTFM_KEY, or None. The kill-switch convention: unset means
    Wikipedia-only, which is a complete and correct pass, not a degraded
    one."""
    return os.environ.get("MERLE_LASTFM_KEY", "").strip() or None


def lastfm_bio(mbid, key):
    """artist.getInfo by MBID -- the fallback where Wikipedia has no article.
    By MBID rather than name because we already resolved the identity and
    re-introducing a name lookup here would re-introduce the ambiguity the
    whole resolver exists to avoid."""
    params = urllib.parse.urlencode({
        "method": "artist.getinfo", "mbid": mbid, "api_key": key,
        "format": "json"})
    d = get_json("%s?%s" % (LASTFM_API, params))
    artist = (d or {}).get("artist") or {}
    text = clean_lastfm((artist.get("bio") or {}).get("content"))
    if not text:
        return (None, None)
    return (text, artist.get("url"))


def propose_for_artist(name, my_albums, rules, throttle=None, key=None):
    """THE UNIT OF WORK: one artist in, one proposal dict out.

    Returns {"status", "name", "bio", "bio_src", "bio_url", "mbid",
    "reason", "candidates"} and touches no database. main() writes it; the
    planned refresh button shows it for approval and writes only on a yes.
    `candidates` rides along on an unresolved result because a review queue
    entry without the options is not reviewable."""
    # `reason` explains the FINAL status; `match` records why this identity
    # was accepted and survives the later stages, because "we found prose"
    # and "here is why we believe this is the right band" are the two
    # separate things a human approving a refresh needs to read.
    out = {"status": UNRESOLVED, "name": name, "bio": None, "bio_src": None,
           "bio_url": None, "mbid": None, "reason": "", "match": "",
           "candidates": []}
    folded = fold(name)

    if folded in rules["skip"]:
        out["status"] = SKIPPED
        out["reason"] = "rules-file skip"
        return out

    pinned = rules["pin"].get(folded)
    if pinned:
        mbid, reason = pinned, "pinned in bio_rules.yaml"
    else:
        candidates = mb_search_artist(name, throttle)
        out["candidates"] = [
            {"name": c.get("name"), "id": c.get("id"),
             "score": c.get("score"),
             "disambiguation": c.get("disambiguation", "")}
            for c in candidates]
        exact = exact_matches(name, candidates,
                              rules["tuning"]["min_score"])
        # Detail is fetched ONLY for the exact-name matches -- usually one,
        # occasionally none. Fetching it for all eight search hits would
        # multiply a 1-req/s pass by eight for evidence about bands that
        # aren't even name matches.
        mb_albums, urls_by_id = {}, {}
        for c in exact:
            albums, urls = mb_artist_detail(c["id"], throttle)
            # Top up a truncated subquery before judging: exactly the cap
            # means "probably more", and condemning a prolific artist to the
            # review queue over MusicBrainz's page size is a false negative,
            # not caution.
            if (len(albums) >= MB_SUBQUERY_CAP
                    and not album_overlap(my_albums, albums)):
                albums = mb_browse_release_groups(c["id"], throttle) or albums
            mb_albums[c["id"]] = albums
            urls_by_id[c["id"]] = urls
        mbid, reason = decide(name, candidates, my_albums, mb_albums,
                              rules["tuning"]["min_score"])
        out["reason"] = reason
        if not mbid:
            return out
        urls = urls_by_id.get(mbid, {})

    out["mbid"] = mbid
    out["match"] = reason
    out["reason"] = reason
    if pinned:
        _, urls = mb_artist_detail(mbid, throttle)

    text, url, src = (None, None, None)
    wikidata = urls.get("wikidata")
    if wikidata:
        qid = wikidata.rstrip("/").split("/")[-1]
        title = wikidata_enwiki_title(qid)
        if title:
            text, url = wikipedia_extract(title)
            src = music_catalog.BIO_WIKIPEDIA if text else None
    if not text and key:
        text, url = lastfm_bio(mbid, key)
        src = music_catalog.BIO_LASTFM if text else None

    if not text:
        out["status"] = NO_PROSE
        out["reason"] = "identity resolved, no article found"
        return out

    out.update({"status": OK, "bio": text, "bio_src": src, "bio_url": url})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python3 -m jukebox.music_bio",
        description="Fetch artist bios: MusicBrainz identity, Wikipedia "
                    "prose, Last.fm fallback.")
    ap.add_argument("--db", default=None, help="catalog path")
    ap.add_argument("--rules", default=None, help="bio_rules.yaml path")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N artists (smoke test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report, write nothing")
    ap.add_argument("--retry-missing", action="store_true",
                    help="re-probe artists a previous run attempted and "
                         "found nothing for")
    ap.add_argument("--artist", default=None,
                    help="one artist name -- re-propose and print the "
                         "result (what the GUI refresh button will call)")
    args = ap.parse_args(argv)

    # Rules load and validate BEFORE the database opens, music_genre.py's
    # order: a typo in the rules file should cost nothing.
    try:
        rules = load_rules(args.rules or rules_path())
    except RulesError as e:
        print("[music] %s" % e)
        return 1

    conn = music_catalog.connect(args.db or music_catalog.db_path())
    throttle = Throttle(MB_MIN_INTERVAL_S)
    key = lastfm_key()
    print("[music] bio pass: Last.fm fallback %s"
          % ("enabled" if key else "OFF (MERLE_LASTFM_KEY unset)"))

    if args.artist:
        proposal = propose_for_artist(
            args.artist, music_catalog.artist_albums(conn, args.artist),
            rules, throttle, key)
        print("[music] %s -> %s (%s)" % (args.artist, proposal["status"],
                                         proposal["reason"]))
        print("[music] bio: %r" % ((proposal["bio"] or "")[:300],))
        for c in proposal["candidates"]:
            print("    candidate %s %r score=%s %s"
                  % (c["id"], c["name"], c["score"], c["disambiguation"]))
        return 0

    work = music_catalog.artists_missing_bio(conn, args.retry_missing)
    print("[music] bio pass: %d artists on the worklist" % len(work))

    tally = {"written": 0, "skipped": 0, "unresolved": 0, "no_prose": 0,
             "error": 0}
    unresolved = []
    started = time.time()
    for i, (name, albums) in enumerate(sorted(work.items()), 1):
        if args.limit and i > args.limit:
            break
        try:
            p = propose_for_artist(name, albums, rules, throttle, key)
        except Exception as e:
            print("[music] bio failed, skipping: %s -- %s: %s"
                  % (name, type(e).__name__, e))
            tally["error"] += 1
            continue
        if p["status"] == UNRESOLVED:
            unresolved.append(p)
        if not args.dry_run and p["status"] in (OK, NO_PROSE):
            # NO_PROSE writes too: fetched_at is what stops the next run
            # re-probing a known miss, and --retry-missing is the way back.
            music_catalog.set_artist_bio(
                conn, name, p["bio"], p["bio_src"], p["bio_url"], p["mbid"],
                int(time.time()))
        tally[TALLY_KEY[p["status"]]] += 1
        if i % LOG_EVERY == 0:
            if not args.dry_run:
                conn.commit()
            print("[music] %d/%d -- %d written, %d unresolved, %d no prose"
                  % (i, len(work), tally["written"], tally["unresolved"],
                     tally["no_prose"]))
    if not args.dry_run:
        conn.commit()

    print("[music] bio pass %sdone in %.1f min -- %d written, %d unresolved, "
          "%d no prose, %d skipped, %d errors"
          % ("(dry run) " if args.dry_run else "",
             (time.time() - started) / 60, tally["written"],
             tally["unresolved"], tally["no_prose"], tally["skipped"],
             tally["error"]))

    if unresolved:
        # The review queue. Printed with candidates so clearing it is reading
        # this block and pasting MBIDs into bio_rules.yaml -- the genre pass's
        # UNMAPPED-report loop.
        print("\n[music] UNRESOLVED -- %d artists need a bio_rules.yaml pin "
              "or skip:" % len(unresolved))
        for p in unresolved:
            print("  %s  (%s)" % (p["name"], p["reason"]))
            for c in p["candidates"][:4]:
                print("      %s  %r  score=%s  %s"
                      % (c["id"], c["name"], c["score"],
                         c["disambiguation"]))
    print("[music] catalog: %r" % (music_catalog.counts(conn),))
    return 0


if __name__ == "__main__":
    sys.exit(main())
