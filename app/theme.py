"""Visual theme — palette + fonts + style hooks.

Roles are named strings (e.g. "fg_bright", "bold") rather than direct
attribute access at the call site, so a future config-from-JSON loader
or a runtime theme switch can swap values without code changes.

A Theme also carries two style hints:
    clock_style   "digital" | "vfd"  (analog reserved)
    button_style  "outline" | "rounded"

These are read by widgets at render time. To add a theme: build a
Theme instance below and append it to THEMES.
"""
from __future__ import annotations

from dataclasses import dataclass, field


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    bg: RGB = (0, 0, 0)
    fg_bright: RGB = (235, 235, 240)
    fg_dim: RGB = (160, 160, 170)
    fg_accent: RGB = (220, 220, 230)
    fg_subtle: RGB = (180, 180, 195)
    outline: RGB = (70, 70, 80)
    # Weather icon palette — slight warm/cool tinting so the forecast
    # scene reads at a glance instead of being uniform gray.
    weather_sun: RGB = (255, 210, 100)
    weather_cloud: RGB = (180, 185, 200)
    weather_rain: RGB = (140, 200, 235)
    weather_snow: RGB = (235, 235, 245)
    weather_storm: RGB = (255, 220, 110)


@dataclass(frozen=True)
class Fonts:
    bold: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    regular: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    # Tabular monospace for the VFD matrix clock — all digits same width
    # so the time doesn't shift columns when "1" → "2".
    mono: str = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


@dataclass(frozen=True)
class Theme:
    name: str = "Dark"
    palette: Palette = field(default_factory=Palette)
    fonts: Fonts = field(default_factory=Fonts)
    clock_style: str = "digital"   # "digital" | "vfd"
    button_style: str = "rounded"  # "outline" | "rounded"


def color(theme: Theme, role: str) -> RGB:
    return getattr(theme.palette, role)


def font_path(theme: Theme, role: str) -> str:
    return getattr(theme.fonts, role)


# --- presets ---------------------------------------------------------

DARK = Theme(
    name="Dark",
    palette=Palette(
        bg=(0, 0, 0),
        # Ivory primary + burnt-orange accent — vintage tape deck pair.
        fg_bright=(205, 200, 185),       # softer ivory for the clock
        fg_dim=(150, 145, 130),           # muted ivory for headers
        fg_subtle=(195, 185, 165),        # cream for secondary text
        fg_accent=(235, 130, 50),         # burnt orange for buttons
        outline=(135, 70, 25),             # darker orange outline
        weather_sun=(255, 200, 90),
        weather_cloud=(190, 185, 170),
        weather_rain=(150, 200, 230),
        weather_snow=(240, 235, 220),
        weather_storm=(255, 215, 100),
    ),
)

VFD = Theme(
    name="VFD Green",
    palette=Palette(
        bg=(0, 0, 0),
        # Green family — clock + primary/secondary text
        fg_bright=(45, 175, 105),      # deeper phosphor green for the clock
        fg_dim=(70, 140, 110),          # subdued green for headers
        fg_subtle=(110, 200, 160),      # mid green for secondary text
        # Amber family — controls (button labels + outlines) sit in
        # the complementary warm half of the wheel, the way the green
        # display + amber buttons read on a Marantz/Pioneer hi-fi.
        fg_accent=(255, 180, 90),       # warm amber for button labels
        outline=(140, 95, 40),           # darker amber for button outlines
        weather_sun=(255, 230, 80),
        weather_cloud=(120, 200, 165),
        weather_rain=(80, 230, 230),
        weather_snow=(200, 255, 230),
        weather_storm=(255, 235, 90),
    ),
    clock_style="digital",
    button_style="rounded",
)

AMBER = Theme(
    name="Amber Night",
    palette=Palette(
        # Amber CRT phosphor + cool teal "chrome" — like an old amber
        # monitor with cyan trim. Two distinct hues that read at a glance.
        bg=(8, 12, 22),                    # near-black with a navy tint
        fg_bright=(215, 145, 55),          # softer warm amber for the clock
        fg_dim=(165, 110, 50),              # muted amber for headers
        fg_subtle=(220, 175, 110),          # pale amber for secondary text
        fg_accent=(95, 215, 230),           # teal cyan for buttons
        outline=(45, 115, 130),              # darker teal for outlines
        weather_sun=(255, 200, 90),
        weather_cloud=(180, 200, 215),
        weather_rain=(120, 210, 230),
        weather_snow=(220, 240, 245),
        weather_storm=(255, 220, 105),
    ),
    clock_style="digital",
    button_style="rounded",
)

RED_LED = Theme(
    name="Red LED",
    palette=Palette(
        # Bedside-friendly take on the 80s/90s clock-radio look: dimmed
        # red 7-seg glow + soft cream controls. Not a dashboard at noon
        # — a clock you can sleep next to.
        bg=(0, 0, 0),
        fg_bright=(170, 28, 22),          # softer red glow for the clock
        fg_dim=(95, 25, 18),               # deep red for headers
        fg_subtle=(140, 55, 48),           # muted rose for hints
        fg_accent=(165, 155, 135),         # dim cream for buttons
        outline=(90, 80, 65),              # deep cream for outlines
        weather_sun=(200, 165, 70),
        weather_cloud=(150, 140, 130),
        weather_rain=(140, 165, 190),
        weather_snow=(195, 190, 175),
        weather_storm=(210, 175, 85),
    ),
    clock_style="digital",
    button_style="rounded",
)

SYNTHWAVE = Theme(
    name="Synthwave",
    palette=Palette(
        # Dusty-neon take on the 80s arcade pair: muted magenta clock +
        # subdued teal controls. Bedside-friendly — same vibe, less
        # retina burn at 3am.
        bg=(8, 5, 25),
        fg_bright=(190, 75, 140),         # dusty magenta for the clock
        fg_dim=(105, 45, 85),              # deep plum for headers
        fg_subtle=(155, 125, 175),         # muted lavender for secondary
        fg_accent=(85, 170, 190),          # dusty teal for buttons
        outline=(45, 90, 105),              # deep teal for outlines
        weather_sun=(210, 130, 90),
        weather_cloud=(155, 135, 175),
        weather_rain=(85, 170, 190),
        weather_snow=(190, 175, 210),
        weather_storm=(210, 90, 150),
    ),
    clock_style="digital",
    button_style="rounded",
)

LIGHT = Theme(
    name="Daylight",
    palette=Palette(
        # Mid-century print: deep navy primary + brick-red accent on
        # warm cream — like a 1960s travel poster.
        bg=(248, 242, 225),                 # warm cream
        fg_bright=(20, 40, 90),             # deep navy for the clock
        fg_dim=(95, 110, 145),              # slate for headers
        fg_subtle=(135, 150, 175),          # dusty blue-grey for hints
        fg_accent=(195, 70, 50),             # brick red for buttons
        outline=(165, 105, 90),               # dusty rose for outlines
        weather_sun=(220, 145, 35),
        weather_cloud=(105, 125, 150),
        weather_rain=(50, 115, 185),
        weather_snow=(160, 180, 205),
        weather_storm=(210, 150, 40),
    ),
    clock_style="digital",
    button_style="rounded",
)


THEMES: list[Theme] = [DARK, VFD, AMBER, RED_LED, SYNTHWAVE, LIGHT]
DEFAULT = DARK
