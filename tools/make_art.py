"""Renders Arcee's art once, so the app plays it instead of drawing it live.

    uv run python tools/make_art.py

- arcee/app/assets/wave-{idle,active}-{dark,light}.webp: the Agent page's ring of
  particles in sunrise amber, loose ribbons of light round a small geodesic sphere,
  transparent, in a look for dark windows and one for light ones. Idle breathes
  slowly; active (listening, working) pulses. Each loops without a seam: every
  motion turns a whole number of times per loop. (Animated WebP: a GIF's
  transparency is all or nothing, which frays soft light.)
- art/arcee.svg: the icon (the ring on a dark disc) for the app, Chrome and the
  package; art/arcee-panel.svg: the ring alone, for GNOME's top bar.
- browser-ext/icons/icon-{16,32,48,128}.png, rendered from art/arcee.svg.

The ring used to be drawn by the app 60 times a second; on slow machines that kept
the GTK thread busy enough for GNOME to offer Force Quit.
"""

import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SIZE = 300  # the loops' pixels, shown as they are
SUPER = 2  # drawn at twice the size, then shrunk, for smooth particles
# Sunrise amber, from deep to bright.
DEEP = (224, 90, 0)
AMBER = (255, 140, 26)
GOLD = (255, 190, 90)
LIGHT = (255, 228, 170)
STAGE_CENTRE = (16, 10, 6)
STAGE_EDGE = (4, 3, 2)


