"""
ui_holo.py — geometry for the holographic JARVIS core (Mark LIII.2).

The look follows the reference the user supplied (the film's JARVIS "mind"
sphere as recreated in the "Real JARVIS UI" video): a glowing core inside a
volumetric cloud of particles, wrapped in fragmented orbital rings — a dashed
equatorial "track" with tick marks, broken great-circle rings with radial
hairs, a couple of bright comet arcs — all turning slowly in 3-D.

This module is pure maths (numpy, no Qt): it builds the sphere once and, per
frame, returns what to draw — particle positions and line segments already
projected to screen space and grouped by brightness — so the Qt widget only
issues a handful of draw calls, and the same geometry can be rendered off-
screen for tests / previews without Qt.

    sphere = HoloSphere(quality="high")
    f = sphere.frame(t, cx, cy, R, spin=1.0, amp=0.0, excite=0.0)
    f.points  → [(level, big, xs, ys), …]      level 0..LEVELS-1 (brightness)
    f.lines   → [(level, kind, x0, y0, x1, y1), …]  kind: ring | tick | comet | track
    f.core    → (cx, cy, radius, intensity)
    f.brain   → (size, light) of the brain's heartbeat (Phase 9)

Phase 9 — ALIVE. Two systems, one machine:
  * THE HEARTBEAT BELONGS TO THE CORE. heart_wave() is one organic beat —
    pulse → STRONG pulse → brief contraction → expansion → pause. Only the
    brain answers it: its tangle of veins and the dust orbiting it expand and
    contract (core_warp, which is ~1 beyond r ≈ 0.6, so the sphere itself
    never grows or shrinks), brighten, and the strong pulse sends a small
    energy impulse out through the core region. The voice, too, is felt by
    the core only.
  * THE VEINS CARRY IT. A vascular tree of arteries grows from the brain to
    the shell (added with its own random stream — every existing filament,
    hair, dust grain and orbit is unchanged). VeinFlow is a pooled particle
    system: each strong pulse launches bursts into a few arteries at slightly
    different moments; a burst rides its trunk as one, splits where the
    artery forks, eases out of the brain, slows, runs on along the shell and
    fades. Between beats a few dim particles drift through the network.
  frame(..., parts="shell") is what never beats (the HUD caches it);
  frame(..., parts="core", beat=CoreBeat(...)) is the brain, drawn every
  frame; sphere.flow_frame(flow, ...) projects the pulses onto the veins.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

LEVELS = 5                                   # brightness buckets per primitive kind
QUALITY = {"low": 0.45, "medium": 0.75, "high": 1.0}

FLOW_DS = 0.01               # the vein routes are resampled every 0.01 (unit sphere)

# ─────────────────────────────────────────────────────────────────────────────
# Phase 9 — the heartbeat belongs to the core. One beat, in seconds after it
# starts (at a resting tempo; faster tempos compress the shape a little so a
# short pause always remains before the next beat):
#
#     pulse → STRONG pulse → brief contraction → expansion → pause
#
# The strong pulse sends an energy impulse out of the brain and a burst of
# light into a few arteries (VeinFlow); the expansion sends a small echo.
# ─────────────────────────────────────────────────────────────────────────────

HEART_EVENTS = ((0.07, 0.030, 0.40, 0.45),     # (centre s, width s, size, light): the first pulse
                (0.18, 0.032, 1.00, 1.00),     # the strong pulse — it powers the veins
                (0.29, 0.045, -0.45, -0.20),   # a brief contraction
                (0.41, 0.070, 0.28, 0.22))     # the expansion
HEART_MAIN_S = 0.18          # the strong pulse launches the main burst into the veins
HEART_ECHO_S = 0.41          # the expansion launches a small echo
IMPULSE_LIFE = 0.45          # seconds the energy impulse takes to cross the core region


# How alive the core and its veins are, per state (Friendly) — the HUD eases
# between these. Columns: tempo (bpm), beat strength, arteries fed per beat,
# share of their branches that carry it, flow speed, quiet drift particles a
# second, core glow, how long a pulse stays lit (s).
#                     bpm    strength trunks branch speed drift  glow  fade
LIFE_PROFILES = {"IDLE":      (56.0,  0.85,   3.0,   0.45,  0.85,  1.6,  1.00, 1.40),
                 "LISTENING": (66.0,  1.00,   3.5,   0.60,  1.15,  2.8,  1.12, 1.30),
                 "SPEAKING":  (70.0,  0.95,   3.5,   0.55,  1.00,  2.4,  1.22, 1.30),
                 "THINKING":  (84.0,  1.15,   5.0,   0.80,  1.40,  4.0,  1.20, 1.05),
                 "EXECUTING": (90.0,  1.20,   5.5,   0.85,  1.50,  4.5,  1.25, 1.00),
                 "WARNING":   (98.0,  1.20,   5.0,   0.75,  1.45,  3.5,  1.20, 1.00),
                 "ERROR":     (100.0, 1.25,   5.0,   0.75,  1.45,  3.5,  1.25, 1.00),
                 "MUTED":     (46.0,  0.55,   2.0,   0.30,  0.70,  0.8,  0.75, 1.40)}
# Serious mode: a stronger, slightly faster heart, faster circulation, a busier network
SERIOUS_GAIN = (1.06, 1.30, 1.25, 1.10, 1.18, 1.35, 1.20, 0.92)


def life_profile(state: str, serious: bool = False) -> tuple:
    """The profile for a state ("IDLE", "LISTENING", "SPEAKING", "THINKING",
    "EXECUTING", "WARNING", "ERROR", "MUTED"), scaled for Serious mode."""
    base = LIFE_PROFILES.get(state, LIFE_PROFILES["IDLE"])
    return tuple(b * g for b, g in zip(base, SERIOUS_GAIN)) if serious else base


def heart_timescale(period: float) -> float:
    """1 at resting tempos; < 1 when the heart beats fast, so the beat's shape
    is compressed and a pause still remains before the next one."""
    return min(1.0, max(0.2, float(period)) / 0.95) ** 0.5


def heart_wave(tau: float, period: float) -> tuple[float, float]:
    """(size, light) of the core `tau` seconds into a beat lasting `period`
    seconds: 0 at rest, ~1 at the strong pulse, below 0 in the contraction.
    Continuous across beats (the neighbouring beats' tails are included)."""
    k = heart_timescale(period)
    size = light = 0.0
    for t in (tau, tau - period, tau + period):
        for c, w, s, lt in HEART_EVENTS:
            g = math.exp(-((t - c * k) / (w * k)) ** 2)
            size += s * g
            light += lt * g
    return size, light


def impulse_front(age: float) -> tuple[float, float]:
    """The energy impulse `age` seconds after the strong pulse: (its radius as a
    fraction of the sphere, its strength 1 → 0). It never leaves the core
    region (radius ≤ 0.48)."""
    u = min(1.0, max(0.0, age / IMPULSE_LIFE))
    return 0.07 + 0.41 * (1.0 - (1.0 - u) ** 2), (1.0 - u) ** 1.5


@dataclass
class CoreBeat:
    """What the HUD tells the core each frame (Phase 9). The defaults are a
    resting core. Only the core reacts to any of this — never the shell."""
    size: float = 0.0          # heart_wave size (+ breathing), about -0.5 .. 1.2
    light: float = 0.0         # heart_wave light (+ breathing)
    impulse: float = -1.0      # seconds since the strong pulse (< 0 = no impulse)
    voice: float = 0.0         # live speech level 0..1 — subtle, around the core
    stir: float = 0.0          # inner turbulence 0..1 (thinking / executing)
    strength: float = 1.0      # how hard this heart beats (state × mode)


def core_warp(r, beat: CoreBeat):
    """Radial scale for points at radius r of the unit sphere: the brain expands
    and contracts with the beat (and a little with the voice), and the impulse
    pushes what it passes slightly outward. ~1 beyond r ≈ 0.6, so the shell —
    the sphere itself — never moves."""
    r = np.asarray(r, dtype=np.float64)
    k = 1.0 + (0.11 * beat.size * beat.strength + 0.05 * beat.voice) * np.exp(-(r / 0.30) ** 2)
    if 0.0 <= beat.impulse < IMPULSE_LIFE:
        front, fade = impulse_front(beat.impulse)
        k = k + 0.022 * fade * beat.strength * np.exp(-((r - front) / 0.05) ** 2) / np.maximum(r, 0.03)
    return k


def _crossed(prev: float, cur: float, c: float) -> bool:
    """Did a beat clock that went from `prev` to `cur` (seconds into the beat;
    it wraps to 0 at each new beat) pass the moment `c`?"""
    if prev < 0:
        return False
    if cur >= prev:
        return prev < c <= cur
    return c > prev or c <= cur


class HeartClock:
    """Phase 9 — the heart's clock, run by the HUD every frame (no Qt, so it is
    tested by real execution): eases the tempo (never jumps), finds the strong
    pulse and the echo, times the energy impulse, adds the slow breathing of
    the core, and returns the frame's CoreBeat plus the event ("main",
    "echo" or None) that tells the veins to receive a pulse."""

    BREATH_S = 4.8              # the core breathes even at rest

    def __init__(self, bpm: float = 56.0):
        self.bpm = float(bpm)
        self.beat = 0.0             # beats since start
        self.tau = -1.0             # seconds into the current beat (-1 = not beating)
        self.impulse = -1.0         # seconds since the last strong pulse (-1 = none)
        self.size = self.light = 0.0

    def advance(self, dt: float, now: float, bpm_target: float, strength: float = 1.0, voice: float = 0.0,
                stir: float = 0.0, beating: bool = True):
        event = None
        if self.impulse >= 0.0:
            self.impulse += dt
            if self.impulse > IMPULSE_LIFE:
                self.impulse = -1.0
        if beating:
            self.bpm += (bpm_target - self.bpm) * min(1.0, dt * 1.2)
            period = 60.0 / max(30.0, self.bpm)
            prev = self.tau
            self.beat += dt * self.bpm / 60.0
            self.tau = (self.beat % 1.0) * period
            self.size, self.light = heart_wave(self.tau, period)
            ks = heart_timescale(period)
            if _crossed(prev, self.tau, HEART_MAIN_S * ks):
                event = "main"
                self.impulse = (self.tau - HEART_MAIN_S * ks) % period
            elif _crossed(prev, self.tau, HEART_ECHO_S * ks):
                event = "echo"
        else:
            self.tau, self.size, self.light = -1.0, 0.0, 0.0
        breath = 0.06 * math.sin(now * 2.0 * math.pi / self.BREATH_S)
        return CoreBeat(size=self.size + breath, light=self.light + 0.5 * breath, impulse=self.impulse,
                        voice=voice, stir=stir, strength=strength), event


def _spin_about(P: np.ndarray, K: np.ndarray, th: np.ndarray) -> np.ndarray:
    """Rotate each point P[i] about its own unit axis K[i] by th[i] (Rodrigues)."""
    c, s = np.cos(th)[:, None], np.sin(th)[:, None]
    return P * c + np.cross(K, P) * s + K * np.sum(K * P, axis=1, keepdims=True) * (1.0 - c)


def _resample(P: np.ndarray, ds: float):
    """A polyline → (points every `ds` along it, its length, the arc length at each vertex)."""
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = np.arange(0.0, cum[-1] + 1e-9, ds)
    if len(s) < 2:
        s = np.array([0.0, cum[-1]])
    return np.stack([np.interp(s, cum, P[:, j]) for j in range(3)], axis=1), float(cum[-1]), cum


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def _basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = n / np.linalg.norm(n)
    a = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 0.0, 1.0])
    u = np.cross(n, a)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return u, v


