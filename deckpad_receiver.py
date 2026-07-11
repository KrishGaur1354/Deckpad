#!/usr/bin/env python3
"""DeckPad receiver — run this on the machine you want to control.

Listens for input streamed by deckpad_sender.py on the Steam Deck and
presents it as a virtual Xbox 360 controller:

  Windows:  uses vgamepad (ViGEm).  Install once:  pip install vgamepad
  Linux:    uses /dev/uinput directly — no packages needed.
            Run with sudo, or grant yourself uinput access (see README).

Usage:
  python deckpad_receiver.py [--port 30666] [--name my-laptop]

If the Deck stops sending (sleep, crash, Wi-Fi drop) the virtual pad is
reset to neutral after 0.5 s so nothing stays "held down".
"""

import argparse
import os
import socket
import struct
import sys
import time

PROTO_MAGIC = b"DKP1"
STATE_FMT = "<4sII6h"
STATE_SIZE = struct.calcsize(STATE_FMT)
DISCOVER_MSG = b"DKP1?DISCOVER"
HERE_PREFIX = b"DKP1!HERE"
DEFAULT_PORT = 30666
FAILSAFE_SECONDS = 0.5

def die(msg):
    """Exit with a message; when running as a double-clicked .exe, keep the
    console window open so the message can actually be read."""
    print(msg, file=sys.stderr)
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            input("\nPress Enter to close…")
        except EOFError:
            pass
    sys.exit(1)


# Bit positions in the buttons bitmask (must match the sender).
BITS = {
    "a": 0, "b": 1, "x": 2, "y": 3,
    "back": 4, "guide": 5, "start": 6,
    "ls": 7, "rs": 8, "lb": 9, "rb": 10,
    "dpad_up": 11, "dpad_down": 12, "dpad_left": 13, "dpad_right": 14,
}