class Light:
    """Light that adds up: a dim particle alone, brighter where strands cross."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.rgb = [0.0] * (size * size * 3)

    def add(self, x: float, y: float, colour: tuple[int, int, int], strength: float) -> None:
        # Shared between the four nearest pixels, so motion stays smooth.
        ix, iy = int(x), int(y)
        fx, fy = x - ix, y - iy
        for px, py, w in (
            (ix, iy, (1 - fx) * (1 - fy)),
            (ix + 1, iy, fx * (1 - fy)),
            (ix, iy + 1, (1 - fx) * fy),
            (ix + 1, iy + 1, fx * fy),
        ):
            if 0 <= px < self.size and 0 <= py < self.size:
                i = (py * self.size + px) * 3
                k = w * strength
                self.rgb[i] += colour[0] * k
                self.rgb[i + 1] += colour[1] * k
                self.rgb[i + 2] += colour[2] * k

    def image(self) -> Image.Image:
        return Image.frombytes(
            "RGB", (self.size, self.size), bytes(min(255, int(v)) for v in self.rgb)
        )


@dataclass(frozen=True)
class Motion:
    frames: int
    delay_ms: int
    swell: float  # how far the ribbons wander from the circle
    spread: float  # how wide each ribbon's strands fan out
    turns: int  # wave crests travelling round per loop
    pulse: float  # how much the sphere breathes


IDLE = Motion(frames=32, delay_ms=100, swell=0.035, spread=0.020, turns=1, pulse=0.03)
# The icon: a still from a livelier ring, with brighter, wider strands to read small.
ICON = Motion(frames=1, delay_ms=0, swell=0.07, spread=0.05, turns=1, pulse=0.0)
# Calmer than it was: the ribbons swing half as far, one wave, nearly 3 s a loop.
ACTIVE = Motion(frames=30, delay_ms=95, swell=0.05, spread=0.035, turns=1, pulse=0.05)


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def stage(size: int) -> Image.Image:
    """A dark disc with a faint amber warmth behind the ring (the app rounds its corners)."""
    image = Image.new("RGB", (size, size), STAGE_EDGE)
    draw = ImageDraw.Draw(image)
    centre = size / 2
    for r in range(int(centre * 1.45), 0, -2):
        t = r / (centre * 1.45)
        colour = lerp(STAGE_CENTRE, STAGE_EDGE, t)
        warmth = max(0.0, 1 - abs(t - 0.45) * 4) * 0.25  # a soft glow where the ring sits
        colour = lerp(colour, DEEP, warmth * 0.35)
        draw.ellipse((centre - r, centre - r, centre + r, centre + r), fill=colour)
    return image


def ribbons(light: Light, size: int, phase: float, motion: Motion, boost: float = 1.0) -> None:
    """Three loose ribbons, each a bundle of strands that twist round each other."""
    centre, base = size / 2, size * 0.33
    rng = random.Random(7)
    for ribbon in range(3):
        offset = ribbon * 2.1
        strands = 9
        for strand in range(strands):
            share = strand / (strands - 1) - 0.5
            points = 900
            for i in range(points):
                angle = math.tau * i / points
                wander = motion.swell * (
                    math.sin(angle * 3 + offset + phase * motion.turns)
                    + 0.6 * math.sin(angle * 5 - offset * 1.7 - phase * motion.turns)
                    + 0.3 * math.sin(angle * 2 + phase)
                )
                twist = math.cos(angle * 4 + offset + phase * (motion.turns + 1))
                radius = base * (1 + wander + motion.spread * share * (1 + twist))
                radius += rng.gauss(0, size * 0.0012)  # a little dust off the line
                x = centre + radius * math.cos(angle)
                y = centre + radius * math.sin(angle)
                facing = 0.5 + 0.5 * twist * (1 if share > 0 else -1)
                top = LIGHT if boost == 1.0 else GOLD  # icons stay amber where strands pile up
                colour = lerp(AMBER, top, facing * 0.7) if ribbon != 1 else lerp(DEEP, GOLD, facing)
                light.add(x, y, colour, (0.55 + 0.6 * facing) * boost)


Vertex = tuple[float, float, float]


def geodesic(subdivisions: int = 2) -> tuple[list[Vertex], set[tuple[int, int]]]:
    """A geodesic sphere: an icosahedron with a vertex up, its faces split into triangles.

    With a vertex on the vertical axis, a fifth of a turn about it looks the same,
    so the sphere can turn by that much per loop without a seam.
    """
    top, bottom = (0.0, 1.0, 0.0), (0.0, -1.0, 0.0)
    y, r = 1 / math.sqrt(5), 2 / math.sqrt(5)
    upper = [(r * math.cos(math.tau * k / 5), y, r * math.sin(math.tau * k / 5)) for k in range(5)]
    lower = [
        (r * math.cos(math.tau * (k + 0.5) / 5), -y, r * math.sin(math.tau * (k + 0.5) / 5))
        for k in range(5)
    ]
    points: list[Vertex] = [top, *upper, *lower, bottom]
    faces: list[tuple[int, int, int]] = []
    for k in range(5):
        u, u2, lo, lo2 = 1 + k, 1 + (k + 1) % 5, 6 + k, 6 + (k + 1) % 5
        faces += [(0, u, u2), (u, lo, u2), (u2, lo, lo2), (lo, 11, lo2)]
    for _ in range(subdivisions):
        middle: dict[tuple[int, int], int] = {}
        split: list[tuple[int, int, int]] = []
        for a, b, c in faces:
            ab, bc, ca = (_middle(points, middle, x, y) for x, y in ((a, b), (b, c), (c, a)))
            split += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        faces = split
    edges = {
        (min(a, b), max(a, b)) for f in faces for a, b in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0]))
    }
    return points, edges


def _middle(points: list[Vertex], middle: dict[tuple[int, int], int], a: int, b: int) -> int:
    """The vertex halfway between a and b, pushed out onto the sphere (made once)."""
    key = (min(a, b), max(a, b))
    if key not in middle:
        x = [(points[a][i] + points[b][i]) / 2 for i in range(3)]
        n = math.sqrt(sum(v * v for v in x))
        points.append((x[0] / n, x[1] / n, x[2] / n))
        middle[key] = len(points) - 1
    return middle[key]


SPHERE = geodesic()
# The icon's sphere: one split of the icosahedron (120 edges, not 480), to stay
# clear at 64 pixels.
ICON_SPHERE = geodesic(subdivisions=1)


def sphere(
    draw: ImageDraw.ImageDraw,
    light: Light,
    size: int,
    phase: float,
    motion: Motion,
    *,
    scale: float = 1.0,
    bright: float = 1.0,
    mesh: tuple[list[Vertex], set[tuple[int, int]]] | None = None,
) -> None:
    """The core: a geodesic sphere of fine light that turns slowly, inside a soft
    scalloped ring, as in the reference the user chose. The icon draws it larger,
    brighter and with fewer triangles (`scale`, `bright`, `mesh`), to read small."""
    points, edges = mesh or SPHERE
    centre = size / 2
    radius = size * 0.09 * scale * (1 + motion.pulse * math.sin(phase))
    inner = size * 0.15
    # A darker disc under the core, so it reads against the ring's glow.
    draw.ellipse(
        (centre - inner, centre - inner, centre + inner, centre + inner),
        fill=lerp(STAGE_EDGE, STAGE_CENTRE, 0.4),
    )
    # The scalloped ring: twelve soft lobes.
    ring = size * 0.165
    for i in range(720):
        a = math.tau * i / 720
        rr = ring * (1 + 0.05 * abs(math.sin(6 * a + phase / 12)))
        light.add(centre + rr * math.cos(a), centre + rr * math.sin(a), DEEP, 0.18 * bright)
    # The sphere, turned a fifth of a turn per loop and tilted towards us.
    spin = phase / 5
    tilt = math.radians(20)
    projected = []
    for x, y, z in points:
        x, z = x * math.cos(spin) - z * math.sin(spin), x * math.sin(spin) + z * math.cos(spin)
        y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
        projected.append((centre + radius * x, centre - radius * y, z))
    for a, b in edges:
        (x1, y1, z1), (x2, y2, z2) = projected[a], projected[b]
        depth = (z1 + z2) / 2  # 1 facing us, -1 behind
        strength = (0.12 + 0.38 * (depth + 1) / 2) * bright
        colour = lerp(DEEP, GOLD, (depth + 1) / 2)
        steps = max(4, int(math.hypot(x2 - x1, y2 - y1) / 1.5))
        for k in range(steps):
            t = k / steps
            light.add(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, colour, strength)
    # Its outline, crisp.
    for i in range(360):
        a = math.tau * i / 360
        light.add(
            centre + radius * math.cos(a), centre + radius * math.sin(a), AMBER, 0.55 * bright
        )


def frame(phase: float, motion: Motion, size: int = SIZE, *, icon: bool = False) -> Image.Image:
    """One picture of the ring. As an icon: brighter light, and the geodesic sphere
    larger and with fewer triangles, so both still read at 64 pixels."""
    big = size * SUPER
    image = stage(big)
    draw = ImageDraw.Draw(image)
    buffer = Light(big)
    ribbons(buffer, big, phase, motion, boost=1.4 if icon else 1.0)
    if icon:
        sphere(draw, buffer, big, phase, motion, scale=1.45, bright=2.2, mesh=ICON_SPHERE)
    else:
        sphere(draw, buffer, big, phase, motion)
    light = buffer.image()
    glow = light.filter(ImageFilter.GaussianBlur(big * 0.01))
    # The particles, and a faint halo round them.
    image = _add(_add(image, glow, 0.8 if icon else 0.55), light, 1.0)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def clear_frame(phase: float, motion: Motion, shade: str) -> Image.Image:
    """The ring as light with alpha, for the app's own background: no disc.

    "dark": amber light that glows on a dark window. "light": deeper orange ink that
    reads on a white one.
    """
    big = SIZE * SUPER
    buffer = Light(big)
    ribbons(buffer, big, phase, motion)
    scratch = Image.new("RGB", (big, big))  # the core's dark disc goes nowhere
    sphere(ImageDraw.Draw(scratch), buffer, big, phase, motion)
    light = buffer.image()
    glow = light.filter(ImageFilter.GaussianBlur(big * 0.01))
    lit = _add(Image.eval(glow, lambda v: round(v * 0.55)), light, 1.0)
    lit = lit.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    out = bytearray()
    for r, g, b in zip(*(iter(lit.tobytes()),) * 3, strict=True):
        alpha = max(r, g, b)
        if alpha == 0:
            out += b"\0\0\0\0"
            continue
        # Light added on black, undone: the colour at full strength, and how much of it.
        cr, cg, cb = (round(v * 255 / alpha) for v in (r, g, b))
        if shade == "light":
            cr, cg, cb = round(cr * 0.86), round(cg * 0.6), round(cb * 0.28)
            alpha = min(255, round(alpha * 1.25))
        out += bytes((cr, cg, cb, alpha))
    return Image.frombytes("RGBA", (SIZE, SIZE), bytes(out))


def webp(motion: Motion, shade: str, path: Path) -> None:
    """An animated WebP with alpha: soft edges a GIF cannot hold, and smaller."""
    frames = [
        clear_frame(math.tau * f / motion.frames, motion, shade) for f in range(motion.frames)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=motion.delay_ms,
        loop=0,
        quality=45,
        alpha_quality=50,
        method=6,
    )
    print(f"{path.relative_to(ROOT)}: {len(frames)} frames, {path.stat().st_size // 1024} KB")


def core(light: Light, size: int) -> None:
    """A soft amber sun in the middle."""
    centre = size / 2
    reach = size * 0.12
    steps = int(reach)
    for y in range(-steps, steps + 1):
        for x in range(-steps, steps + 1):
            d = math.hypot(x, y) / reach
            if d < 1:
                light.add(centre + x, centre + y, lerp(AMBER, LIGHT, 1 - d), (1 - d) ** 2 * 0.7)


def _add(base: Image.Image, light: Image.Image, strength: float) -> Image.Image:
    from PIL import ImageChops

    return ImageChops.add(base, Image.eval(light, lambda v: round(v * strength)))


def gif(motion: Motion, path: Path) -> None:
    frames = [frame(math.tau * f / motion.frames, motion) for f in range(motion.frames)]
    # One palette for every frame, so colours do not flicker between frames.
    sheet = Image.new("RGB", (SIZE, SIZE * len(frames)))
    for i, image in enumerate(frames):
        sheet.paste(image, (0, SIZE * i))
    palette = sheet.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    indexed = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    path.parent.mkdir(parents=True, exist_ok=True)
    indexed[0].save(
        path,
        save_all=True,
        append_images=indexed[1:],
        duration=motion.delay_ms,
        loop=0,
        optimize=True,
        disposal=1,
    )
    print(f"{path.relative_to(ROOT)}: {len(frames)} frames, {path.stat().st_size // 1024} KB")


# --- the icon -------------------------------------------------------------------------


def ring_path(cx: float, cy: float, r: float, swell: float, crests: int, shift: float) -> str:
    points = []
    for i in range(121):
        a = math.tau * i / 120
        rr = r * (
            1
            + swell * math.sin(a * crests + shift)
            + swell * 0.5 * math.sin(a * (crests + 2) - shift)
        )
        points.append(f"{cx + rr * math.cos(a):.2f},{cy + rr * math.sin(a):.2f}")
    return "M" + " L".join(points) + " Z"


def icon_svg(with_disc: bool) -> str:
    """The ring for small sizes: three thin wavy strands that cross, round a warm core."""
    s = 128
    c = s / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 {s} {s}">'
    ]
    parts.append(
        "<defs>"
        '<radialGradient id="disc" cx="50%" cy="50%" r="50%">'
        '<stop offset="0" stop-color="#1c1108"/><stop offset="1" stop-color="#060403"/>'
        "</radialGradient>"
        '<radialGradient id="core" cx="50%" cy="50%" r="50%">'
        '<stop offset="0" stop-color="#ffe4aa"/><stop offset="0.45" stop-color="#ff9a2e"/>'
        '<stop offset="1" stop-color="#ff8c1a" stop-opacity="0"/></radialGradient>'
        "</defs>"
    )
    if with_disc:
        parts.append(f'<circle cx="{c}" cy="{c}" r="{c - 1}" fill="url(#disc)"/>')
    # The panel's copy is shown at 16 pixels: fewer, thicker strands.
    strands = (
        ((0.10, 3, 0.0, "#ffd27a"), (0.11, 3, 2.1, "#ff8c1a"), (0.09, 4, 4.2, "#e05a00"))
        if with_disc
        else ((0.07, 3, 0.0, "#ffc062"), (0.08, 3, 2.6, "#ff8c1a"))
    )
    width = 3.0 if with_disc else 8.0
    radius = 42 if with_disc else 44
    parts.append('<g fill="none" stroke-linejoin="round">')
    for swell, crests, shift, colour in strands:
        parts.append(
            f'<path d="{ring_path(c, c, radius, swell, crests, shift)}" stroke="{colour}" '
            f'stroke-width="{width}" stroke-opacity="0.95"/>'
        )
    parts.append("</g>")
    core = 16 if with_disc else 22
    parts.append(f'<circle cx="{c}" cy="{c}" r="{core}" fill="url(#core)"/></svg>\n')
    return "".join(parts)


def icon_png(path: Path, px: int) -> None:
    """The particle ring itself, as the large icon: the same light as the animation."""
    from PIL import ImageChops

    image = frame(math.tau * 0.3, ICON, size=max(px, 256), icon=True).resize(
        (px, px), Image.Resampling.LANCZOS
    )
    mask = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(mask).ellipse((4, 4, px * 4 - 4, px * 4 - 4), fill=255)
    rgba = image.convert("RGBA")
    rgba.putalpha(
        ImageChops.multiply(
            mask.resize((px, px), Image.Resampling.LANCZOS), Image.new("L", (px, px), 255)
        )
    )
    rgba.save(path)


def icons() -> None:
    art = ROOT / "art"
    art.mkdir(exist_ok=True)
    (art / "arcee.svg").write_text(icon_svg(with_disc=True))
    (art / "arcee-panel.svg").write_text(icon_svg(with_disc=False))
    for px in (64, 128, 256, 512):
        icon_png(art / f"arcee-{px}.png", px)
    # Under the app's id, so a checkout can add art/ to GTK's icon search path.
    shutil.copy(art / "arcee-256.png", art / "me.iteshxt.Arcee.png")
    # The top bar's icon, inside the GNOME Shell extension.
    shutil.copy(
        art / "arcee-panel.svg", ROOT / "extension" / "arcee@iteshxt.me" / "icons" / "arcee.svg"
    )
    out = ROOT / "browser-ext" / "icons"
    out.mkdir(exist_ok=True)
    for px in (16, 32):
        _render(art / "arcee.svg", out / f"icon-{px}.png", px)
    for px in (48, 128):
        icon_png(out / f"icon-{px}.png", px)
    print(
        "art/: arcee.svg (small sizes), arcee-panel.svg, arcee-{64,128,256,512}.png; "
        "browser-ext/icons/"
    )


def _render(svg: Path, png: Path, px: int) -> None:
    """Through GdkPixbuf's SVG loader (librsvg), as GNOME draws icons."""
    code = (
        "import gi, sys; gi.require_version('GdkPixbuf', '2.0');"
        "from gi.repository import GdkPixbuf;"
        "GdkPixbuf.Pixbuf.new_from_file_at_size(sys.argv[1], int(sys.argv[3]), int(sys.argv[3]))"
        ".savev(sys.argv[2], 'png', [], [])"
    )
    subprocess.run(["/usr/bin/python3", "-c", code, str(svg), str(png), str(px)], check=True)


