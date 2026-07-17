# =============================================================================
# project-squirrel -- test_music_art.py
#
# The art pass's brain (issue #153): the pick rules (largest embedded image,
# promoted artist cover with its stable tie-break), the content-addressed
# naming, folder fallback, and the store's idempotence. mutagen extraction
# against real tagged files is proven by the pass on pearl, not here (the
# SOAP-half precedent); Pillow IS exercised -- it's in the CI install line.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import io
import os

from jukebox import music_art as ma


# --- naming ---------------------------------------------------------------------

def test_art_names_are_the_hash_plus_variants():
    orig, thumb, large = ma.art_names("abc123")
    assert orig == "abc123.orig"
    assert thumb == "abc123.thumb.webp"
    assert large == "abc123.large.webp"


# --- the picks ------------------------------------------------------------------

def test_largest_picture_wins_by_bytes():
    assert ma.largest_picture([b"aa", b"aaaa", b"a"]) == b"aaaa"


def test_largest_picture_tie_goes_to_the_earliest():
    """Stable across runs: the worklist feeds paths sorted, so the earliest
    of equal-sized images is the same one every time."""
    first, second = b"abcd", b"wxyz"
    assert ma.largest_picture([first, second]) is first


def test_largest_picture_survives_empties_and_nothing():
    assert ma.largest_picture([]) is None
    assert ma.largest_picture([b"", None]) is None
    assert ma.largest_picture([None, b"x"]) == b"x"


def test_promotion_picks_highest_score():
    rows = [
        {"artist": "A", "album_key": "A␟One", "art_hash": "h1",
         "w": 500, "h": 500, "score": 0},
        {"artist": "A", "album_key": "A␟Two", "art_hash": "h2",
         "w": 500, "h": 500, "score": 4},
    ]
    assert ma.promotion_pick(rows)["A"]["art_hash"] == "h2"


def test_promotion_tie_breaks_on_lowest_album_key():
    """The issue's contract: deterministic across runs, byte-stable."""
    rows = [
        {"artist": "A", "album_key": "A␟Zebra", "art_hash": "hz",
         "w": 1, "h": 1, "score": 2},
        {"artist": "A", "album_key": "A␟Aardvark", "art_hash": "ha",
         "w": 1, "h": 1, "score": 2},
    ]
    assert ma.promotion_pick(rows)["A"]["art_hash"] == "ha"


def test_promotion_handles_many_artists_independently():
    rows = [
        {"artist": "A", "album_key": "A␟X", "art_hash": "h1",
         "w": 1, "h": 1, "score": 0},
        {"artist": "B", "album_key": "B␟Y", "art_hash": "h2",
         "w": 1, "h": 1, "score": -3},
    ]
    picks = ma.promotion_pick(rows)
    assert picks["A"]["art_hash"] == "h1"
    assert picks["B"]["art_hash"] == "h2"  # a panned album still beats none


# --- folder fallback ------------------------------------------------------------

def test_folder_picture_finds_the_wmp_era_file(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "Folder.jpg").write_bytes(b"JPEGISH")
    assert ma.folder_picture([str(d / "01 Song.m4a")]) == b"JPEGISH"


def test_folder_picture_prefers_earlier_names(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "Folder.jpg").write_bytes(b"FOLDER")
    (d / "cover.jpg").write_bytes(b"COVER")
    assert ma.folder_picture([str(d / "x.mp3")]) == b"FOLDER"


def test_folder_picture_none_when_bare(tmp_path):
    assert ma.folder_picture([str(tmp_path / "x.mp3")]) is None
    assert ma.folder_picture([]) is None


# --- the store ------------------------------------------------------------------

def png_bytes(w=300, h=300, color=(200, 40, 40)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_store_image_writes_original_and_both_sizes(tmp_path):
    data = png_bytes(700, 700)
    art_hash, w, h, focal = ma.store_image(str(tmp_path), data)
    assert (w, h) == (700, 700)
    assert focal == 0.5  # a flat color has no interest anywhere: center
    orig, thumb, large = (tmp_path / n for n in ma.art_names(art_hash))
    assert orig.read_bytes() == data  # the original is UNTOUCHED bytes
    from PIL import Image
    assert Image.open(thumb).size == (160, 160)
    assert Image.open(large).size == (600, 600)


def test_store_image_never_upscales(tmp_path):
    art_hash, w, h, _ = ma.store_image(str(tmp_path), png_bytes(120, 120))
    from PIL import Image
    _, thumb, large = (tmp_path / n for n in ma.art_names(art_hash))
    assert Image.open(thumb).size == (120, 120)
    assert Image.open(large).size == (120, 120)


def test_store_image_is_idempotent_and_content_addressed(tmp_path):
    data = png_bytes()
    h1 = ma.store_image(str(tmp_path), data)
    mtimes = {p: os.path.getmtime(tmp_path / p) for p in os.listdir(tmp_path)}
    h2 = ma.store_image(str(tmp_path), data)
    assert h1 == h2
    # Second call wrote nothing: same files, untouched mtimes.
    assert {p: os.path.getmtime(tmp_path / p)
            for p in os.listdir(tmp_path)} == mtimes


def test_store_image_rejects_garbage_without_litter(tmp_path):
    assert ma.store_image(str(tmp_path), b"not an image at all") is None
    assert os.listdir(str(tmp_path)) == []  # no half-written files


# --- focal analysis (issue #159) ------------------------------------------------

def busy_bottom_image(w=300, h=300):
    """A synthetic Here Come the Runts: flat 'wall' above, busy texture
    below -- a checkerboard in the bottom third is unambiguous edge
    density where the subject would be."""
    from PIL import Image
    img = Image.new("L", (w, h), 230)
    px = img.load()
    for y in range(int(h * 2 / 3), h):
        for x in range(w):
            px[x, y] = 255 if (x // 6 + y // 6) % 2 else 0
    return img


def test_focal_uniform_image_is_center():
    from PIL import Image
    assert ma.focal_from_image(Image.new("RGB", (400, 400), (90, 12, 34))) == 0.5


def test_focal_follows_the_interest():
    """The issue's acceptance case in miniature: subject low in the frame
    pulls the focal well below center."""
    focal = ma.focal_from_image(busy_bottom_image())
    assert focal > 0.6


def test_focal_clamps_at_the_band_edges():
    """All the ink at the very bottom still keeps context above the
    subject -- the crop anchor never pins to an edge."""
    from PIL import Image
    img = Image.new("L", (300, 300), 230)
    px = img.load()
    for y in range(280, 300):
        for x in range(300):
            px[x, y] = 255 if (x // 3) % 2 else 0
    assert ma.focal_from_image(img) <= 0.8
    assert ma.focal_from_image(img.transpose(Image.FLIP_TOP_BOTTOM)) >= 0.2


def test_focal_is_deterministic():
    img = busy_bottom_image()
    assert ma.focal_from_image(img) == ma.focal_from_image(img)
