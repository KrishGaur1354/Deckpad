#!/usr/bin/env python3
"""DeckPad sender - turns a Steam Deck into a network game controller.

Runs on the Steam Deck (add DeckPad.sh as a non-Steam game). Reads the Deck's
built-in controls through SDL2 (already present on SteamOS, accessed via
ctypes so nothing needs to be installed) and streams the state over UDP to
deckpad_receiver.py running on another machine, which presents a virtual
Xbox 360 controller there, plus a virtual mouse fed by the Deck's
trackpad (map it "As Mouse" in Steam Input) and, optionally, the gyro.

Controls inside the app:
  View + Menu (both at once) ... open/close the settings menu
  D-pad up/down ................ select a setting
  D-pad left/right ............. change its value
  B (in menu) .................. close the menu (config is saved)

While the menu is open, neutral controller state is sent so you don't
accidentally control the host machine.
"""

import ctypes
import json
import math
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
PROTO_MAGIC = b"DKP2"
STATE_FMT = "<4sII9h"          # magic, seq, buttons, lx ly rx ry lt rt, mouse dx dy, wheel
DISCOVER_MSG = b"DKP2?DISCOVER"
HERE_PREFIX = b"DKP2!HERE"
RUMBLE_PREFIX = b"DKP2!RUMBLE"  # + 2 bytes: large motor, small motor (0-255)
DEFAULT_PORT = 30666

# Canonical output button bit positions (shared with the receiver).
# mouse_* bits are injected as mouse clicks on the receiver, not gamepad
# buttons, so paddles (L4/L5/R4/R5) can be mapped to clicks too.
OUT_BITS = {
    "a": 0, "b": 1, "x": 2, "y": 3,
    "back": 4, "guide": 5, "start": 6,
    "ls": 7, "rs": 8, "lb": 9, "rb": 10,
    "dpad_up": 11, "dpad_down": 12, "dpad_left": 13, "dpad_right": 14,
    "mouse_left": 15, "mouse_right": 16, "mouse_middle": 17,
}

# SDL_GameController button index -> deck-side button name
SDL_BUTTON_NAMES = {
    0: "a", 1: "b", 2: "x", 3: "y",
    4: "back", 5: "guide", 6: "start",
    7: "ls", 8: "rs", 9: "lb", 10: "rb",
    11: "dpad_up", 12: "dpad_down", 13: "dpad_left", 14: "dpad_right",
    15: "misc1",
    16: "paddle1", 17: "paddle2", 18: "paddle3", 19: "paddle4",
}

AXIS_LX, AXIS_LY, AXIS_RX, AXIS_RY, AXIS_LT, AXIS_RT = range(6)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    # "auto" = find the receiver by broadcast; or set a fixed IP like "192.168.1.20"
    "target_ip": "auto",
    "send_rate_hz": 120,
    "deadzone_left": 0.08,
    "deadzone_right": 0.08,
    "trigger_deadzone": 0.02,
    "sensitivity_left": 1.0,
    "sensitivity_right": 1.0,
    "invert_left_y": False,
    "invert_right_y": False,
    "swap_ab": False,
    "swap_xy": False,
    # Gyro: "off", "mouse" (aim moves the PC mouse) or "right_stick"
    # (aim is added to the right stick for gyro aiming in games).
    "gyro_mode": "off",
    "gyro_sensitivity": 1.0,
    # Stream trackpad/touch mouse movement + clicks to the PC mouse.
    # (In Steam Input, map a trackpad "As Mouse"; its clicks come along.)
    "touch_mouse": True,
    "mouse_sensitivity": 1.0,
    # Play game rumble sent back by the receiver on the Deck's motors.
    "rumble": True,
    # Full remapping: deck button name -> output button name (see OUT_BITS keys).
    # Anything not listed maps to itself. paddles/misc1 are unmapped by default;
    # e.g. "paddle1": "a" makes a back grip act as A.
    "button_map": {},
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        for k in cfg:
            if k in user:
                cfg[k] = user[k]
    except FileNotFoundError:
        save_config(cfg)
    except (ValueError, OSError) as e:
        print(f"config.json unreadable ({e}), using defaults", file=sys.stderr)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        print(f"could not save config: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SDL2 via ctypes
# ---------------------------------------------------------------------------
sdl = ctypes.CDLL("libSDL2-2.0.so.0")
ttf = ctypes.CDLL("libSDL2_ttf-2.0.so.0")

SDL_INIT_VIDEO = 0x20
SDL_INIT_GAMECONTROLLER = 0x2000
SDL_WINDOW_SHOWN = 0x4
SDL_WINDOW_FULLSCREEN_DESKTOP = 0x1001
SDL_WINDOWPOS_CENTERED = 0x2FFF0000
SDL_RENDERER_ACCELERATED = 0x2
SDL_RENDERER_PRESENTVSYNC = 0x4

SDL_QUIT = 0x100
SDL_CONTROLLERDEVICEADDED = 0x653
SDL_CONTROLLERDEVICEREMOVED = 0x654
SDL_MOUSEMOTION = 0x400
SDL_MOUSEBUTTONDOWN = 0x401
SDL_MOUSEBUTTONUP = 0x402
SDL_MOUSEWHEEL = 0x403
SDL_SENSOR_GYRO = 2


class SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


class SDL_Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8),
                ("b", ctypes.c_uint8), ("a", ctypes.c_uint8)]


