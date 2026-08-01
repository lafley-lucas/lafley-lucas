#!/usr/bin/env python3
"""Render a neofetch-style ASCII profile card as an SVG.

Produces the same card layout as crafter-station/gh-ascii (https://gh.crafter.run),
but sources the artwork from a *local image file* so the card can use a portrait
photo that is not (yet) the GitHub account avatar.

  python tools/render_ascii_card.py \
      --image assets/avatar.png --handle lafley-lucas \
      --theme dark --out assets/ascii-dark.svg

Only dependency is Pillow. Profile stats come from the public GitHub REST API;
set GITHUB_TOKEN to lift the 60 req/h anonymous rate limit.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from xml.sax.saxutils import escape, unescape

from PIL import Image, ImageFilter, ImageOps

API = "https://api.github.com"

# Coverage ramp, sparse -> dense. On a dark card a dense glyph reads as *bright*,
# so luminance maps straight onto this ramp; on a light card it is inverted.
RAMP = " .:-=+*#%@"

# Card geometry, mirrored from gh-ascii's output so both renderers stay swappable.
FONT = "'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace"
ART_FONT_SIZE = 8.0
ART_CHAR_W = ART_FONT_SIZE * 0.6  # 4.8
ART_LINE_H = ART_FONT_SIZE * 1.2  # 9.6
PANEL_FONT_SIZE = 16.0
PANEL_CHAR_W = PANEL_FONT_SIZE * 0.6  # 9.6
PANEL_LINE_H = 20.0
PAD = 28.0
GAP = 32.0
RULE_W = 58  # characters in a "─ Section ────" divider
ROW_W = 57  # characters in a ". Label: ....... value" row
HALF_W = 26  # characters in one half of a two-column stats row

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "art": "#c9d1d9",
        "rule": "#3d444d",
        "title": "#58a6ff",
        "label": "#ffa657",
        "dots": "#484f58",
        "value": "#c9d1d9",
        "num": "#79c0ff",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "art": "#24292f",
        "rule": "#d0d7de",
        "title": "#0969da",
        "label": "#953800",
        "dots": "#8c959f",
        "value": "#24292f",
        "num": "#0550ae",
    },
}


# --------------------------------------------------------------------------- art


def _s_curve(v: float, k: float) -> float:
    return 1.0 / (1.0 + math.exp(-k * (v - 0.5)))


def _backdrop_mask(img: Image.Image, tol: int, work: int = 256) -> Image.Image:
    """White where the pixel belongs to the studio backdrop.

    Region-grows inward from the border under two limits at once: a step limit
    against the neighbour it came from (so a gradient backdrop stays walkable)
    and a leash to the median border tone (so the walk cannot drift across a
    long smooth ramp — skin — and swallow the subject). Either limit alone
    fails: step-only leaks through a cheek, leash-only stops at the vignette.
    """
    w, h = img.size
    small = img.resize((work, max(1, round(work * h / w))), Image.LANCZOS)
    sw, sh = small.size
    px = small.tobytes()  # 'L' mode: one byte per pixel, row-major

    ring = [px[x] for x in range(sw)] + [px[(sh - 1) * sw + x] for x in range(sw)]
    ring += [px[y * sw] for y in range(sh)] + [px[y * sw + sw - 1] for y in range(sh)]
    ring.sort()
    seed = ring[len(ring) // 2]
    leash = max(12, tol * 4)

    seen = bytearray(sw * sh)
    queue = deque()
    for x in range(sw):
        for y in (0, sh - 1):
            i = y * sw + x
            if not seen[i]:
                seen[i] = 1
                queue.append(i)
    for y in range(sh):
        for x in (0, sw - 1):
            i = y * sw + x
            if not seen[i]:
                seen[i] = 1
                queue.append(i)

    while queue:
        i = queue.popleft()
        v = px[i]
        x, y = i % sw, i // sw
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < sw and 0 <= ny < sh:
                j = ny * sw + nx
                if not seen[j] and abs(px[j] - v) <= tol and abs(px[j] - seed) <= leash:
                    seen[j] = 1
                    queue.append(j)

    mask = Image.frombytes("L", (sw, sh), bytes(255 if s else 0 for s in seen))
    return mask.filter(ImageFilter.MedianFilter(3))


def ascii_art(
    path: str,
    cols: int,
    invert: bool,
    contrast: float,
    blur: float,
    unsharp: float,
    unsharp_radius: float,
    black: float,
    white: float,
    cutout: int,
) -> list[str]:
    """Downsample an image onto a character grid whose cells are 2:1 (h:w).

    A ten-step ramp on its own turns a studio portrait into mush: the flat
    backdrop eats the middle of the ramp and the face lands as one bright blob.
    So before sampling we (1) unsharp-mask to pull local detail — eyes, nostrils,
    the hair/skin boundary — up out of the noise floor, and (2) clip the levels
    so the backdrop collapses toward the sparse end and the subject keeps the
    dense end to itself.
    """
    img = ImageOps.grayscale(Image.open(path).convert("RGB"))
    backdrop = _backdrop_mask(img, cutout) if cutout else None

    if unsharp > 0:
        img = img.filter(ImageFilter.UnsharpMask(radius=unsharp_radius, percent=int(unsharp * 100), threshold=2))
    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    w, h = img.size
    rows = max(1, round(cols * (h / w) * 0.5))
    small = ImageOps.autocontrast(img.resize((cols, rows), Image.LANCZOS), cutoff=2)
    px = small.load()
    bg = backdrop.resize((cols, rows), Image.LANCZOS).load() if backdrop else None

    lo, hi = _s_curve(0.0, contrast), _s_curve(1.0, contrast)
    span = max(1e-6, white - black)
    last = len(RAMP) - 1
    # With the backdrop cut away, the subject must never fall to a blank cell or
    # dark hair would punch a hole straight through the silhouette.
    floor = 1 if bg else 0
    out = []
    for y in range(rows):
        line = []
        for x in range(cols):
            if bg and bg[x, y] > 128:
                line.append(RAMP[0])
                continue
            v = max(0.0, min(1.0, (px[x, y] / 255.0 - black) / span))
            if contrast > 0:
                v = (_s_curve(v, contrast) - lo) / (hi - lo)
            if invert:
                v = 1.0 - v
            line.append(RAMP[max(floor, round(max(0.0, min(1.0, v)) * last))])
        out.append("".join(line))
    return out


# ------------------------------------------------------------------------- stats


def _get(url: str, token: str | None):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ascii-card-renderer",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _try(fn, default):
    try:
        return fn()
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, TimeoutError) as exc:
        print(f"  ! stat lookup failed ({exc}); using {default!r}", file=sys.stderr)
        return default


def _uptime(created_at: str) -> str:
    born = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - born).days
    years, rem = divmod(days, 365)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    parts.append(f"{rem} day{'s' if rem != 1 else ''}")
    return ", ".join(parts)


def fetch_profile(handle: str, token: str | None) -> dict:
    user = _try(lambda: _get(f"{API}/users/{handle}", token), {})
    repos = _try(lambda: _get(f"{API}/users/{handle}/repos?per_page=100&sort=pushed", token), [])

    langs, stars = {}, 0
    for repo in repos:
        if repo.get("fork"):
            continue
        stars += repo.get("stargazers_count", 0)
        lang = repo.get("language")
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    # Name breaks count ties so the row does not flip-flop with repo push order,
    # which would otherwise produce a fresh commit on every scheduled run.
    top = ", ".join(k for k, _ in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))[:4]) or "—"

    commits = _try(
        lambda: _get(f"{API}/search/commits?q=author:{handle}&per_page=1", token).get("total_count", 0),
        0,
    )

    return {
        "handle": handle,
        "name": user.get("name") or handle,
        "focus": user.get("bio") or "",
        "stack": "",
        "email": "",
        "location": user.get("location") or "",
        "web": (user.get("blog") or "").replace("https://", "").replace("http://", ""),
        "uptime": _uptime(user["created_at"]) if user.get("created_at") else "—",
        "languages": top,
        "repos": user.get("public_repos", len(repos)),
        "stars": stars,
        "commits": commits,
        "followers": user.get("followers", 0),
        "following": user.get("following", 0),
        "gists": user.get("public_gists", 0),
    }


# -------------------------------------------------------------------------- svg


def _w(text: str) -> int:
    """Advance width in monospace cells — CJK glyphs occupy two, so a Korean
    name in the panel must not be counted as one cell per character."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _t(text: str, fill: str) -> str:
    return f'<tspan fill="{fill}">{escape(text)}</tspan>'


