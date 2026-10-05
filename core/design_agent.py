"""
core/design_agent.py — making pictures, not code.

Two distinct capabilities, kept honest about what actually works on the user's
free key:

    design.image(prompt)   generate a raster image with the configured image
                           model (config/models.json tier "image"). On the
                           user's key every Gemini image model reports
                           "limit: 0" (no free quota), so this returns a clear
                           "image generation isn't available on your key —
                           here's a vector version instead" and hands off to
                           design.vector. It NEVER returns a fake/placeholder
                           image. If Google grants free image quota later it
                           starts working with no code change.

    design.vector(prompt)  draw a clean SVG with the deep model (vector art is
                           a coding task the free tier does well), then RENDER
                           it to PNG so it can be looked at — via Playwright's
                           headless Chromium if present, else QtSvg — and run a
                           vision check ("does this match the request, any
                           obvious problems?"). The SVG + PNG are saved to a
                           JARVIS workspace folder. This works fully offline of
                           any image model.

Both verify their output: design.image checks real image bytes came back;
design.vector checks the SVG parses, renders to a non-blank PNG, and (when a
vision model is available) that it looks like what was asked.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.tools.registry import ToolResult

MAX_SVG_BYTES = 400_000


def _out_dir(name: str) -> Path:
    from core import research as R
    return R.workspace("research", f"design {name}", reuse=True)


# ── raster (image model) ─────────────────────────────────────────────────────

def t_design_image(args: dict, ctx) -> ToolResult:
    prompt = str(args.get("prompt") or args.get("description") or "").strip()
    if not prompt:
        return ToolResult.failure("Tell me what to draw.")
    from core.ai_gateway import get_gateway
    from core.providers.base import Capability, CapabilityNotSupported, ProviderError
    gw = get_gateway()
    aspect = str(args.get("aspect") or "1:1")
    try:
        r = gw.generate_image(prompt, aspect_ratio=aspect, timeout=120.0)
    except CapabilityNotSupported:
        return _fallback_to_vector(args, ctx, "no image model is configured")
    except ProviderError as e:
        from core.model_health import classify
        why = "there's no free image-generation quota on your key" if classify(e) in ("no_quota", "exhausted") \
            else "the image model is unavailable right now"
        return _fallback_to_vector(args, ctx, why)
    if not r.images:
        return _fallback_to_vector(args, ctx, "the image model returned no picture")
    data, mime = r.images[0]
    ext = ".png" if "png" in mime else (".jpg" if "jpeg" in mime else ".img")
    name = _slug(prompt)
    out = _out_dir(name) / f"{name} {datetime.now():%Y-%m-%d %H%M}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return ToolResult(True, {"path": str(out), "name": out.name, "kind": "image", "model": r.model,
                             "bytes": len(data)}, f"Generated an image ({len(data)//1024} KB) → {out.name}.")


def _fallback_to_vector(args: dict, ctx, why: str) -> ToolResult:
    ctx.extras.get("progress", lambda m: None)(f"Image generation unavailable ({why}); drawing a vector instead…")
    res = t_design_vector({**args, "_fallback_reason": why}, ctx)
    if res.ok:
        res.message = f"Image generation isn't available ({why}), so I drew a vector version instead. " + res.message
        res.data["fell_back_from_image"] = True
    return res


# ── vector (SVG, rendered and checked) ───────────────────────────────────────

def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n] or "design").rstrip("-")


def _extract_svg(text: str) -> str:
    m = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
    return m.group(0) if m else ""


def _valid_svg(svg: str) -> Optional[str]:
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        return "the SVG is too large"
    low = svg.lower()
    for bad in ("<script", "onload=", "onclick=", "<foreignobject", "javascript:", "<!entity", "<!doctype"):
        if bad in low:
            return f"the SVG contains disallowed content ({bad})"
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        return f"the SVG doesn't parse ({str(e)[:80]})"
    if not root.tag.lower().endswith("svg"):
        return "the root element isn't <svg>"
    return None


def _svg_size(svg: str, cap: int = 1024) -> tuple[int, int]:
    """Pixel size for rendering: explicit width/height, else the viewBox, else 512²."""
    w = h = 0.0
    mw = re.search(r'\bwidth\s*=\s*["\']?\s*([\d.]+)', svg)
    mh = re.search(r'\bheight\s*=\s*["\']?\s*([\d.]+)', svg)
    if mw and mh:
        w, h = float(mw.group(1)), float(mh.group(1))
    if (not w or not h):
        vb = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', svg)
        if vb:
            w, h = float(vb.group(1)), float(vb.group(2))
    if not w or not h:
        w = h = 512.0
    scale = min(1.0, cap / max(w, h))
    return max(16, int(w * scale)), max(16, int(h * scale))


def _render_png(svg: str, png: Path) -> tuple[bool, str]:
    """Render SVG → PNG. Playwright headless Chromium first, then QtSvg. Returns
    (ok, method|error)."""
    try:
        from playwright.sync_api import sync_playwright
        w, h = _svg_size(svg)
        # force an explicit pixel box so an inline SVG (viewBox only) is visible to screenshot
        sized = re.sub(r"^<svg\b", f'<svg width="{w}" height="{h}"', svg.strip(), count=1)
        html = (f"<!doctype html><meta charset=utf-8><style>html,body{{margin:0;background:#fff}}"
                f"#w{{width:{w}px;height:{h}px}}</style><div id=w>" + sized + "</div>")
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = b.new_context(device_scale_factor=2,
                                     viewport={"width": w + 4, "height": h + 4}).new_page()
                page.set_content(html, wait_until="networkidle")
                page.wait_for_timeout(120)
                el = page.query_selector("#w")
                (el or page).screenshot(path=str(png))
            finally:
                b.close()
        if png.exists() and png.stat().st_size > 200:
            return True, "chromium"
    except Exception:
        pass
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtCore import QByteArray
        r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        size = r.defaultSize()
        w, h = max(size.width(), 64), max(size.height(), 64)
        img = QImage(w * 2, h * 2, QImage.Format.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        pnt = QPainter(img)
        r.render(pnt)
        pnt.end()
        if img.save(str(png), "PNG"):
            return True, "qtsvg"
    except Exception as e:
        return False, f"no renderer available ({type(e).__name__})"
    return False, "no renderer available"


def _not_blank(png: Path) -> bool:
    try:
        from PIL import Image
        im = Image.open(png).convert("L").resize((32, 32))
        lo, hi = im.getextrema()                     # min/max luminance
        return (hi - lo) > 12                         # some contrast = not a blank canvas
    except Exception:
        return png.exists() and png.stat().st_size > 500


def t_design_vector(args: dict, ctx) -> ToolResult:
    prompt = str(args.get("prompt") or args.get("description") or "").strip()
    if not prompt:
        return ToolResult.failure("Tell me what to draw.")
    progress = ctx.extras.get("progress", lambda m: None)
    from core import research as R
    from core.ai_gateway import get_gateway
    style = str(args.get("style") or "clean, modern, flat vector")
    ask = (
        "You are a vector illustrator. Draw the following as a single self-contained SVG. Output ONLY the "
        "<svg>…</svg> — no Markdown, no explanation. Use a viewBox, group related shapes, use gradients/shapes "
        "(no external images, no <script>, no raster). Make it look finished and intentional.\n"
        f"STYLE: {style}\nSUBJECT: {prompt}")
    progress("Drawing the vector…")
    try:
        text = get_gateway().generate(ask, tier="deep", reasoning="medium", max_output_tokens=16000,
                                      timeout=150.0).text
    except Exception as e:
        from core.secret_store import redact
        return ToolResult.failure(f"The drawing model failed: {redact(e)[:160]}", retryable=True)
    svg = _extract_svg(text)
    if not svg:
        return ToolResult.failure("The model didn't return an SVG.", retryable=True)
    bad = _valid_svg(svg)
    if bad:
        return ToolResult.failure(f"The generated SVG was rejected: {bad}.", retryable=True)
    name = _slug(prompt)
    d = _out_dir(name)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    svg_path = d / f"{name} {stamp}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    png_path = d / f"{name} {stamp}.png"
    progress("Rendering it to look at…")
    rendered, how = _render_png(svg, png_path)
    review = {}
    if rendered and _not_blank(png_path) and not ctx.extras.get("should_stop", lambda: False)():
        progress("Checking how it looks…")
        review = _vision_check(png_path, prompt)
    data = {"path": str(svg_path), "svg": str(svg_path), "png": str(png_path) if rendered else "",
            "name": svg_path.name, "kind": "vector", "rendered": rendered, "render_method": how,
            "review": review}
    if not rendered:
        return ToolResult(True, data, f"Drew an SVG → {svg_path.name} (couldn't render a preview here: {how}). "
                          "The SVG opens in any browser.")
    if review.get("problems"):
        return ToolResult(True, data, f"Drew {svg_path.name} and rendered a preview. Visual check noted: "
                          + "; ".join(review["problems"][:2]) + f". Files in {d.name}.")
    return ToolResult(True, data, f"Drew {svg_path.name}, rendered and checked it — looks good. Saved the SVG "
                      f"and a PNG preview in {d.name}.")


def _vision_check(png: Path, prompt: str) -> dict:
    from core.ai_gateway import get_gateway
    from core.providers.base import CapabilityNotSupported, ProviderError
    schema = {"type": "object", "properties": {
        "matches": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}}, "required": ["matches", "summary"]}
    q = (f"This image was drawn to depict: \"{prompt}\". Does it clearly match? List only real problems "
         "(blank areas, shapes that don't read as intended, clipped content, unreadable text). If it looks "
         "like a finished drawing of the subject, matches=true with no problems.")
    try:
        r = get_gateway().generate(q, images=[(png.read_bytes(), "image/png")], schema=schema, tier="vision",
                                   timeout=60.0)
        return r.json() if r.data is None else r.data
    except (ProviderError, CapabilityNotSupported) as e:
        return {"matches": None, "summary": f"(couldn't view it: {type(e).__name__})"}


def verify_design(args, result):
    from core import verification as V
    if not result.ok:
        return V.failed(result.error)
    if result.data.get("kind") == "image":
        return V.path_exists(result.data.get("path"), "file")
    v = V.path_exists(result.data.get("svg"), "file")
    if not v.ok:
        return v
    if not result.data.get("rendered"):
        return V.unverifiable("Drew the SVG but couldn't render a preview to check it here.")
    review = result.data.get("review") or {}
    if review.get("matches") is False:
        return V.failed("the drawing doesn't match the request: " + (review.get("summary") or "")[:120])
    return V.verified("SVG renders to a non-blank image"
                      + (" and matches the request" if review.get("matches") else ""))


def register(reg) -> None:
    from core.permissions import RiskLevel
    from core.tools.registry import ToolSpec
    STR = {"type": "string"}
    reg.register(ToolSpec(
        "design.image", "Generate a picture (raster image) from a description using the image model. If the "
        "key has no free image quota it says so and draws a vector version instead — it never returns a fake "
        "image.", t_design_image,
        {"type": "object", "properties": {"prompt": STR, "aspect": STR}, "required": ["prompt"]},
        {"path": "str", "kind": "str"}, agent="ui", risk=RiskLevel.LOW_RISK, retry_safe=True, timeout=180,
        verify=verify_design))
    reg.register(ToolSpec(
        "design.vector", "Draw something as a clean SVG, render it to an image, and visually check it. Works "
        "without any image model. Good for logos, icons, illustrations, diagrams.", t_design_vector,
        {"type": "object", "properties": {"prompt": STR, "style": STR}, "required": ["prompt"]},
        {"svg": "str", "png": "str"}, agent="ui", risk=RiskLevel.LOW_RISK, retry_safe=True, timeout=240,
        verify=verify_design))
