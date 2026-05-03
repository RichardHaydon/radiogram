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
        # Cyan-tinted phosphor — what an actual VFD (vacuum-fluorescent
        # display) looks like under smoked glass. The previous "deeper
        # phosphor green" was too desaturated and disappeared into the
        # map. This is the brighter aqua-green you see on Sony/Marantz
        # tape decks and microwaves.
        fg_bright=(80, 235, 175),       # bright aqua-phosphor for the clock
        fg_dim=(80, 175, 140),           # mid phosphor for secondary text
        fg_subtle=(140, 220, 190),       # pale phosphor for hints
        # Amber controls (warm complementary) — slightly brighter than
        # before so buttons hold their own next to the brighter clock.
        fg_accent=(255, 195, 100),       # warm amber for button labels
        outline=(160, 110, 50),           # amber outline
        weather_sun=(255, 230, 90),
        weather_cloud=(140, 215, 180),
        weather_rain=(95, 235, 230),
        weather_snow=(210, 255, 235),
        weather_storm=(255, 235, 100),
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

ESPRESSO = Theme(
    name="Espresso",
    palette=Palette(
        # Warm dark coffee bg with cream foreground — same cosy bedside
        # mood as the old "Tape Deck" Dark theme but a bit deeper and
        # warmer. Replaces "Ink & Paper" (light theme; the black-on-
        # paper variant didn't pair well with the maps).
        bg=(28, 22, 18),                     # dark espresso
        fg_bright=(232, 215, 188),           # warm cream
        fg_dim=(160, 140, 115),              # milky coffee
        fg_subtle=(205, 185, 155),           # milk foam
        fg_accent=(225, 150, 65),            # caramel
        outline=(125, 80, 35),                # dim caramel outline
        weather_sun=(255, 200, 90),
        weather_cloud=(180, 170, 155),
        weather_rain=(140, 175, 205),
        weather_snow=(225, 215, 195),
        weather_storm=(245, 195, 95),
    ),
    clock_style="digital",
    button_style="rounded",
)

MARINE = Theme(
    name="Marine Chart",
    palette=Palette(
        # Marine chart: cool ink-blue field with creamy bone for the
        # clock and a single brass accent for controls. Pairs naturally
        # with Slate / Blueprint maps.
        bg=(14, 22, 38),                     # deep ink blue
        fg_bright=(225, 215, 195),          # bone / parchment
        fg_dim=(135, 150, 175),              # weathered sea-grey
        fg_subtle=(180, 195, 215),           # pale chart blue
        fg_accent=(205, 160, 70),            # muted brass
        outline=(120, 95, 50),                # dim brass for outlines
        weather_sun=(240, 195, 90),
        weather_cloud=(160, 175, 195),
        weather_rain=(115, 165, 215),
        weather_snow=(220, 230, 240),
        weather_storm=(240, 200, 95),
    ),
    clock_style="digital",
    button_style="rounded",
)

SLATE_THEME = Theme(
    name="Slate",
    palette=Palette(
        # Cool grey neutral — the most map-deferential choice. Brighter
        # silver primary + more vivid steel-blue accent than the first
        # cut so the clock reads cleanly over the busy map without the
        # hue fighting the cartography's own blue-greys.
        bg=(22, 26, 32),                     # charcoal
        fg_bright=(238, 242, 248),           # bright silver
        fg_dim=(150, 160, 175),              # ash grey
        fg_subtle=(195, 205, 218),           # pale silver
        fg_accent=(140, 200, 240),           # vivid steel blue
        outline=(70, 110, 145),               # mid steel for outlines
        weather_sun=(240, 210, 120),
        weather_cloud=(195, 205, 220),
        weather_rain=(135, 195, 230),
        weather_snow=(225, 235, 245),
        weather_storm=(245, 215, 125),
    ),
    clock_style="digital",
    button_style="rounded",
)


FOREST = Theme(
    name="Forest",
    palette=Palette(
        # Deep moss-green field with cream / rust accents. Pairs well
        # with the Atlas (green land) and Vintage (warm brown) maps —
        # the foreground hue mirrors the cartography. Bedside-friendly
        # at 3am because the bg is genuinely dark.
        bg=(14, 24, 18),                     # deep forest
        fg_bright=(220, 230, 195),           # bone / parchment
        fg_dim=(135, 160, 120),              # moss
        fg_subtle=(180, 200, 160),           # dry leaf
        fg_accent=(225, 145, 60),             # rust
        outline=(120, 80, 35),                 # dim rust
        weather_sun=(245, 200, 90),
        weather_cloud=(170, 185, 165),
        weather_rain=(125, 175, 200),
        weather_snow=(215, 230, 210),
        weather_storm=(235, 195, 95),
    ),
    clock_style="digital",
    button_style="rounded",
)


# Order shown in the Theme picker. Map-friendly dark themes lead;
# Daylight (the only light theme) trails so it's easy to find.
THEMES: list[Theme] = [
    DARK, SLATE_THEME, MARINE, FOREST, VFD, AMBER, ESPRESSO, LIGHT,
]
DEFAULT = DARK
