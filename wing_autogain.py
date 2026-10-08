#!/usr/bin/env python3
"""wing-autogain -- "Gus", an auto gain / gain wizard for the Behringer WING.

While someone plays, talks or sings, Gus reads each input's level from the
WING over the network and sets the preamp gain so the signal peaks at a sensible
target, then says what he did in plain sentences.

Built 2026-10-08, BEFORE the console arrived. Everything was tested against a
simulator and a fake network WING written from the official protocol document:

    RP = "WING Remote Protocols V3.1.0" (file rev 3.1-03), Patrick-Gilles
         Maillot, ~/Documents/Gear Manuals/Behringer WING/
    UM = WING series User Manual, 2025-10-20

Every protocol fact below is tagged in a comment:
    [RP <section>]   read in the official document
    [libwing]        seen in a third-party implementation (libwing 1.0.5, Rust)
    [ASSUMED]        inferred; MUST be checked on the real console first

Layers (the protocol layer is swappable; real-console testing only exercises
WingConsole):
    WingConsole   real console: OSC (UDP 2223) for parameters, native binary
                  protocol (TCP 2222, channel 3) for meters, data on UDP
    SimConsole    simulator: fake meter levels from a synthetic signal + gain
    decide()/analyze()/run_autogain()  console-independent logic

Python 3.9+, standard library only.
"""

import argparse
import contextlib
import datetime as _dt
import fcntl
import json
import math
import os
import queue
import random
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib

VERSION = "1.0 (2026-10-08, pre-console)"
NAME = "Gus"

HOME = os.path.expanduser("~")
DEFAULT_LOG = os.path.join(HOME, ".wing-autogain.log")
STATE_DIR = os.path.join(HOME, ".cache", "wing-autogain")
CONFIG_FILE = os.path.join(HOME, ".config", "wing-autogain", "config.json")
SHOWS_DIR = os.path.join(HOME, "Documents", "WING Shows")
DEFAULT_SIM_SHOW = "The Woodshed"

# ---------------------------------------------------------------------------
# Protocol facts
# ---------------------------------------------------------------------------
OSC_PORT = 2223          # [RP "Remote communications with WING"]: OSC on UDP 2223
NATIVE_PORT = 2222       # [RP same]: native on TCP 2222; discovery "WING?" on UDP 2222

# Source groups that HAVE a preamp gain parameter /io/in/<grp>/<n>/g.
# [RP "Input/Output Settings"]: LCL, A, B, C list "/g  F  -3..45.5  98 steps".
# AUX, SC, USB, CRD, MOD, PLAY, AES, USR, OSC have no /g row -> no gain to set.
GAIN_GROUPS = ("LCL", "A", "B", "C")
GROUP_SIZE = {"LCL": 8, "A": 48, "B": 48, "C": 48}
# LCL: [RP] node range "1..24" with footnote "All 24 local inputs may not be
# available depending on the console model"; the full-size WING has 8
# (UM "Local In (8 on-board preamps on WING...)"). A/B/C: [RP] "1..48".

# [RP "Input/Output Settings"] -3..45.5 dB, 98 steps => 0.5 dB per step.
# Note the RP is NOT self-consistent: /ch/1/in/set/$g says "-2.5..45 20 steps
# (LCL)" vs "-3.0..45.5 98 steps (AES)". So the tool asks the console for the
# live range (node description, "?" argument [RP "Special Node
# Type/Arguments"]) and only falls back to these numbers.
DOC_GAIN_RANGE = (-3.0, 45.5, 0.5)

# Meter request tokens [RP "Channel 3: Metering", Table 4].
MTR_PORT = 0xD3          # word: client UDP port
MTR_ID = 0xD4            # long: report id; repeat to renew (data lasts 5 s)
MTR_START = 0xDC
MTR_END = 0xDE
MTR_CHANNEL = 0xA0       # channel 1..40 -> 8 words each [RP Table 5]
MTR_AUX = 0xA1
MTR_SOURCE = 0xA7        # "source (input) device (1...16)" -> group levels
METER_CHID = 3           # [RP Table 1]: ChID 3 = Meter Data Requests ("dfd3")
METER_RENEW_S = 2.0      # data times out after 5 s [RP]; renew well inside it
METER_FRAME_S = 0.05     # "every approximately every 50ms" [RP "Meters"]

# [ASSUMED] Source "device" numbering for MTR_SOURCE. The RP only says
# "source (input) device (1...16)" and "source group levels (i.e. local ins:
# 8 meters)". We assume the devices follow the order in which the RP lists
# input groups (/io/in/LCL, AUX, A, B, C, SC, USB, CRD, MOD, PLAY, AES, USR,
# OSC = 13 groups, fits in 16). Supporting evidence: output devices are
# "1...11" and there are exactly 11 output groups in the same document order.
# CHECK WITH `wing-autogain --probe A` ON THE REAL CONSOLE.
SOURCE_DEVICE = {"LCL": 1, "AUX": 2, "A": 3, "B": 4, "C": 5, "SC": 6,
                 "USB": 7, "CRD": 8, "MOD": 9, "PLAY": 10, "AES": 11,
                 "USR": 12, "OSC": 13}

# [ASSUMED] Meter values are dBFS (0 = full scale) at 1/256 dB per count. The
# RP says "Level values are in 1/256 dB" but not the reference, and not
# whether a frame is a peak or an RMS value. We treat each ~50 ms frame as a
# peak-ish reading, take the max as "peak" and the power mean of the active
# frames as "average".
METER_SCALE = 256.0

SILENCE_DB = -50.0       # below this a frame counts as no signal
CLIP_DB = -0.5           # at/above this, call it clipping
MIN_ACTIVE_FRAMES = 6    # ~0.3 s of real signal at 20 frames/s
MIN_ACTIVE_FRACTION = 0.10
HARD_TARGET_CEILING = -6.0   # no preset or --target may aim hotter than this
HARD_MAX_STEP = 18.0

# ---------------------------------------------------------------------------
# Targets (peak dBFS on the input meter). See the guide for sources.
# ---------------------------------------------------------------------------
PRESETS = {
    "vocal":      (-12.0, "singing; singers get louder in the show"),
    "speech":     (-10.0, "talking; steadier than singing"),
    "drums":      (-10.0, "kick, snare, toms, cymbals"),
    "perc":       (-12.0, "hand percussion"),
    "horns":      (-12.0, "brass and reeds; loud and dynamic"),
    "line":       (-10.0, "keys, DI, playback; steady line level"),
    "instrument": (-10.0, "guitar and bass, mic or DI"),
    "room":       (-14.0, "room and ambience mics; pick up the whole band"),
    "default":    (-12.0, "anything else"),
}
TOLERANCE_DB = 2.0

PRESET_WORDS = [
    ("speech", r"talk|speech|spoken|\bmc\b|host|announce|pastor|preach"),
    ("room", r"room|ambi|audience|crowd"),
    ("vocal", r"vox|vocal|voice|sing|choir|bgv|harmony"),
    ("horns", r"horn|sax|tpt|trumpet|tbn|trombone|reed|clarinet|flute|brass|tuba|flugel"),
    ("perc", r"perc|conga|bongo|tumba|quinto|timbale|shaker|cajon|tamb|cowbell|djembe"),
    ("drums", r"kick|snare|tom\b|tom \d|hi-?hat|\bhh\b|ride|overhead|\boh\b|drum|cymbal|crash"),
    ("instrument", r"bass|gtr|guitar|uke|banjo|mandolin|violin|vln|viola|cello|fiddle"),
    ("line", r"key|piano|organ|leslie|synth|montage|rhodes|stage|juno|yc61|hydra|legend|pad|playback|computer|track|di\b"),
]


def guess_preset(name):
    low = (name or "").lower()
    for preset, pat in PRESET_WORDS:
        if re.search(pat, low):
            return preset
    return "default"


# ---------------------------------------------------------------------------
# Speech-friendly numbers
# ---------------------------------------------------------------------------
def say_num(x, half=False):
    """-22.4 -> 'minus 22'; half=True keeps .5 for gains ('32.5')."""
    if x is None:
        return "unknown"
    if half:
        v = round(x * 2) / 2.0
        s = ("%.1f" % abs(v)).rstrip("0").rstrip(".")
    else:
        v = round(x)
        s = "%d" % abs(v)
    if v < 0:
        return "minus " + s
    return s


def say_db(x):
    return say_num(x, half=True) + " dB"


