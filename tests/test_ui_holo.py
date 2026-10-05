"""Mark LIII.2 holographic core geometry (ui_holo.py).

Verification level: GENUINE EXECUTION — ui_holo is pure numpy, so the real
HoloSphere is built and real frames are computed and rasterised here. The Qt
painting on top (ArcReactorHud in ui_command_center.py) needs PyQt6 and is
checked structurally in test_ui_structure.py and live on Windows.
"""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    import ui_holo as H
except Exception:                                   # pragma: no cover - numpy missing
    H = None


@unittest.skipIf(H is None, "numpy not available")
class HoloSphereTests(unittest.TestCase):
    def test_geometry_counts_scale_with_quality(self):
        hi, lo = H.HoloSphere("high").stats(), H.HoloSphere("low").stats()
        small = H.HoloSphere("high", compact=True).stats()
        self.assertGreater(hi["particles"], 2500)
        self.assertLess(lo["particles"], hi["particles"])
        self.assertLess(small["particles"], hi["particles"] * 0.5)
        self.assertGreater(hi["segments"], 300)

    def test_frame_is_finite_and_inside_the_canvas(self):
        s = H.HoloSphere("high")
        for t, amp, spin in ((0.0, 0.0, 1.0), (3.7, 0.9, 2.1), (120.0, 0.3, 0.4)):
            f = s.frame(t, 400, 300, 200, spin=spin, amp=amp, excite=0.5)
            self.assertTrue(f.points and f.lines)
            kinds = {k for _, k, *_ in f.lines}
            self.assertTrue({"ring", "tick", "track", "comet"} <= kinds, kinds)
            for L, big, xs, ys in f.points:
                self.assertTrue(0 <= L < H.LEVELS)
                self.assertTrue(np.isfinite(xs).all() and np.isfinite(ys).all())
                # a speaking pulse may push particles out a little, never off the canvas
                self.assertTrue((np.abs(xs - 400) < 200 * 1.8).all())
            for L, kind, x0, y0, x1, y1 in f.lines:
                self.assertTrue(np.isfinite(x0).all() and np.isfinite(y1).all())
            cx, cy, r, ci = f.core
            self.assertEqual((cx, cy), (400, 300))
            self.assertTrue(0 < ci <= 1.0)

    def test_speaking_pulse_grows_the_sphere(self):
        s = H.HoloSphere("medium")

        def spread(amp):
            f = s.frame(1.0, 0, 0, 100, amp=amp)
            xs = np.concatenate([p[2] for p in f.points])
            return float(np.percentile(np.abs(xs), 95))
        self.assertGreater(spread(1.0), spread(0.0) * 1.08)

    def test_motion_is_deterministic_and_animated(self):
        a = H.HoloSphere("low", seed=3).frame(2.0, 0, 0, 100)
        b = H.HoloSphere("low", seed=3).frame(2.0, 0, 0, 100)
        c = H.HoloSphere("low", seed=3).frame(2.5, 0, 0, 100)
        xa = np.concatenate([p[2] for p in a.points])
        self.assertTrue(np.allclose(xa, np.concatenate([p[2] for p in b.points])))
        xc = np.concatenate([p[2] for p in c.points])
        self.assertFalse(len(xa) == len(xc) and np.allclose(xa, xc))

    def test_frame_cost_is_small(self):
        s = H.HoloSphere("high")
        s.frame(0.0, 400, 300, 200)
        t = time.perf_counter()
        for i in range(20):
            s.frame(i * 0.033, 400, 300, 200, amp=0.4)
        per = (time.perf_counter() - t) / 20
        self.assertLess(per, 0.025, f"{per*1000:.1f} ms per frame")     # generous for slow CI

    def test_offscreen_render_is_centered_and_coloured(self):
        img = H.render_rgb(H.HoloSphere("low"), 1.0, size=200, color=(220, 52, 70))
        self.assertEqual(img.shape, (200, 200, 3))
        self.assertEqual(img.dtype, np.uint8)
        centre, corner = img[90:110, 90:110].mean(), img[:15, :15].mean()
        self.assertGreater(centre, corner + 40)
        r, g, b = (img[..., i].astype(float).mean() for i in range(3))
        self.assertGreater(r, b)                                # red theme stays red


if __name__ == "__main__":
    unittest.main()
