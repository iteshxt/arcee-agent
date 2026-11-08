# ruff: noqa: E501  (SVG path data reads best on one line)
"""Draws the eight voice avatars (arcee/app/assets/voices/<name>.svg) for Settings → Voice.

Each is a flat portrait on a round, two-tone background: its own skin tone, hair and
one accessory, chosen to match how the voice sounds (Bruno the deepest, a beard; Kiki
the brightest, space buns and a star). Run it again after changing a look:

    python3 packaging/voice_avatars.py
"""

from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "arcee" / "app" / "assets" / "voices"


@dataclass(frozen=True)
class Look:
    name: str
    bg: tuple[str, str]  # gradient, top-left to bottom-right
    skin: str
    hair: str
    shirt: str
    back_hair: str = ""  # drawn behind the face
    front_hair: str = ""  # drawn over the head
    extra: str = ""  # glasses, earrings, headphones...
    blush: float = 0.35


FACE_SHADE = "#000000"
INK = "#2b2230"


def face(look: Look) -> str:
    return f"""
  <path d="M14 96 C16 80 30 72 48 72 C66 72 80 80 82 96 Z" fill="{look.shirt}"/>
  <path d="M41 62 h14 v12 c-4 4 -10 4 -14 0 Z" fill="{look.skin}"/>
  <path d="M41 66 h14 v4 c-4 2 -10 2 -14 0 Z" fill="{FACE_SHADE}" opacity="0.10"/>
  <ellipse cx="48" cy="46" rx="17" ry="20" fill="{look.skin}"/>
  <ellipse cx="31.5" cy="48" rx="3" ry="4.5" fill="{look.skin}"/>
  <ellipse cx="64.5" cy="48" rx="3" ry="4.5" fill="{look.skin}"/>
  <ellipse cx="41.5" cy="47" rx="2" ry="2.6" fill="{INK}"/>
  <ellipse cx="54.5" cy="47" rx="2" ry="2.6" fill="{INK}"/>
  <circle cx="42.2" cy="46.1" r="0.7" fill="#ffffff"/>
  <circle cx="55.2" cy="46.1" r="0.7" fill="#ffffff"/>
  <path d="M38 41.5 q3.5 -2.2 7 -0.6" stroke="{INK}" stroke-width="1.4" fill="none" stroke-linecap="round" opacity="0.75"/>
  <path d="M51 40.9 q3.5 -1.6 7 0.6" stroke="{INK}" stroke-width="1.4" fill="none" stroke-linecap="round" opacity="0.75"/>
  <path d="M47 50 q1 3 2.4 2.6" stroke="{FACE_SHADE}" stroke-width="1.2" fill="none" stroke-linecap="round" opacity="0.18"/>
  <path d="M43 56.5 q5 4.2 10 0" stroke="#b5485a" stroke-width="1.8" fill="none" stroke-linecap="round"/>
  <ellipse cx="38" cy="53.5" rx="3.4" ry="2.1" fill="#ff7a8a" opacity="{look.blush}"/>
  <ellipse cx="58" cy="53.5" rx="3.4" ry="2.1" fill="#ff7a8a" opacity="{look.blush}"/>"""


def svg(look: Look) -> str:
    a, b = look.bg
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{a}"/>
      <stop offset="1" stop-color="{b}"/>
    </linearGradient>
    <clipPath id="round"><circle cx="48" cy="48" r="48"/></clipPath>
  </defs>
  <g clip-path="url(#round)">
  <rect width="96" height="96" fill="url(#bg)"/>
  <circle cx="74" cy="20" r="22" fill="#ffffff" opacity="0.10"/>
  {look.back_hair}{face(look)}
  {look.front_hair}
  {look.extra}
  </g>
