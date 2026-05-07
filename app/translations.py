"""Flat translation tables, one dict per language.

Keys are dotted strings grouped by surface (`scene.<x>.title`,
`button.<x>`, `demo.step.<x>`, ...). English is the source of truth;
other languages may omit keys and the i18n_service falls back to the
English string at lookup time.

Format strings use `{name}` placeholders consumed by str.format().
Translators MUST preserve every `{...}` placeholder exactly as written
in the English source — the i18n service falls back to English if a
translation drops one, but the user briefly sees an English string.
"""
from __future__ import annotations


# Display order in the language picker. Native names so Spanish users
# see "Espanol" rather than "Spanish" etc.
LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"),
    ("es", "Espanol"),
    ("no", "Norsk"),
]


# --- English (source of truth) ---------------------------------------
EN: dict[str, str] = {
    # Scene titles
    "scene.launcher.title": "Apps",
    "scene.settings.title": "Settings",
    "scene.theme.title": "Theme",
    "scene.language.title": "Language",
    "scene.wifi.title": "Wifi",
    "scene.wifi_password.title": "Wifi password — {ssid}",
    "scene.bluetooth.title": "Bluetooth",
    "scene.verse.title": "Verse of the Day",
    "scene.weather.title": "Weather",
    "scene.station_list.title": "Stations",
    "scene.alarm_list.title": "Alarms",
    "scene.alarm_edit.title.new": "New Alarm",
    "scene.alarm_edit.title.edit": "Edit Alarm",
    "scene.about.title": "About",
    "scene.brightness.title": "Brightness",
    "scene.audio_output.title": "Audio Output",
    "scene.background.title": "Background",
    "scene.map_center.title": "Map Centre",
    "scene.demo_intro.title": "Demo Tour",

    # Settings rows
    "settings.row.wifi": "WIFI",
    "settings.row.bluetooth": "BLUETOOTH",
    "settings.row.audio": "AUDIO",
    "settings.row.theme": "THEME",
    "settings.row.language": "LANGUAGE",
    "settings.row.background": "BACKGROUND",
    "settings.row.brightness": "BRIGHTNESS",
    "settings.row.demo": "DEMO",
    "settings.row.about": "ABOUT",

    # Launcher tiles
    "launcher.tile.radio": "RADIO",
    "launcher.tile.alarms": "ALARMS",
    "launcher.tile.weather": "WEATHER",
    "launcher.tile.verse": "VERSE",
    "launcher.tile.camera": "CAMERA",
    "launcher.tile.settings": "SETTINGS",

    # Quick-panel header / actions
    "quick.radio": "Radio",
    "quick.next_label": "Next: {time} {days}",
    "quick.stop_radio": "STOP RADIO",
    "quick.skip_next_alarm": "SKIP NEXT ALARM",
    "quick.unskip_next": "UNSKIP NEXT",
    "button.close": "CLOSE",

    # Action / nav buttons
    "button.home": "HOME",
    "button.cancel": "CANCEL",
    "button.save": "SAVE",
    "button.delete": "DELETE",
    "button.forget": "FORGET",
    "button.start": "START",
    "button.stop": "STOP",
    "button.play": "PLAY",
    "button.pause": "PAUSE",
    "button.exit": "EXIT",
    "button.next": "NEXT",
    "button.ok": "OK",
    "button.show": "SHOW",
    "button.hide": "HIDE",
    "button.shift": "SHIFT",
    "button.del": "DEL",
    "button.space": "SPACE",
    "button.refresh": "REFRESH",
    "button.rescan": "RESCAN",
    "button.add": "+ ADD",
    "button.stations": "STATIONS",
    "button.vol_down": "VOL−",
    "button.vol_up": "VOL+",
    "button.use_output": "USE THIS OUTPUT",
    "button.run_tour": "RUN GUIDED TOUR",
    "button.skip_next": "SKIP NEXT",
    "button.unskip": "UNSKIP",

    # Alarm edit / list
    "alarm.enabled": "ENABLED",
    "alarm.disabled": "DISABLED",
    "alarm.on_prefix": "ON ",
    "alarm.off_prefix": "OFF",
    "alarm.no_alarm": "No alarm",
    "alarm.no_alarms_hint": "(no alarms — tap +ADD)",
    "alarm.snoozed": "Snoozed",
    "alarm.snoozed_until": "Snoozed until {time}",
    "alarm.snz_short": "Snz",
    "alarm.skip_marker": "skip next",

    # Day labels (alarm.days_label)
    "days.once": "once",
    "days.weekdays": "Mon–Fri",
    "days.weekend": "Sat–Sun",
    "days.every_day": "every day",
    "day.short.mon": "Mon",
    "day.short.tue": "Tue",
    "day.short.wed": "Wed",
    "day.short.thu": "Thu",
    "day.short.fri": "Fri",
    "day.short.sat": "Sat",
    "day.short.sun": "Sun",
    # Alarm-edit single-letter day toggles. English doubles up
    # T (Tue/Thu) and S (Sat/Sun); translators may pick distinct
    # letters (e.g. Norwegian "L" for Lordag distinguishes Sat).
    "day.letter.mon": "M",
    "day.letter.tue": "T",
    "day.letter.wed": "W",
    "day.letter.thu": "T",
    "day.letter.fri": "F",
    "day.letter.sat": "S",
    "day.letter.sun": "S",

    # Brightness scene
    "brightness.active": "Active",
    "brightness.idle_dim": "Idle dim",
    "brightness.night_red": "Night red",

    # Background scene
    "background.style.none": "None",
    "background.style.slate": "Slate",
    "background.style.atlas": "Atlas",
    "background.style.vintage": "Vintage",
    "background.style.blueprint": "Blueprint",
    "background.style.globe": "Globe",
    "background.style.starmap": "Star Map",
    "background.overlay.city_lights": "City Lights",
    "background.overlay.water": "Lakes & Rivers",
    "background.overlay.political": "Political Borders",
    "background.overlay.annotations": "Latitudes & Terminator",
    "background.center_button": "Centre: {location}  ▸",

    # Map centre cities
    "map_center.greenwich": "London (Greenwich)",
    "map_center.jerusalem": "Jerusalem",
    "map_center.mecca": "Mecca",
    "map_center.new_york": "New York",
    "map_center.chicago": "Chicago",
    "map_center.seattle": "Seattle",
    "map_center.honolulu": "Honolulu",
    "map_center.tokyo": "Tokyo",
    "map_center.beijing": "Beijing",
    "map_center.sydney": "Sydney",
    "map_center.cape_town": "Cape Town",
    "map_center.buenos_aires": "Buenos Aires",

    # Wifi
    "wifi.connecting": "Connecting…",
    "wifi.error": "Error: {message}",
    "wifi.connected": "On: {ssid} {signal}% {ip}",
    "wifi.not_connected": "Not connected ({state})",
    "wifi.empty_list": "(no networks — tap RESCAN)",
    "wifi.password_hint": "(tap keys)",

    # Bluetooth
    "bluetooth.scanning": "Searching…",
    "bluetooth.busy": "Working…",
    "bluetooth.empty_list": (
        "(no devices — put your speaker into pairing mode and tap RESCAN)"
    ),
    "bluetooth.connected": "Connected: {name}",
    "bluetooth.idle": "(no speaker connected)",
    "bluetooth.unavailable": "Bluetooth unavailable",
    "bluetooth.error": "Error: {message}",
    "bluetooth.tag.audio": "♪",
    "bluetooth.tag.connected": "[connected]",
    "bluetooth.tag.paired": "[paired]",

    # Verse / weather loading
    "verse.loading": "Loading…",
    "weather.locating": "Locating…",

    # Audio output
    "audio.no_outputs": "No outputs reported by MPD",
    "audio.active": "ACTIVE",
    "audio.row.plugin": "Plugin   {plugin}",
    "audio.row.device": "Device   {device}",

    # Stations
    "station.empty_list": "(no stations — see README)",
    "station.unknown": "(unknown station)",

    # About scene rows
    "about.row.host": "Host    {hostname}",
    "about.row.kernel": "Kernel  {release}",
    "about.row.ip": "IP      {ip}",
    "about.row.theme": "Theme   {name}",
    "about.row.language": "Lang    {name}",
    "about.row.alarms": "Alarms  {count}",
    "about.row.stations": "Stations  {count}",
    "about.row.mpd": "MPD     {state}",

    # Misc placeholders
    "misc.dash": "—",

    # Weather conditions (WMO labels)
    "weather.code.clear": "Clear",
    "weather.code.mostly_clear": "Mostly clear",
    "weather.code.partly_cloudy": "Partly cloudy",
    "weather.code.cloudy": "Cloudy",
    "weather.code.fog": "Fog",
    "weather.code.drizzle": "Drizzle",
    "weather.code.frz_drizzle": "Frz drizzle",
    "weather.code.rain": "Rain",
    "weather.code.heavy_rain": "Heavy rain",
    "weather.code.frz_rain": "Frz rain",
    "weather.code.snow": "Snow",
    "weather.code.heavy_snow": "Heavy snow",
    "weather.code.showers": "Showers",
    "weather.code.heavy_showers": "Heavy showers",
    "weather.code.snow_showers": "Snow showers",
    "weather.code.storm": "Storm",
    "weather.code.storm_hail": "Storm + hail",
    "weather.code.unknown": "Code {code}",
    "weather.cond_line": "{label}    wind {wind} km/h",

    # Demo intro scene
    "demo_intro.description": (
        "A guided tour walks through the world map, themes, "
        "brightness, wifi, radio, alarms, weather and verse. "
        "Your current settings are restored when the tour ends."
    ),
    "demo_intro.option.full": "Full tour (~90 sec)",
    "demo_intro.option.short": "Short tour (~65 sec)",
    "demo_intro.option.wifi": "Include wifi setup",

    # Demo step captions
    "demo.step.hello": "Hello.",
    "demo.step.welcome": "Welcome to your clock radio.",
    "demo.step.quick_tour": "Here's a quick tour.",
    "demo.step.home_intro": (
        "This is the clock face — the world map shows real-time "
        "daylight across the planet."
    ),
    "demo.step.lit_hemisphere": (
        "The lit hemisphere follows the sun in real time."
    ),
    "demo.step.styles_full": (
        "Other map styles include atlas, slate, vintage and blueprint."
    ),
    "demo.step.styles_short": (
        "Map styles include globe, atlas, slate, vintage and blueprint."
    ),
    "demo.step.settings": (
        "Settings — wifi, audio, themes, background, "
        "brightness, about."
    ),
    "demo.step.background": (
        "Pick a base map style and stack overlays "
        "(city lights, water, borders, annotations)."
    ),
    "demo.step.themes": (
        "Themes change the colour palette across every screen."
    ),
    "demo.step.brightness": (
        "Two brightness levels — active and idle dim — "
        "and a night-red mode that preserves dark adaptation."
    ),
    "demo.step.wifi": (
        "Wifi: tap RESCAN, pick a network, enter its password "
        "to connect."
    ),
    "demo.step.launcher": (
        "Tap anywhere on the clock face to open Apps."
    ),
    "demo.step.stations": (
        "Internet radio — tap a station to start streaming."
    ),
    "demo.step.alarms": (
        "Alarms — set a time, days of the week, "
        "and which station plays."
    ),
    "demo.step.weather": (
        "A short forecast for your saved location."
    ),
    "demo.step.verse": (
        "A daily verse — quiet bedside reading."
    ),
    "demo.step.outro_splash": "That's the tour.",
    "demo.step.outro_home": (
        "Your previous settings have been restored."
    ),
}