# ---------------------------------------------------------------------------
# Backend: Windows (vgamepad / ViGEm)
# ---------------------------------------------------------------------------
class VgamepadBackend:
    def __init__(self):
        try:
            # importing vgamepad already connects to the ViGEmBus driver
            import vgamepad
            self.pad = vgamepad.VX360Gamepad()
        except ImportError:
            die("vgamepad is required on Windows.\n"
                "Install it with:  pip install vgamepad")
        except Exception as e:
            die(f"Could not create the virtual controller: {e}\n\n"
                "This usually means the ViGEmBus driver is not installed.\n"
                "Download and install it once from:\n"
                "  https://github.com/nefarius/ViGEmBus/releases\n"
                "then run DeckPad receiver again.")
        self.vg = vgamepad
        B = vgamepad.XUSB_BUTTON
        self.button_map = {
            "a": B.XUSB_GAMEPAD_A, "b": B.XUSB_GAMEPAD_B,
            "x": B.XUSB_GAMEPAD_X, "y": B.XUSB_GAMEPAD_Y,
            "back": B.XUSB_GAMEPAD_BACK, "guide": B.XUSB_GAMEPAD_GUIDE,
            "start": B.XUSB_GAMEPAD_START,
            "ls": B.XUSB_GAMEPAD_LEFT_THUMB, "rs": B.XUSB_GAMEPAD_RIGHT_THUMB,
            "lb": B.XUSB_GAMEPAD_LEFT_SHOULDER, "rb": B.XUSB_GAMEPAD_RIGHT_SHOULDER,
            "dpad_up": B.XUSB_GAMEPAD_DPAD_UP, "dpad_down": B.XUSB_GAMEPAD_DPAD_DOWN,
            "dpad_left": B.XUSB_GAMEPAD_DPAD_LEFT, "dpad_right": B.XUSB_GAMEPAD_DPAD_RIGHT,
        }

    def update(self, buttons, lx, ly, rx, ry, lt, rt):
        for name, vbtn in self.button_map.items():
            if buttons >> BITS[name] & 1:
                self.pad.press_button(vbtn)
            else:
                self.pad.release_button(vbtn)
        # SDL Y axis is + down; XInput is + up.
        self.pad.left_joystick(x_value=lx, y_value=-ly if ly != -32768 else 32767)
        self.pad.right_joystick(x_value=rx, y_value=-ry if ry != -32768 else 32767)
        self.pad.left_trigger(value=lt * 255 // 32767)
        self.pad.right_trigger(value=rt * 255 // 32767)
        self.pad.update()

    def close(self):
        self.pad.reset()
        self.pad.update()


# ---------------------------------------------------------------------------
# Backend: Linux (/dev/uinput, no dependencies)
# ---------------------------------------------------------------------------
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
BTN = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134,
    "lb": 0x136, "rb": 0x137,
    "back": 0x13A, "start": 0x13B, "guide": 0x13C,
    "ls": 0x13D, "rs": 0x13E,
}
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


class UinputBackend:
    def __init__(self):
        import fcntl
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError:
            sys.exit("No permission for /dev/uinput.\n"
                     "Run with sudo, or add a udev rule (see README).")
        for ev in (EV_KEY, EV_ABS, EV_SYN):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for code in BTN.values():
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ,
                     ABS_HAT0X, ABS_HAT0Y):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)

        # struct uinput_user_dev: name[80], input_id, ff_effects_max,
        # absmax[64], absmin[64], absfuzz[64], absflat[64]
        absmax = [0] * 64
        absmin = [0] * 64
        absflat = [0] * 64
        for code in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
            absmin[code], absmax[code], absflat[code] = -32768, 32767, 128
        for code in (ABS_Z, ABS_RZ):
            absmin[code], absmax[code] = 0, 255
        for code in (ABS_HAT0X, ABS_HAT0Y):
            absmin[code], absmax[code] = -1, 1
        dev = struct.pack("<80sHHHHI64i64i64i64i",
                          b"DeckPad Virtual X-Box 360 pad",
                          0x03, 0x045E, 0x028E, 0x0110, 0,
                          *absmax, *absmin, *([0] * 64), *absflat)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self._last = {}
        time.sleep(0.3)  # give the device node a moment to appear

    def _emit(self, etype, code, value):
        key = (etype, code)
        if self._last.get(key) == value:
            return
        self._last[key] = value
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, etype, code, value))

    def update(self, buttons, lx, ly, rx, ry, lt, rt):
        for name, code in BTN.items():
            self._emit(EV_KEY, code, buttons >> BITS[name] & 1)
        self._emit(EV_ABS, ABS_HAT0X,
                   (buttons >> BITS["dpad_right"] & 1) - (buttons >> BITS["dpad_left"] & 1))
        self._emit(EV_ABS, ABS_HAT0Y,
                   (buttons >> BITS["dpad_down"] & 1) - (buttons >> BITS["dpad_up"] & 1))
        self._emit(EV_ABS, ABS_X, lx)
        self._emit(EV_ABS, ABS_Y, ly)
        self._emit(EV_ABS, ABS_RX, rx)
        self._emit(EV_ABS, ABS_RY, ry)
        self._emit(EV_ABS, ABS_Z, lt * 255 // 32767)
        self._emit(EV_ABS, ABS_RZ, rt * 255 // 32767)
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, EV_SYN, SYN_REPORT, 0))

    def close(self):
        import fcntl
        self.update(0, 0, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def make_backend():
    if sys.platform == "win32":
        return VgamepadBackend()
    if sys.platform.startswith("linux"):
        return UinputBackend()
    sys.exit(f"Unsupported platform: {sys.platform} "
             "(no virtual gamepad backend available)")


def main():
    ap = argparse.ArgumentParser(description="DeckPad receiver")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=socket.gethostname(),
                    help="name shown on the Deck's screen")
    args = ap.parse_args()

    backend = make_backend()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)

    print(f"DeckPad receiver '{args.name}' listening on UDP {args.port}")
    print("Virtual Xbox 360 controller created. Waiting for the Deck…")

    here = HERE_PREFIX + b":" + args.name.encode()
    last_packet = 0.0
    last_seq = 0
    sender = None
    neutralized = True

    try:
        while True:
            try:
                data, addr = sock.recvfrom(256)
            except socket.timeout:
                if not neutralized and time.time() - last_packet > FAILSAFE_SECONDS:
                    backend.update(0, 0, 0, 0, 0, 0, 0)
                    neutralized = True
                    print("Signal lost — controller reset to neutral")
                continue

            if data == DISCOVER_MSG:
                sock.sendto(here, addr)
                continue
            if len(data) != STATE_SIZE:
                continue
            magic, seq, buttons, lx, ly, rx, ry, lt, rt = struct.unpack(STATE_FMT, data)
            if magic != PROTO_MAGIC:
                continue
            # Drop stale/reordered packets (allow seq reset on sender restart).
            if sender == addr[0] and 0 < (last_seq - seq) & 0xFFFFFFFF < 1000:
                continue
            last_seq = seq
            if sender != addr[0]:
                sender = addr[0]
                print(f"Receiving from {sender}")
            last_packet = time.time()
            neutralized = False
            backend.update(buttons, lx, ly, rx, ry, lt, rt)
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
