"""Mark LIII interface — STRUCTURAL validation (no Qt import).

PyQt6 isn't installable in the build sandbox, so these checks parse ui.py and
ui_command_center.py with `ast` instead of importing them:

  * both files parse;
  * every private attribute MainWindow's methods read (self._log, self._input,
    self._mute_btn, self.hud …) is either set by MainWindow itself or created
    by CommandCenter (`win._x = …`) — i.e. the new layout is a true drop-in
    for the classic one and main.py / overlays / camera keep working;
  * the classic layout is still reachable (fallback + "ui_layout": "classic");
  * the HUD subclass keeps HudCanvas._step (colours, voice reactivity) by NOT
    overriding it.

The rendering itself was verified by running tools/preview_ui.py on the
target Windows machine and inspecting screenshots (see CHANGELOG).
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cls(tree, name):
    return [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name][0]


class UIStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui_src = (ROOT / "ui.py").read_text(encoding="utf-8")
        cls.cc_src = (ROOT / "ui_command_center.py").read_text(encoding="utf-8")
        cls.ui = ast.parse(cls.ui_src)
        cls.cc = ast.parse(cls.cc_src)

    def test_command_center_is_a_drop_in(self):
        mw = _cls(self.ui, "MainWindow")
        classic_only = {"_build_classic_layout", "_build_header", "_build_left_panel", "_build_right_panel",
                        "_build_input_row", "_build_content_panel", "_build_footer"}
        used, assigned = set(), set()
        for fn in mw.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name in classic_only:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                    (assigned if isinstance(node.ctx, ast.Store) else used).add(node.attr)
        methods = {f.name for f in mw.body if isinstance(f, ast.FunctionDef)}
        cc_assigned = set(re.findall(r"win\.(_?\w+)\s*=", self.cc_src))
        missing = sorted(a for a in used
                         if a.startswith("_") and not a.endswith("_sig")
                         and a not in assigned and a not in methods and a not in cc_assigned)
        self.assertEqual(missing, [], f"CommandCenter does not provide: {missing}")
        self.assertIn("hud", cc_assigned)

    def test_classic_fallback_present(self):
        self.assertIn("def _build_classic_layout", self.ui_src)
        self.assertIn("self._build_classic_layout(face_path, _display)", self.ui_src)
        self.assertIn('"classic"', self.ui_src)

    def test_hud_keeps_phase3_state_logic(self):
        hud = _cls(self.cc, "ArcReactorHud")
        names = {f.name for f in hud.body if isinstance(f, ast.FunctionDef)}
        self.assertIn("paintEvent", names)
        for inherited in ("_step", "set_audio_level", "set_personality_mode", "_resolve_theme_key"):
            self.assertNotIn(inherited, names, f"{inherited} must stay inherited from HudCanvas")
        self.assertEqual([b.id for b in hud.bases if isinstance(b, ast.Name)], ["HudCanvas"])

    def test_only_http_and_local_quick_links(self):
        # quick-link targets from config must never become arbitrary commands
        self.assertIn('if re.match(r"^https?://", target)', self.cc_src)
        self.assertNotIn("shell=True", self.cc_src)

    def test_emergency_stop_wiring(self):
        # Phase 5: F12 + STOP button in both layouts, callback exposed on JarvisUI
        self.assertIn('QKeySequence("F12")', self.ui_src)
        self.assertIn("def _do_emergency_stop", self.ui_src)
        self.assertIn("def on_emergency_stop", self.ui_src)
        self.assertIn("self._stop_btn.clicked.connect(self._do_emergency_stop)", self.ui_src)
        self.assertIn("win._stop_btn.clicked.connect(win._do_emergency_stop)", self.cc_src)

    def test_crlf(self):
        for f in ("ui.py", "ui_command_center.py", "tools/preview_ui.py"):
            raw = (ROOT / f).read_bytes()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), f)


if __name__ == "__main__":
    unittest.main()