# --- Espanol ----------------------------------------------------------
ES: dict[str, str] = {
    # Scene titles
    "scene.launcher.title": "Aplicaciones",
    "scene.settings.title": "Ajustes",
    "scene.theme.title": "Tema",
    "scene.language.title": "Idioma",
    "scene.wifi.title": "Wifi",
    "scene.wifi_password.title": "Contrasena wifi — {ssid}",
    "scene.bluetooth.title": "Bluetooth",
    "scene.verse.title": "Versiculo del dia",
    "scene.weather.title": "Tiempo",
    "scene.station_list.title": "Emisoras",
    "scene.alarm_list.title": "Alarmas",
    "scene.alarm_edit.title.new": "Nueva alarma",
    "scene.alarm_edit.title.edit": "Editar alarma",
    "scene.about.title": "Acerca de",
    "scene.brightness.title": "Brillo",
    "scene.audio_output.title": "Salida de audio",
    "scene.background.title": "Fondo",
    "scene.map_center.title": "Centro del mapa",
    "scene.demo_intro.title": "Demostracion",

    # Settings rows
    "settings.row.wifi": "WIFI",
    "settings.row.bluetooth": "BLUETOOTH",
    "settings.row.audio": "AUDIO",
    "settings.row.theme": "TEMA",
    "settings.row.language": "IDIOMA",
    "settings.row.background": "FONDO",
    "settings.row.brightness": "BRILLO",
    "settings.row.demo": "DEMO",
    "settings.row.about": "INFO",

    # Launcher tiles
    "launcher.tile.radio": "RADIO",
    "launcher.tile.alarms": "ALARMAS",
    "launcher.tile.weather": "TIEMPO",
    "launcher.tile.verse": "VERSICULO",
    "launcher.tile.camera": "CAMARA",
    "launcher.tile.settings": "AJUSTES",

    # Quick-panel
    "quick.radio": "Radio",
    "quick.next_label": "Proxima: {time} {days}",
    "quick.stop_radio": "PARAR RADIO",
    "quick.skip_next_alarm": "OMITIR PROX. ALARMA",
    "quick.unskip_next": "REACTIVAR PROX.",
    "button.close": "CERRAR",

    # Action / nav buttons
    "button.home": "INICIO",
    "button.cancel": "CANCELAR",
    "button.save": "GUARDAR",
    "button.delete": "BORRAR",
    "button.forget": "OLVIDAR",
    "button.start": "INICIAR",
    "button.stop": "PARAR",
    "button.play": "REPR.",
    "button.pause": "PAUSA",
    "button.exit": "SALIR",
    "button.next": "SIG.",
    "button.ok": "OK",
    "button.show": "VER",
    "button.hide": "OCULTAR",
    "button.shift": "MAYUS",
    "button.del": "BORR",
    "button.space": "ESPACIO",
    "button.refresh": "ACTUALIZAR",
    "button.rescan": "BUSCAR",
    "button.add": "+ ANADIR",
    "button.stations": "EMISORAS",
    "button.vol_down": "VOL−",
    "button.vol_up": "VOL+",
    "button.use_output": "USAR ESTA SALIDA",
    "button.run_tour": "INICIAR DEMOSTRACION",
    "button.skip_next": "OMITIR",
    "button.unskip": "REACTIVAR",

    # Alarm
    "alarm.enabled": "ACTIVA",
    "alarm.disabled": "INACTIVA",
    "alarm.on_prefix": "ON ",
    "alarm.off_prefix": "OFF",
    "alarm.no_alarm": "Sin alarma",
    "alarm.no_alarms_hint": "(sin alarmas — toca +ANADIR)",
    "alarm.snoozed": "Pospuesta",
    "alarm.snoozed_until": "Pospuesta hasta {time}",
    "alarm.snz_short": "Pos",
    "alarm.skip_marker": "omitir prox.",

    # Days
    "days.once": "una vez",
    "days.weekdays": "Lun–Vie",
    "days.weekend": "Sab–Dom",
    "days.every_day": "todos los dias",
    "day.short.mon": "Lun",
    "day.short.tue": "Mar",
    "day.short.wed": "Mie",
    "day.short.thu": "Jue",
    "day.short.fri": "Vie",
    "day.short.sat": "Sab",
    "day.short.sun": "Dom",
    "day.letter.mon": "L",
    "day.letter.tue": "M",
    "day.letter.wed": "X",
    "day.letter.thu": "J",
    "day.letter.fri": "V",
    "day.letter.sat": "S",
    "day.letter.sun": "D",

    # Brightness
    "brightness.active": "Activo",
    "brightness.idle_dim": "Reposo",
    "brightness.night_red": "Rojo nocturno",

    # Background
    "background.style.none": "Ninguno",
    "background.style.slate": "Pizarra",
    "background.style.atlas": "Atlas",
    "background.style.vintage": "Vintage",
    "background.style.blueprint": "Plano",
    "background.style.globe": "Globo",
    "background.style.starmap": "Mapa estelar",
    "background.overlay.city_lights": "Luces urbanas",
    "background.overlay.water": "Lagos y rios",
    "background.overlay.political": "Fronteras politicas",
    "background.overlay.annotations": "Latitudes y terminador",
    "background.center_button": "Centro: {location}  ▸",

    # Map centre
    "map_center.greenwich": "Londres (Greenwich)",
    "map_center.jerusalem": "Jerusalen",
    "map_center.mecca": "La Meca",
    "map_center.new_york": "Nueva York",
    "map_center.chicago": "Chicago",
    "map_center.seattle": "Seattle",
    "map_center.honolulu": "Honolulu",
    "map_center.tokyo": "Tokio",
    "map_center.beijing": "Pekin",
    "map_center.sydney": "Sidney",
    "map_center.cape_town": "Ciudad del Cabo",
    "map_center.buenos_aires": "Buenos Aires",

    # Wifi
    "wifi.connecting": "Conectando…",
    "wifi.error": "Error: {message}",
    "wifi.connected": "On: {ssid} {signal}% {ip}",
    "wifi.not_connected": "Sin conexion ({state})",
    "wifi.empty_list": "(sin redes — toca BUSCAR)",
    "wifi.password_hint": "(toca las teclas)",

    # Bluetooth
    "bluetooth.scanning": "Buscando…",
    "bluetooth.busy": "Trabajando…",
    "bluetooth.empty_list": (
        "(sin dispositivos — pon el altavoz en modo emparejamiento "
        "y toca BUSCAR)"
    ),
    "bluetooth.connected": "Conectado: {name}",
    "bluetooth.idle": "(ningun altavoz conectado)",
    "bluetooth.unavailable": "Bluetooth no disponible",
    "bluetooth.error": "Error: {message}",
    "bluetooth.tag.audio": "♪",
    "bluetooth.tag.connected": "[conectado]",
    "bluetooth.tag.paired": "[emparejado]",

    # Loading
    "verse.loading": "Cargando…",
    "weather.locating": "Localizando…",

    # Audio
    "audio.no_outputs": "MPD no reporta salidas",
    "audio.active": "ACTIVA",
    "audio.row.plugin": "Plugin   {plugin}",
    "audio.row.device": "Disp.    {device}",

    # Stations
    "station.empty_list": "(sin emisoras — ver README)",
    "station.unknown": "(emisora desconocida)",

    # About
    "about.row.host": "Host    {hostname}",
    "about.row.kernel": "Kernel  {release}",
    "about.row.ip": "IP      {ip}",
    "about.row.theme": "Tema    {name}",
    "about.row.language": "Idioma  {name}",
    "about.row.alarms": "Alarmas {count}",
    "about.row.stations": "Emisoras  {count}",
    "about.row.mpd": "MPD     {state}",

    "misc.dash": "—",

    # Weather conditions
    "weather.code.clear": "Despejado",
    "weather.code.mostly_clear": "Mayormente desp.",
    "weather.code.partly_cloudy": "Parcial nublado",
    "weather.code.cloudy": "Nublado",
    "weather.code.fog": "Niebla",
    "weather.code.drizzle": "Llovizna",
    "weather.code.frz_drizzle": "Llov. helada",
    "weather.code.rain": "Lluvia",
    "weather.code.heavy_rain": "Lluvia fuerte",
    "weather.code.frz_rain": "Lluvia helada",
    "weather.code.snow": "Nieve",
    "weather.code.heavy_snow": "Nieve fuerte",
    "weather.code.showers": "Chubascos",
    "weather.code.heavy_showers": "Chubascos fuertes",
    "weather.code.snow_showers": "Aguanieve",
    "weather.code.storm": "Tormenta",
    "weather.code.storm_hail": "Tormenta + granizo",
    "weather.code.unknown": "Codigo {code}",
    "weather.cond_line": "{label}    viento {wind} km/h",

    # Demo intro
    "demo_intro.description": (
        "Una visita guiada recorre el mapa mundial, los temas, "
        "el brillo, wifi, radio, alarmas, tiempo y versiculo. "
        "Tus ajustes se restauran al terminar."
    ),
    "demo_intro.option.full": "Tour completo (~90 s)",
    "demo_intro.option.short": "Tour breve (~65 s)",
    "demo_intro.option.wifi": "Incluir configuracion wifi",

    # Demo steps
    "demo.step.hello": "Hola.",
    "demo.step.welcome": "Bienvenido a tu radio reloj.",
    "demo.step.quick_tour": "Esta es una visita rapida.",
    "demo.step.home_intro": (
        "Esta es la esfera del reloj — el mapa mundial muestra "
        "la luz del dia en tiempo real."
    ),
    "demo.step.lit_hemisphere": (
        "El hemisferio iluminado sigue al sol en tiempo real."
    ),
    "demo.step.styles_full": (
        "Otros estilos de mapa: atlas, pizarra, vintage y plano."
    ),
    "demo.step.styles_short": (
        "Estilos de mapa: globo, atlas, pizarra, vintage y plano."
    ),
    "demo.step.settings": (
        "Ajustes — wifi, audio, temas, fondo, brillo, info."
    ),
    "demo.step.background": (
        "Elige un estilo de mapa base y combina capas "
        "(luces, agua, fronteras, anotaciones)."
    ),
    "demo.step.themes": (
        "Los temas cambian la paleta de color en cada pantalla."
    ),
    "demo.step.brightness": (
        "Dos niveles de brillo — activo e inactivo — "
        "y un modo rojo nocturno para preservar la vision nocturna."
    ),
    "demo.step.wifi": (
        "Wifi: pulsa BUSCAR, elige una red, escribe la contrasena."
    ),
    "demo.step.launcher": (
        "Toca cualquier punto del reloj para abrir Aplicaciones."
    ),
    "demo.step.stations": (
        "Radio por internet — toca una emisora para escucharla."
    ),
    "demo.step.alarms": (
        "Alarmas — ajusta hora, dias y emisora que sonara."
    ),
    "demo.step.weather": (
        "Una breve prevision para tu ubicacion guardada."
    ),
    "demo.step.verse": (
        "Un versiculo diario — lectura tranquila junto a la cama."
    ),
    "demo.step.outro_splash": "Fin del tour.",
    "demo.step.outro_home": (
        "Tus ajustes anteriores han sido restaurados."
    ),
}