CUE_PX = 160  # shown at 96 logical px, sharp at 1.5x scale


def cue_frames() -> None:
    """The top-centre cue's frames for the GNOME Shell extension: the active loop
    (dark look), cut down to CUE_PX. GNOME 50's shell has no sprite-sheet animation, so
    the extension shows these one after another."""
    from PIL import ImageSequence

    source = ROOT / "arcee" / "app" / "assets" / "wave-active-dark.webp"
    out = ROOT / "extension" / "arcee@iteshxt.me" / "icons" / "cue"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    with Image.open(source) as image:
        for i, frame in enumerate(ImageSequence.Iterator(image)):
            small = frame.convert("RGBA").resize((CUE_PX, CUE_PX), Image.Resampling.LANCZOS)
            small.save(out / f"{i:02d}.png", optimize=True)
    total = sum(f.stat().st_size for f in out.glob("*.png"))
    print(f"extension/.../icons/cue: {len(list(out.glob('*.png')))} frames, {total // 1024} KB")


if __name__ == "__main__":
    import sys

    if "--cue" in sys.argv:  # --cue: the shell cue's frames alone, from the loops
        cue_frames()
        sys.exit()
    if "--icons" not in sys.argv:  # --icons: the icons alone (the loops take longer)
        assets = ROOT / "arcee" / "app" / "assets"
        for shade in ("dark", "light"):
            webp(IDLE, shade, assets / f"wave-idle-{shade}.webp")
            webp(ACTIVE, shade, assets / f"wave-active-{shade}.webp")
        cue_frames()
    icons()