def _sig(lib, name, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


P = ctypes.c_void_p
SDL_Init = _sig(sdl, "SDL_Init", ctypes.c_int, [ctypes.c_uint32])
SDL_GetError = _sig(sdl, "SDL_GetError", ctypes.c_char_p, [])
SDL_SetHint = _sig(sdl, "SDL_SetHint", ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p])
SDL_CreateWindow = _sig(sdl, "SDL_CreateWindow", P,
                        [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_uint32])
SDL_CreateRenderer = _sig(sdl, "SDL_CreateRenderer", P, [P, ctypes.c_int, ctypes.c_uint32])
SDL_SetRenderDrawColor = _sig(sdl, "SDL_SetRenderDrawColor", ctypes.c_int,
                              [P, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8])
SDL_RenderClear = _sig(sdl, "SDL_RenderClear", ctypes.c_int, [P])
SDL_RenderFillRect = _sig(sdl, "SDL_RenderFillRect", ctypes.c_int, [P, ctypes.POINTER(SDL_Rect)])
SDL_RenderDrawRect = _sig(sdl, "SDL_RenderDrawRect", ctypes.c_int, [P, ctypes.POINTER(SDL_Rect)])
SDL_RenderPresent = _sig(sdl, "SDL_RenderPresent", None, [P])
SDL_PollEvent = _sig(sdl, "SDL_PollEvent", ctypes.c_int, [ctypes.c_void_p])
SDL_NumJoysticks = _sig(sdl, "SDL_NumJoysticks", ctypes.c_int, [])
SDL_IsGameController = _sig(sdl, "SDL_IsGameController", ctypes.c_int, [ctypes.c_int])
SDL_GameControllerOpen = _sig(sdl, "SDL_GameControllerOpen", P, [ctypes.c_int])
SDL_GameControllerClose = _sig(sdl, "SDL_GameControllerClose", None, [P])
SDL_GameControllerName = _sig(sdl, "SDL_GameControllerName", ctypes.c_char_p, [P])
SDL_GameControllerGetButton = _sig(sdl, "SDL_GameControllerGetButton", ctypes.c_uint8, [P, ctypes.c_int])
SDL_GameControllerGetAxis = _sig(sdl, "SDL_GameControllerGetAxis", ctypes.c_int16, [P, ctypes.c_int])
SDL_CreateTextureFromSurface = _sig(sdl, "SDL_CreateTextureFromSurface", P, [P, P])
SDL_FreeSurface = _sig(sdl, "SDL_FreeSurface", None, [P])
SDL_DestroyTexture = _sig(sdl, "SDL_DestroyTexture", None, [P])
SDL_QueryTexture = _sig(sdl, "SDL_QueryTexture", ctypes.c_int,
                        [P, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int),
                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)])
SDL_RenderCopy = _sig(sdl, "SDL_RenderCopy", ctypes.c_int,
                      [P, P, ctypes.POINTER(SDL_Rect), ctypes.POINTER(SDL_Rect)])
SDL_ShowCursor = _sig(sdl, "SDL_ShowCursor", ctypes.c_int, [ctypes.c_int])
SDL_SetRelativeMouseMode = _sig(sdl, "SDL_SetRelativeMouseMode", ctypes.c_int, [ctypes.c_int])

# Sensor API needs SDL >= 2.0.14 (SteamOS ships newer; be graceful elsewhere).
try:
    SDL_GameControllerHasSensor = _sig(sdl, "SDL_GameControllerHasSensor",
                                       ctypes.c_int, [P, ctypes.c_int])
    SDL_GameControllerSetSensorEnabled = _sig(sdl, "SDL_GameControllerSetSensorEnabled",
                                              ctypes.c_int, [P, ctypes.c_int, ctypes.c_int])
    SDL_GameControllerGetSensorData = _sig(sdl, "SDL_GameControllerGetSensorData",
                                           ctypes.c_int,
                                           [P, ctypes.c_int, ctypes.POINTER(ctypes.c_float),
                                            ctypes.c_int])
    HAS_SENSOR_API = True
except AttributeError:
    HAS_SENSOR_API = False