def _rule(title: str, c: dict) -> str:
    head = f"─ {title} "
    return _t("─", c["rule"]) + _t(head[1:], c["title"]) + _t("─" * max(3, RULE_W - _w(head)), c["rule"])


def _row(label: str, value: str, c: dict, value_color: str | None = None, width: int = ROW_W) -> str:
    left, right = f". {label}: ", f" {value}"
    dots = max(3, width - _w(left) - _w(right))
    return _t(left, c["label"]) + _t("." * dots, c["dots"]) + _t(right, value_color or c["value"])


def _pair(a: tuple[str, str], b: tuple[str, str], c: dict) -> str:
    return (
        _row(a[0], a[1], c, c["num"], HALF_W)
        + _t(" | ", c["rule"])
        + _row(b[0], b[1], c, c["num"], HALF_W)
    )


def build_panel(p: dict, c: dict) -> list[str]:
    """Neofetch-style info block. Anything falsy is dropped, so a sparse
    card.json degrades to just the live GitHub numbers rather than to blanks."""
    lines = [_rule(f"{p['handle']}@github", c)]
    for label, value in (
        ("Name", p["name"] if p["name"] != p["handle"] else ""),
        ("Focus", p["focus"]),
        ("Location", p["location"]),
        ("Uptime", p["uptime"]),
        ("Languages", p["languages"]),
        ("Stack", p["stack"]),
    ):
        if value:
            lines.append(_row(label, value, c))

    lines += ["", _rule("Contact", c), _row("GitHub", f"github.com/{p['handle']}", c)]
    for label, value in (("Web", p["web"]), ("Email", p["email"])):
        if value:
            lines.append(_row(label, value, c))

    lines += [
        "",
        _rule("GitHub Stats", c),
        _pair(("Repos", str(p["repos"])), ("Stars", str(p["stars"])), c),
        _pair(("Commits", str(p["commits"])), ("Followers", str(p["followers"])), c),
        _pair(("Gists", str(p["gists"])), ("Following", str(p["following"])), c),
    ]
    return lines


