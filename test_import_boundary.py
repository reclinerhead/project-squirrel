# =============================================================================
# project-squirrel -- test_import_boundary.py
#
# The Pi's venv, enforced (issue #123, epic #110 Phase 0).
#
# merle (192.168.1.103) runs ONE thing: narrator-jim. Per Servers/Merle.md its
# venv is `pip install paho-mqtt pyyaml` -- deliberately NOT requirements.txt,
# because the vision stack (opencv/torch/ultralytics/fastapi/numpy) has no
# business on a Pi 5. Nothing enforced that. It held by luck: narrator.py
# imports only `bus`, and bus.py imports only paho.
#
# Luck is a bad contract when the failure mode is this one: add a convenience
# re-export to narration/__init__.py, or an `import perception` to narrator.py
# for one helper, and merle dies with `ModuleNotFoundError: cv2` -- not at
# review time, not in CI, but 60 seconds after a merge, when merle-autodeploy
# pulls and restarts a unit on a box you weren't looking at. #110 names this
# exact trap ("that boundary dies quietly") and asks for this test.
#
# It works by IMPORTING THE NARRATOR IN A SUBPROCESS WITH THE VISION MODULES
# POISONED: any attempt to import one raises. That tests the real import graph
# -- including transitive and lazy-at-module-scope imports -- rather than
# grepping for import statements, which a re-export in an __init__.py would
# sail straight past.
#
# It is deliberately NOT a mirror of merle's venv (uninstalling cv2 in CI to
# see what breaks): CI installs the full set for every other test, and a test
# that depends on what is absent from an environment is a test that passes for
# the wrong reason the day someone adds a dep.
# =============================================================================

import subprocess
import sys
import textwrap

# What merle's venv does NOT have. numpy is on the list because it's the one
# that looks harmless -- it arrives as an opencv/torch dependency, not on its
# own merit, and Merle.md's two-package venv doesn't include it.
VISION_DEPS = ("cv2", "torch", "ultralytics", "fastapi", "uvicorn", "numpy")

# What the Pi actually has (Servers/Merle.md step 3). The narrator may import
# these freely; the point of the test is everything NOT on this list.
PI_DEPS = ("paho", "yaml")


def _import_under_poison(target):
    """Import `target` in a subprocess where every VISION_DEPS module raises on
    import, and return (ok, message). A subprocess because the poisoning has to
    happen before the first import and can't be undone in-process -- pytest has
    already imported half of these for the other tests."""
    script = textwrap.dedent("""
        import sys

        BANNED = %r

        class Poison:
            def find_module(self, name, path=None):
                root = name.split(".")[0]
                return self if root in BANNED else None
            def load_module(self, name):
                raise ImportError(
                    "BOUNDARY VIOLATION: %%s is not on merle's venv" %% name)
            # PEP 451
            def find_spec(self, name, path=None, target=None):
                root = name.split(".")[0]
                if root in BANNED:
                    raise ImportError(
                        "BOUNDARY VIOLATION: %%s is not on merle's venv" %% name)
                return None

        sys.meta_path.insert(0, Poison())
        import %s
        print("OK")
    """) % (VISION_DEPS, target)
    p = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def test_narrator_imports_without_any_vision_dep():
    """The contract merle's venv depends on. If this fails, read the traceback:
    it names the module that reached for a vision dep, and merle would have
    died on it 60s after the merge."""
    ok, out = _import_under_poison("narration.narrator")
    assert ok, "narrator's import graph reached a vision dep:\n%s" % out


def test_bus_imports_without_any_vision_dep():
    """bus.py is the narrator's only local import, so it inherits the same
    contract -- and it's imported by every bus process on every box."""
    ok, out = _import_under_poison("bus")
    assert ok, "bus's import graph reached a vision dep:\n%s" % out


def test_narration_package_import_is_clean():
    """Importing the PACKAGE must not drag in the world either. This is the
    #110 trap stated directly: a convenience re-export in narration/__init__.py
    is how the boundary dies, and it would die here first."""
    ok, out = _import_under_poison("narration")
    assert ok, "narration/__init__.py is not empty enough:\n%s" % out


def test_the_poison_actually_bites():
    """A guard against the guard. If the poisoning silently stopped working,
    every test above would pass by doing nothing -- the exact failure mode this
    file exists to prevent, wearing a green check."""
    ok, out = _import_under_poison("cv2")
    assert not ok, "the poison didn't fire -- these tests prove nothing"
    assert "BOUNDARY VIOLATION" in out


def test_daemon_does_need_vision_deps():
    """The other half of the boundary: the daemon is SUPPOSED to want the
    vision stack. If this ever passes, the poison is aimed at nothing and the
    narrator tests above are vacuous."""
    ok, _ = _import_under_poison("vision.merle_daemon")
    assert not ok, ("vision.merle_daemon imported with the vision stack "
                    "poisoned -- the poison list must be wrong")