# Rumble needs SDL >= 2.0.9, battery query >= 2.0.4.
try:
    SDL_GameControllerRumble = _sig(sdl, "SDL_GameControllerRumble", ctypes.c_int,
                                    [P, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32])
    HAS_RUMBLE_API = True
except AttributeError:
    HAS_RUMBLE_API = False
try:
    SDL_GameControllerGetJoystick = _sig(sdl, "SDL_GameControllerGetJoystick", P, [P])
    SDL_JoystickCurrentPowerLevel = _sig(sdl, "SDL_JoystickCurrentPowerLevel",
                                         ctypes.c_int, [P])
    HAS_BATTERY_API = True
except AttributeError:
    HAS_BATTERY_API = False

BATTERY_LEVELS = {0: "empty", 1: "low", 2: "medium", 3: "full", 4: "wired", 5: "full"}


def battery_text(pad):
    if not (pad and HAS_BATTERY_API):
        return "?"
    joy = SDL_GameControllerGetJoystick(pad)
    if not joy:
        return "?"
    return BATTERY_LEVELS.get(SDL_JoystickCurrentPowerLevel(joy), "?")

TTF_Init = _sig(ttf, "TTF_Init", ctypes.c_int, [])
TTF_OpenFont = _sig(ttf, "TTF_OpenFont", P, [ctypes.c_char_p, ctypes.c_int])
TTF_RenderUTF8_Blended = _sig(ttf, "TTF_RenderUTF8_Blended", P, [P, ctypes.c_char_p, SDL_Color])

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]


class TextRenderer:
    """Renders text through SDL_ttf with a small texture cache."""

    def __init__(self, renderer):
        self.renderer = renderer
        self.fonts = {}
        self.cache = {}
        self.font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
        if self.font_path is None:
            raise RuntimeError("no usable TTF font found")

    def _font(self, size):
        if size not in self.fonts:
            f = TTF_OpenFont(self.font_path.encode(), size)
            if not f:
                raise RuntimeError("TTF_OpenFont failed")
            self.fonts[size] = f
        return self.fonts[size]

    def draw(self, text, x, y, size=22, color=(230, 230, 235), center=False):
        if not text:
            return
        key = (text, size, color)
        entry = self.cache.get(key)
        if entry is None:
            if len(self.cache) > 512:
                for tex, _, _ in self.cache.values():
                    SDL_DestroyTexture(tex)
                self.cache.clear()
            surf = TTF_RenderUTF8_Blended(self._font(size), text.encode(),
                                          SDL_Color(color[0], color[1], color[2], 255))
            if not surf:
                return
            tex = SDL_CreateTextureFromSurface(self.renderer, surf)
            SDL_FreeSurface(surf)
            if not tex:
                return
            fmt = ctypes.c_uint32()
            acc = ctypes.c_int()
            w = ctypes.c_int()
            h = ctypes.c_int()
            SDL_QueryTexture(tex, ctypes.byref(fmt), ctypes.byref(acc),
                             ctypes.byref(w), ctypes.byref(h))
            entry = (tex, w.value, h.value)
            self.cache[key] = entry
        tex, w, h = entry
        if center:
            x -= w // 2
        dst = SDL_Rect(int(x), int(y), w, h)
        SDL_RenderCopy(self.renderer, tex, None, ctypes.byref(dst))


# ---------------------------------------------------------------------------
# Input processing
# ---------------------------------------------------------------------------
def shape_stick(x, y, deadzone, sensitivity, invert_y):
    """Radial deadzone + sensitivity scaling. In/out range: SDL int16."""
    fx, fy = x / 32767.0, y / 32767.0
    mag = math.hypot(fx, fy)
    if mag <= deadzone:
        return 0, 0
    scaled = min(1.0, (mag - deadzone) / (1.0 - deadzone) * sensitivity)
    fx, fy = fx / mag * scaled, fy / mag * scaled
    if invert_y:
        fy = -fy
    return int(fx * 32767), int(fy * 32767)


def shape_trigger(v, deadzone):
    f = v / 32767.0
    if f <= deadzone:
        return 0
    return int(min(1.0, (f - deadzone) / (1.0 - deadzone)) * 32767)


def enable_gyro(pad):
    """Turn on the gyro sensor for a controller. Returns True if usable."""
    if not (pad and HAS_SENSOR_API):
        return False
    if not SDL_GameControllerHasSensor(pad, SDL_SENSOR_GYRO):
        return False
    return SDL_GameControllerSetSensorEnabled(pad, SDL_SENSOR_GYRO, 1) == 0


def read_gyro(pad):
    """Angular rates in rad/s: (pitch, yaw, roll)."""
    data = (ctypes.c_float * 3)()
    if SDL_GameControllerGetSensorData(pad, SDL_SENSOR_GYRO, data, 3) != 0:
        return 0.0, 0.0, 0.0
    return data[0], data[1], data[2]