def _plain_width(markup: str) -> int:
    """Cell width of a panel line, ignoring the tspan wrappers we emitted."""
    return _w(unescape(re.sub(r"<[^>]+>", "", markup)))


def _sweep(x0: float, x1: float, ys: list[float], dur: float, begin: float, c: dict, h: float, w: float) -> str:
    """One block cursor that retraces every row, as a single element.

    Per-row cursors would be n more nodes; instead x carries a saw-tooth values
    list (duplicated keyTimes make the carriage return instantaneous) and y steps
    discretely alongside it.
    """
    n = len(ys)
    xs = ";".join(f"{x0:g};{x1:g}" for _ in range(n))
    xk = ";".join(f"{i / n:.5f};{(i + 1) / n:.5f}" for i in range(n))
    yv = ";".join(f"{y - h * 0.8:.2f}" for y in ys)
    yk = ";".join(f"{i / n:.5f}" for i in range(n))
    return (
        f'  <rect width="{w:g}" height="{h:g}" fill="{c["title"]}" opacity="0.85">\n'
        f'    <animate attributeName="x" values="{xs}" keyTimes="{xk}" '
        f'begin="{begin:g}s" dur="{dur:g}s" fill="freeze"/>\n'
        f'    <animate attributeName="y" values="{yv}" keyTimes="{yk}" calcMode="discrete" '
        f'begin="{begin:g}s" dur="{dur:g}s" fill="freeze"/>\n'
        f'    <set attributeName="opacity" to="0" begin="{begin + dur:g}s" fill="freeze"/>\n'
        f'    <set attributeName="opacity" to="0" begin="0s" end="{begin:g}s"/>\n'
        f"  </rect>"
    )


