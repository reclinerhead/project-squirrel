# Tests for make_2class.py's pure transform logic (issue #12). The filesystem
# walk is I/O and is verified by the script's own self-check against the real
# export; what must never silently break is the remap itself.

import pytest

import make_2class

# The real mapping for ['chipmunk', 'squirrel', 'turkey'] -> ['squirrel', 'turkey']
DROP_ID = 0
REMAP = {1: 0, 2: 1}


def test_squirrel_and_turkey_renumber():
    lines = ["1 0.5 0.5 0.1 0.1", "2 0.2 0.2 0.05 0.08"]
    out, dropped = make_2class.transform_lines(lines, DROP_ID, REMAP)
    assert out == ["0 0.5 0.5 0.1 0.1", "1 0.2 0.2 0.05 0.08"]
    assert dropped == 0


def test_chipmunk_boxes_are_stripped_not_renamed():
    lines = ["0 0.5 0.5 0.1 0.1", "1 0.2 0.2 0.05 0.08"]
    out, dropped = make_2class.transform_lines(lines, DROP_ID, REMAP)
    assert out == ["0 0.2 0.2 0.05 0.08"]   # the squirrel, renumbered -- not the chipmunk
    assert dropped == 1


def test_coordinates_survive_untouched():
    lines = ["2 0.123456 0.654321 0.011111 0.099999"]
    out, _ = make_2class.transform_lines(lines, DROP_ID, REMAP)
    assert out == ["1 0.123456 0.654321 0.011111 0.099999"]


def test_unknown_class_id_aborts():
    with pytest.raises(ValueError, match="unknown class id 3"):
        make_2class.transform_lines(["3 0.5 0.5 0.1 0.1"], DROP_ID, REMAP)


def test_blank_lines_are_ignored():
    out, dropped = make_2class.transform_lines(["", "  ", "1 0.5 0.5 0.1 0.1"], DROP_ID, REMAP)
    assert out == ["0 0.5 0.5 0.1 0.1"]
    assert dropped == 0


def test_chipmunk_only_frame_is_dropped():
    out, dropped = make_2class.transform_lines(["0 0.5 0.5 0.1 0.1"], DROP_ID, REMAP)
    assert make_2class.file_action(out, dropped) == "drop"


def test_mixed_frame_is_kept():
    out, dropped = make_2class.transform_lines(
        ["0 0.5 0.5 0.1 0.1", "1 0.2 0.2 0.1 0.1"], DROP_ID, REMAP)
    assert make_2class.file_action(out, dropped) == "keep"


def test_deliberate_background_frame_is_kept():
    # An already-empty label file is a background frame someone chose to ship;
    # only chipmunk-created emptiness drops the frame.
    out, dropped = make_2class.transform_lines([], DROP_ID, REMAP)
    assert make_2class.file_action(out, dropped) == "keep"