# ---------------------------------------------------------------------------
# Gus's voice. Character lives only in sentences already being said; --plain
# drops every flavour phrase. Choice is a stable hash, so tests are repeatable
# and the same input reads the same way on a re-run.
# ---------------------------------------------------------------------------
FLAVOUR = {
    "listen": ["Give me some music.", "Play like it's the show.",
               "Sing it like you mean it.", "Whenever you're ready."],
    "raised": ["Nice and healthy.", "That'll sit nicely.",
               "Plenty of meat on it now.", "Much better."],
    "lowered": ["Plenty of headroom now.", "Breathing room restored.",
                "Calmer, and safer.", "Should be comfortable now."],
    "ontarget": ["Already sitting pretty.", "Nothing to fix.",
                 "Whoever set that knew what they were doing."],
    "silent": ["Is it plugged in, unmuted, and on phantom if it needs it?",
               "Check the cable and the mute."],
    "atmax": ["It needs a closer mic or a louder source.",
              "Move the mic in, or ask for more from the source."],
    "atmin": ["Turn the source down, or use a pad.",
              "That one's hot at the source; turn it down there."],
    "clip": ["Ouch.", "That was too hot."],
    "summary_good": ["Gain staging done. Go make some noise.",
                     "All set. Over to you.", "That's a wrap on gains."],
    "summary_warn": ["A few need a human; details above.",
                     "Mostly there; see the warnings above."],
    "dry": ["Nothing was changed, this was a dry run.",
            "Dry run, so hands off the console."],
}