def build_state(pad, cfg, neutral=False):
    """Returns (buttons_bitmask, [lx, ly, rx, ry, lt, rt])."""
    if neutral or not pad:
        return 0, [0, 0, 0, 0, 0, 0]

    bmap = dict(cfg["button_map"])
    if cfg["swap_ab"]:
        bmap.setdefault("a", "b")
        bmap.setdefault("b", "a")
    if cfg["swap_xy"]:
        bmap.setdefault("x", "y")
        bmap.setdefault("y", "x")

    buttons = 0
    for sdl_idx, name in SDL_BUTTON_NAMES.items():
        if not SDL_GameControllerGetButton(pad, sdl_idx):
            continue
        out = bmap.get(name, name)
        bit = OUT_BITS.get(out)
        if bit is not None:
            buttons |= 1 << bit

    lx, ly = shape_stick(SDL_GameControllerGetAxis(pad, AXIS_LX),
                         SDL_GameControllerGetAxis(pad, AXIS_LY),
                         cfg["deadzone_left"], cfg["sensitivity_left"],
                         cfg["invert_left_y"])
    rx, ry = shape_stick(SDL_GameControllerGetAxis(pad, AXIS_RX),
                         SDL_GameControllerGetAxis(pad, AXIS_RY),
                         cfg["deadzone_right"], cfg["sensitivity_right"],
                         cfg["invert_right_y"])
    lt = shape_trigger(SDL_GameControllerGetAxis(pad, AXIS_LT), cfg["trigger_deadzone"])
    rt = shape_trigger(SDL_GameControllerGetAxis(pad, AXIS_RT), cfg["trigger_deadzone"])
    return buttons, [lx, ly, rx, ry, lt, rt]


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
class NetLink:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setblocking(False)
        self.seq = 0
        self.target = None if cfg["target_ip"] == "auto" else (cfg["target_ip"], cfg["port"])
        self.fixed_target = self.target is not None
        self.receiver_name = ""
        self.last_reply = 0.0
        self.last_ping = 0.0
        self.sent = 0
        self.rtt_ms = None        # round-trip of the last ping/HERE exchange
        self.rumble = None        # (large, small) 0-255 pending from receiver

    @property
    def connected(self):
        return self.target is not None and (time.time() - self.last_reply) < 5.0

    def tick(self, buttons, axes):
        now = time.time()
        # Ping / discover every 2s: keeps "connected" fresh and finds receivers.
        if now - self.last_ping > 2.0:
            self.last_ping = now
            for addr in {("255.255.255.255", self.cfg["port"]),
                         self.target or ("255.255.255.255", self.cfg["port"])}:
                try:
                    self.sock.sendto(DISCOVER_MSG, addr)
                except OSError:
                    pass
        # Collect replies.
        while True:
            try:
                data, addr = self.sock.recvfrom(256)
            except (BlockingIOError, OSError):
                break
            if data.startswith(RUMBLE_PREFIX):
                if len(data) == len(RUMBLE_PREFIX) + 2 and \
                        self.target and addr[0] == self.target[0]:
                    self.rumble = (data[-2], data[-1])
            elif data.startswith(HERE_PREFIX):
                if not self.fixed_target:
                    self.target = (addr[0], self.cfg["port"])
                if self.target and addr[0] == self.target[0]:
                    self.last_reply = now
                    self.receiver_name = data[len(HERE_PREFIX) + 1:].decode(errors="replace")
                    self.rtt_ms = (now - self.last_ping) * 1000.0
        # Send state.
        if self.target:
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            pkt = struct.pack(STATE_FMT, PROTO_MAGIC, self.seq, buttons, *axes)
            try:
                self.sock.sendto(pkt, self.target)
                self.sent += 1
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Settings menu
# ---------------------------------------------------------------------------
class MenuItem:
    def __init__(self, label, key, kind, lo=0, hi=1, step=0.01, fmt=None, options=None):
        self.label, self.key, self.kind = label, key, kind
        self.lo, self.hi, self.step = lo, hi, step
        self.options = options or []
        self.fmt = fmt or (lambda v: str(v))

    def value_str(self, cfg):
        return self.fmt(cfg[self.key])

    def adjust(self, cfg, direction):
        v = cfg[self.key]
        if self.kind == "bool":
            cfg[self.key] = not v
        elif self.kind == "enum":
            i = self.options.index(v) if v in self.options else 0
            cfg[self.key] = self.options[(i + direction) % len(self.options)]
        elif self.kind == "int":
            cfg[self.key] = int(min(self.hi, max(self.lo, v + direction * self.step)))
        else:
            cfg[self.key] = round(min(self.hi, max(self.lo, v + direction * self.step)), 3)