</svg>
"""


LOOKS = (
    Look(
        "Jasper",
        ("#7cc6fe", "#3a7bd5"),
        skin="#f2c29b",
        hair="#8a5a2b",
        shirt="#1f4e8c",
        front_hair="""<path d="M30 44 C28 26 40 19 50 20 C62 20 69 28 67 44 C64 35 58 30 50 31 C46 34 40 35 34 34 C32 37 31 40 30 44 Z" fill="#8a5a2b"/>
  <path d="M44 22 c4 -6 12 -5 14 0 c-5 -2 -9 -1 -14 0 Z" fill="#a06a35"/>""",
        extra="""<path d="M33 86 l15 -6 l15 6" stroke="#ffffff" stroke-width="2" fill="none" opacity="0.5"/>""",
        blush=0.25,
    ),
    Look(
        "Bella",
        ("#ffd3a5", "#fd6585"),
        skin="#e8b48f",
        hair="#4a2b22",
        shirt="#c0396b",
        back_hair="""<path d="M27 46 C24 24 39 15 50 16 C65 17 72 29 69 47 C71 60 76 72 72 82 L24 82 C20 70 27 60 27 46 Z" fill="#4a2b22"/>""",
        front_hair="""<path d="M31 44 C30 28 40 21 52 22 C61 23 67 30 66 40 C58 38 52 32 49 26 C45 33 38 39 31 44 Z" fill="#5b362a"/>""",
        extra="""<circle cx="31.5" cy="55" r="1.8" fill="#ffd36e"/><circle cx="64.5" cy="55" r="1.8" fill="#ffd36e"/>""",
    ),
    Look(
        "Bruno",
        ("#8e9eab", "#3c4a5c"),
        skin="#b97a56",
        hair="#1f1a17",
        shirt="#2d3a4a",
        front_hair="""<path d="M31 42 C30 27 40 21 49 21 C59 21 67 27 66 42 C63 33 57 29 49 29 C41 29 34 33 31 42 Z" fill="#1f1a17"/>
  <path d="M31 48 C31 66 40 73 48 73 C56 73 65 66 65 48 C62 57 57 61 48 61 C39 61 34 57 31 48 Z" fill="#1f1a17"/>
  <path d="M41 55 q7 -3.5 14 0 q-7 2 -14 0 Z" fill="#1f1a17"/>""",
        extra="",
        blush=0.15,
    ),
    Look(
        "Luna",
        ("#a18cd1", "#5b3e96"),
        skin="#f0c8a8",
        hair="#26203a",
        shirt="#3d2a6b",
        back_hair="""<path d="M27 50 C25 28 37 18 49 18 C62 18 72 28 69 50 L67 60 L29 60 Z" fill="#26203a"/>""",
        front_hair="""<path d="M30 46 C29 30 39 21 50 21 C60 21 68 29 67 44 C60 40 52 34 47 28 C43 35 37 41 30 46 Z" fill="#332a4d"/>""",
        extra="""<path d="M64 57 a3.4 3.4 0 1 0 2.6 5.6 a2.6 2.6 0 1 1 -2.6 -5.6 Z" fill="#ffe08a"/>
  <path d="M71 18 l1.3 2.7 l2.9 0.4 l-2.1 2 l0.5 2.9 l-2.6 -1.4 l-2.6 1.4 l0.5 -2.9 l-2.1 -2 l2.9 -0.4 Z" fill="#ffe08a" opacity="0.9"/>""",
    ),
    Look(
        "Hugo",
        ("#f6d365", "#fd9a3c"),
        skin="#d9a17a",
        hair="#2c1d14",
        shirt="#d35400",
        front_hair="""<g fill="#2c1d14"><circle cx="34" cy="36" r="6"/><circle cx="40" cy="28" r="7"/><circle cx="49" cy="25" r="7.5"/><circle cx="58" cy="28" r="7"/><circle cx="63" cy="36" r="6"/><circle cx="45" cy="31" r="6"/><circle cx="54" cy="31" r="6"/></g>""",
        extra="""<g fill="none" stroke="#1b1b1b" stroke-width="1.8"><circle cx="41.5" cy="47" r="5.2"/><circle cx="54.5" cy="47" r="5.2"/><path d="M46.7 46.5 q1.3 -1.2 2.6 0"/><path d="M36.3 46 l-4 -1"/><path d="M59.7 46 l4 -1"/></g>""",
        blush=0.3,
    ),
    Look(
        "Rosie",
        ("#fbc2eb", "#e86f9c"),
        skin="#f5d0b5",
        hair="#b5542d",
        shirt="#e0e7ff",
        back_hair="""<circle cx="68" cy="34" r="8" fill="#b5542d"/><path d="M68 40 C76 50 74 62 70 68 C68 58 66 50 64 42 Z" fill="#b5542d"/>""",
        front_hair="""<path d="M30 44 C29 27 39 19 50 19 C61 19 68 27 67 42 C60 33 52 30 44 30 C37 31 33 37 30 44 Z" fill="#c4623a"/>""",
        extra="""<g transform="translate(64 26)"><circle r="4" fill="#e63e6d"/><circle r="2.2" fill="#ff8fab"/><circle cx="-4" cy="3" r="1.8" fill="#4caf50"/></g>""",
    ),
    Look(
        "Leo",
        ("#84fab0", "#2f9e7a"),
        skin="#c98e6b",
        hair="#3b2a20",
        shirt="#22574a",
        front_hair="""<path d="M30 40 C30 22 40 15 48 15 C58 15 67 22 66 40 Z" fill="#f2a541"/>
  <rect x="28" y="36" width="40" height="7" rx="3.5" fill="#e08e2a"/>
  <circle cx="48" cy="13" r="3.5" fill="#f2a541"/>""",
        extra="""<path d="M27 50 C26 32 36 24 48 24 C60 24 70 32 69 50" stroke="#263238" stroke-width="3" fill="none"/>
  <rect x="23" y="45" width="8" height="12" rx="3" fill="#263238"/><rect x="65" y="45" width="8" height="12" rx="3" fill="#263238"/>""",
        blush=0.2,
    ),
    Look(
        "Kiki",
        ("#fff6a5", "#ffb347"),
        skin="#f7d6bf",
        hair="#1b1b2f",
        shirt="#7b5cff",
        back_hair="""<circle cx="33" cy="22" r="8" fill="#1b1b2f"/><circle cx="63" cy="22" r="8" fill="#1b1b2f"/>""",
        front_hair="""<path d="M30 44 C29 28 39 21 50 21 C61 21 68 28 67 43 C64 36 58 32 52 33 C48 30 42 31 38 34 C34 37 31 40 30 44 Z" fill="#1b1b2f"/>""",
        extra="""<path d="M64 30 l1.6 3.3 l3.6 0.5 l-2.6 2.5 l0.6 3.6 l-3.2 -1.7 l-3.2 1.7 l0.6 -3.6 l-2.6 -2.5 l3.6 -0.5 Z" fill="#ffd400" stroke="#e0a800" stroke-width="0.6"/>""",
        blush=0.45,
    ),
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for look in LOOKS:
        lines = (line.rstrip() for line in svg(look).splitlines())
        (OUT / f"{look.name.lower()}.svg").write_text(
            "\n".join(kept for kept in lines if kept) + "\n"
        )
    print(f"wrote {len(LOOKS)} avatars to {OUT}")


if __name__ == "__main__":
    main()