class Voice:
    def __init__(self, plain=False):
        self.plain = plain

    def flavour(self, key, seed=""):
        if self.plain:
            return ""
        opts = FLAVOUR.get(key) or [""]
        return opts[zlib.crc32((key + "|" + seed).encode()) % len(opts)]

    def join(self, *parts):
        return " ".join(p.strip() for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# Speech output: one line per input as it finishes, through VoiceOver if it is
# running (the ~/bin/notify-mac technique), else `say`. Never fatal.
# ---------------------------------------------------------------------------
def _voiceover_running():
    try:
        r = subprocess.run(["pgrep", "-x", "VoiceOver"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


class Speaker:
    """Background queue so speaking never blocks or breaks the run.

    VoiceOver's `output` interrupts whatever it is saying, so lines are paced:
    after each one we wait roughly as long as it takes to say it.
    """

    WORDS_PER_SEC = 3.2

    def __init__(self, enabled):
        self.enabled = enabled and sys.platform == "darwin"
        self.q = queue.Queue()
        self.thread = None
        self.use_vo = None
        if self.enabled:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def say(self, text):
        if self.enabled and text:
            self.q.put(text)

    def _run(self):
        while True:
            text = self.q.get()
            if text is None:
                return
            try:
                self._speak(text)
            except Exception:
                pass
            finally:
                self.q.task_done()

    def _speak(self, text):
        if self.use_vo is None:
            self.use_vo = _voiceover_running()
        if self.use_vo:
            r = subprocess.run(
                ["osascript", "-e", "on run argv",
                 "-e", 'tell application "VoiceOver" to output (item 1 of argv)',
                 "-e", "end run", "--", text],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                time.sleep(min(20.0, 0.6 + len(text.split()) / self.WORDS_PER_SEC))
                return
        subprocess.run(["say", text], capture_output=True, timeout=60)

    def finish(self, timeout=90.0):
        if not self.enabled:
            return
        try:
            deadline = time.time() + timeout
            while not self.q.empty() or self.q.unfinished_tasks:
                if time.time() > deadline:
                    break
                time.sleep(0.1)
            self.q.put(None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class Target:
    """One thing to gain: a mono source, or a stereo pair (odd n, n+1)."""

    def __init__(self, grp, n, name, stereo=False, where="", preset=None,
                 warnings=None):
        self.grp = grp
        self.n = n
        self.name = name or ("%s%d" % (grp, n))
        self.stereo = stereo
        self.where = where          # e.g. "channel 32"
        self.preset = preset or guess_preset(self.name)
        self.warnings = list(warnings or [])

    @property
    def key(self):
        return (self.grp, self.n)

    def keys(self):
        if self.stereo:
            return [(self.grp, self.n), (self.grp, self.n + 1)]
        return [(self.grp, self.n)]

    def src_label(self):
        g = "Local " if self.grp == "LCL" else self.grp
        if self.stereo:
            return "%s%d and %d" % (g, self.n, self.n + 1)
        return "%s%d" % (g, self.n)

    def __repr__(self):
        return "Target(%s %s%d%s)" % (self.name, self.grp, self.n,
                                      " ST" if self.stereo else "")


class Measurement:
    def __init__(self, frames):
        self.frames = list(frames)
        n = len(self.frames)
        self.count = n
        self.peak = max(self.frames) if n else None
        active = [f for f in self.frames if f > SILENCE_DB]
        self.active = len(active)
        if active:
            p = sum(10 ** (f / 10.0) for f in active) / len(active)
            self.avg = 10 * math.log10(p)
        else:
            self.avg = None
        self.clip_frames = sum(1 for f in self.frames if f >= CLIP_DB)

    @property
    def no_data(self):
        return self.count == 0

    @property
    def silent(self):
        return self.count > 0 and (self.peak is None or self.peak <= SILENCE_DB)

    @property
    def enough(self):
        need = max(MIN_ACTIVE_FRAMES, int(MIN_ACTIVE_FRACTION * self.count))
        return self.active >= need

    @property
    def clipped(self):
        return self.clip_frames > 0


def analyze(frames):
    return Measurement(frames)


def merge_stereo(meas_list):
    """A stereo pair is judged by its louder side, frame by frame."""
    lists = [m.frames for m in meas_list if m.count]
    if not lists:
        return Measurement([])
    n = min(len(x) for x in lists)
    return Measurement([max(x[i] for x in lists) for i in range(n)])


# ---------------------------------------------------------------------------
# The decision -- pure function, no I/O
# ---------------------------------------------------------------------------
class Decision:
    def __init__(self, action, old, new=None, delta_wanted=0.0, capped=False,
                 limit=None, expected_peak=None):
        self.action = action        # raise, lower, hold, silent, weak, nodata
        self.old = old
        self.new = old if new is None else new
        self.delta_wanted = delta_wanted
        self.capped = capped
        self.limit = limit          # None, "max", "min", "user-max"
        self.expected_peak = expected_peak

    @property
    def change(self):
        return round(self.new - self.old, 3)

    def __repr__(self):
        return "Decision(%s %.1f->%.1f limit=%s capped=%s)" % (
            self.action, self.old, self.new, self.limit, self.capped)


def quantize_floor(x, lo, step):
    """Snap to the console's step grid, always rounding toward LESS gain."""
    k = math.floor((x - lo) / step + 1e-9)
    return round(lo + k * step, 4)


def decide(meas, gain, rng, target_peak, max_step=12.0, tolerance=TOLERANCE_DB,
           max_gain=None):
    lo, hi, step = rng
    if max_gain is not None:
        hi = min(hi, max_gain)
    target_peak = min(target_peak, HARD_TARGET_CEILING)
    max_step = max(step, min(max_step, HARD_MAX_STEP))

    if meas.no_data:
        return Decision("nodata", gain)
    if meas.clipped:
        # True level is unknown above the clip point, so drop at least 6 dB
        # and re-measure.
        wanted = min(target_peak - meas.peak, -6.0)
    elif meas.silent or not meas.enough:
        return Decision("silent" if meas.silent else "weak", gain)
    else:
        wanted = target_peak - meas.peak
        if abs(wanted) <= tolerance:
            return Decision("hold", gain, delta_wanted=wanted,
                            expected_peak=meas.peak)

    capped = abs(wanted) > max_step
    delta = max(-max_step, min(max_step, wanted))
    new = quantize_floor(gain + delta, lo, step)
    limit = None
    if new > hi:
        new = quantize_floor(hi, lo, step)
        limit = "user-max" if (max_gain is not None and max_gain < rng[1]) else "max"
    if new < lo:
        new = lo
        limit = "min"
    if wanted > 0 and new < gain:      # never lower when we wanted to raise
        new = gain
    if wanted < 0 and new > gain:      # never raise when we wanted to lower
        new = gain
    if wanted > 0 and new <= gain + 1e-9:
        if gain >= hi - 1e-9:
            limit = limit or ("user-max" if (max_gain is not None and max_gain < rng[1]) else "max")
        return Decision("hold", gain, delta_wanted=wanted, limit=limit,
                        expected_peak=meas.peak)
    if wanted < 0 and new >= gain - 1e-9:
        if gain <= lo + 1e-9:
            limit = "min"
        return Decision("hold", gain, delta_wanted=wanted, limit=limit,
                        expected_peak=meas.peak)
    action = "raise" if new > gain else "lower"
    exp = None if meas.clipped else meas.peak + (new - gain)
    return Decision(action, gain, new, wanted, capped, limit, exp)


# ---------------------------------------------------------------------------
# OSC codec [RP "OSC Data Types", "WING OSC Messages"]
# ---------------------------------------------------------------------------
def _osc_str(s):
    b = s.encode("utf-8") + b"\0"
    return b + b"\0" * ((4 - len(b) % 4) % 4)


def osc_encode(address, *args):
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            a = int(a)
        if isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _osc_str(a)
        elif isinstance(a, (bytes, bytearray)):
            tags += "b"
            blob = bytes(a)
            payload += struct.pack(">i", len(blob)) + blob + b"\0" * ((4 - len(blob) % 4) % 4)
        else:
            raise TypeError("unsupported OSC arg %r" % (a,))
    return _osc_str(address) + _osc_str(tags) + payload


def _read_osc_str(data, i):
    j = data.index(b"\0", i)
    s = data[i:j].decode("utf-8", "replace")
    j += 1
    j += (4 - j % 4) % 4
    return s, j


def osc_decode(data):
    addr, i = _read_osc_str(data, 0)
    if i >= len(data):
        return addr, []
    tags, i = _read_osc_str(data, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
        elif t == "s":
            s, i = _read_osc_str(data, i); args.append(s)
        elif t == "b":
            n = struct.unpack(">i", data[i:i + 4])[0]; i += 4
            args.append(data[i:i + n]); i += n + (4 - n % 4) % 4
    return addr, args


def osc_value(args):
    """WING answers a get with ',sff' (text, raw 0..1, value) for floats,
    ',sfi' for ints, ',s' for strings [RP "Reading (Get) Parameter"]. Return
    the real value: the last argument, or the string."""
    if not args:
        return None
    if len(args) >= 3 and isinstance(args[-1], (int, float)):
        return args[-1]
    return args[0]


DESC_GAIN_RE = re.compile(
    r"(?:^|[\s~])g\s+lin\s*\[\s*(-?[\d.]+)\s*\.\.\s*(-?[\d.]+)[^\]]*\]\s*,\s*(\d+)\s*steps")


def parse_gain_range(desc):
    """Parse the 'g' line out of a '?' node description. Format [ASSUMED]
    from the /fx/1 example in [RP "Special Node Type/Arguments"]:
    'fxmix  lin [0 .. 100 %], 101 steps'."""
    m = DESC_GAIN_RE.search(desc or "")
    if not m:
        return None
    lo, hi, steps = float(m.group(1)), float(m.group(2)), int(m.group(3))
    if steps < 2 or hi <= lo:
        return None
    return (lo, hi, round((hi - lo) / (steps - 1), 4))


# ---------------------------------------------------------------------------
# Native protocol framing [RP "WING native / binary data interface"]
# ---------------------------------------------------------------------------
NRP_ESC = 0xDF
NRP_BASE = 0xD0
NRP_NUM = 14


def nrp_frame(chid, payload):
    """Select channel (0xdf, 0xd0+ChID) then escape the payload exactly like
    the RP's 'Sample transmit routine'. ChID 3 -> 'dfd3' (meters), matching
    the RP metering example and libwing."""
    out = bytearray([NRP_ESC, NRP_BASE + chid])
    data = list(payload)
    i = 0
    esc = False
    while i < len(data):
        db = data[i]
        i += 1
        if db == NRP_ESC:
            esc = True
        else:
            if esc and NRP_BASE <= db <= NRP_BASE + NRP_NUM:
                out.append(NRP_ESC - 1)   # insert 0xde, then re-send db
                i -= 1
                esc = False
                continue
            esc = False
        out.append(db)
    if esc:
        out.append(NRP_ESC - 1)
    return bytes(out)


class NrpDecoder:
    """The RP 'Sample receive routine', yielding (chid, byte)."""

    def __init__(self):
        self.escf = False
        self.ch = -1

    def feed(self, data):
        out = []
        for db in data:
            if db == NRP_ESC and not self.escf:
                self.escf = True
                continue
            if self.escf:
                if db != NRP_ESC:
                    self.escf = False
                    if db == NRP_ESC - 1:
                        db = NRP_ESC
                    elif NRP_BASE <= db < NRP_BASE + NRP_NUM:
                        self.ch = db - NRP_BASE
                        continue
                    elif self.ch >= 0:
                        out.append((self.ch, NRP_ESC))
            if self.ch >= 0:
                out.append((self.ch, db))
        return out


def meter_port_payload(port):
    return bytes([MTR_PORT, (port >> 8) & 0xFF, port & 0xFF])


def meter_request_payload(report_id, kind, indexes):
    """d4 <id.l> dc <kind> <idx-1>... de. Index byte 0x00 = item 1 [RP Table
    4: '0x00...0x7f index 1...128'; the RP example 'dca001de' = channel 2]."""
    b = bytearray([MTR_ID]) + struct.pack(">I", report_id)
    b += bytes([MTR_START, kind])
    for n in indexes:
        if not 1 <= n <= 128:
            raise ValueError("meter index out of range")
        b.append(n - 1)
    b.append(MTR_END)
    return bytes(b)


def meter_renew_payload(report_id):
    return bytes([MTR_ID]) + struct.pack(">I", report_id)


def decode_meter_packet(data):
    """<report id (4 bytes)><n big-endian int16 words in 1/256 dB> [RP]."""
    if len(data) < 4:
        return None, []
    rid = struct.unpack(">I", data[:4])[0]
    body = data[4:]
    n = len(body) // 2
    words = struct.unpack(">%dh" % n, body[:2 * n]) if n else ()
    return rid, [w / METER_SCALE for w in words]


# ---------------------------------------------------------------------------
# The console interface. Both implementations provide:
#   gain(grp,n) -> float          set_gain(grp,n,db) -> float (read back)
#   gain_range(grp,n) -> (lo,hi,step), source string
#   source_info(grp,n) -> dict(name, mode, vph)
#   channels() -> list of dict(kind, num, name, tags, grp, n, altsrc)
#   dca_names() -> {num: name}
#   measure(keys, seconds, on_frame=None) -> {key: [dB frames]}
# ---------------------------------------------------------------------------
GAIN_PATH_RE = re.compile(r"^/io/in/(LCL|A|B|C)/\d{1,2}/g$")


class ConsoleError(Exception):
    pass


class WingConsole:
    """A real WING. OSC for parameters, native protocol for meters."""

    is_sim = False

    def __init__(self, host, osc_port=OSC_PORT, native_port=NATIVE_PORT,
                 timeout=0.6, retries=3, meter_mode="source", meter_port=0):
        self.host = host
        self.osc_port = osc_port
        self.native_port = native_port
        self.timeout = timeout
        self.retries = retries
        self.meter_mode = meter_mode
        self.meter_port = meter_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self._range_cache = {}
        self.channel_of = {}    # (grp,n) -> ("ch", num) for channel meter mode

    def close(self):
        with contextlib.suppress(Exception):
            self.sock.close()

    # -- OSC -------------------------------------------------------------
    def _drain(self):
        self.sock.setblocking(False)
        try:
            while True:
                self.sock.recvfrom(65536)
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.setblocking(True)

    def _request(self, address, *args, match=None):
        match = match or address
        pkt = osc_encode(address, *args)
        for _ in range(self.retries):
            self._drain()
            self.sock.sendto(pkt, (self.host, self.osc_port))
            deadline = time.time() + self.timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.sock.settimeout(left)
                try:
                    data, _addr = self.sock.recvfrom(65536)
                except socket.timeout:
                    break
                try:
                    addr, vals = osc_decode(data)
                except Exception:
                    continue
                if addr == match:
                    return vals
                if addr.startswith("/*") and vals and isinstance(vals[0], str) \
                        and vals[0] != "OK":
                    raise ConsoleError("console said %s for %s" % (vals[0], address))
        raise ConsoleError(
            "No answer from the WING at %s on OSC port %d. Check the address, "
            "the network, and SETUP, REMOTE, REMOTE LOCK." % (self.host, self.osc_port))

    def get(self, path):
        return osc_value(self._request(path))

    def ping(self):
        vals = self._request("/?")     # [RP "WING OSC Messages"]: '/?' -> 'WING,ip,name,model,serial,fw'
        return vals[0] if vals else ""

    def gain(self, grp, n):
        return float(self.get("/io/in/%s/%d/g" % (grp, n)))

    def set_gain(self, grp, n, db):
        path = "/io/in/%s/%d/g" % (grp, n)
        if not GAIN_PATH_RE.match(path):        # the ONLY thing we ever write
            raise ConsoleError("refusing to write %s" % path)
        # [RP "Writing (Set) Parameter"]: ',f' float; WING does not echo UDP sets.
        self.sock.sendto(osc_encode(path, float(db)), (self.host, self.osc_port))
        time.sleep(0.05)
        return self.gain(grp, n)

    def gain_range(self, grp, n):
        if grp in self._range_cache:
            return self._range_cache[grp]
        rng, src = None, "document"
        try:
            vals = self._request("/io/in/%s/%d" % (grp, n), "?")
            rng = parse_gain_range(vals[0] if vals else "")
            if rng:
                src = "console"
        except ConsoleError:
            pass
        out = (rng or DOC_GAIN_RANGE, src)
        self._range_cache[grp] = out
        return out

    def source_info(self, grp, n):
        base = "/io/in/%s/%d/" % (grp, n)
        info = {"name": self.get(base + "name") or "", "mode": self.get(base + "mode") or "M"}
        try:
            info["vph"] = bool(int(self.get(base + "vph")))   # read only, never written
        except Exception:
            info["vph"] = None
        return info

    def channels(self):
        out = []
        for kind, count in (("ch", 40), ("aux", 8)):
            for num in range(1, count + 1):
                b = "/%s/%d/" % (kind, num)
                out.append({
                    "kind": kind, "num": num,
                    "name": self.get(b + "name") or "",
                    "tags": self.get(b + "tags") or "",
                    "grp": self.get(b + "in/conn/grp") or "OFF",
                    "n": int(self.get(b + "in/conn/in") or 0),
                    "altsrc": bool(int(self.get(b + "in/set/altsrc") or 0)),
                })
        return out

    def dca_names(self):
        return {i: (self.get("/dca/%d/name" % i) or "") for i in range(1, 17)}

    # -- meters ----------------------------------------------------------
    def measure(self, keys, seconds, on_frame=None):
        """Stream meters for `seconds`; returns {key: [dB, ...]}.

        [RP "Channel 3: Metering"] request over the native TCP connection
        (port 2222) on channel 3; the console sends UDP packets to the port
        we declare, ~every 50 ms, for 5 s per request/renewal. That the
        REQUEST travels on the TCP connection is [libwing] (it writes the
        request to its TCP stream) -- the RP example does not name the
        transport. TCP connections also need traffic every <10 s; our
        renewals every 2 s cover that.
        """
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("", self.meter_port))
        port = udp.getsockname()[1]
        try:
            tcp = socket.create_connection((self.host, self.native_port), timeout=3)
        except OSError as e:
            udp.close()
            raise ConsoleError("Could not open the native connection to %s:%d (%s)."
                               % (self.host, self.native_port, e))
        reqs = {}   # report id -> ("src", grp) or ("ch", key)
        try:
            payload = meter_port_payload(port)
            if self.meter_mode == "source":
                for grp in sorted({k[0] for k in keys}):
                    rid = 0x57410000 + SOURCE_DEVICE[grp]
                    reqs[rid] = ("src", grp)
                    payload += meter_request_payload(rid, MTR_SOURCE, [SOURCE_DEVICE[grp]])
            else:
                for k in keys:
                    kind, num = self.channel_of.get(k, (None, None))
                    if kind is None:
                        raise ConsoleError("%s%d is not patched to a channel, "
                                           "so channel meters cannot read it" % k)
                    rid = 0x57420000 + (num if kind == "ch" else 100 + num)
                    reqs.setdefault(rid, ("ch", []))[1].append(k)
                    payload += meter_request_payload(
                        rid, MTR_CHANNEL if kind == "ch" else MTR_AUX, [num])
            tcp.sendall(nrp_frame(METER_CHID, payload))
            frames = {k: [] for k in keys}
            t_end = time.time() + seconds
            next_renew = time.time() + METER_RENEW_S
            while time.time() < t_end:
                now = time.time()
                if now >= next_renew:
                    renew = b"".join(meter_renew_payload(r) for r in reqs)
                    tcp.sendall(nrp_frame(METER_CHID, renew))
                    next_renew = now + METER_RENEW_S
                r, _, _ = select.select([udp, tcp], [], [], 0.2)
                if tcp in r:
                    if not tcp.recv(65536):
                        raise ConsoleError("The console closed the native connection.")
                if udp in r:
                    data, _ = udp.recvfrom(65536)
                    rid, vals = decode_meter_packet(data)
                    req = reqs.get(rid)
                    if not req:
                        continue
                    if req[0] == "src":
                        for k in keys:
                            if k[0] == req[1] and k[1] - 1 < len(vals):
                                frames[k].append(vals[k[1] - 1])
                    else:
                        # channel block: input L, input R, out L, out R, ... [RP Table 5]
                        for k in req[1]:
                            if len(vals) >= 2:
                                frames[k].append(max(vals[0], vals[1]))
                    if on_frame:
                        on_frame()
            return frames
        finally:
            with contextlib.suppress(Exception):
                tcp.close()
            udp.close()

    def probe(self, grp, seconds, out=print):
        """Print which source-meter index moves, to verify SOURCE_DEVICE."""
        if grp not in SOURCE_DEVICE:
            raise ConsoleError("unknown group %s" % grp)
        keys = [(grp, i) for i in range(1, GROUP_SIZE.get(grp, 48) + 1)]
        frames = self.measure(keys, seconds)
        got = sum(len(v) for v in frames.values())
        if not got:
            out("No meter data arrived. Either the request format is wrong, or the "
                "Mac firewall is blocking incoming UDP for Python.")
            return 1
        loud = [(max(v), k) for k, v in frames.items() if v and max(v) > SILENCE_DB]
        loud.sort(reverse=True)
        out("Meter data arrived for group %s: %d frames." % (grp, len(frames[keys[0]])))
        if not loud:
            out("Every input in %s stayed below %s dBFS." % (grp, say_num(SILENCE_DB)))
        for pk, k in loud[:8]:
            out("Index %d peaked at %s." % (k[1], say_num(pk)))
        return 0


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
SIM_LEVELS = {   # peak dBFS at 0 dB gain, by preset
    "vocal": -40.0, "speech": -44.0, "drums": -24.0, "perc": -30.0,
    "horns": -30.0, "line": -14.0, "instrument": -22.0, "room": -46.0,
    "default": -34.0,
}


class SimModel:
    """A pretend WING: sources with a gain and a synthetic signal.

    level[key] = peak dBFS the source would show at 0 dB gain (None = silent).
    A frame = level + gain - some envelope, plus self-noise that rises with
    gain, clamped at 0 dBFS (clipping).
    """

    def __init__(self, sources, levels=None, link_stereo=True, rng_seed=7,
                 follow=True):
        self.sources = sources          # {(grp,n): {"name","mode","g","vph"}}
        self.levels = levels or {}
        self.link_stereo = link_stereo  # [ASSUMED] gain on odd source moves both
        self.follow = follow            # False: gain changes but level doesn't (stage box not following)
        self.rng = random.Random(rng_seed)
        self.writes = []
        self.frame_no = {}
        self.audible_gain = {}          # what the "preamp" really is at (for follow=False)

    def range(self, grp):
        return DOC_GAIN_RANGE

    def gain(self, key):
        return float(self.sources[key]["g"])

    def set_gain(self, key, db):
        lo, hi, step = self.range(key[0])
        v = min(hi, max(lo, lo + round((db - lo) / step) * step))
        self.writes.append((key, v))
        self._set(key, v)
        if self.link_stereo and self.sources[key].get("mode") == "ST" and key[1] % 2 == 1:
            other = (key[0], key[1] + 1)
            if other in self.sources:
                self._set(other, v)
        return self.gain(key)

    def _set(self, key, v):
        if key not in self.audible_gain:
            self.audible_gain[key] = self.sources[key]["g"]
        self.sources[key]["g"] = v
        if self.follow:
            self.audible_gain[key] = v

    def level(self, key):
        lv = self.levels.get(key, "auto")
        if lv == "auto":
            src = self.sources.get(key, {})
            name = src.get("name", "")
            base = SIM_LEVELS[guess_preset(name)]
            jitter = (zlib.crc32(("%s%d" % key).encode()) % 9) - 4
            lv = base + jitter
            if src.get("mode") == "ST" and key[1] % 2 == 0:
                lv -= 2
        return lv

    def frame(self, key):
        g = self.audible_gain.get(key, self.sources[key]["g"])
        i = self.frame_no.get(key, 0)
        self.frame_no[key] = i + 1
        noise = -100.0 + g + self.rng.uniform(-2, 2)
        lv = self.level(key)
        if lv is None:
            v = noise
        elif (i // 6) % 4 == 3:            # a breath / gap between phrases
            v = max(noise, lv + g - 30 + self.rng.uniform(-3, 3))
        elif i % 10 == 0:
            v = lv + g                      # hits the true peak
        else:
            v = lv + g - abs(self.rng.gauss(0, 4))
        return min(0.0, max(-128.0, v))


class SimConsole:
    is_sim = True

    def __init__(self, model, channels, dcas):
        self.model = model
        self._channels = channels
        self._dcas = dcas
        self.channel_of = {}

    def close(self):
        pass

    def ping(self):
        return "WING,simulated,SIM,ngc-full,SIM,3.1.1"

    def gain(self, grp, n):
        return self.model.gain((grp, n))

    def set_gain(self, grp, n, db):
        if not GAIN_PATH_RE.match("/io/in/%s/%d/g" % (grp, n)):
            raise ConsoleError("refusing to write that")
        return self.model.set_gain((grp, n), db)

    def gain_range(self, grp, n):
        return self.model.range(grp), "simulator"

    def source_info(self, grp, n):
        s = self.model.sources.get((grp, n), {})
        return {"name": s.get("name", ""), "mode": s.get("mode", "M"),
                "vph": s.get("vph")}

    def channels(self):
        return self._channels

    def dca_names(self):
        return self._dcas

    def measure(self, keys, seconds, on_frame=None):
        frames = {k: [] for k in keys}
        for _ in range(int(round(seconds / METER_FRAME_S))):
            for k in keys:
                frames[k].append(self.model.frame(k))
        return frames


def parse_sim_signal(spec):
    """'A1=-30,B22=silent,A3=clip' -> {(grp,n): level or None}."""
    out = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        if "=" not in part:
            raise ValueError("expected INPUT=LEVEL in --sim-signal, got %r" % part)
        k, v = part.split("=", 1)
        key = parse_input(k)
        v = v.strip().lower()
        if v in ("silent", "none", "off"):
            out[key] = None
        elif v == "clip":
            out[key] = 6.0
        else:
            out[key] = float(v)
    return out


# ---------------------------------------------------------------------------
# Show files and name resolution
# ---------------------------------------------------------------------------
def find_show(name):
    if os.path.isfile(name):
        return name
    for cand in (name, name + ".snap"):
        p = os.path.join(SHOWS_DIR, cand)
        if os.path.isfile(p):
            return p
    if os.path.isdir(SHOWS_DIR):
        low = name.lower()
        hits = [f for f in os.listdir(SHOWS_DIR)
                if f.endswith(".snap") and low in f.lower()]
        if len(hits) == 1:
            return os.path.join(SHOWS_DIR, hits[0])
    raise SystemExit("Can't find a show called %r in %s." % (name, SHOWS_DIR))


def load_show(path):
    """Read-only. Returns (sources, channels, dcas) in the console's shapes."""
    with open(path) as f:
        d = json.load(f)
    ae = d["ae_data"]
    sources = {}
    for grp in GAIN_GROUPS:
        for n, s in ae["io"]["in"].get(grp, {}).items():
            sources[(grp, int(n))] = {"name": s.get("name", ""),
                                      "mode": s.get("mode", "M"),
                                      "g": float(s.get("g", 0) or 0),
                                      "vph": bool(s.get("vph"))}
    chans = []
    for kind in ("ch", "aux"):
        for num, c in ae.get(kind, {}).items():
            conn = c.get("in", {}).get("conn", {})
            chans.append({"kind": kind, "num": int(num), "name": c.get("name", ""),
                          "tags": c.get("tags", "") or "",
                          "grp": conn.get("grp", "OFF"), "n": int(conn.get("in", 0) or 0),
                          "altsrc": bool(c.get("in", {}).get("set", {}).get("altsrc"))})
    chans.sort(key=lambda c: (c["kind"] != "ch", c["num"]))
    dcas = {int(k): v.get("name", "") for k, v in ae.get("dca", {}).items()}
    return sources, chans, dcas


INPUT_RE = re.compile(r"^\s*(LCL|LOCAL|L|A|B|C|AUX|USB|SC|CRD|AES|MOD|PLAY|USR|OSC)\s*[-:]?\s*(\d{1,2})\s*$", re.I)


def parse_input(text):
    m = INPUT_RE.match(text or "")
    if not m:
        raise ValueError("%r is not an input like A1, B25 or LCL3" % text)
    grp = m.group(1).upper()
    if grp in ("L", "LOCAL"):
        grp = "LCL"
    return (grp, int(m.group(2)))


def split_list(values):
    out = []
    for v in values or []:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def channel_label(c):
    return ("channel %d" % c["num"]) if c["kind"] == "ch" else ("aux %d" % c["num"])


def pick_by_name(items, query, label_fn, what):
    """Exact (case-insensitive), then unique prefix, then unique substring."""
    q = query.strip().lower()
    for test in (lambda s: s == q, lambda s: s.startswith(q), lambda s: q in s):
        hits = [it for it in items if label_fn(it) and test(label_fn(it).lower())]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(label_fn(h) for h in hits[:8])
            raise SystemExit("%r matches more than one %s: %s. Be more specific."
                             % (query, what, names))
    raise SystemExit("No %s called %r." % (what, query))


class Resolver:
    def __init__(self, console, show=None):
        self.console = console
        self.show = show            # (sources, channels, dcas) or None
        self._chans = None
        self._dcas = None

    def chans(self):
        if self._chans is None:
            self._chans = self.show[1] if self.show else self.console.channels()
        return self._chans

    def dcas(self):
        if self._dcas is None:
            self._dcas = self.show[2] if self.show else self.console.dca_names()
        return self._dcas

    def source_info(self, key):
        if self.show and key in self.show[0]:
            return dict(self.show[0][key])
        return self.console.source_info(*key)

    def target_for_source(self, key, name=None, where="", warnings=None):
        grp, n = key
        if grp not in GAIN_GROUPS:
            raise SkipTarget("%s %s%d has no preamp gain to set (only local and "
                             "AES50 A, B, C inputs do)." % (name or "That input", grp, n))
        limit = GROUP_SIZE[grp]
        if not 1 <= n <= limit:
            raise SkipTarget("%s%d doesn't exist; %s goes 1 to %d." % (grp, n, grp, limit))
        info = self.source_info(key)
        stereo = info.get("mode") in ("ST", "M/S")
        if stereo and n % 2 == 0:
            # the pair is led by its odd input [UM/RP: stereo sources start on odd]
            n -= 1
            info = self.source_info((grp, n))
        t = Target(grp, n, name or info.get("name") or None, stereo, where,
                   warnings=warnings)
        if info.get("mode") == "M/S":
            t.warnings.append("it's a mid-side pair, judged by the louder side")
        return t

    def target_for_channel(self, c):
        name = c["name"] or None
        warnings = []
        if c.get("altsrc"):
            warnings.append("its channel is listening to the ALT source right "
                            "now, but the meter reads the live input anyway")
        if c["grp"] in ("OFF", None) or not c["n"]:
            raise SkipTarget("%s (%s) has no input patched." % (name or channel_label(c), channel_label(c)))
        t = self.target_for_source((c["grp"], c["n"]), name=name,
                                   where=channel_label(c), warnings=warnings)
        self.console.channel_of[t.key] = (c["kind"], c["num"])
        return t

    def resolve(self, inputs=(), channels=(), dcas=(), everything=False):
        targets, skipped = [], []

        def add(fn, *a):
            try:
                targets.append(fn(*a))
            except SkipTarget as e:
                skipped.append(str(e))

        for text in inputs:
            try:
                key = parse_input(text)
            except ValueError as e:
                raise SystemExit(str(e))
            add(self.target_for_source, key)
        for text in channels:
            chans = self.chans()
            m = re.match(r"^\s*(ch|channel|aux)?\s*(\d{1,2})\s*$", text, re.I)
            if m:
                kind = "aux" if (m.group(1) or "").lower() == "aux" else "ch"
                hit = [c for c in chans if c["kind"] == kind and c["num"] == int(m.group(2))]
                if not hit:
                    raise SystemExit("No %s %s." % (kind, m.group(2)))
                c = hit[0]
            else:
                c = pick_by_name(chans, text, lambda c: c["name"], "channel")
            add(self.target_for_channel, c)
        for text in dcas:
            names = self.dcas()
            if text.strip().isdigit():
                num = int(text)
            else:
                num = pick_by_name(sorted(names.items()), text, lambda kv: kv[1], "DCA")[0]
            tag = "#D%d" % num
            members = [c for c in self.chans()
                       if tag in [t.strip() for t in (c["tags"] or "").split(",")]]
            if not members:
                skipped.append("DCA %d (%s) has no channels." % (num, names.get(num, "")))
            for c in members:
                add(self.target_for_channel, c)
        if everything:
            for c in self.chans():
                if c["grp"] in GAIN_GROUPS and c["n"]:
                    add(self.target_for_channel, c)
        # de-duplicate by source (two channels can share one source)
        seen, uniq = set(), []
        for t in targets:
            if t.key not in seen:
                seen.add(t.key)
                uniq.append(t)
        return uniq, skipped


class SkipTarget(Exception):
    pass


# ---------------------------------------------------------------------------
# Log + undo (JSON lines in ~/.wing-autogain.log)
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, path):
        self.path = path

    def write(self, rec):
        rec = dict(rec)
        rec.setdefault("time", _dt.datetime.now().isoformat(timespec="seconds"))
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            print("Warning: couldn't write the log %s: %s" % (self.path, e), file=sys.stderr)

    def records(self):
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    with contextlib.suppress(ValueError):
                        out.append(json.loads(line))
        except FileNotFoundError:
            pass
        return out

    def last_undoable_run(self, host):
        recs = self.records()
        undone = {r.get("undo_of") for r in recs if r.get("kind") == "undo-run"}
        changes = {}
        order = []
        for r in recs:
            if r.get("kind") == "change" and r.get("host") == host and not r.get("undo"):
                if r["run"] not in changes:
                    order.append(r["run"])
                changes.setdefault(r["run"], []).append(r)
        for run in reversed(order):
            if run not in undone:
                return run, changes[run]
        return None, []


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class Result:
    def __init__(self, target):
        self.t = target
        self.first = None        # Measurement
        self.last = None
        self.start_gain = None
        self.gain = None
        self.decisions = []
        self.rng = None
        self.warnings = list(target.warnings)
        self.final_measured = False
        self.done = False
        self.error = None
        self.not_following = False

    @property
    def outcome(self):
        if self.error:
            return "error"
        d0 = self.decisions[0] if self.decisions else None
        if d0 is None:
            return "error"
        if d0.action in ("silent", "weak", "nodata"):
            return d0.action
        moved = round(self.gain - self.start_gain, 3)
        if moved > 0:
            return "raised"
        if moved < 0:
            return "lowered"
        return "held"

    @property
    def limit(self):
        for d in reversed(self.decisions):
            if d.limit:
                return d.limit
        return None


def target_peak_for(t, override):
    if override is not None:
        return min(float(override), HARD_TARGET_CEILING)
    return PRESETS.get(t.preset, PRESETS["default"])[0]


def measure_targets(console, targets, seconds):
    keys = []
    for t in targets:
        keys.extend(t.keys())
    frames = console.measure(keys, seconds)
    out = {}
    for t in targets:
        ms = [analyze(frames.get(k, [])) for k in t.keys()]
        out[t.key] = merge_stereo(ms) if t.stereo else ms[0]
    return out


def apply_gain(console, res, new):
    t = res.t
    got = console.set_gain(t.grp, t.n, new)
    if t.stereo:
        other = console.gain(t.grp, t.n + 1)
        if abs(other - got) > 0.01:
            # [ASSUMED unknown] whether the pair's gain is linked; if the
            # partner didn't follow, set it too.
            console.set_gain(t.grp, t.n + 1, new)
    res.gain = got
    if abs(got - new) > 0.26:
        res.warnings.append("the console took %s instead of %s" % (say_num(got, True), say_num(new, True)))
    return got


def run_autogain(console, targets, opts, log, out, speaker=None, voice=None,
                 host="sim"):
    """opts: listen, target (override or None), max_step, max_gain, tolerance,
    dry_run, confirm, max_passes, each."""
    voice = voice or Voice(opts.get("plain", False))
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + "%04x" % random.getrandbits(16)
    results = [Result(t) for t in targets]
    log.write({"kind": "run", "run": run_id, "host": host,
               "dry_run": opts.get("dry_run", False),
               "targets": ["%s %s%d" % (t.name, t.grp, t.n) for t in targets]})

    for r in results:
        r.rng, _src = console.gain_range(r.t.grp, r.t.n)
        r.start_gain = r.gain = console.gain(r.t.grp, r.t.n)

    groups = [[r] for r in results] if opts.get("each") else [results]
    for group in groups:
        if opts.get("each"):
            line = voice.join("Next: %s. Listening for %s %s." % (
                group[0].t.name, say_num(opts["listen"]), "second" if float(opts["listen"]) == 1 else "seconds"), voice.flavour("listen", group[0].t.name))
            out(line)
            if speaker:
                speaker.say(line)
            if opts.get("wait_enter") and sys.stdin.isatty():
                with contextlib.suppress(EOFError):
                    input("Press Return when they're playing. ")
        else:
            n = len(group)
            line = voice.join("Listening to %s for %s %s." % (
                ("1 input" if n == 1 else "%d inputs" % n), say_num(opts["listen"]),
                "second" if float(opts["listen"]) == 1 else "seconds"),
                voice.flavour("listen", str(n)))
            out(line)
            if speaker:
                speaker.say(line)
        _run_group(console, group, opts, log, run_id, host, out, speaker, voice)
    return run_id, results


def _run_group(console, group, opts, log, run_id, host, out, speaker, voice):
    pending = list(group)
    max_passes = max(1, int(opts.get("max_passes", 3)))
    confirm = opts.get("confirm", True) and not opts.get("dry_run")
    for p in range(max_passes + 1):
        if not pending:
            break
        meas = measure_targets(console, [r.t for r in pending], opts["listen"])
        nxt = []
        for r in pending:
            m = meas[r.t.key]
            if r.first is None:
                r.first = m
            elif r.decisions and r.decisions[-1].change and m.enough and not m.clipped:
                d = r.decisions[-1]
                if d.expected_peak is not None and r.last is not None:
                    level_move = m.peak - r.last.peak
                    if abs(d.change) >= 3 and abs(level_move - d.change) > 4.0:
                        r.not_following = True
            r.last = m
            r.final_measured = True
            if p == max_passes:      # measure-only confirmation pass
                continue
            d = decide(m, r.gain, r.rng, target_peak_for(r.t, opts.get("target")),
                       max_step=opts.get("max_step", 12.0),
                       tolerance=opts.get("tolerance", TOLERANCE_DB),
                       max_gain=opts.get("max_gain"))
            if p > 0 and d.action in ("silent", "weak", "nodata"):
                # it played on the first pass; stopped now. Keep what we did.
                r.warnings.append("it went quiet on the check pass, so I couldn't confirm it")
                r.final_measured = False
                continue
            r.decisions.append(d)
            if d.change and not opts.get("dry_run"):
                old = r.gain
                try:
                    got = apply_gain(console, r, d.new)
                except ConsoleError as e:
                    r.error = str(e)
                    continue
                r.final_measured = False
                log.write({"kind": "change", "run": run_id, "host": host,
                           "name": r.t.name, "grp": r.t.grp, "n": r.t.n,
                           "stereo": r.t.stereo, "old": old, "new": got,
                           "peak": m.peak, "avg": m.avg, "clipped": m.clipped,
                           "target": target_peak_for(r.t, opts.get("target"))})
                if d.capped or m.clipped or confirm:
                    nxt.append(r)
            elif opts.get("dry_run"):
                r.gain_would = d.new
        pending = nxt
        if not confirm:
            # only capped/clipped ones need another look, never a pure confirm
            pending = [r for r in pending if r.decisions[-1].capped or (r.last and r.last.clipped)]
            if p == max_passes - 1:
                break
    for r in group:
        r.done = True
        line = describe(r, opts, voice)
        out(line)
        if speaker:
            speaker.say(line)
        log.write({"kind": "result", "run": run_id, "host": host,
                   "name": r.t.name, "grp": r.t.grp, "n": r.t.n,
                   "outcome": r.outcome, "start": r.start_gain, "gain": r.gain,
                   "peak": r.first.peak if r.first else None,
                   "final_peak": r.last.peak if r.last else None,
                   "text": line})


def describe(r, opts, voice):
    """One sentence group per input: numbers and outcome first, flavour last."""
    t = r.t
    name = t.name
    dry = opts.get("dry_run")
    tgt = target_peak_for(t, opts.get("target"))
    first = r.first
    oc = r.outcome
    seed = name
    if r.error:
        return "%s: couldn't set the gain. %s Gain left at %s." % (
            name, r.error, say_num(r.start_gain, True))
    d0 = r.decisions[0] if r.decisions else None
    if oc == "nodata":
        return ("%s: WARNING, no meter data arrived from the console, so I left "
                "the gain alone at %s." % (name, say_num(r.start_gain, True)))
    if oc == "silent":
        return voice.join("%s: WARNING, no signal heard. Gain left at %s." % (
            name, say_num(r.start_gain, True)), voice.flavour("silent", seed))
    if oc == "weak":
        return voice.join(
            "%s: WARNING, only a blip of signal, peaking at %s. Not enough to judge, "
            "gain left at %s. Try again with them playing the whole time." % (
                name, say_num(first.peak), say_num(r.start_gain, True)))

    parts = []
    clipped = first.clipped
    if clipped:
        parts.append("%s: WARNING, CLIPPING, peaks hit %s." % (name, say_num(first.peak)))
    else:
        parts.append("%s: peaks were %s." % (name, say_num(first.peak)))

    if dry:
        d = d0
        if d.change > 0:
            parts.append("Would raise gain %s, from %s to %s." % (
                say_db(d.change), say_num(d.old, True), say_num(d.new, True)))
        elif d.change < 0:
            parts.append("Would lower gain %s, from %s to %s." % (
                say_db(-d.change), say_num(d.old, True), say_num(d.new, True)))
        else:
            if d.limit:
                parts.append("Would leave gain at %s." % say_num(d.old, True))
            else:
                parts.append("Would leave gain at %s, that's on target." % say_num(d.old, True))
        if d.expected_peak is not None and d.change:
            parts.append("That should peak around %s." % say_num(d.expected_peak))
        if d.capped:
            parts.append("That's capped at %s for one pass; a real run would check and go again."
                         % say_db(opts.get("max_step", 12.0)))
    else:
        moved = round(r.gain - r.start_gain, 3)
        if moved > 0:
            parts.append("Raised gain %s to %s." % (say_db(moved), say_num(r.gain, True)))
        elif moved < 0:
            parts.append("Lowered gain %s to %s." % (say_db(-moved), say_num(r.gain, True)))
        else:
            parts.append("Gain stays at %s." % say_num(r.gain, True))
        if moved and r.final_measured and r.last and r.last.enough:
            parts.append("Now peaking around %s." % say_num(r.last.peak))
            if r.last.clipped:
                parts.append("WARNING, still clipping at minimum gain, %s. Turn the source "
                             "down or use a pad." % say_num(r.rng[0], True))
        elif moved:
            dl = r.decisions[-1]
            if dl.expected_peak is not None:
                parts.append("Should now peak around %s." % say_num(dl.expected_peak))

    limit = r.limit
    final_peak = (r.last.peak if (r.last and r.last.enough) else first.peak)
    if not dry:
        off = final_peak is not None and abs(final_peak - tgt) > opts.get("tolerance", TOLERANCE_DB)
    else:
        off = True
    if limit in ("max", "user-max") and off and (not r.last or (r.last.peak or -99) < tgt):
        which = "the maximum you allowed" if limit == "user-max" else "maximum gain"
        parts.append("WARNING, at %s, %s, and still short of %s." % (
            which, say_num(r.rng[1] if limit == "max" else opts.get("max_gain"), True), say_num(tgt)))
        parts.append(voice.flavour("atmax", seed))
    elif limit == "min" and off and not (r.last and r.last.clipped and not dry):
        parts.append("WARNING, at minimum gain, %s, and still hotter than %s." % (
            say_num(r.rng[0], True), say_num(tgt)))
        parts.append(voice.flavour("atmin", seed))
    if r.not_following:
        parts.append("WARNING, the level didn't move with the gain. Either they changed "
                     "how loud they played, or the stage box isn't taking remote gain.")
    for w in r.warnings:
        parts.append("Note: %s." % w)
    if not limit and not r.not_following:
        if clipped:
            parts.append(voice.flavour("clip", seed))
        elif oc == "raised":
            parts.append(voice.flavour("raised", seed))
        elif oc == "lowered":
            parts.append(voice.flavour("lowered", seed))
        elif oc == "held" and not dry:
            parts.append(voice.flavour("ontarget", seed))
    return voice.join(*parts)


def summary(results, opts, voice):
    c = {}
    for r in results:
        c[r.outcome] = c.get(r.outcome, 0) + 1
    warn = sum(1 for r in results if r.outcome in ("silent", "weak", "nodata", "error")
               or r.limit or r.not_following or (r.last and r.last.clipped))
    bits = []
    verbs = [("raised", "raised"), ("lowered", "lowered"), ("held", "already right"),
             ("silent", "silent"), ("weak", "too quiet to judge"),
             ("nodata", "no meter data"), ("error", "failed")]
    if opts.get("dry_run"):
        would = sum(1 for r in results if r.decisions and r.decisions[0].change)
        line = "Dry run: %d of %d would change." % (would, len(results))
        return voice.join(line, voice.flavour("dry", str(len(results))))
    for key, word in verbs:
        if c.get(key):
            bits.append("%d %s" % (c[key], word))
    line = "Done: " + ", ".join(bits) + "." if bits else "Done."
    if any(r.outcome in ("raised", "lowered") for r in results):
        line += " wing-autogain --undo puts them back."
    return voice.join(line, voice.flavour("summary_warn" if warn else "summary_good",
                                          str(len(results))))


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------
def do_undo(console, log, host, out, force=False, voice=None):
    run, changes = log.last_undoable_run(host)
    if not run:
        out("Nothing to undo for %s." % ("the simulator" if host == "sim" else host))
        return 0
    # earliest old value per source = what it was before the run
    first_old, last_new, names = {}, {}, {}
    for c in changes:
        k = (c["grp"], c["n"])
        first_old.setdefault(k, (c["old"], c.get("stereo")))
        last_new[k] = c["new"]
        names[k] = c["name"]
    undo_id = "undo-" + run
    restored = skipped = 0
    for k, (old, stereo) in first_old.items():
        now = console.gain(*k)
        if abs(now - last_new[k]) > 0.26 and not force:
            out("%s: gain is %s now, not the %s I set, so someone changed it since. "
                "Left alone; --force to restore anyway." % (
                    names[k], say_num(now, True), say_num(last_new[k], True)))
            skipped += 1
            continue
        got = console.set_gain(k[0], k[1], old)
        if stereo:
            if abs(console.gain(k[0], k[1] + 1) - got) > 0.01:
                console.set_gain(k[0], k[1] + 1, old)
        log.write({"kind": "change", "run": undo_id, "host": host, "name": names[k],
                   "grp": k[0], "n": k[1], "stereo": stereo, "old": now, "new": got,
                   "undo": True})
        out("%s: gain back to %s." % (names[k], say_num(got, True)))
        restored += 1
    log.write({"kind": "undo-run", "run": undo_id, "undo_of": run, "host": host})
    out("Undo done: %d restored%s." % (restored, (", %d left alone" % skipped) if skipped else ""))
    return 0 if not skipped else 1


# ---------------------------------------------------------------------------
# Discovery [RP "Remote communications with WING"]: UDP 'WING?' to port 2222
# ---------------------------------------------------------------------------
def discover(timeout=1.5, out=print):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.sendto(b"WING?", ("255.255.255.255", NATIVE_PORT))
    found = []
    end = time.time() + timeout
    while time.time() < end:
        s.settimeout(max(0.05, end - time.time()))
        try:
            data, addr = s.recvfrom(1024)
        except socket.timeout:
            break
        txt = data.decode("ascii", "replace")
        if txt.startswith("WING,"):
            parts = txt.split(",")
            found.append((addr[0], parts))
    s.close()
    if not found:
        out("No WING answered on this network.")
        return 1
    for ip, p in found:
        out("Found %s at %s, model %s, firmware %s." % (
            p[2] if len(p) > 2 else "a WING", ip, p[3] if len(p) > 3 else "?",
            p[5] if len(p) > 5 else "?"))
    return 0


# ---------------------------------------------------------------------------
# Simulator state persistence (so --undo and --status mean something)
# ---------------------------------------------------------------------------
def sim_state_path():
    return os.environ.get("WING_AUTOGAIN_SIM_STATE",
                          os.path.join(STATE_DIR, "sim-state.json"))


def build_sim(show_path, signal_spec=None, link_stereo=True, persist=True,
              reset=False, follow=True):
    sources, chans, dcas = load_show(show_path)
    if persist and not reset:
        try:
            with open(sim_state_path()) as f:
                saved = json.load(f)
            if saved.get("show") == os.path.basename(show_path):
                for k, g in saved.get("gains", {}).items():
                    grp, n = k.split(":")
                    if (grp, int(n)) in sources:
                        sources[(grp, int(n))]["g"] = g
        except (OSError, ValueError):
            pass
    model = SimModel(sources, parse_sim_signal(signal_spec), link_stereo=link_stereo,
                     follow=follow)
    return SimConsole(model, chans, dcas), (sources, chans, dcas)


def save_sim(console, show_path):
    os.makedirs(os.path.dirname(sim_state_path()), exist_ok=True)
    gains = {"%s:%d" % k: v["g"] for k, v in console.model.sources.items() if v["g"] != 0}
    with open(sim_state_path(), "w") as f:
        json.dump({"show": os.path.basename(show_path), "gains": gains}, f)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_parser():
    p = argparse.ArgumentParser(
        prog="wing-autogain",
        description="Gus, an auto gain for the Behringer WING. Listens to inputs "
                    "while someone plays and sets preamp gain to a sensible peak "
                    "level. Only ever changes preamp gain; never phantom power.")
    w = p.add_argument_group("what to gain")
    w.add_argument("--input", "-i", action="append", metavar="A1",
                   help="an input by its source, like A1, B25 or LCL3. Repeat or comma-separate.")
    w.add_argument("--channel", "-c", action="append", metavar="NAME",
                   help='a channel by name or number, like "Matt Vox", 32, or "aux 1"')
    w.add_argument("--dca", "-d", action="append", metavar="NAME",
                   help="every channel in a DCA, by name or number, like VOCALS or 8")
    w.add_argument("--all", action="store_true",
                   help="every channel patched to a local or AES50 input")
    c = p.add_argument_group("console")
    c.add_argument("--host", help="the WING's IP address (or set it once with --set-host)")
    c.add_argument("--set-host", metavar="IP", help="remember the WING's address")
    c.add_argument("--show", metavar="NAME",
                   help='read names from a show file, like "The Woodshed", instead of the console')
    c.add_argument("--simulate", action="store_true",
                   help="use the built-in pretend WING instead of a real one")
    c.add_argument("--sim-signal", metavar="SPEC",
                   help='simulator only: input levels at 0 dB gain, like "A1=-30,B22=silent,A3=clip"')
    c.add_argument("--sim-reset", action="store_true", help="simulator only: start from the show's gains")
    c.add_argument("--meter", choices=("source", "channel"), default="source",
                   help="which meters to read: the source itself (default) or its channel's input meter")
    h = p.add_argument_group("how")
    h.add_argument("--listen", "-l", type=float, default=8.0, metavar="SECONDS",
                   help="how long to listen each pass (default 8)")
    h.add_argument("--target", "-t", metavar="PRESET_OR_DB",
                   help="a preset (%s) or a peak level like -14. Default: picked from each name."
                   % ", ".join(sorted(PRESETS)))
    h.add_argument("--max-step", type=float, default=12.0, metavar="DB",
                   help="the most to move gain in one pass (default 12, never above 18)")
    h.add_argument("--max-gain", type=float, metavar="DB", help="never set gain above this")
    h.add_argument("--each", action="store_true",
                   help="one input at a time instead of everyone together (less bleed)")
    h.add_argument("--wait", action="store_true",
                   help="with --each, wait for Return before each input")
    h.add_argument("--no-confirm", action="store_true",
                   help="skip the check pass after a change (faster, but unverified)")
    h.add_argument("--dry-run", "-n", action="store_true",
                   help="measure and say what would change; change nothing")
    o = p.add_argument_group("other jobs")
    o.add_argument("--status", action="store_true",
                   help="read back current gains and phantom state, and the last run")
    o.add_argument("--undo", action="store_true", help="put back the gains from the last run")
    o.add_argument("--force", action="store_true",
                   help="with --undo, restore even inputs someone changed since")
    o.add_argument("--discover", action="store_true", help="look for a WING on the network")
    o.add_argument("--probe", metavar="GROUP",
                   help="real console: show which source-meter index moves in A, B, C or LCL")
    o.add_argument("--presets", action="store_true", help="list the target presets")
    out = p.add_argument_group("output")
    out.add_argument("--plain", action="store_true", help="facts only, no jokes")
    out.add_argument("--quiet", "-q", action="store_true",
                     help="don't speak results (speaking is on when run in a terminal on the Mac)")
    out.add_argument("--say", action="store_true",
                     help="speak results even when not run in a terminal")
    out.add_argument("--json", action="store_true", help="print results as JSON")
    out.add_argument("--log", default=os.environ.get("WING_AUTOGAIN_LOG", DEFAULT_LOG),
                     help="log file (default ~/.wing-autogain.log)")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p


@contextlib.contextmanager
def run_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(os.path.join(STATE_DIR, "lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Another wing-autogain is already running. One at a time.")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def main(argv=None, out=None):
    args = build_parser().parse_args(argv)
    out = out or (lambda s: print(s, flush=True))
    voice = Voice(args.plain)
    cfg = load_config()

    if args.presets:
        for k in sorted(PRESETS):
            d = PRESETS[k][1]
            out("%s: peaks around %s dBFS. %s." % (k, say_num(PRESETS[k][0]), d[0].upper() + d[1:]))
        out("Within %s dB of the target counts as right, so nothing moves." % say_num(TOLERANCE_DB))
        return 0
    if args.set_host:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        cfg["host"] = args.set_host
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        out("Remembered the WING at %s." % args.set_host)
        return 0
    if args.discover:
        return discover(out=out)

    target_override = None
    if args.target:
        if args.target.lower() in PRESETS:
            target_override = None
        else:
            try:
                target_override = float(args.target)
            except ValueError:
                raise SystemExit("--target wants a preset (%s) or a number like -14."
                                 % ", ".join(sorted(PRESETS)))
            if target_override > HARD_TARGET_CEILING:
                out("Note: %s is hotter than I'll go; using %s." % (
                    say_num(target_override), say_num(HARD_TARGET_CEILING)))
                target_override = HARD_TARGET_CEILING

    show = None
    show_path = None
    if args.simulate:
        show_path = find_show(args.show or DEFAULT_SIM_SHOW)
        console, show = build_sim(show_path, args.sim_signal, reset=args.sim_reset)
        host = "sim"
    else:
        host = args.host or os.environ.get("WING_HOST") or cfg.get("host")
        if not host:
            raise SystemExit("Which WING? Give --host 192.168.x.x, or --set-host once, "
                             "or --discover to find it. --simulate tries it on a pretend one.")
        console = WingConsole(host, meter_mode=args.meter)
        if args.show:
            show_path = find_show(args.show)
            show = load_show(show_path)
    log = Log(args.log)

    try:
        if args.probe:
            if console.is_sim:
                raise SystemExit("--probe is for the real console.")
            return console.probe(args.probe.upper(), args.listen, out=out)
        if args.undo:
            with run_lock():
                rc = do_undo(console, log, host, out, force=args.force, voice=voice)
            if console.is_sim:
                save_sim(console, show_path)
            return rc

        resolver = Resolver(console, show)
        targets, skipped = resolver.resolve(split_list(args.input), split_list(args.channel),
                                            split_list(args.dca), args.all)
        if args.target and args.target.lower() in PRESETS:
            for t in targets:
                t.preset = args.target.lower()
        if show and not console.is_sim:
            # a show was named for a real console: make sure it's the one loaded
            for t in targets:
                with contextlib.suppress(ConsoleError):
                    live = console.source_info(t.grp, t.n).get("name", "")
                    want = show[0].get(t.key, {}).get("name", "")
                    if want and live and live != want:
                        t.warnings.append("the show calls %s%d %s, the console calls it %s; "
                                          "is the right show loaded" % (t.grp, t.n, want, live))
        for s in skipped:
            out("Skipped: " + s)

        if args.status:
            return do_status(console, targets, log, host, out)
        if not targets:
            out("Nothing to do. Name an --input, --channel, --dca, or --all.")
            return 2

        speak_on = (args.say or (sys.stdout.isatty() and not args.json)) and not args.quiet
        speaker = Speaker(speak_on)
        opts = {"listen": args.listen, "target": target_override,
                "max_step": args.max_step, "max_gain": args.max_gain,
                "tolerance": TOLERANCE_DB, "dry_run": args.dry_run,
                "confirm": not args.no_confirm, "max_passes": 3,
                "each": args.each, "wait_enter": args.wait, "plain": args.plain}
        lines = []
        printer = (lambda s: lines.append(s)) if args.json else out
        try:
            with run_lock():
                run_id, results = run_autogain(console, targets, opts, log, printer,
                                               speaker, voice, host)
        except KeyboardInterrupt:
            out("Stopped. Any gain I already changed is in the log; "
                "wing-autogain --undo puts it back.")
            speaker.finish(timeout=5)
            return 130
        finally:
            if console.is_sim:
                save_sim(console, show_path)
        line = summary(results, opts, voice)
        if args.json:
            print(json.dumps({"run": run_id, "dry_run": args.dry_run, "lines": lines + [line],
                              "inputs": [{"name": r.t.name, "source": "%s%d" % r.t.key,
                                          "stereo": r.t.stereo, "outcome": r.outcome,
                                          "gain_before": r.start_gain, "gain_after": r.gain,
                                          "peak_before": r.first.peak if r.first else None,
                                          "peak_after": r.last.peak if r.last else None,
                                          "limit": r.limit} for r in results]}, indent=1))
        else:
            out(line)
        speaker.say(line)
        speaker.finish()
        bad = any(r.outcome in ("silent", "weak", "nodata", "error") or r.limit
                  or r.not_following for r in results)
        return 1 if bad else 0
    except ConsoleError as e:
        out("Problem: %s" % e)
        return 2
    finally:
        console.close()


def do_status(console, targets, log, host, out):
    for t in targets:
        g = console.gain(t.grp, t.n)
        info = console.source_info(t.grp, t.n)
        ph = info.get("vph")
        phs = "phantom on" if ph else ("phantom off" if ph is not None else "phantom unknown")
        out("%s, %s%s: gain %s, %s, %s, target %s." % (
            t.name, t.src_label(), (", " + t.where) if t.where else "",
            say_num(g, True), phs, "stereo" if t.stereo else "mono",
            say_num(PRESETS[t.preset][0])))
    recs = [r for r in log.records() if r.get("host") == host and r.get("kind") == "result"]
    if recs:
        last = recs[-1]["run"]
        n = sum(1 for r in recs if r["run"] == last)
        undone = any(r.get("undo_of") == last for r in log.records())
        out("Last run %s, %d input%s%s:" % (recs[-1]["time"], n, "" if n == 1 else "s",
                                            ", since undone" if undone else ""))
        for r in recs:
            if r["run"] == last:
                out("  " + r["text"])
    elif not targets:
        out("No runs logged for %s yet." % host)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
