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

INK = Theme(
    name="Ink & Paper",
    palette=Palette(
        # Newsprint: warm off-white field, near-black ink for the clock,
        # deep sepia for accents. Reads like a hand-set headline over
        # the parchment-toned Vintage map style.
        bg=(238, 230, 210),                 # aged paper
        fg_bright=(28, 24, 22),             # india-ink black
        fg_dim=(100, 88, 78),                # warm grey for secondary
        fg_subtle=(150, 138, 122),           # faded ink for hints
        fg_accent=(135, 70, 35),             # burnt sienna for controls
        outline=(165, 130, 95),               # tan rule line
        weather_sun=(180, 130, 35),
        weather_cloud=(130, 120, 105),
        weather_rain=(60, 95, 135),
        weather_snow=(190, 180, 165),
        weather_storm=(195, 145, 50),
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
        # Cool grey neutral — the most map-deferential choice. Pewter
        # numerals on charcoal, with a steel-blue accent that reads
        # without competing with the world map's own greys.
        bg=(22, 26, 32),                     # charcoal
        fg_bright=(220, 224, 232),          # pewter
        fg_dim=(135, 145, 160),              # ash grey
        fg_subtle=(180, 188, 200),           # silver
        fg_accent=(120, 175, 215),           # cool steel blue
        outline=(60, 90, 115),                # deep steel for outlines
        weather_sun=(230, 200, 110),
        weather_cloud=(180, 190, 205),
        weather_rain=(120, 180, 220),
        weather_snow=(220, 230, 240),
        weather_storm=(235, 205, 115),
    ),
    clock_style="digital",
    button_style="rounded",
)


# Order shown in the Theme picker. The map-friendly classics lead;
# Daylight (the only light theme) trails so it's easy to find.
THEMES: list[Theme] = [DARK, SLATE_THEME, MARINE, VFD, AMBER, INK, LIGHT]
DEFAULT = DARK