@dataclass
class _Ring:
    kind: str                      # track | ring | comet | inner
    u: np.ndarray
    v: np.ndarray
    n: np.ndarray
    radius: float
    omega: float                   # own spin, rad/s
    t0: np.ndarray                 # segment start angles
    t1: np.ndarray                 # segment end angles
    offset: float = 0.0            # shift along the normal (parallel lines of a track)
    gain: float = 1.0
    ticks: np.ndarray | None = None   # angles of tick marks (perpendicular to the ring)
    tick_len: float = 0.0


@dataclass
class Frame:
    points: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    core: tuple = (0.0, 0.0, 0.0, 0.0)
    brain: tuple = (0.0, 0.0)   # Phase 9: (size, light) of the brain's heartbeat


class HoloSphere:
    has_veins = False             # Phase 9: the particle style has no veins for the pulses

    def __init__(self, quality: str = "high", seed: int = 7, compact: bool = False):
        rng = np.random.default_rng(seed)
        q = QUALITY.get(quality, 1.0) * (0.42 if compact else 1.0)
        self.compact = compact

        # ── particle cloud: dense towards the centre, a thin shell of dust ──
        n = int(3000 * q)
        d = rng.normal(size=(n, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        r = rng.random(n) ** 1.4 * 0.97
        shell = rng.random(n) < 0.16                           # some dust near the surface
        r[shell] = 0.82 + rng.random(shell.sum()) * 0.2
        self.p_pos = d * r[:, None]
        self.p_rad = r
        self.p_base = np.clip(0.28 + 0.72 * np.clip(1.0 - r, 0, 1) ** 1.4 + rng.normal(0, 0.08, n), 0.08, 1.0)
        self.p_big = rng.random(n) < 0.06
        self.p_omega = (0.05 + 0.25 * (1.0 - r)) * rng.choice([1.0, 1.0, 1.0, -1.0], n)   # swirl
        self.p_tw_w = rng.uniform(0.6, 3.2, n)
        self.p_tw_p = rng.uniform(0, 2 * math.pi, n)
        self.p_jit = rng.normal(0, 1, (n, 3))

        # ── rings ──
        self.rings: list[_Ring] = []

        def dashes(density: float, lo: float, hi: float, gap_lo: float, gap_hi: float):
            a, s, e = rng.uniform(0, 2 * math.pi), [], []
            end = a + 2 * math.pi
            while a < end:
                ln = math.radians(rng.uniform(lo, hi))
                if rng.random() < density:
                    s.append(a)
                    e.append(min(a + ln, end))
                a += ln + math.radians(rng.uniform(gap_lo, gap_hi))
            return np.array(s), np.array(e)

        def split(t0, t1, step=math.radians(5)):      # long dashes → short chords
            s, e = [], []
            for a, b in zip(t0, t1):
                k = max(1, int(math.ceil((b - a) / step)))
                edges = np.linspace(a, b, k + 1)
                s.extend(edges[:-1])
                e.extend(edges[1:])
            return np.array(s), np.array(e)

        # equatorial data track: three parallel dashed lines + ticks
        tn = _rot_x(math.radians(-8)) @ np.array([0.0, 1.0, 0.0])
        tu, tv = _basis(tn)
        for off, dens, g in ((-0.028, 0.85, 0.75), (0.0, 0.95, 1.0), (0.028, 0.85, 0.75)):
            s, e = split(*dashes(dens, 2.0, 7.0, 0.6, 2.2))
            self.rings.append(_Ring("track", tu, tv, tn, 1.1, 0.10, s, e, offset=off, gain=g))
        tk = np.arange(0, 2 * math.pi, math.radians(4.0 if not compact else 8.0))
        self.rings[-2].ticks = tk[rng.random(len(tk)) < 0.55]
        self.rings[-2].tick_len = 0.05

        # fragmented great circles on the shell
        for k in range(5 if not compact else 3):
            nn = rng.normal(size=3)
            u, v = _basis(nn)
            s, e = split(*dashes(0.75, 3.0, 26.0, 2.0, 16.0))
            ring = _Ring("ring", u, v, nn / np.linalg.norm(nn), rng.uniform(0.97, 1.04),
                         rng.uniform(-0.18, 0.18), s, e, gain=rng.uniform(0.55, 0.95))
            ring.ticks = rng.uniform(0, 2 * math.pi, 26 if not compact else 12)
            ring.tick_len = 0.05
            self.rings.append(ring)

        # bright comet arcs through the volume
        for k in range(2):
            nn = rng.normal(size=3)
            u, v = _basis(nn)
            a0 = rng.uniform(0, 2 * math.pi)
            seg = np.linspace(a0, a0 + math.radians(rng.uniform(95, 150)), 34)
            self.rings.append(_Ring("comet", u, v, nn / np.linalg.norm(nn), rng.uniform(0.92, 1.0),
                                    rng.choice([-1, 1]) * rng.uniform(0.35, 0.6), seg[:-1], seg[1:],
                                    gain=1.0))

        # faint inner orbits
        for k in range(2):
            nn = rng.normal(size=3)
            u, v = _basis(nn)
            s, e = split(*dashes(0.9, 1.0, 4.0, 2.0, 5.0))
            self.rings.append(_Ring("inner", u, v, nn / np.linalg.norm(nn), rng.uniform(0.4, 0.62),
                                    rng.uniform(-0.3, 0.3), s, e, gain=0.45))

        # radial hairs on the surface
        m = 250 if not compact else 90
        hd = rng.normal(size=(m, 3))
        self.h_dir = hd / np.linalg.norm(hd, axis=1, keepdims=True)
        self.h_r0 = rng.uniform(0.97, 1.03, m)
        self.h_len = rng.uniform(0.012, 0.06, m)
        self.h_gain = rng.uniform(0.3, 0.9, m)

    # ── per frame ────────────────────────────────────────────────────────────
    @staticmethod
    def _project(P: np.ndarray, cx: float, cy: float, R: float, cam: float = 3.4):
        f = cam / (cam - P[:, 2])
        return cx + P[:, 0] * f * R, cy - P[:, 1] * f * R, P[:, 2]

    @staticmethod
    def _levels(alpha: np.ndarray) -> np.ndarray:
        return np.clip((np.nan_to_num(alpha) * LEVELS).astype(np.int32), 0, LEVELS - 1)

    def frame(self, t: float, cx: float, cy: float, R: float, spin: float = 1.0, amp: float = 0.0,
              excite: float = 0.0, tilt: float = -0.42, parts: str = "all",
              beat: "CoreBeat | None" = None) -> Frame:
        fr = Frame()
        b = beat if beat is not None else CoreBeat(voice=amp)    # Phase 9: the heart is the core's
        fr.brain = (b.size * b.strength + 0.5 * b.voice, b.light * b.strength + 0.35 * b.voice)
        fr.core = (cx, cy, R * 0.3, min(1.0, 0.55 + excite * 0.15))
        if parts == "core":
            return fr
        yaw = t * 0.16 * spin
        G = _rot_x(tilt + 0.05 * math.sin(t * 0.23)) @ _rot_y(yaw)

        # particles: per-particle swirl around Y, then the global rotation
        a = self.p_omega * t * spin
        ca, sa = np.cos(a), np.sin(a)
        x, y, z = self.p_pos[:, 0], self.p_pos[:, 1], self.p_pos[:, 2]
        P = np.stack([x * ca + z * sa, y, -x * sa + z * ca], axis=1)
        if amp > 0.001 or excite > 0.001:
            P = P * (1.0 + (amp * 0.22 + excite * 0.05) * (0.4 + self.p_rad))[:, None] \
                + self.p_jit * (amp * 0.012)
        P = P @ G.T
        xs, ys, zs = self._project(P, cx, cy, R)
        depth = 0.32 + 0.68 * np.clip((zs + 1.0) * 0.5, 0, 1)
        tw = 0.62 + 0.38 * np.sin(t * self.p_tw_w * (1.0 + amp * 1.5) + self.p_tw_p)
        alpha = np.clip(self.p_base * depth * tw * (1.0 + amp * 0.6), 0.0, 1.0)
        lv = self._levels(alpha)
        for big in (False, True):
            sel_big = self.p_big if big else ~self.p_big
            for L in range(LEVELS):
                m = sel_big & (lv == L)
                if m.any():
                    fr.points.append((L, big, xs[m], ys[m]))

        # rings
        for ring in self.rings:
            w = ring.omega * t * spin
            t0, t1 = ring.t0 + w, ring.t1 + w
            base = ring.n * ring.offset
            A = (np.cos(t0)[:, None] * ring.u + np.sin(t0)[:, None] * ring.v) * ring.radius + base
            B = (np.cos(t1)[:, None] * ring.u + np.sin(t1)[:, None] * ring.v) * ring.radius + base
            A, B = A @ G.T, B @ G.T
            x0, y0, z0 = self._project(A, cx, cy, R)
            x1, y1, z1 = self._project(B, cx, cy, R)
            zc = (z0 + z1) * 0.5
            if ring.kind == "comet":
                ramp = np.linspace(0.05, 1.0, len(zc)) ** 1.6
                al = ramp * (0.45 + 0.55 * np.clip((zc + 1) * 0.5, 0, 1)) * (1.0 + amp * 0.5)
            else:
                al = ring.gain * (0.18 + 0.82 * np.clip((zc + 1) * 0.5, 0, 1) ** 1.3) * (1.0 + amp * 0.35)
            al = np.clip(al, 0, 1)
            lv = self._levels(al)
            for L in range(LEVELS):
                m = lv == L
                if m.any():
                    fr.lines.append((L, ring.kind, x0[m], y0[m], x1[m], y1[m]))
            if ring.ticks is not None and len(ring.ticks):
                ta = ring.ticks + w
                Pt = (np.cos(ta)[:, None] * ring.u + np.sin(ta)[:, None] * ring.v) * ring.radius + base
                if ring.kind == "track":           # ticks cross the track (along the normal)
                    Q0 = Pt - ring.n * ring.tick_len * 0.5
                    Q1 = Pt + ring.n * ring.tick_len * 0.5
                else:                              # radial hairs off the ring
                    Q0 = Pt
                    Q1 = Pt * (1.0 + ring.tick_len)
                Q0, Q1 = Q0 @ G.T, Q1 @ G.T
                a0, b0, c0 = self._project(Q0, cx, cy, R)
                a1, b1, c1 = self._project(Q1, cx, cy, R)
                al = np.clip(ring.gain * 0.85 * (0.15 + 0.85 * np.clip((c0 + 1) * 0.5, 0, 1) ** 1.3), 0, 1)
                lv = self._levels(al)
                for L in range(LEVELS):
                    m = lv == L
                    if m.any():
                        fr.lines.append((L, "tick", a0[m], b0[m], a1[m], b1[m]))

        # surface hairs
        Hd = self.h_dir @ G.T
        r0 = self.h_r0 * (1.0 + amp * 0.08)
        H0 = Hd * r0[:, None]
        H1 = Hd * (r0 + self.h_len * (1.0 + amp * 1.6))[:, None]
        a0, b0, c0 = self._project(H0, cx, cy, R)
        a1, b1, _ = self._project(H1, cx, cy, R)
        al = np.clip(self.h_gain * (0.1 + 0.9 * np.clip((c0 + 1) * 0.5, 0, 1) ** 1.5), 0, 1)
        lv = self._levels(al)
        for L in range(LEVELS):
            m = lv == L
            if m.any():
                fr.lines.append((L, "tick", a0[m], b0[m], a1[m], b1[m]))

        return fr

    def stats(self) -> dict:
        segs = sum(len(r.t0) + (0 if r.ticks is None else len(r.ticks)) for r in self.rings)
        return {"particles": len(self.p_pos), "segments": segs + len(self.h_dir)}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7 — the filament core (the user's second reference video) wrapped in the
# orbit rings of the current core (the first). Same Frame format, so the Qt
# widget, the ambient orb and the off-screen renderer draw it unchanged.
# ─────────────────────────────────────────────────────────────────────────────

class FilamentSphere:
    """A sphere woven from hundreds of thin glowing filaments — dense, tangled,
    brightest at the rim and in a hot core — with fine hairs bristling off the
    surface, circled by fragmented orbit rings, a dashed data track and comet
    arcs that sit OUTSIDE the sphere and turn on their own axes.

        f.lines kinds: filament · hot (core tangle) · hair · track · ring · tick · comet
                       · pulse (Phase 8: the veins the heartbeat is running through)
        f.points     : a little dust in the core
    Light travels along the filaments (a slow wave) and the orbits speed up
    while thinking/executing. Phase 9: the brain (hot tangle + dust) beats and
    answers the voice; arteries carry the pulses it sends (VeinFlow); the
    shell itself never beats."""

    def __init__(self, quality: str = "high", seed: int = 11, compact: bool = False):
        rng = np.random.default_rng(seed)
        q = QUALITY.get(quality, 1.0) * (0.45 if compact else 1.0)
        self.compact = compact

        # ── filaments: smooth random walks hugging the inside of the shell ──
        def walk(n_steps: int, r_lo: float, r_hi: float, step: float, curl: float, dive: float):
            d0 = rng.normal(size=3)
            p = d0 / np.linalg.norm(d0) * rng.uniform(r_lo, r_hi)
            v = rng.normal(size=3)
            pts = [p.copy()]
            for _ in range(n_steps):
                v = v + rng.normal(size=3) * curl
                rad = p / (np.linalg.norm(p) + 1e-9)
                v = v - rad * np.dot(v, rad) * (1.0 - dive)          # mostly tangential
                v /= np.linalg.norm(v) + 1e-9
                p = p + v * step
                n = np.linalg.norm(p)
                if n > r_hi:                                          # bounce off the shell
                    p = p / n * (2 * r_hi - n)
                elif n < r_lo * 0.6:
                    p = p / (n + 1e-9) * r_lo * 0.6
                pts.append(p.copy())
            return np.array(pts)

        fil_pts, seg_a, seg_b, seg_phase, seg_gain, seg_hot, seg_pos = [], [], [], [], [], [], []
        base = 0

        def add(curve: np.ndarray, gain: float, hot: bool):
            nonlocal base
            k = len(curve)
            fil_pts.append(curve)
            ph = rng.uniform(0, 2 * math.pi)
            for j in range(k - 1):
                seg_a.append(base + j)
                seg_b.append(base + j + 1)
                seg_phase.append(ph + j * 0.42)
                seg_gain.append(gain)
                seg_hot.append(hot)
                seg_pos.append(j / max(1, k - 2))
            base += k

        n_fil = int(220 * q)
        for _ in range(n_fil):
            if rng.random() < 0.78:            # shell weave
                c = walk(int(rng.integers(20, 38)), 0.9, 1.0, 0.062, 0.5, 0.06)
                add(c, rng.uniform(0.45, 1.0), False)
            else:                              # filaments that cross the volume
                c = walk(int(rng.integers(14, 26)), 0.3, 0.95, 0.08, 0.7, 0.5)
                add(c, rng.uniform(0.25, 0.65), False)
        for _ in range(int(26 * max(q, 0.5))):  # the hot core tangle
            c = walk(int(rng.integers(10, 18)), 0.03, 0.24, 0.045, 0.9, 0.6)
            add(c, rng.uniform(0.7, 1.0), True)
        self.f_pts = np.concatenate(fil_pts, axis=0)
        self.f_rad = np.linalg.norm(self.f_pts, axis=1)
        self.s_a = np.array(seg_a)
        self.s_b = np.array(seg_b)
        self.s_phase = np.array(seg_phase)
        self.s_gain = np.array(seg_gain)
        self.s_hot = np.array(seg_hot)
        self.s_taper = np.sin(np.clip(np.array(seg_pos), 0, 1) * math.pi) ** 0.6   # fade at the ends
        self.f_jit = rng.normal(0, 1, self.f_pts.shape)

        # ── dust in the core ──
        n = int(260 * q)
        d = rng.normal(size=(n, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        r = rng.random(n) ** 2.2 * 0.5
        self.p_pos = d * r[:, None]
        self.p_base = np.clip(0.5 + 0.5 * (1 - r / 0.5), 0.2, 1.0)
        self.p_big = rng.random(n) < 0.08
        self.p_tw = rng.uniform(0, 2 * math.pi, n)

        # ── hairs bristling off the surface ──
        m = int(480 * q)
        hd = rng.normal(size=(m, 3))
        self.h_dir = hd / np.linalg.norm(hd, axis=1, keepdims=True)
        self.h_r0 = rng.uniform(0.99, 1.01, m)
        self.h_len = rng.uniform(0.01, 0.045, m) * np.where(rng.random(m) < 0.1, 2.0, 1.0)
        self.h_gain = rng.uniform(0.25, 0.85, m)

        # ── the orbits of the current core, moved OUTSIDE the sphere ──
        self.rings: list[_Ring] = []

        def dashes(density, lo, hi, gap_lo, gap_hi):
            a, s, e = rng.uniform(0, 2 * math.pi), [], []
            end = a + 2 * math.pi
            while a < end:
                ln = math.radians(rng.uniform(lo, hi))
                if rng.random() < density:
                    s.append(a)
                    e.append(min(a + ln, end))
                a += ln + math.radians(rng.uniform(gap_lo, gap_hi))
            return np.array(s), np.array(e)

        def split(t0, t1, step=math.radians(4)):
            s, e = [], []
            for a, b in zip(t0, t1):
                k = max(1, int(math.ceil((b - a) / step)))
                edges = np.linspace(a, b, k + 1)
                s.extend(edges[:-1])
                e.extend(edges[1:])
            return np.array(s), np.array(e)

        tn = _rot_x(math.radians(-10)) @ np.array([0.0, 1.0, 0.0])
        tu, tv = _basis(tn)
        for off, dens, g in ((-0.03, 0.85, 0.7), (0.0, 0.95, 1.0), (0.03, 0.85, 0.7)):
            s, e = split(*dashes(dens, 2.0, 7.0, 0.6, 2.2))
            self.rings.append(_Ring("track", tu, tv, tn, 1.30, 0.10, s, e, offset=off, gain=g))
        tk = np.arange(0, 2 * math.pi, math.radians(4.0 if not compact else 8.0))
        self.rings[-2].ticks = tk[rng.random(len(tk)) < 0.55]
        self.rings[-2].tick_len = 0.05
        for k in range(4 if not compact else 2):
            nn = rng.normal(size=3)
            u, v = _basis(nn)
            s, e = split(*dashes(0.72, 4.0, 30.0, 3.0, 18.0))
            ring = _Ring("ring", u, v, nn / np.linalg.norm(nn), rng.uniform(1.12, 1.22),
                         rng.choice([-1, 1]) * rng.uniform(0.06, 0.2), s, e, gain=rng.uniform(0.5, 0.9))
            ring.ticks = rng.uniform(0, 2 * math.pi, 22 if not compact else 10)
            ring.tick_len = 0.045
            self.rings.append(ring)
        for k in range(2):
            nn = rng.normal(size=3)
            u, v = _basis(nn)
            a0 = rng.uniform(0, 2 * math.pi)
            seg = np.linspace(a0, a0 + math.radians(rng.uniform(100, 160)), 40)
            self.rings.append(_Ring("comet", u, v, nn / np.linalg.norm(nn), rng.uniform(1.14, 1.24),
                                    rng.choice([-1, 1]) * rng.uniform(0.3, 0.55), seg[:-1], seg[1:], gain=1.0))

        # ── Phase 9 — everything below uses its own random streams, so every
        # filament, dust grain, hair and orbit above is exactly as it was ──
        self.has_veins = True
        self.n_base_pts, self.n_base_segs = len(self.f_pts), len(self.s_a)
        hot = np.flatnonzero(self.s_hot)                 # the brain's own tangle: drawn live by the HUD
        self.hot_segs = hot
        self.hot_pts = np.unique(np.concatenate([self.s_a[hot], self.s_b[hot]]))
        remap = np.full(len(self.f_pts), -1, dtype=np.int64)
        remap[self.hot_pts] = np.arange(len(self.hot_pts))
        self.hot_a, self.hot_b = remap[self.s_a[hot]], remap[self.s_b[hot]]
        self._shell_fils = self._filament_ranges()
        self._build_arteries(seed, q)
        self.shell_segs = np.flatnonzero(~self.s_hot)     # everything else: the cached layer
        vr = np.random.default_rng(seed + 9100)
        n = len(self.p_pos)
        ax = np.array([0.0, 1.0, 0.0]) + vr.normal(0.0, 0.45, (n, 3))   # the dust orbits the brain,
        self.d_axis = ax / np.linalg.norm(ax, axis=1, keepdims=True)    # mostly the same way round
        self.d_r = np.linalg.norm(self.p_pos, axis=1)
        self.d_omega = (vr.uniform(0.35, 0.85, n) * np.sqrt(0.2 / (self.d_r + 0.05))
                        * np.where(vr.random(n) < 0.85, 1.0, -1.0))
        self.f_tp = vr.uniform(0.0, 2 * math.pi, len(self.f_pts))       # turbulence phase per vein point
        self._build_flow_paths(seed)

    # ── Phase 9: the vascular tree and the routes the pulses take ──────────────
    def _filament_ranges(self) -> list:
        """(first, last) point index of every shell-weave filament (mean radius > 0.88)."""
        a, b = self.s_a, self.s_b
        brk = np.flatnonzero(a[1:] != b[:-1]) + 1
        starts = np.concatenate([[0], brk])
        ends = np.concatenate([brk - 1, [len(a) - 1]])
        out = []
        for s0, s1 in zip(starts, ends):
            if self.s_hot[s0]:
                continue
            i0, i1 = int(a[s0]), int(b[s1])
            if np.linalg.norm(self.f_pts[i0:i1 + 1], axis=1).mean() > 0.88:
                out.append((i0, i1))
        return out

    def _build_arteries(self, seed: int, q: float) -> None:
        """Arteries grow from the brain out to the shell, forking on the way (a
        trunk, one or two branches, sometimes a twig). They are drawn like every
        other filament — thin and dim at rest — and the heart's pulses ride them.
        self.trees[i] = [(route points root → leaf, [(vertex where an edge
        starts, edge id), ...]), ...]: routes of one tree share their first
        edges, so a burst rides the trunk as one and splits where they part."""
        vr = np.random.default_rng(seed + 9000)
        step = 0.045

        def grow(p, v, r_end):
            pts = [p.copy()]
            for _ in range(70):
                rad = p / (np.linalg.norm(p) + 1e-9)
                v = v + vr.normal(size=3) * 0.30
                v = v / np.linalg.norm(v)
                v = v * 0.70 + rad * 0.30                     # wander, but keep heading out
                v = v / np.linalg.norm(v)
                p = p + v * step
                nr = np.linalg.norm(p)
                if nr >= r_end:
                    pts.append(p / nr * r_end)
                    break
                pts.append(p.copy())
            return np.array(pts)

        def fork(curve, lo, hi, a_lo, a_hi):
            rr = np.linalg.norm(curve, axis=1)
            cand = np.flatnonzero((rr > lo) & (rr < hi))
            cand = cand[cand < len(curve) - 2]
            if not len(cand):
                return None
            k = int(vr.choice(cand))
            d = curve[k + 1] - curve[k]
            d = d / np.linalg.norm(d)
            side = vr.normal(size=3)
            side = side - side.dot(d) * d
            side = side / np.linalg.norm(side)
            ang = math.radians(vr.uniform(a_lo, a_hi))
            return k, grow(curve[k].copy(), d * math.cos(ang) + side * math.sin(ang), vr.uniform(0.90, 0.98))

        n_tree = max(4, int(round(12 * max(q, 0.4))))
        curves, gains = [], []
        self.trees = []
        eid = 0
        for _ in range(n_tree):
            d = vr.normal(size=3)
            d = d / np.linalg.norm(d)
            trunk = grow(d * vr.uniform(0.04, 0.08), d, vr.uniform(0.93, 0.985))
            curves.append(trunk)
            gains.append(vr.uniform(0.40, 0.62))
            forks = []
            f1 = fork(trunk, 0.26, 0.50, 28, 52)
            if f1 is not None:
                forks.append(f1)
            if vr.random() < 0.6:
                f2 = fork(trunk, 0.50, 0.76, 24, 46)
                if f2 is not None and all(f2[0] != f[0] for f in forks):
                    forks.append(f2)
            forks.sort(key=lambda f: f[0])
            t_edges = [(0, eid)]
            eid += 1
            for k, _ in forks:
                t_edges.append((k, eid))
                eid += 1
            routes = [(trunk, t_edges)]
            for k, br in forks:
                curves.append(br)
                gains.append(vr.uniform(0.32, 0.52))
                pre = [e for e in t_edges if e[0] < k]
                b_id = eid
                eid += 1
                bpath = np.concatenate([trunk[:k + 1], br[1:]])
                tw = fork(br, 0.58, 0.86, 22, 42) if vr.random() < 0.45 else None
                if tw is None:
                    routes.append((bpath, pre + [(k, b_id)]))
                else:
                    k2, twig = tw
                    curves.append(twig)
                    gains.append(vr.uniform(0.28, 0.45))
                    c_id, t_id = eid, eid + 1
                    eid += 2
                    routes.append((bpath, pre + [(k, b_id), (k + k2, c_id)]))
                    routes.append((np.concatenate([trunk[:k + 1], br[1:k2 + 1], twig[1:]]),
                                   pre + [(k, b_id), (k + k2, t_id)]))
            self.trees.append(routes)

        # draw them like every other filament (appended: nothing before them changes)
        base = len(self.f_pts)
        sa, sb, ph, gn, pos, pts = [], [], [], [], [], []
        for c, g in zip(curves, gains):
            k = len(c)
            p0 = vr.uniform(0, 2 * math.pi)
            for j in range(k - 1):
                sa.append(base + j)
                sb.append(base + j + 1)
                ph.append(p0 + j * 0.42)
                gn.append(g)
                pos.append(j / max(1, k - 2))
            pts.append(c)
            base += k
        allp = np.concatenate(pts)
        self.art_seg0 = len(self.s_a)                       # the first artery segment
        self.f_pts = np.concatenate([self.f_pts, allp])
        self.f_rad = np.linalg.norm(self.f_pts, axis=1)
        self.s_a = np.concatenate([self.s_a, np.array(sa, dtype=self.s_a.dtype)])
        self.s_b = np.concatenate([self.s_b, np.array(sb, dtype=self.s_b.dtype)])
        self.s_phase = np.concatenate([self.s_phase, ph])
        self.s_gain = np.concatenate([self.s_gain, gn])
        self.s_hot = np.concatenate([self.s_hot, np.zeros(len(sa), dtype=bool)])
        self.s_taper = np.concatenate([self.s_taper, np.sin(np.clip(np.array(pos), 0, 1) * math.pi) ** 0.6])
        self.f_jit = np.concatenate([self.f_jit, vr.normal(0, 1, allp.shape)])

    def _build_flow_paths(self, seed: int) -> None:
        """Every route a pulse can take, resampled every FLOW_DS: each artery
        root → leaf, continued for a while along the shell weave where the leaf
        meets it; plus shell routes (both ways) for the quiet drift between beats."""
        vr = np.random.default_rng(seed + 9200)
        fils = self._shell_fils
        shell_idx = np.concatenate([np.arange(i0, i1 + 1) for i0, i1 in fils])
        owner = np.concatenate([np.full(i1 - i0 + 1, j) for j, (i0, i1) in enumerate(fils)])
        routes, edges = [], []
        self.tree_paths = []
        for tree in self.trees:
            ids = []
            for P, ed in tree:
                d = np.linalg.norm(self.f_pts[shell_idx] - P[-1], axis=1)
                j = int(np.argmin(d))
                if d[j] < 0.14:                               # run on along the shell weave
                    i0, i1 = fils[int(owner[j])]
                    at = int(shell_idx[j])
                    if vr.random() < 0.5:
                        cont = self.f_pts[at:min(i1, at + 9) + 1]
                    else:
                        cont = self.f_pts[max(i0, at - 9):at + 1][::-1]
                    if len(cont) >= 2:
                        P = np.concatenate([P, cont])
                ids.append(len(routes))
                routes.append(P)
                edges.append(ed)
            self.tree_paths.append(ids)
        n_art = len(routes)
        pick = vr.permutation(len(fils))[:max(8, min(len(fils), 44))]
        for j in pick:                                        # quiet drift: shell routes, both ways
            i0, i1 = fils[int(j)]
            P = self.f_pts[i0:i1 + 1]
            for Q in (P, P[::-1]):
                routes.append(Q)
                edges.append([(0, -1)])
        self.drift_routes = np.concatenate([np.arange(n_art, len(routes)),
                                            np.arange(n_art)])     # arteries drift outward too
        res = [_resample(P, FLOW_DS) for P in routes]
        m = max(len(r[0]) for r in res)
        self.flow_P = np.zeros((len(res), m, 3))
        self.p_len = np.zeros(len(res))
        self.p_m = np.zeros(len(res), dtype=np.int64)
        self.pe_s = np.full((len(res), 4), np.inf)
        self.pe_id = np.full((len(res), 4), -1, dtype=np.int64)
        for i, ((Q, L, cum), ed) in enumerate(zip(res, edges)):
            self.flow_P[i, :len(Q)] = Q
            self.flow_P[i, len(Q):] = Q[-1]
            self.p_len[i], self.p_m[i] = L, len(Q)
            for j, (v, e) in enumerate(ed[:4]):
                self.pe_s[i, j] = cum[v]
                self.pe_id[i, j] = e
        self.pe_s[:, 0] = 0.0

    def view(self, t: float, spin: float = 1.0, tilt: float = -0.38) -> np.ndarray:
        """The sphere's orientation at time t — shared by the layer, the core and the pulses."""
        return _rot_x(tilt + 0.05 * math.sin(t * 0.21)) @ _rot_y(t * 0.11 * spin)

    def flow_frame(self, flow: "VeinFlow", t: float, cx: float, cy: float, R: float, spin: float = 1.0,
                   tilt: float = -0.38, beat: "CoreBeat | None" = None):
        """The pulses in the veins, projected exactly like the veins at time t: (lines, heads)."""
        return flow.frame(self.view(t, spin, tilt), cx, cy, R * 0.82, beat)

    _project = staticmethod(HoloSphere._project)
    _levels = staticmethod(HoloSphere._levels)

    def frame(self, t: float, cx: float, cy: float, R: float, spin: float = 1.0, amp: float = 0.0,
              excite: float = 0.0, tilt: float = -0.38, parts: str = "all",
              beat: "CoreBeat | None" = None) -> Frame:
        """parts: "all" | "shell" (veins, hairs, orbits, the steady glow — what
        never beats; the HUD caches it) | "core" (the brain: its tangle of veins
        and the dust orbiting it — beating, drawn every frame). Only the core
        feels the heartbeat (`beat`) and the voice (`beat.voice`, or `amp`)."""
        fr = Frame()
        R = R * 0.82                                  # leave room for the orbits outside
        G = self.view(t, spin, tilt)
        fr.core = (cx, cy, R * 0.26, min(1.0, 0.7 + excite * 0.15))   # the steady nebula glow
        if parts in ("all", "shell"):
            self._shell(fr, t, G, cx, cy, R, spin, excite)
        if parts in ("all", "core"):
            self._core(fr, t, G, cx, cy, R, excite, beat if beat is not None else CoreBeat(voice=amp))
        return fr

    def _shell(self, fr: Frame, t: float, G: np.ndarray, cx: float, cy: float, R: float, spin: float,
               excite: float) -> None:
        # veins: the shell weave, the volume filaments and the arteries (light travels slowly along them)
        P = self.f_pts if excite <= 0.001 else self.f_pts * (1.0 + excite * 0.02)
        xs, ys, zs = self._project(P @ G.T, cx, cy, R)
        sg = self.shell_segs
        a, b = self.s_a[sg], self.s_b[sg]
        zc = (zs[a] + zs[b]) * 0.5
        depth = 0.30 + 0.70 * np.clip((zc + 1.0) * 0.5, 0, 1) ** 1.2
        flow = 0.55 + 0.45 * np.sin(self.s_phase[sg] - t * 1.6)
        rr = np.hypot((xs[a] + xs[b]) * 0.5 - cx, (ys[a] + ys[b]) * 0.5 - cy) / max(R, 1.0)
        limb = 0.55 + 0.85 * np.clip(rr, 0, 1.2) ** 6           # the bright rim of the reference
        al = np.clip(self.s_gain[sg] * self.s_taper[sg] * depth * flow * limb * (0.9 + excite * 0.15), 0, 1)
        lv = self._levels(al)
        for L in range(LEVELS):
            m = lv == L
            if m.any():
                fr.lines.append((L, "filament", xs[a[m]], ys[a[m]], xs[b[m]], ys[b[m]]))

        # hairs
        Hd = self.h_dir @ G.T
        H0 = Hd * self.h_r0[:, None]
        H1 = Hd * (self.h_r0 + self.h_len)[:, None]
        a0, b0, c0 = self._project(H0, cx, cy, R)
        a1, b1, _ = self._project(H1, cx, cy, R)
        hal = np.clip(self.h_gain * (0.15 + 0.85 * np.clip((c0 + 1) * 0.5, 0, 1) ** 1.4), 0, 1)
        hlv = self._levels(hal)
        for L in range(LEVELS):
            m = hlv == L
            if m.any():
                fr.lines.append((L, "hair", a0[m], b0[m], a1[m], b1[m]))

        # orbits (own spin, faster while thinking)
        for ring in self.rings:
            w = ring.omega * t * spin * (1.0 + excite * 0.8)
            t0, t1 = ring.t0 + w, ring.t1 + w
            base = ring.n * ring.offset
            A = (np.cos(t0)[:, None] * ring.u + np.sin(t0)[:, None] * ring.v) * ring.radius + base
            B = (np.cos(t1)[:, None] * ring.u + np.sin(t1)[:, None] * ring.v) * ring.radius + base
            A, B = A @ G.T, B @ G.T
            x0, y0, z0 = self._project(A, cx, cy, R)
            x1, y1, z1 = self._project(B, cx, cy, R)
            zc = (z0 + z1) * 0.5
            if ring.kind == "comet":
                ramp = np.linspace(0.05, 1.0, len(zc)) ** 1.6
                ral = ramp * (0.45 + 0.55 * np.clip((zc + 1.2) / 2.4, 0, 1))
            else:
                ral = ring.gain * (0.2 + 0.8 * np.clip((zc + 1.2) / 2.4, 0, 1) ** 1.3)
            rlv = self._levels(np.clip(ral, 0, 1))
            for L in range(LEVELS):
                m = rlv == L
                if m.any():
                    fr.lines.append((L, ring.kind, x0[m], y0[m], x1[m], y1[m]))
            if ring.ticks is not None and len(ring.ticks):
                ta = ring.ticks + w
                Pt = (np.cos(ta)[:, None] * ring.u + np.sin(ta)[:, None] * ring.v) * ring.radius + base
                if ring.kind == "track":
                    Q0, Q1 = Pt - ring.n * ring.tick_len * 0.5, Pt + ring.n * ring.tick_len * 0.5
                else:
                    Q0, Q1 = Pt, Pt * (1.0 + ring.tick_len)
                Q0, Q1 = Q0 @ G.T, Q1 @ G.T
                q0, w0, k0 = self._project(Q0, cx, cy, R)
                q1, w1, _ = self._project(Q1, cx, cy, R)
                tal = np.clip(ring.gain * 0.85 * (0.15 + 0.85 * np.clip((k0 + 1.2) / 2.4, 0, 1) ** 1.3), 0, 1)
                tlv = self._levels(tal)
                for L in range(LEVELS):
                    m = tlv == L
                    if m.any():
                        fr.lines.append((L, "tick", q0[m], w0[m], q1[m], w1[m]))

    def _core(self, fr: Frame, t: float, G: np.ndarray, cx: float, cy: float, R: float, excite: float,
              beat: CoreBeat) -> None:
        st = beat.strength
        stir = min(1.0, max(beat.stir, excite * 0.6))
        # the brain's own tangle of veins: turbulent, beating, the brightest lines of all
        hp = self.hot_pts
        P = self.f_pts[hp]
        wob = np.sin(t * (2.3 + 2.2 * stir) + self.f_tp[hp])[:, None]
        P = P + self.f_jit[hp] * wob * (0.007 + 0.010 * stir + 0.012 * beat.voice)
        P = P * core_warp(np.linalg.norm(P, axis=1), beat)[:, None]
        xs, ys, zs = self._project(P @ G.T, cx, cy, R)
        hs, a, b = self.hot_segs, self.hot_a, self.hot_b
        zc = (zs[a] + zs[b]) * 0.5
        depth = 0.30 + 0.70 * np.clip((zc + 1.0) * 0.5, 0, 1) ** 1.2
        flow = 0.55 + 0.45 * np.sin(self.s_phase[hs] - t * (1.6 + 1.4 * stir))
        lit = 1.0 + 0.85 * max(-0.4, beat.light) * st + 0.45 * beat.voice
        al = np.clip(self.s_gain[hs] * self.s_taper[hs] * depth * flow * 0.55 * (0.9 + excite * 0.15) * lit,
                     0, 1)
        lv = self._levels(al)
        for L in range(LEVELS):
            m = lv == L
            if m.any():
                fr.lines.append((L, "hot", xs[a[m]], ys[a[m]], xs[b[m]], ys[b[m]]))

        # the dust orbiting the brain — stirred while thinking, pushed by the impulse
        D = _spin_about(self.p_pos, self.d_axis, self.d_omega * t * (1.0 + 0.8 * stir))
        D = D * core_warp(self.d_r, beat)[:, None]
        dx, dy, _ = self._project(D @ G.T, cx, cy, R)
        tw = 0.6 + 0.4 * np.sin(t * (2.3 + 1.5 * stir) + self.p_tw)
        pal = self.p_base * tw * (0.8 + 0.45 * max(0.0, beat.light) * st + 0.6 * beat.voice)
        if 0.0 <= beat.impulse < IMPULSE_LIFE:
            front, fade = impulse_front(beat.impulse)
            pal = pal * (1.0 + 1.3 * fade * np.exp(-((self.d_r - front) / 0.06) ** 2))
        plv = self._levels(np.clip(pal, 0, 1))
        for big in (False, True):
            sb = self.p_big if big else ~self.p_big
            for L in range(LEVELS):
                m = sb & (plv == L)
                if m.any():
                    fr.points.append((L, big, dx[m], dy[m]))
        fr.brain = (beat.size * st + 0.5 * beat.voice, beat.light * st + 0.35 * beat.voice)

    def stats(self) -> dict:
        segs = len(self.s_a) + len(self.h_dir) + sum(len(r.t0) + (0 if r.ticks is None else len(r.ticks))
                                                      for r in self.rings)
        return {"particles": len(self.p_pos), "segments": segs}


class VeinFlow:
    """Phase 9 — light travelling THROUGH the veins: a pooled particle system
    (fixed arrays; nothing is allocated per pulse and nothing grows).

    The heart's strong pulse calls launch(): a few arteries — chosen at random
    from a seeded stream, so it is varied but reproducible — each get a burst
    at a slightly different moment. A burst rides its trunk as one, splits
    where the artery forks (each branch then at its own pace), eases out of
    the brain, slows as it travels, runs on along the shell weave and fades.
    Between beats step() lets a few dim particles drift through the network,
    so it is never frozen. frame() projects bright heads with short glowing
    tails, exactly like the veins (same rotation, same depth shading)."""

    POOL = 224
    TAIL = np.array([0.0, 0.010, 0.022, 0.036, 0.054, 0.078, 0.108, 0.146, 0.192, 0.25])   # behind the head
    TAIL_W = np.array([1.0, 0.82, 0.64, 0.48, 0.35, 0.25, 0.17, 0.11, 0.07])              # per tail segment

    def __init__(self, sphere, seed: int = 29):
        self.sp = sphere
        self.rng = np.random.default_rng(seed)
        n = self.POOL
        self.on = np.zeros(n, dtype=bool)
        self.route = np.zeros(n, dtype=np.int64)
        self.s = np.zeros(n)             # distance travelled along the route
        self.v0 = np.zeros(n)            # launch speed (unit sphere / s)
        self.age = np.zeros(n)
        self.delay = np.zeros(n)         # waits this long before it starts (branches go at different moments)
        self.life = np.ones(n)
        self.gain = np.zeros(n)
        self.fade = np.ones(n)           # brightness time constant of a pulse
        self.drift = np.zeros(n, dtype=bool)
        self.var = np.zeros((n, 4))      # speed variation per edge of its route (shared within a burst)
        self._drift_acc = 0.0
        self.launches = 0
        self._rested = None              # trees fed by the last beat rest during the next one

    def active(self) -> int:
        return int(self.on.sum())

    def _slots(self, k: int, evict: bool = True) -> np.ndarray:
        free = np.flatnonzero(~self.on)
        if len(free) >= k or not evict:
            return free[:k]
        busy = np.flatnonzero(self.on)
        old = busy[np.argsort(-self.age[busy], kind="stable")][:k - len(free)]   # recycle the oldest
        return np.concatenate([free, old])

    def launch(self, trunks: float = 3.0, branching: float = 0.5, speed: float = 1.0,
               strength: float = 1.0, fade: float = 1.4) -> int:
        """One heartbeat's burst: `trunks` arteries (fractions round at random;
        arteries that carried the last beat go last), each branch taken with
        probability `branching`. Returns the number of pulses sent."""
        sp, rng = self.sp, self.rng
        n_tree = len(sp.tree_paths)
        if n_tree == 0:
            return 0
        k = int(trunks) + (1 if rng.random() < trunks - int(trunks) else 0)
        k = max(1, min(n_tree, k))
        fresh = [i for i in range(n_tree) if self._rested is None or i not in self._rested]
        tired = [i for i in range(n_tree) if self._rested is not None and i in self._rested]
        order = list(rng.permutation(fresh)) + list(rng.permutation(tired))
        chosen = [int(i) for i in order[:k]]
        self._rested = set(chosen)
        sent = 0
        for tr in chosen:
            routes = sp.tree_paths[int(tr)]
            pick = [r for r in routes if rng.random() < branching]
            if not pick:
                pick = [routes[int(rng.integers(len(routes)))]]
            var = {int(e): float(rng.uniform(-0.15, 0.18)) for e in np.unique(sp.pe_id[pick]) if e >= 0}
            var[int(sp.pe_id[routes[0], 0])] = 0.0           # the trunk's first edge: all together
            delay = float(rng.uniform(0.0, 0.11))
            v0 = float(rng.uniform(1.45, 1.85)) * speed
            g = float(rng.uniform(0.8, 1.0)) * strength
            for sl, r in zip(self._slots(len(pick)), pick):
                self.on[sl], self.drift[sl] = True, False
                self.route[sl], self.s[sl], self.age[sl] = r, 0.0, 0.0
                self.v0[sl], self.delay[sl], self.life[sl] = v0, delay, 4.0
                self.gain[sl], self.fade[sl] = g, fade
                self.var[sl] = [var.get(int(e), 0.0) for e in sp.pe_id[r]]
            sent += len(pick)
        self.launches += 1
        return sent

    def step(self, dt: float, drift_rate: float = 1.5, speed: float = 1.0) -> None:
        """Advance every pulse by dt seconds; let `drift_rate` quiet particles a
        second appear somewhere in the network."""
        if dt <= 0:
            return
        sp = self.sp
        if self.on.any():
            self.delay[self.on] -= dt
            live = np.flatnonzero(self.on & (self.delay <= 0))
            if len(live):
                a = self.age[live] + dt
                self.age[live] = a
                r = self.route[live]
                s = self.s[live]
                e = np.clip((sp.pe_s[r] <= s[:, None]).sum(axis=1) - 1, 0, 3)
                f = 1.0 + self.var[live, e]
                dr = self.drift[live]
                ease = np.where(dr, 1.0 - np.exp(-a / 0.4), 1.0 - np.exp(-a / 0.06))   # smooth start
                slow = np.where(dr, 1.0, 0.5 + 0.5 * np.exp(-a / 0.7))                 # pulses decelerate
                s = s + self.v0[live] * ease * slow * f * dt
                self.s[live] = s
                done = (s >= sp.p_len[r]) | (a >= self.life[live])
                if done.any():
                    self.on[live[done]] = False
        self._drift_acc += max(0.0, drift_rate) * dt
        n = int(self._drift_acc)
        if n:
            self._drift_acc -= n
            rng = self.rng
            for sl in self._slots(n, evict=False):           # drift never pushes a pulse out
                r = int(rng.choice(sp.drift_routes))
                self.on[sl], self.drift[sl] = True, True
                self.route[sl], self.s[sl], self.age[sl] = r, float(rng.uniform(0.0, 0.6 * sp.p_len[r])), 0.0
                self.v0[sl], self.delay[sl] = float(rng.uniform(0.10, 0.26)) * speed, 0.0
                self.life[sl], self.gain[sl], self.fade[sl] = float(rng.uniform(1.6, 3.2)), \
                    float(rng.uniform(0.22, 0.42)), 1.0
                self.var[sl] = 0.0

    def brightness(self, idx: np.ndarray) -> np.ndarray:
        a = self.age[idx]
        end = np.clip((self.sp.p_len[self.route[idx]] - self.s[idx]) / 0.12, 0.0, 1.0)
        pulse = (1.0 - np.exp(-a / 0.035)) * np.exp(-a / self.fade[idx])
        drift = np.sin(math.pi * np.clip(a / self.life[idx], 0.0, 1.0))
        return self.gain[idx] * np.where(self.drift[idx], drift, pulse) * end

    def frame(self, G: np.ndarray, cx: float, cy: float, R: float, beat: CoreBeat | None = None):
        """(lines, heads): tail segments [(level, "flow", x0, y0, x1, y1)] and
        heads [(level, big, xs, ys)] (big = a heartbeat pulse, small = drift)."""
        sp = self.sp
        idx = np.flatnonzero(self.on & (self.delay <= 0))
        if not len(idx):
            return [], []
        b = self.brightness(idx)
        keep = b > 0.015
        if not keep.any():
            return [], []
        idx, b = idx[keep], b[keep]
        r, dr = self.route[idx], self.drift[idx]
        S = self.s[idx][:, None] - self.TAIL[None, :]
        ok = S >= 0.0                                    # the tail can't reach back past the start
        fi = np.clip(S, 0.0, None) / FLOW_DS
        i0 = np.clip(fi.astype(np.int64), 0, (sp.p_m[r] - 2)[:, None])
        fr_ = np.clip(fi - i0, 0.0, 1.0)[..., None]
        A = sp.flow_P[r[:, None], i0]
        P = A + (sp.flow_P[r[:, None], i0 + 1] - A) * fr_
        if beat is not None:                             # near the brain they beat with it
            P = P * core_warp(np.linalg.norm(P, axis=2), beat)[..., None]
        n, K = S.shape
        xs, ys, zs = HoloSphere._project(P.reshape(-1, 3) @ G.T, cx, cy, R)
        xs, ys, zs = xs.reshape(n, K), ys.reshape(n, K), zs.reshape(n, K)
        depth = 0.30 + 0.70 * np.clip((zs + 1.0) * 0.5, 0, 1) ** 1.2      # nearer = brighter
        dm = (depth[:, :-1] + depth[:, 1:]) * 0.5
        al = b[:, None] * self.TAIL_W[None, :] * dm * ok[:, 1:] * np.where(dr, 0.55, 1.0)[:, None]
        lv = HoloSphere._levels(al * 1.1)
        vis = al > 0.02
        x0, y0, x1, y1 = xs[:, :-1], ys[:, :-1], xs[:, 1:], ys[:, 1:]
        lines = []
        for L in range(LEVELS):
            m = vis & (lv == L)
            if m.any():
                lines.append((L, "flow", x0[m], y0[m], x1[m], y1[m]))
        ha = b * depth[:, 0]
        hl = HoloSphere._levels(ha * 1.1)
        heads = []
        for big in (True, False):
            sel = (~dr if big else dr) & (ha > 0.02)
            for L in range(LEVELS):
                m = sel & (hl == L)
                if m.any():
                    heads.append((L, big, xs[m, 0], ys[m, 0]))
        return lines, heads


def make_core(style: str = "filament", quality: str = "high", compact: bool = False):
    """The JARVIS core for the configured style: 'filament' (default, Phase 7)
    or 'particles' (the Mark LIII.2 particle cloud)."""
    if str(style or "").lower().startswith("particle"):
        return HoloSphere(quality, compact=compact)
    return FilamentSphere(quality, compact=compact)


# ─────────────────────────────────────────────────────────────────────────────
# Off-screen renderer (numpy only) — used by tests and the preview tool.
# The Qt widget draws the same Frame with QPainter.
# ─────────────────────────────────────────────────────────────────────────────

def render_rgb(sphere, t: float, size: int = 640, color=(62, 166, 255), amp: float = 0.0,
               spin: float = 1.0, excite: float = 0.0, scale: float = 1.0, beat: CoreBeat | None = None,
               flow: VeinFlow | None = None) -> np.ndarray:
    """Additive rasteriser → uint8 H×W×3 image (for previews/tests): the whole
    sphere with its beating core (`beat`, a CoreBeat; `amp` = the voice, felt
    by the core only) and, if given, the pulses of a VeinFlow in its veins."""
    H = W = size
    acc = np.zeros((H, W), dtype=np.float32)
    col = np.array(color, dtype=np.float32) / 255.0
    cx = cy = size / 2
    R = size * 0.34 * scale
    f = sphere.frame(t, cx, cy, R, spin=spin, amp=amp, excite=excite, beat=beat)

    def splat(xs, ys, v, rad=0):
        xi, yi = np.round(xs).astype(int), np.round(ys).astype(int)
        for dx in range(-rad, rad + 1):
            for dy in range(-rad, rad + 1):
                m = (xi + dx >= 0) & (xi + dx < W) & (yi + dy >= 0) & (yi + dy < H)
                np.add.at(acc, (yi[m] + dy, xi[m] + dx), v)

    lines, heads = list(f.lines), []
    if flow is not None and getattr(sphere, "has_veins", False):
        fl, heads = sphere.flow_frame(flow, t, cx, cy, R, spin, beat=beat)
        lines += fl
    for L, big, xs, ys in f.points:
        splat(xs, ys, (L + 1) / LEVELS * (0.9 if big else 0.55), 1 if big else 0)
    for L, big, xs, ys in heads:
        splat(xs, ys, (L + 1) / LEVELS * (1.6 if big else 0.6), 1 if big else 0)
    gain = {"track": 0.55, "ring": 0.5, "tick": 0.45, "comet": 0.9, "inner": 0.3, "filament": 0.62,
            "hot": 0.9, "hair": 0.45, "flow": 1.5}
    for L, kind, x0, y0, x1, y1 in lines:
        n = np.maximum(2, (np.hypot(x1 - x0, y1 - y0)).astype(int))
        for i in range(len(x0)):
            k = int(n[i])
            s = np.linspace(0, 1, k)
            splat(x0[i] + (x1[i] - x0[i]) * s, y0[i] + (y1[i] - y0[i]) * s, (L + 1) / LEVELS * gain[kind])
    yy, xx = np.mgrid[0:H, 0:W]
    ccx, ccy, cr, ci = f.core
    d = np.hypot(xx - ccx, yy - ccy) / max(cr, 1.0)
    acc += ci * (np.exp(-d * d * 2.2) * 1.3 + np.exp(-d * 0.9) * 0.35)
    bsize, blight = f.brain                              # the heart's own light, inside the core
    lit = max(0.0, 0.2 + 0.8 * blight)
    if lit > 0:
        acc += lit * np.exp(-(d / (0.9 * max(0.6, 1.0 + 0.12 * bsize))) ** 2)
    acc = 1.0 - np.exp(-acc * 1.3)                      # soft tone-map
    img = acc[..., None] * (col * 0.75 + 0.25 * acc[..., None] ** 2)  # hot centre whitens
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)