# --- Norsk (Bokmal) ---------------------------------------------------
NO: dict[str, str] = {
    # Scene titles
    "scene.launcher.title": "Apper",
    "scene.settings.title": "Innstillinger",
    "scene.theme.title": "Tema",
    "scene.language.title": "Sprak",
    "scene.wifi.title": "Wifi",
    "scene.wifi_password.title": "Wifi-passord — {ssid}",
    "scene.bluetooth.title": "Bluetooth",
    "scene.verse.title": "Dagens vers",
    "scene.weather.title": "Vaer",
    "scene.station_list.title": "Stasjoner",
    "scene.alarm_list.title": "Alarmer",
    "scene.alarm_edit.title.new": "Ny alarm",
    "scene.alarm_edit.title.edit": "Rediger alarm",
    "scene.about.title": "Om",
    "scene.brightness.title": "Lysstyrke",
    "scene.audio_output.title": "Lydutgang",
    "scene.background.title": "Bakgrunn",
    "scene.map_center.title": "Kartsentrum",
    "scene.demo_intro.title": "Omvisning",

    # Settings rows
    "settings.row.wifi": "WIFI",
    "settings.row.bluetooth": "BLUETOOTH",
    "settings.row.audio": "LYD",
    "settings.row.theme": "TEMA",
    "settings.row.language": "SPRAK",
    "settings.row.background": "BAKGRUNN",
    "settings.row.brightness": "LYSSTYRKE",
    "settings.row.demo": "DEMO",
    "settings.row.about": "OM",

    # Launcher tiles
    "launcher.tile.radio": "RADIO",
    "launcher.tile.alarms": "ALARMER",
    "launcher.tile.weather": "VAER",
    "launcher.tile.verse": "VERS",
    "launcher.tile.camera": "KAMERA",
    "launcher.tile.settings": "INNST.",

    # Quick-panel
    "quick.radio": "Radio",
    "quick.next_label": "Neste: {time} {days}",
    "quick.stop_radio": "STOPP RADIO",
    "quick.skip_next_alarm": "HOPP OVER NESTE",
    "quick.unskip_next": "ANGRE NESTE",
    "button.close": "LUKK",

    # Action / nav buttons
    "button.home": "HJEM",
    "button.cancel": "AVBRYT",
    "button.save": "LAGRE",
    "button.delete": "SLETT",
    "button.forget": "GLEM",
    "button.start": "START",
    "button.stop": "STOPP",
    "button.play": "SPILL",
    "button.pause": "PAUSE",
    "button.exit": "AVSLUTT",
    "button.next": "NESTE",
    "button.ok": "OK",
    "button.show": "VIS",
    "button.hide": "SKJUL",
    "button.shift": "SKIFT",
    "button.del": "SLETT",
    "button.space": "MELLOMROM",
    "button.refresh": "OPPDATER",
    "button.rescan": "SOK PA NYTT",
    "button.add": "+ LEGG TIL",
    "button.stations": "STASJONER",
    "button.vol_down": "VOL−",
    "button.vol_up": "VOL+",
    "button.use_output": "BRUK DENNE UTGANGEN",
    "button.run_tour": "START OMVISNING",
    "button.skip_next": "HOPP OVER",
    "button.unskip": "ANGRE",

    # Alarm
    "alarm.enabled": "PA",
    "alarm.disabled": "AV",
    "alarm.on_prefix": "ON ",
    "alarm.off_prefix": "OFF",
    "alarm.no_alarm": "Ingen alarm",
    "alarm.no_alarms_hint": "(ingen alarmer — trykk +LEGG TIL)",
    "alarm.snoozed": "Slumret",
    "alarm.snoozed_until": "Slumret til {time}",
    "alarm.snz_short": "Slm",
    "alarm.skip_marker": "hopp over neste",

    # Days
    "days.once": "en gang",
    "days.weekdays": "Man–Fre",
    "days.weekend": "Lor–Son",
    "days.every_day": "hver dag",
    "day.short.mon": "Man",
    "day.short.tue": "Tir",
    "day.short.wed": "Ons",
    "day.short.thu": "Tor",
    "day.short.fri": "Fre",
    "day.short.sat": "Lor",
    "day.short.sun": "Son",
    "day.letter.mon": "M",
    "day.letter.tue": "T",
    "day.letter.wed": "O",
    "day.letter.thu": "T",
    "day.letter.fri": "F",
    "day.letter.sat": "L",
    "day.letter.sun": "S",

    # Brightness
    "brightness.active": "Aktiv",
    "brightness.idle_dim": "Hvile",
    "brightness.night_red": "Nattrod",

    # Background
    "background.style.none": "Ingen",
    "background.style.slate": "Skifer",
    "background.style.atlas": "Atlas",
    "background.style.vintage": "Vintage",
    "background.style.blueprint": "Tegning",
    "background.style.globe": "Globus",
    "background.style.starmap": "Stjernekart",
    "background.overlay.city_lights": "Bylys",
    "background.overlay.water": "Innsjoer & elver",
    "background.overlay.political": "Landegrenser",
    "background.overlay.annotations": "Breddegrader & terminator",
    "background.center_button": "Sentrum: {location}  ▸",

    # Map centre
    "map_center.greenwich": "London (Greenwich)",
    "map_center.jerusalem": "Jerusalem",
    "map_center.mecca": "Mekka",
    "map_center.new_york": "New York",
    "map_center.chicago": "Chicago",
    "map_center.seattle": "Seattle",
    "map_center.honolulu": "Honolulu",
    "map_center.tokyo": "Tokyo",
    "map_center.beijing": "Beijing",
    "map_center.sydney": "Sydney",
    "map_center.cape_town": "Cape Town",
    "map_center.buenos_aires": "Buenos Aires",

    # Wifi
    "wifi.connecting": "Kobler til…",
    "wifi.error": "Feil: {message}",
    "wifi.connected": "On: {ssid} {signal}% {ip}",
    "wifi.not_connected": "Ikke tilkoblet ({state})",
    "wifi.empty_list": "(ingen nettverk — trykk SOK PA NYTT)",
    "wifi.password_hint": "(trykk taster)",

    # Bluetooth
    "bluetooth.scanning": "Soker…",
    "bluetooth.busy": "Arbeider…",
    "bluetooth.empty_list": (
        "(ingen enheter — sett hoyttaleren i parringsmodus "
        "og trykk SOK PA NYTT)"
    ),
    "bluetooth.connected": "Tilkoblet: {name}",
    "bluetooth.idle": "(ingen hoyttaler tilkoblet)",
    "bluetooth.unavailable": "Bluetooth utilgjengelig",
    "bluetooth.error": "Feil: {message}",
    "bluetooth.tag.audio": "♪",
    "bluetooth.tag.connected": "[tilkoblet]",
    "bluetooth.tag.paired": "[parret]",

    # Loading
    "verse.loading": "Laster…",
    "weather.locating": "Finner sted…",

    # Audio
    "audio.no_outputs": "MPD rapporterer ingen utganger",
    "audio.active": "AKTIV",
    "audio.row.plugin": "Plugin   {plugin}",
    "audio.row.device": "Enhet    {device}",

    # Stations
    "station.empty_list": "(ingen stasjoner — se README)",
    "station.unknown": "(ukjent stasjon)",

    # About
    "about.row.host": "Vert    {hostname}",
    "about.row.kernel": "Kjerne  {release}",
    "about.row.ip": "IP      {ip}",
    "about.row.theme": "Tema    {name}",
    "about.row.language": "Sprak   {name}",
    "about.row.alarms": "Alarmer {count}",
    "about.row.stations": "Stasjoner  {count}",
    "about.row.mpd": "MPD     {state}",

    "misc.dash": "—",

    # Weather conditions
    "weather.code.clear": "Klart",
    "weather.code.mostly_clear": "Stort sett klart",
    "weather.code.partly_cloudy": "Delvis skyet",
    "weather.code.cloudy": "Skyet",
    "weather.code.fog": "Take",
    "weather.code.drizzle": "Yr",
    "weather.code.frz_drizzle": "Frosset yr",
    "weather.code.rain": "Regn",
    "weather.code.heavy_rain": "Kraftig regn",
    "weather.code.frz_rain": "Frosset regn",
    "weather.code.snow": "Sno",
    "weather.code.heavy_snow": "Kraftig sno",
    "weather.code.showers": "Byger",
    "weather.code.heavy_showers": "Kraftige byger",
    "weather.code.snow_showers": "Snobyger",
    "weather.code.storm": "Storm",
    "weather.code.storm_hail": "Storm + hagl",
    "weather.code.unknown": "Kode {code}",
    "weather.cond_line": "{label}    vind {wind} km/h",

    # Demo intro
    "demo_intro.description": (
        "En guidet omvisning gar gjennom verdenskartet, temaer, "
        "lysstyrke, wifi, radio, alarmer, vaer og dagens vers. "
        "Innstillingene dine gjenopprettes nar omvisningen avsluttes."
    ),
    "demo_intro.option.full": "Full omvisning (~90 sek)",
    "demo_intro.option.short": "Kort omvisning (~65 sek)",
    "demo_intro.option.wifi": "Inkluder wifi-oppsett",

    # Demo steps
    "demo.step.hello": "Hei.",
    "demo.step.welcome": "Velkommen til klokkeradioen din.",
    "demo.step.quick_tour": "Her er en rask omvisning.",
    "demo.step.home_intro": (
        "Dette er klokkeflaten — verdenskartet viser "
        "dagslys i sanntid."
    ),
    "demo.step.lit_hemisphere": (
        "Den opplyste halvkulen folger sola i sanntid."
    ),
    "demo.step.styles_full": (
        "Andre kartstiler: atlas, skifer, vintage og tegning."
    ),
    "demo.step.styles_short": (
        "Kartstiler: globus, atlas, skifer, vintage og tegning."
    ),
    "demo.step.settings": (
        "Innstillinger — wifi, lyd, temaer, bakgrunn, "
        "lysstyrke, om."
    ),
    "demo.step.background": (
        "Velg en kartstil og legg til lag "
        "(bylys, vann, grenser, anmerkninger)."
    ),
    "demo.step.themes": (
        "Temaer endrer fargepaletten pa alle skjermbilder."
    ),
    "demo.step.brightness": (
        "To lysstyrkenivaer — aktiv og hvilemodus — "
        "og en nattrodmodus som bevarer morketilpasning."
    ),
    "demo.step.wifi": (
        "Wifi: trykk SOK PA NYTT, velg et nettverk, "
        "skriv passordet."
    ),
    "demo.step.launcher": (
        "Trykk hvor som helst pa klokkeflaten for a apne Apper."
    ),
    "demo.step.stations": (
        "Internettradio — trykk en stasjon for a spille av."
    ),
    "demo.step.alarms": (
        "Alarmer — angi tid, ukedager og hvilken stasjon "
        "som skal spilles."
    ),
    "demo.step.weather": (
        "En kort vaermelding for stedet du har lagret."
    ),
    "demo.step.verse": (
        "Et daglig vers — stille lesning ved sengen."
    ),
    "demo.step.outro_splash": "Slutt pa omvisningen.",
    "demo.step.outro_home": (
        "Innstillingene dine er gjenopprettet."
    ),
}


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": EN,
    "es": ES,
    "no": NO,
}