def pct(v):
    return f"{v * 100:.0f}%"


MENU = [
    MenuItem("Send rate", "send_rate_hz", "int", 30, 250, 10, lambda v: f"{v} Hz"),
    MenuItem("Left stick deadzone", "deadzone_left", "float", 0.0, 0.6, 0.01, pct),
    MenuItem("Right stick deadzone", "deadzone_right", "float", 0.0, 0.6, 0.01, pct),
    MenuItem("Trigger deadzone", "trigger_deadzone", "float", 0.0, 0.6, 0.01, pct),
    MenuItem("Left stick sensitivity", "sensitivity_left", "float", 0.1, 2.5, 0.05, lambda v: f"{v:.2f}x"),
    MenuItem("Right stick sensitivity", "sensitivity_right", "float", 0.1, 2.5, 0.05, lambda v: f"{v:.2f}x"),
    MenuItem("Invert left Y", "invert_left_y", "bool", fmt=lambda v: "ON" if v else "OFF"),
    MenuItem("Invert right Y", "invert_right_y", "bool", fmt=lambda v: "ON" if v else "OFF"),
    MenuItem("Swap A/B (Nintendo)", "swap_ab", "bool", fmt=lambda v: "ON" if v else "OFF"),
    MenuItem("Swap X/Y", "swap_xy", "bool", fmt=lambda v: "ON" if v else "OFF"),
    MenuItem("Gyro", "gyro_mode", "enum", options=["off", "mouse", "right_stick"],
             fmt=lambda v: {"off": "OFF", "mouse": "MOUSE", "right_stick": "R-STICK"}[v]),
    MenuItem("Gyro sensitivity", "gyro_sensitivity", "float", 0.1, 5.0, 0.1,
             lambda v: f"{v:.1f}x"),
    MenuItem("Touch mouse", "touch_mouse", "bool", fmt=lambda v: "ON" if v else "OFF"),
    MenuItem("Mouse sensitivity", "mouse_sensitivity", "float", 0.1, 5.0, 0.1,
             lambda v: f"{v:.1f}x"),
    MenuItem("Rumble", "rumble", "bool", fmt=lambda v: "ON" if v else "OFF"),
]


# ---------------------------------------------------------------------------
# UI drawing
# ---------------------------------------------------------------------------
BG = (18, 18, 24)
PANEL = (30, 30, 40)
ACCENT = (86, 170, 255)
GOOD = (90, 200, 120)
BAD = (230, 100, 90)
DIM = (140, 140, 150)


def fill(ren, x, y, w, h, c):
    SDL_SetRenderDrawColor(ren, c[0], c[1], c[2], 255)
    r = SDL_Rect(int(x), int(y), int(w), int(h))
    SDL_RenderFillRect(ren, ctypes.byref(r))


def outline(ren, x, y, w, h, c):
    SDL_SetRenderDrawColor(ren, c[0], c[1], c[2], 255)
    r = SDL_Rect(int(x), int(y), int(w), int(h))
    SDL_RenderDrawRect(ren, ctypes.byref(r))


def draw_stick(ren, text, cx, cy, size, x, y, label):
    half = size // 2
    fill(ren, cx - half, cy - half, size, size, PANEL)
    outline(ren, cx - half, cy - half, size, size, DIM)
    px = cx + int(x / 32767.0 * (half - 12))
    py = cy + int(y / 32767.0 * (half - 12))
    fill(ren, px - 8, py - 8, 16, 16, ACCENT)
    text.draw(label, cx, cy + half + 8, 18, DIM, center=True)