def render(art: list[str], panel: list[str], theme: str, animate: float) -> str:
    c = THEMES[theme]
    cols = len(art[0])

    art_w = cols * ART_CHAR_W
    art_h = len(art) * ART_LINE_H
    panel_x = PAD + art_w + GAP
    panel_w = RULE_W * PANEL_CHAR_W
    panel_h = len(panel) * PANEL_LINE_H

    width = round(panel_x + panel_w + PAD, 1)
    height = round(max(art_h, panel_h) + PAD * 2, 1)

    art_y0 = PAD + (height - PAD * 2 - art_h) / 2 + ART_FONT_SIZE * 0.82
    panel_y0 = PAD + (height - PAD * 2 - panel_h) / 2 + PANEL_FONT_SIZE * 0.82
    art_ys = [round(art_y0 + i * ART_LINE_H, 2) for i in range(len(art))]
    panel_ys = [round(panel_y0 + i * PANEL_LINE_H, 2) for i in range(len(panel))]

    # Two thirds of the reveal draws the portrait, the rest types the panel out.
    art_span = animate * 0.66
    panel_span = animate - art_span
    art_step = art_span / max(1, len(art))
    filled = [i for i, line in enumerate(panel) if line]
    panel_step = panel_span / max(1, len(filled))

    defs, body = [], []
    for i, line in enumerate(art):
        clip = ""
        if animate:
            defs.append(
                f'    <clipPath id="a{i}"><rect x="{PAD:g}" y="{art_ys[i] - ART_LINE_H:.2f}" '
                f'width="0" height="{ART_LINE_H * 1.6:g}">'
                f'<animate attributeName="width" values="0;{art_w:g}" '
                f'begin="{i * art_step:.3f}s" dur="{art_step:.3f}s" fill="freeze"/>'
                f"</rect></clipPath>"
            )
            clip = f' clip-path="url(#a{i})"'
        body.append(
            f'  <text x="{PAD:g}" y="{art_ys[i]}" fill="{c["art"]}" font-family="{FONT}" '
            f'xml:space="preserve" font-size="{ART_FONT_SIZE:g}" textLength="{art_w:g}" '
            f'lengthAdjust="spacingAndGlyphs"{clip}>{escape(line)}</text>'
        )

    for i, line in enumerate(panel):
        if not line:
            continue
        line_w = _plain_width(line) * PANEL_CHAR_W
        clip = ""
        if animate:
            begin = art_span + filled.index(i) * panel_step
            defs.append(
                f'    <clipPath id="p{i}"><rect x="{panel_x:g}" y="{panel_ys[i] - PANEL_LINE_H:.2f}" '
                f'width="0" height="{PANEL_LINE_H * 1.4:g}">'
                f'<animate attributeName="width" values="0;{line_w:g}" '
                f'begin="{begin:.3f}s" dur="{panel_step:.3f}s" fill="freeze"/>'
                f"</rect></clipPath>"
            )
            clip = f' clip-path="url(#p{i})"'
        body.append(
            f'  <text x="{panel_x:g}" y="{panel_ys[i]}" font-family="{FONT}" xml:space="preserve" '
            f'font-size="{PANEL_FONT_SIZE:g}" textLength="{line_w:g}" '
            f'lengthAdjust="spacingAndGlyphs"{clip}>{line}</text>'
        )

    if animate:
        body.append(_sweep(PAD, PAD + art_w, art_ys, art_span, 0.0, c, ART_FONT_SIZE, ART_CHAR_W))
        body.append(
            _sweep(panel_x, panel_x + panel_w, [panel_ys[i] for i in filled],
                   panel_span, art_span, c, PANEL_FONT_SIZE, PANEL_CHAR_W)
        )

    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="ASCII GitHub profile card">',
        f'  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="8" '
        f'fill="{c["bg"]}" stroke="{c["border"]}"/>',
    ]
    if defs:
        head += ["  <defs>", *defs, "  </defs>"]
    return "\n".join(head + body + ["</svg>"]) + "\n"


# ------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Render an ASCII profile card SVG.")
    ap.add_argument("--image", required=True, help="source image for the ASCII artwork")
    ap.add_argument("--handle", required=True, help="GitHub handle used for the info panel")
    ap.add_argument("--theme", choices=("dark", "light"), default="dark")
    ap.add_argument("--cols", type=int, default=120, help="ASCII columns (40-200)")
    ap.add_argument("--contrast", type=float, default=7.0, help="S-curve strength; 0 disables")
    ap.add_argument("--blur", type=float, default=1.1, help="pre-downsample blur radius")
    ap.add_argument("--unsharp", type=float, default=1.8, help="local-contrast boost; 0 disables")
    ap.add_argument("--unsharp-radius", type=float, default=3.0, help="unsharp radius in source px")
    ap.add_argument("--black", type=float, default=0.0, help="input black point, 0-1")
    ap.add_argument("--white", type=float, default=1.0, help="input white point, 0-1")
    ap.add_argument("--cutout", type=int, default=16,
                    help="backdrop matting tolerance in grey levels; 0 disables")
    ap.add_argument("--animate", type=float, default=4.2,
                    help="self-typing reveal, seconds; 0 renders a static card")
    ap.add_argument("--profile-json", help="JSON file of panel overrides (name, focus, stack, ...)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not 40 <= args.cols <= 200:
        ap.error("--cols must be between 40 and 200")
    if not 0.0 <= args.black < args.white <= 1.0:
        ap.error("--black must be less than --white, both within 0-1")

    art = ascii_art(
        args.image,
        cols=args.cols,
        invert=(args.theme == "light"),
        contrast=args.contrast,
        blur=args.blur,
        unsharp=args.unsharp,
        unsharp_radius=args.unsharp_radius,
        black=args.black,
        white=args.white,
        cutout=args.cutout,
    )
    profile = fetch_profile(args.handle, os.environ.get("GITHUB_TOKEN"))
    if args.profile_json:
        with open(args.profile_json, encoding="utf-8") as fh:
            profile.update({k: v for k, v in json.load(fh).items() if v})

    svg = render(art, build_panel(profile, THEMES[args.theme]), args.theme, args.animate)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"  {args.out}  ({len(art[0])}x{len(art)} chars, {len(svg)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