def draw_trigger(ren, text, x, y, w, h, v, label):
    fill(ren, x, y, w, h, PANEL)
    outline(ren, x, y, w, h, DIM)
    fh = int(h * v / 32767.0)
    if fh:
        fill(ren, x, y + h - fh, w, fh, ACCENT)
    text.draw(label, x + w // 2, y + h + 8, 18, DIM, center=True)


BUTTON_GRID = [
    ("a", "A"), ("b", "B"), ("x", "X"), ("y", "Y"),
    ("lb", "LB"), ("rb", "RB"), ("back", "View"), ("start", "Menu"),
    ("ls", "L3"), ("rs", "R3"), ("mouse_left", "M-L"), ("mouse_right", "M-R"),
    ("dpad_up", "▲"), ("dpad_down", "▼"), ("dpad_left", "◀"), ("dpad_right", "▶"),
]


# Raw SDL button indices for deck-only inputs (only visible if the
# Steam Input template passes them through).
RAW_EXTRAS = [(16, "P1"), (17, "P2"), (18, "P3"), (19, "P4"), (15, "Misc")]


def draw_main(ren, text, W, H, pad, link, buttons, axes, cfg, gyro_ok, mouse_mode):
    text.draw("DeckPad", 40, 28, 40, ACCENT)
    text.draw("Steam Deck → network controller", 210, 44, 20, DIM)

    if pad:
        name = SDL_GameControllerName(pad)
        text.draw(f"Controller: {(name or b'?').decode(errors='replace')}", 40, 90, 20, GOOD)
    else:
        text.draw("Controller: not detected", 40, 90, 20, BAD)

    if link.connected:
        text.draw(f"Receiver: {link.receiver_name or link.target[0]} ({link.target[0]})",
                  40, 118, 20, GOOD)
    elif link.target:
        text.draw(f"Receiver: {link.target[0]} - no reply, still sending...", 40, 118, 20, BAD)
    else:
        text.draw("Receiver: searching on this network… "
                  "(start deckpad_receiver.py on the other machine)", 40, 118, 20, BAD)
    ping = f"{link.rtt_ms:.0f} ms" if (link.connected and link.rtt_ms is not None) else "-"
    text.draw(f"Packets sent: {link.sent}   Rate: {cfg['send_rate_hz']} Hz   "
              f"Port: {cfg['port']}   Ping: {ping}   Battery: {battery_text(pad)}",
              40, 146, 18, DIM)

    gmode = cfg["gyro_mode"]
    gyro_label = {"off": "OFF", "mouse": "MOUSE", "right_stick": "R-STICK"}[gmode]
    if gmode != "off" and not gyro_ok:
        gyro_label += " (sensor not available)"
    text.draw(f"Gyro: {gyro_label}   Touch mouse: "
              f"{'ON' if cfg['touch_mouse'] else 'OFF'}   Rumble: "
              f"{'ON' if cfg['rumble'] else 'OFF'}", 40, 172, 18, DIM)

    # Sticks + triggers
    draw_stick(ren, text, 200, 400, 220, axes[0], axes[1], "Left stick")
    draw_stick(ren, text, 480, 400, 220, axes[2], axes[3], "Right stick")
    draw_trigger(ren, text, 620, 290, 40, 220, axes[4], "LT")
    draw_trigger(ren, text, 690, 290, 40, 220, axes[5], "RT")

    # Buttons
    bx, by = 790, 290
    for i, (name, label) in enumerate(BUTTON_GRID):
        col, row = i % 4, i // 4
        x, y = bx + col * 105, by + row * 62
        on = bool(buttons >> OUT_BITS[name] & 1)
        fill(ren, x, y, 95, 52, ACCENT if on else PANEL)
        outline(ren, x, y, 95, 52, DIM)
        text.draw(label, x + 47, y + 14, 18, (20, 20, 26) if on else DIM, center=True)

    # Raw back grips / misc, shows what Steam Input actually delivers.
    gy_ = by + 4 * 62 + 6
    text.draw("grips (raw):", bx, gy_ + 8, 16, DIM)
    for i, (idx, label) in enumerate(RAW_EXTRAS):
        x = bx + 110 + i * 62
        on = bool(pad and SDL_GameControllerGetButton(pad, idx))
        fill(ren, x, gy_, 54, 32, ACCENT if on else PANEL)
        outline(ren, x, gy_, 54, 32, DIM)
        text.draw(label, x + 27, gy_ + 7, 15, (20, 20, 26) if on else DIM, center=True)

    if mouse_mode:
        text.draw("MOUSE MODE: trackpad/right stick = cursor, "
                  "RT = left click, LT = right click   (View + Y to exit)",
                  40, H - 80, 20, ACCENT)
    text.draw("Hold View + Menu: settings    View + Y: mouse mode", 40, H - 50, 20, DIM)


def draw_menu(ren, text, W, H, cfg, sel):
    fill(ren, W // 2 - 360, 60, 720, 660, PANEL)
    outline(ren, W // 2 - 360, 60, 720, 660, ACCENT)
    text.draw("Settings", W // 2, 78, 30, ACCENT, center=True)
    for i, item in enumerate(MENU):
        y = 126 + i * 34
        selc = ACCENT if i == sel else (230, 230, 235)
        if i == sel:
            fill(ren, W // 2 - 340, y - 5, 680, 31, (45, 45, 60))
        text.draw(item.label, W // 2 - 320, y, 21, selc)
        text.draw(item.value_str(cfg), W // 2 + 200, y, 21, selc)
    text.draw("D-pad: navigate/change    B or View+Menu: close & save",
              W // 2, 660, 18, DIM, center=True)
    text.draw("Full button remapping + fixed IP: edit config.json",
              W // 2, 685, 16, DIM, center=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def open_first_controller():
    for i in range(SDL_NumJoysticks()):
        if SDL_IsGameController(i):
            pad = SDL_GameControllerOpen(i)
            if pad:
                return pad
    return None


def main():
    windowed = "--windowed" in sys.argv
    smoke_test = "--smoke-test" in sys.argv
    if smoke_test:
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    cfg = load_config()

    SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
    if SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER) != 0:
        sys.exit(f"SDL_Init failed: {SDL_GetError().decode()}")
    if TTF_Init() != 0:
        sys.exit("TTF_Init failed")

    W, H = 1280, 800
    flags = SDL_WINDOW_SHOWN | (0 if (windowed or smoke_test) else SDL_WINDOW_FULLSCREEN_DESKTOP)
    win = SDL_CreateWindow(b"DeckPad", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
                           W, H, flags)
    if not win:
        sys.exit(f"CreateWindow failed: {SDL_GetError().decode()}")
    ren = SDL_CreateRenderer(win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC)
    if not ren:
        ren = SDL_CreateRenderer(win, -1, 0)
    SDL_ShowCursor(0)
    text = TextRenderer(ren)

    pad = open_first_controller()
    gyro_ok = enable_gyro(pad)
    link = NetLink(cfg)

    menu_open = False
    menu_sel = 0
    combo_latch = False       # View+Menu edge detection
    nav_repeat = {}           # button -> next allowed repeat time

    mouse_fx = mouse_fy = 0.0  # fractional mouse delta accumulators
    mouse_buttons = 0          # OUT_BITS mouse_* bits from touch clicks
    wheel_acc = 0              # scroll ticks since last packet
    rel_mode = False
    mouse_mode = False         # View+Y: trackpad = cursor, LT/RT = clicks
    mm_latch = False
    MOUSE_BTN_BIT = {1: OUT_BITS["mouse_left"], 2: OUT_BITS["mouse_middle"],
                     3: OUT_BITS["mouse_right"]}

    event = ctypes.create_string_buffer(128)
    running = True
    next_send = time.perf_counter()
    last_loop = None
    frames = 0

    def nav_pressed(idx, now):
        """Edge + auto-repeat for menu navigation buttons."""
        down = pad and SDL_GameControllerGetButton(pad, idx)
        if not down:
            nav_repeat.pop(idx, None)
            return False
        t = nav_repeat.get(idx)
        if t is None:
            nav_repeat[idx] = now + 0.4
            return True
        if now >= t:
            nav_repeat[idx] = now + 0.12
            return True
        return False

    start_time = time.time()
    while running:
        while SDL_PollEvent(event):
            etype = struct.unpack_from("<I", event.raw, 0)[0]
            if etype == SDL_QUIT:
                running = False
            elif etype == SDL_CONTROLLERDEVICEADDED and not pad:
                pad = open_first_controller()
                gyro_ok = enable_gyro(pad)
            elif etype == SDL_CONTROLLERDEVICEREMOVED and pad:
                SDL_GameControllerClose(pad)
                pad = open_first_controller()
                gyro_ok = enable_gyro(pad)
            elif etype == SDL_MOUSEMOTION and (cfg["touch_mouse"] or mouse_mode):
                # SDL_MouseMotionEvent: xrel at byte 28, yrel at 32
                xrel, yrel = struct.unpack_from("<ii", event.raw, 28)
                mouse_fx += xrel * cfg["mouse_sensitivity"]
                mouse_fy += yrel * cfg["mouse_sensitivity"]
            elif etype == SDL_MOUSEWHEEL and (cfg["touch_mouse"] or mouse_mode):
                # SDL_MouseWheelEvent: y (scroll amount) at byte 20
                wheel_acc += struct.unpack_from("<i", event.raw, 20)[0]
            elif etype in (SDL_MOUSEBUTTONDOWN, SDL_MOUSEBUTTONUP) and \
                    (cfg["touch_mouse"] or mouse_mode):
                # SDL_MouseButtonEvent: button id at byte 16
                bit = MOUSE_BTN_BIT.get(event.raw[16])
                if bit is not None:
                    if etype == SDL_MOUSEBUTTONDOWN:
                        mouse_buttons |= 1 << bit
                    else:
                        mouse_buttons &= ~(1 << bit)

        now = time.perf_counter()

        # Menu combo: View(4) + Menu/Start(6)
        combo = bool(pad and SDL_GameControllerGetButton(pad, 4)
                     and SDL_GameControllerGetButton(pad, 6))
        if combo and not combo_latch:
            menu_open = not menu_open
            if menu_open:
                mouse_buttons = 0
            else:
                save_config(cfg)
        combo_latch = combo

        # Mouse mode combo: View(4) + Y(3)
        mm_combo = bool(pad and SDL_GameControllerGetButton(pad, 4)
                        and SDL_GameControllerGetButton(pad, 3))
        if mm_combo and not mm_latch and not menu_open:
            mouse_mode = not mouse_mode
        mm_latch = mm_combo

        # Capture the pointer so trackpad-as-mouse motion arrives as
        # relative deltas we can stream.
        want_rel = (cfg["touch_mouse"] or mouse_mode) and not menu_open and not smoke_test
        if want_rel != rel_mode:
            SDL_SetRelativeMouseMode(1 if want_rel else 0)
            rel_mode = want_rel

        if menu_open and pad:
            if nav_pressed(11, now):
                menu_sel = (menu_sel - 1) % len(MENU)
            if nav_pressed(12, now):
                menu_sel = (menu_sel + 1) % len(MENU)
            if nav_pressed(13, now):
                MENU[menu_sel].adjust(cfg, -1)
            if nav_pressed(14, now):
                MENU[menu_sel].adjust(cfg, +1)
            if nav_pressed(1, now):  # B closes
                menu_open = False
                save_config(cfg)

        buttons, axes = build_state(pad, cfg, neutral=menu_open)

        # Gyro: integrate angular rate into mouse deltas or right stick.
        dt = min(now - last_loop, 0.05) if last_loop is not None else 0.0
        last_loop = now
        gmode = cfg["gyro_mode"]
        if pad and gyro_ok and gmode != "off" and not menu_open:
            pitch, yaw, _roll = read_gyro(pad)
            if gmode == "mouse":
                k = 600.0 * cfg["gyro_sensitivity"] * dt
                mouse_fx += -yaw * k
                mouse_fy += -pitch * k
            else:  # right_stick
                k = cfg["gyro_sensitivity"] / 3.0  # 3 rad/s = full deflection
                rx = max(-1.0, min(1.0, axes[2] / 32767.0 - yaw * k))
                ry = max(-1.0, min(1.0, axes[3] / 32767.0 - pitch * k))
                axes[2], axes[3] = int(rx * 32767), int(ry * 32767)

        # Mouse mode: LT = right click, RT = left click, right stick glides
        # the cursor. These inputs are withheld from the gamepad so games
        # don't see phantom pulls.
        trigger_clicks = 0
        if mouse_mode and not menu_open:
            if axes[4] > 9830:   # about 30 percent pull
                trigger_clicks |= 1 << OUT_BITS["mouse_right"]
            if axes[5] > 9830:
                trigger_clicks |= 1 << OUT_BITS["mouse_left"]
            axes[4] = axes[5] = 0
            k = 900.0 * cfg["mouse_sensitivity"] * dt   # px/s at full tilt
            mouse_fx += axes[2] / 32767.0 * k
            mouse_fy += axes[3] / 32767.0 * k
            axes[2] = axes[3] = 0

        if menu_open:
            mouse_fx = mouse_fy = 0.0
            wheel_acc = 0

        # Rumble echoed back by the receiver -> Deck motors.
        if link.rumble is not None:
            large, small = link.rumble
            link.rumble = None
            if pad and HAS_RUMBLE_API and cfg["rumble"]:
                SDL_GameControllerRumble(pad, large * 257, small * 257, 3000)

        if now >= next_send:
            mdx = max(-32767, min(32767, int(mouse_fx)))
            mdy = max(-32767, min(32767, int(mouse_fy)))
            mwh = max(-32767, min(32767, wheel_acc))
            mouse_fx -= mdx
            mouse_fy -= mdy
            wheel_acc -= mwh
            sent_buttons = buttons if menu_open else \
                buttons | mouse_buttons | trigger_clicks
            link.tick(sent_buttons, axes + [mdx, mdy, mwh])
            interval = 1.0 / max(30, cfg["send_rate_hz"])
            next_send = max(next_send + interval, now - interval)

        # Render at ~30 fps regardless of send rate.
        frames += 1
        SDL_SetRenderDrawColor(ren, BG[0], BG[1], BG[2], 255)
        SDL_RenderClear(ren)
        draw_main(ren, text, W, H, pad, link,
                  buttons if menu_open else buttons | mouse_buttons | trigger_clicks,
                  axes, cfg, gyro_ok, mouse_mode)
        if menu_open:
            draw_menu(ren, text, W, H, cfg, menu_sel)
        SDL_RenderPresent(ren)

        # Pace the loop: render frame took some time; sleep till next send slot.
        sleep_for = next_send - time.perf_counter()
        if sleep_for > 0:
            time.sleep(min(sleep_for, 1 / 30))

        if smoke_test and time.time() - start_time > 2:
            print(f"smoke test OK: pad={'yes' if pad else 'no'}, "
                  f"packets sent={link.sent}, frames={frames}")
            running = False

    save_config(cfg)


if __name__ == "__main__":
    main()
