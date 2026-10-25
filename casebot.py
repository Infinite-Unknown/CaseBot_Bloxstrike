"""casebot: buys cheap crates on the Roblox skin-market while you work on the other screen.

Screen access and the click burst are lifted from SPEC.md, where they were proven live.
Nothing here caches a screen position across a click: every decision re-reads the window.
"""

import argparse
import contextlib
import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict, replace
from typing import Dict, List, Optional, Tuple

# Windows runs the same bot through winplat.py, swapped in at the bottom of this file.
WINDOWS = sys.platform == "win32"
if not WINDOWS:
    import resource
    import objc
    import Quartz
    import Vision
    from Foundation import NSURL
    from AppKit import (
        NSRunningApplication,
        NSEvent,
        NSApplicationActivateIgnoringOtherApps,
    )

# Drains what the macOS frameworks autorelease (see ocr). Windows has nothing to drain.
_pool = contextlib.nullcontext if WINDOWS else objc.autorelease_pool

HERE = os.path.dirname(os.path.abspath(__file__))
# Where settings lived before configs\: moved in as configs\default.json on first start.
SETTINGS_PATH = os.path.join(HERE, "settings.json")
# One settings file per config, by name, and which of them is in use.
CONFIGS_DIR = os.path.join(HERE, "configs")
ACTIVE_CONFIG_PATH = os.path.join(CONFIGS_DIR, "active.txt")
DEFAULT_CONFIG = "default"
# infinite's own settings, shipped: a fresh download starts from these (dry run on).
STARTER_PATH = os.path.join(HERE, "starter_config.json")
PURCHASES_PATH = os.path.join(HERE, "purchases.jsonl")
FAILURES_DIR = os.path.join(HERE, "failures")
LOG_PATH = os.path.join(HERE, "casebot.log")
# The last inventory scan, kept current as the bot buys and lists (see scan_inventory).
INVENTORY_PATH = os.path.join(HERE, "inventory.json")


# --------------------------------------------------------------------------- settings


# The top of what a price box holds: eight digits, like the market filter's To box.
MAX_PRICE = 99_999_999
# The game's cut of every sale, measured off the coin counter (see payout). The loss
# floors never assume less, whatever the Sale tax box says (floor_tax).
GAME_TAX = 0.05
# What each crate costs to buy and what it is listed at, by exact name. This is the
# table infinite's tier settings of 2026-09-27 meant (normal_max 41, expensive_max 59,
# sell 44 / 64, Glove Case 46, Charm Pack never bought), price for price: see
# crates_from_tiers, which a test holds to it.
DEFAULT_CRATES: Dict[str, Dict[str, Optional[int]]] = {
    "Viridian Case": {"buy": 58, "sell": 64},
    "Chrysalis": {"buy": 40, "sell": 44},
    "Nebula Case": {"buy": 58, "sell": 64},
    "Hero of Hell Console": {"buy": 40, "sell": 44},
    "Glove Case 2": {"buy": 40, "sell": 44},
    "Apex Case": {"buy": 40, "sell": 44},
    "Finishline Case": {"buy": 40, "sell": 44},
    "Lionheart": {"buy": 40, "sell": 44},
    # the community favours Glove Case (not Glove Case 2): it may be the next crate
    # taken out of the store, which would push its price up
    "Glove Case": {"buy": 40, "sell": 46},
    # Charm packs are titled "Case | Charm Pack #1" so they parse as crates, but they
    # are worth far less (RAP ~27): relisted if we hold one, never bought
    "Charm Pack #1": {"buy": None, "sell": 44},
}
# The tier fields the crate table replaced. They stay on Settings only so an old
# settings.json still loads and migrates (crates_from_tiers); nothing buys or sells
# by them, and save() leaves them out so a hand edit of one cannot look like it did
# something.
TIER_FIELDS = ("normal_max", "expensive_max", "expensive_names", "skip_names",
               "sell_normal", "sell_expensive", "sell_overrides", "sellable_names")


def _as_price(value, zero_is_none: bool = False) -> Optional[int]:
    """A price as typed or saved: an int from 1 to MAX_PRICE, or None for none.

    ValueError for anything else. int(True) is 1 and int(46.9) is 46, and neither is
    a price anyone typed, so both are refused rather than rounded. zero_is_none is
    for a buy price, where 0 is how a box says never buy.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(value)
    try:
        p = int(str(value).strip().replace(",", "")) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ValueError(value)
    if p == 0 and zero_is_none:
        return None
    if not 1 <= p <= MAX_PRICE:
        raise ValueError(value)
    return p


def payout(sell: int, tax: float) -> int:
    """The coins one sale at sell puts in the wallet: the tax off, and the rest floored.

    The game floors it. In infinite's casebot.log a crate listed at 44 (41.8 after 5%)
    raised the counter by 41 93 times and by 42 never; at 64 (60.8) by 60 17 times
    against 2 of 61; at 62 (58.9) by 58 9 times and by 59 never. Rounded to 6 places
    first, because in floats 500 at 7% is 464.99999999999994, which would floor a
    whole coin away.
    """
    return math.floor(round(sell * (1.0 - tax), 6))


def floor_tax(tax: float) -> float:
    """The tax the loss floors assume: the Sale tax setting, never under GAME_TAX.

    A typo in the box (0.005 for 0.05, or 0) would pay every price out whole on paper
    and switch the floors off, while the game keeps taking its 5% regardless.
    """
    return max(tax, GAME_TAX)


def typed_crate_name(text) -> Tuple[str, Optional[str]]:
    """A crate name for the table, as typed: (name, None), or (name, why it is refused).

    The game titles a crate 'Case | Apex Case' and the bot matches what follows the
    bar (parse_cards), so a pasted whole title loses its prefix. A name that still
    holds a ' | ' is a skin's title, 'AWP | Lore' or 'Package | AK-47 | Lore', which
    parse_cards reads as no case at all: the bot trades cases only, so it is refused.
    """
    name = str(text or "").strip()
    got = _title_name(name)
    # Only a pasted title, with its bar. Vision may drop the bar off a card, so
    # CASE_RE lets it go, but a typed 'Case #1' is a crate called Case #1, and
    # stripping 'Case' left '#1' (infinite, 2026-09-28).
    if got and got[1] and "|" in name:
        name = got[0]
    if GENERIC_RE.match(name):
        return name, "%r is not a case: the bot buys and lists cases only" % name
    return name, None


@dataclass
class Settings:
    # Per crate, by exact name: {"buy": the most it pays, inclusive, or None to never
    # buy it; "sell": what it lists that crate at, or None to never list it}. The
    # table is the allowlist for both sides: a crate not in it is never bought and
    # never listed, and the gloves, the packages and the AWPs worth thousands are
    # kept out that way. Names match whole, without case or outer spaces: 'Glove
    # Case' is a different crate from 'Glove Case 2'.
    crates: Dict[str, Dict[str, Optional[int]]] = field(
        default_factory=lambda: {name: dict(row) for name, row in DEFAULT_CRATES.items()}
    )
    # The old tiers (see TIER_FIELDS): read by crates_from_tiers and nothing else.
    normal_max: int = 40
    expensive_max: int = 60
    expensive_names: List[str] = field(
        default_factory=lambda: ["Nebula Case", "Viridian Case"]
    )
    skip_names: List[str] = field(default_factory=lambda: ["Charm Pack"])
    cases_only: bool = True
    # When on, the matching cap is ignored and the wallet is the only limit: the bot
    # keeps buying until it can no longer afford the cheapest price it would pay.
    dynamic_purchases: bool = False
    dynamic_spend: bool = False
    # Relist whatever was just bought, on the SELL tab, sorted Newest so the fresh
    # item is first. Off by default: it lists real items for real coins.
    auto_sell: bool = False
    sell_normal: int = 45
    sell_expensive: int = 65
    sell_overrides: Dict[str, int] = field(default_factory=lambda: {"Glove Case": 46})
    # The game's cut of a sale, for profit, worth and Earn per flip. The loss floors
    # never take it under GAME_TAX (floor_tax).
    sell_tax: float = GAME_TAX
    sellable_names: List[str] = field(
        default_factory=lambda: [
            "Viridian Case",
            "Chrysalis",
            "Nebula Case",
            "Hero of Hell Console",
            "Glove Case 2",
            "Apex Case",
            "Finishline Case",
            "Lionheart",
            "Glove Case",
            "Charm Pack #1",
        ]
    )
    # Purchases to collect before a selling trip, and the most to put in one basket.
    sell_batch_size: int = 3
    sell_batch_max: int = 5
    idle_seconds: float = 1.5
    dry_run: bool = True
    max_purchases: int = 5
    max_spend: int = 200
    # Only a floor now: button_ready() reads the pill's own colour, which tracks the
    # real countdown (about 7 s) instead of a number someone guessed.
    refresh_cooldown: float = 2.0
    # The market filter infinite uses. Roblox forgets it on a rejoin, and the bot
    # puts it back when restore_filter is on; turn that off to set it by hand.
    filter_price_max: int = 100
    filter_rarity: str = "Red"          # blank: leave the rarity alone, check the price only
    restore_filter: bool = True
    # Roblox focused mode: Roblox stays in front for the whole run, with no hand-back
    # and no idle waits between actions. Moving the mouse (or another app coming to
    # the front) stops the run. For when nobody is using the machine.
    focus_mode: bool = False

    def __post_init__(self):
        self.clean()

    def clean(self) -> List[str]:
        """Normalise the hand-editable fields; say what had to be changed."""
        said = []
        self.filter_rarity = str(self.filter_rarity or "").strip()
        try:
            cap = int(self.filter_price_max)
        except (TypeError, ValueError):
            cap = 0
        # the To box holds eight digits, and 0 would filter the market down to nothing
        if not 1 <= cap <= MAX_PRICE:
            said.append("market filter max price %r is out of range, using 100" % (self.filter_price_max,))
            cap = 100
        self.filter_price_max = cap
        self.crates = self._clean_crates(said)
        return said

    def _clean_crates(self, said: List[str]) -> Dict[str, Dict[str, Optional[int]]]:
        """The crate table with every value it can trust, and a complaint per one dropped.

        A new dict, never an edit in place: dataclasses.replace hands the copy the
        same table object, and cleaning one must not change the other under it.
        A bad value is dropped to None, which is never buy / never list, so a typo
        can only ever make the bot do less.
        """
        raw = self.crates
        if not isinstance(raw, dict):
            said.append("crate list %r is not a name: prices table, dropped" % (raw,))
            raw = {}
        out: Dict[str, Dict[str, Optional[int]]] = {}
        seen = set()
        for name, row in raw.items():
            key, refused = typed_crate_name(name)
            if not key:
                said.append("a crate with no name, dropped")
                continue
            if refused:
                said.append("crate %s, dropped" % refused)
                continue
            if key != str(name).strip():
                said.append("crate %r kept as %r, the name after Case |" % (name, key))
            if key.lower() in seen:
                # names match without case or spacing, so only the first of two
                # spellings would ever be used; say so rather than guess
                said.append("crate %r repeats one already listed, dropped" % (name,))
                continue
            if not isinstance(row, dict):
                said.append("crate %r: %r is not a buy and sell price, dropped" % (key, row))
                continue
            clean = {}
            for side in ("buy", "sell"):
                value = row.get(side)
                try:
                    clean[side] = _as_price(value, zero_is_none=side == "buy")
                except ValueError:
                    said.append("crate %r: %s price %r is not a usable price, dropped" % (key, side, value))
                    clean[side] = None
            # A crate switched off keeps its prices and is skipped by buying and selling.
            # Stored only when off, so a table that never used the switch reads as before.
            on = row.get("on", True)
            if on is False or str(on).strip().lower() in ("0", "false", "off", "no"):
                clean["on"] = False
            buy, sell = clean["buy"], clean["sell"]
            # A payout at or under what the bot pays for that crate loses on every one
            # it relists: a price box wheeled to 1 once did exactly that. Refused here,
            # and again at the moment of listing (under_sell_floor).
            tax = floor_tax(self.sell_tax)
            # infinite 2026-09-27: <= and >=, so paying back exactly what it cost is allowed
            if buy is not None and sell is not None and payout(sell, tax) < buy:
                said.append("crate %r: selling at %d pays %d after the %g%% tax, under the %d it buys at, "
                            "so it will not be listed" % (key, sell, payout(sell, tax), tax * 100, buy))
                clean["sell"] = None
            seen.add(key.lower())
            out[key] = clean
        return out

    # The path is looked up when called, not bound when this module loads: a default
    # bound at import ignored a patched SETTINGS_PATH, and a review run that meant to
    # save a scratch copy overwrote infinite's own settings.json instead.
    @staticmethod
    def load(path: Optional[str] = None) -> "Settings":
        path = path or config_path(active_config())
        try:
            with open(path) as fh:
                raw = json.load(fh)
        except OSError:
            return Settings()
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            # Unreadable (a hand edit gone wrong): kept aside before anything can save
            # defaults over it, which would lose the whole crate table.
            with contextlib.suppress(OSError):
                shutil.copyfile(path, path + ".bad")
            return Settings()
        known = {f for f in Settings.__dataclass_fields__}
        s = Settings(**{k: v for k, v in raw.items() if k in known})
        if raw.get("crates") is None:
            # a file from before the crate table: the same prices, by name
            s.crates = crates_from_tiers(s)
            s.clean()
        return s

    def save(self, path: Optional[str] = None) -> None:
        path = path or config_path(active_config())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if k not in TIER_FIELDS}
        # through a temp file: a crash mid-write never leaves a config cut short
        with open(path + ".tmp", "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(path + ".tmp", path)


# --------------------------------------------------------------------------- configs
# Named settings files in configs\, one of them active. Looked up when called, like
# Settings.load, so a patched CONFIGS_DIR is honoured.

# What Windows refuses in a file name, plus the dot that would hide or stack extensions.
_BAD_NAME_CHARS = set('<>:"/\\|?*.')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "active"} | {"com%d" % i for i in range(10)} \
    | {"lpt%d" % i for i in range(10)}


def config_path(name: str) -> str:
    return os.path.join(CONFIGS_DIR, name + ".json")


def _migrate_settings() -> None:
    """With no config yet: settings.json from before configs\\ moves in as default.json,
    else a fresh download starts from the shipped starter_config.json."""
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    if list_configs():
        return
    if os.path.exists(SETTINGS_PATH):
        os.replace(SETTINGS_PATH, config_path(DEFAULT_CONFIG))
    elif os.path.exists(STARTER_PATH):
        shutil.copyfile(STARTER_PATH, config_path(DEFAULT_CONFIG))


def list_configs() -> List[str]:
    """The configs saved in configs\\, by name, A to Z."""
    try:
        files = os.listdir(CONFIGS_DIR)
    except OSError:
        return []
    return sorted((f[:-5] for f in files if f.lower().endswith(".json")), key=str.lower)


def active_config() -> str:
    """The config in use: the one active.txt names if it still exists, else the first, else default."""
    _migrate_settings()
    try:
        with open(ACTIVE_CONFIG_PATH, encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        name = ""
    have = list_configs()
    # by name whatever the case: Mac and Windows file names ignore it, so 'night'
    # may be saved over Night.json, which keeps its own spelling
    for n in have:
        if n.lower() == name.lower():
            return n
    return DEFAULT_CONFIG if DEFAULT_CONFIG in have or not have else have[0]


def set_active_config(name: str) -> None:
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    with open(ACTIVE_CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write(name + "\n")


def config_name(text) -> Tuple[str, Optional[str]]:
    """A typed config name: (name, None), or (name, why it cannot be a file name)."""
    name = " ".join(str(text or "").split())
    if not name:
        return name, "type a name for the config first"
    if len(name) > 40:
        return name, "a config name is at most 40 characters"
    if any(c in _BAD_NAME_CHARS for c in name):
        return name, "a config name cannot hold any of  < > : \" / \\ | ? * ."
    if name.lower() in _RESERVED_NAMES:
        return name, "%r is a name Windows keeps for itself" % name
    return name, None


def delete_config(name: str) -> None:
    os.remove(config_path(name))


def crates_from_tiers(s: Settings) -> Dict[str, Dict[str, Optional[int]]]:
    """The crate table an old tier settings file meant, price for price.

    The old buy rule was price < cap, so it paid at most cap - 1: that is the new
    buy, which is inclusive. The cap was the expensive one when an expensive_names
    entry was part of the name, and a skip_names entry anywhere in the name meant
    never buy. Only sellable_names were listed, at their override when it was a
    usable price over the old floor (the old Settings.clean dropped any other), else
    at their tier's price. The expensive names come along too, so a dear crate that
    was bought but never listed keeps being bought.
    """
    def names(value) -> List[str]:
        return [str(n).strip() for n in value if str(n).strip()] if isinstance(value, list) else []

    dear_names, skip = [n.lower() for n in names(s.expensive_names)], [n.lower() for n in names(s.skip_names)]
    sellable = names(s.sellable_names)
    overrides = s.sell_overrides if isinstance(s.sell_overrides, dict) else {}
    overrides = {str(k).strip().lower(): v for k, v in overrides.items()}
    out: Dict[str, Dict[str, Optional[int]]] = {}
    for name in sellable + names(s.expensive_names):
        if name.lower() in {n.lower() for n in out}:
            continue
        dear = any(d in name.lower() for d in dear_names)
        try:
            cap = int(s.expensive_max if dear else s.normal_max)
        except (TypeError, ValueError):
            cap = 0
        buy = None if any(k in name.lower() for k in skip) or cap - 1 < 1 else cap - 1
        sell = None
        if name.lower() in {n.lower() for n in sellable}:
            sell = s.sell_expensive if dear else s.sell_normal
            try:
                pinned = _as_price(overrides.get(name.lower()))
            except ValueError:
                pinned = None
            # the old rule's own unfloored arithmetic, not payout(): this is which
            # override the old code honoured, and clean() then applies today's floor
            if pinned is not None and pinned * (1.0 - s.sell_tax) > cap:
                sell = pinned
        out[name] = {"buy": buy, "sell": sell}
    return out


# --------------------------------------------------------------------------- screen


@dataclass(frozen=True)
class Line:
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


def find_roblox() -> Tuple[int, int, dict]:
    """(pid, window id, bounds dict with X/Y/Width/Height in points, top-left origin)."""
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    with objc.autorelease_pool():
        wl = list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])
    for w in wl:
        if (
            w.get("kCGWindowOwnerName") == "Roblox"
            and w.get("kCGWindowLayer", 0) == 0
            and w["kCGWindowBounds"]["Width"] > 400
        ):
            return w["kCGWindowOwnerPID"], w["kCGWindowNumber"], dict(w["kCGWindowBounds"])
    raise RuntimeError("Roblox window not found (is Roblox Player running windowed?)")


FRAME_PATH = os.path.join(tempfile.gettempdir(), "casebot_frame.tif")


def grab(wid: int):
    """Capture that window only, even when it sits behind other windows.

    This used to call CGWindowListCreateImage, which is deprecated and, on this Mac
    on 2026-09-19, stopped working mid-session: every call blocked for exactly 30 s
    and then returned nothing, with screen recording still granted and both displays
    awake. A bot that stalls half a minute per frame is a dead bot, and the 30 s is
    inside a C call so it cannot be interrupted from here.

    screencapture drives the modern path and works on the same window id while it is
    occluded. The frame goes through TIFF rather than PNG: PNG spends most of its
    time deflating pixels nobody keeps, and the decode is lazy, so what it really
    costs shows up later inside the OCR pass. Measured live 2026-09-20 on the
    1920x1050 window, 36 interleaved pairs so a drifting scene hits both equally:

        png    grab 147 ms   ocr 101 ms   full read 249 ms
        tiff   grab 114 ms   ocr  87 ms   full read 201 ms

    A fifth off every poll, all night, and the OCR pass is cheaper too because it is
    no longer the first thing to touch the pixels.

    Nothing the bot sees changes, because the two containers are lossless and agree
    to the bit: on two windows that were holding still, `screencapture -t tiff` and
    the PNG path returned byte-identical pixel buffers, and every fixture plus a
    live frame gives the same hash, the same OCR, the same cards, the same balance,
    the same ui_state and the same pill colour through either container. A lossy
    format would not, which is why jpg is not an option here at any speed.

    The file is 8 MB rather than 1.4 MB, on a path in /tmp that is overwritten in
    place a few times a second and never accumulates.
    """
    return _capture(["-o", "-l", str(wid)])


def notify(message: str) -> None:
    """A macOS notification with a sound, for something only a person can fix.

    2026-10-02: screen capture stopped for every app on the Mac (the screenshot keys
    too), and the bot could only retry. It still retries and carries on when capture
    comes back; this is so the person sees it while it is happening.
    """
    script = 'display notification %s with title "casebot" sound name "Basso"' % json.dumps(message)
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def grab_region(bounds: dict):
    """Capture the screen area a window covers: whatever is drawn there, on top.

    The fallback for a window screencapture -l stops returning (Bot._grab): seen
    four times, the window plainly open and found in the list, until Roblox was
    restarted. Same pixels when Roblox is in front: measured 2026-09-29, the same
    3456x2168 frame and the same state, 99 of 105 text lines in the same places.
    """
    rect = "%d,%d,%d,%d" % (round(bounds["X"]), round(bounds["Y"]),
                            round(bounds["Width"]), round(bounds["Height"]))
    return _capture(["-R" + rect])


# What screencapture last said when it gave back nothing, for the log.
GRAB_ERROR: List[str] = [""]


# A capture takes 0.1 to 0.5 s. One that hangs is macOS holding it for a Screen
# Recording prompt (2026-09-30: 10 s each, a scan stuck for minutes), and no frame
# is worth more than a few seconds of waiting on that.
CAPTURE_TIMEOUT = 4.0


def _capture(target: List[str]):
    """screencapture into FRAME_PATH as TIFF (see grab), loaded, or None."""
    GRAB_ERROR[0] = ""
    try:
        r = subprocess.run(
            ["screencapture", "-x", "-t", "tiff"] + target + [FRAME_PATH],
            check=False,
            capture_output=True,
            timeout=CAPTURE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        GRAB_ERROR[0] = str(exc)
        return None
    if r.returncode != 0 or not os.path.exists(FRAME_PATH):
        GRAB_ERROR[0] = (r.stderr or b"").decode(errors="replace").strip() or "exit %d" % r.returncode
        return None
    try:
        with objc.autorelease_pool():
            return load_png(FRAME_PATH)
    except RuntimeError:
        return None


def load_png(path: str):
    """Decode an image file into a CGImage. PNG for the fixtures, TIFF for a frame.

    CGImageSource sniffs the container, so the name is history rather than a limit:
    samples/ holds PNGs and a live frame arrives as a TIFF, and both come back as
    the same kind of CGImage.

    Pooled on its own account, not just inside grab(): the NSURL is autoreleased,
    and a Python-allocation trace of a 437-cycle soak showed 300 live objects from
    this line. The image survives the pool because CGImageSourceCreateImageAtIndex
    follows the Create rule, so PyObjC owns that reference rather than the pool.
    """
    with objc.autorelease_pool():
        src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
        if src is None:
            raise RuntimeError("cannot read image: %s" % path)
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        del src
        return img


def save_png(img, path: str) -> None:
    # In a pool like every other framework call on the worker thread: the NSURL is
    # autoreleased, and a night of bail_out captures would otherwise pile them into a
    # pool nothing drains.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with objc.autorelease_pool():
        dest = Quartz.CGImageDestinationCreateWithURL(
            NSURL.fileURLWithPath_(path), "public.png", 1, None
        )
        if dest is None:
            return
        Quartz.CGImageDestinationAddImage(dest, img, None)
        Quartz.CGImageDestinationFinalize(dest)


KEEP_FAILURES = 40


def prune_failures(keep: int = KEEP_FAILURES) -> int:
    """Keep only the newest failure captures.

    Each one is a full 1.3 MB window grab, and nothing ever deleted them: a few
    hours of running had already left 139 MB in there, so an unattended night would
    have run into gigabytes. The recent ones are the ones worth looking at.
    """
    try:
        shots = [
            os.path.join(FAILURES_DIR, f)
            for f in os.listdir(FAILURES_DIR)
            if f.endswith(".png")
        ]
    except OSError:
        return 0
    if len(shots) <= keep:
        return 0
    shots.sort(key=lambda f: os.path.getmtime(f))
    dropped = 0
    for old in shots[: len(shots) - keep]:
        try:
            os.remove(old)
            dropped += 1
        except OSError:
            pass
    return dropped


# Cyrillic letters Vision returns for their Latin twins: the trading server's ACCEPT
# button read as 'АССЕРТ' (2026-10-07) and matched nothing. The game writes English.
_LATIN = str.maketrans("АВСЕНКМОРТХаеорсухі", "ABCEHKMOPTXaeopcyxi")


def img_size(img) -> Tuple[int, int]:
    """(width, height) of a capture, in pixels."""
    return Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)


def ocr(img) -> List[Line]:
    """Apple Vision text recognition. Pixel boxes, top-left origin.

    Everything here runs inside an autorelease pool. Vision and Core Graphics hand
    back autoreleased objects, and a worker thread has no runloop to drain the pool
    they land in, so without this they accumulate for as long as the bot runs.
    Measured before the fix: about 0.13 MB per read, which over a night at two reads
    a second is gigabytes, and is why the overnight run died with no crash report.
    """
    with objc.autorelease_pool():
        W = Quartz.CGImageGetWidth(img)
        H = Quartz.CGImageGetHeight(img)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        handler.performRequests_error_([req], None)
        out = []
        for r in req.results() or []:
            cands = r.topCandidates_(1)
            if not cands:
                continue
            bb = r.boundingBox()
            out.append(
                Line(
                    text=str(cands[0].string()).translate(_LATIN),
                    conf=float(cands[0].confidence()),
                    x=bb.origin.x * W,
                    y=(1 - bb.origin.y - bb.size.height) * H,
                    w=bb.size.width * W,
                    h=bb.size.height * H,
                )
            )
    out.sort(key=lambda l: (l.y, l.x))
    return out


def _sample(img, x0: float, y0: float, x1: float, y1: float, grid: int = 96):
    """Average (r, g, b) of one rectangle of a capture.

    Never call bytes() on a capture's pixel provider. Measured 2026-09-20: that leaks
    about 30 MB every call and never gives it back, which is how python reached 15 GB
    while the bot ran. Cropping first does not help either, because a cropped image
    still shares its parent's full pixel buffer.

    What does work is redrawing the region into a small bitmap we own and reading it
    through a memoryview, with no bytes() copy anywhere: measured flat at 89.1 MB
    across 300 calls, not one kilobyte of growth. The redraw averages the region for
    us, which is all either caller wanted.
    """
    W, H = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    x0 = max(0.0, min(float(x0), W - 1))
    y0 = max(0.0, min(float(y0), H - 1))
    x1 = max(x0 + 1, min(float(x1), W))
    y1 = max(y0 + 1, min(float(y1), H))
    w = max(1, min(grid, int(x1 - x0)))
    h = max(1, min(grid, int(y1 - y0)))
    with objc.autorelease_pool():
        sub = Quartz.CGImageCreateWithImageInRect(
            img, Quartz.CGRectMake(x0, y0, x1 - x0, y1 - y0)
        )
        if sub is None:
            return (0.0, 0.0, 0.0)
        ctx = Quartz.CGBitmapContextCreate(
            None, w, h, 8, w * 4,
            Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        if ctx is None:
            return (0.0, 0.0, 0.0)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), sub)
        small = Quartz.CGBitmapContextCreateImage(ctx)
        if small is None:
            return (0.0, 0.0, 0.0)
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(small))
        if data is None:
            return (0.0, 0.0, 0.0)
        mv = memoryview(data)
        bpr = Quartz.CGImageGetBytesPerRow(small)
        r = g = b = n = 0
        for y in range(h):
            row = y * bpr
            for x in range(w):
                o = row + x * 4
                if o + 2 >= len(mv):
                    continue
                b += mv[o]
                g += mv[o + 1]
                r += mv[o + 2]
                n += 1
        del mv
    if not n:
        return (0.0, 0.0, 0.0)
    return (r / n, g / n, b / n)


def _luma_grid(img, x0: float, y0: float, x1: float, y1: float, cols: int, rows: int):
    """One region of a capture as rows x cols of brightness (0-255).

    Same redraw-into-our-own-bitmap route as _sample, for the same reason: no bytes()
    on a capture's pixel provider, ever.
    """
    W, H = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
    x1, y1 = min(float(x1), W), min(float(y1), H)
    cols, rows = max(1, int(cols)), max(1, int(rows))
    if x1 <= x0 or y1 <= y0:
        return []
    with objc.autorelease_pool():
        sub = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(x0, y0, x1 - x0, y1 - y0))
        ctx = Quartz.CGBitmapContextCreate(
            None, cols, rows, 8, cols * 4,
            Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        if sub is None or ctx is None:
            return []
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, cols, rows), sub)
        small = Quartz.CGBitmapContextCreateImage(ctx)
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(small)) if small else None
        if data is None:
            return []
        mv = memoryview(data)
        bpr = Quartz.CGImageGetBytesPerRow(small)
        out = [
            [(mv[r * bpr + c * 4] + mv[r * bpr + c * 4 + 1] + mv[r * bpr + c * 4 + 2]) / 3.0
             for c in range(cols)]
            for r in range(rows)
        ]
        del mv
    return out


def find_hub_shop(img) -> Optional[Line]:
    """The trading server's golden shop icon, bottom left, as a Line over it; else None.

    The trading hub (2026-10-07) opens the market from an icon with no text: a pale
    yellow storefront on a dark square, the first of the row along the bottom left.
    Measured on infinite's screenshot: red and green about 250, blue 100 to 177, on
    (30, 30, 30). Sand, wood and the orange loading screen all fail red-and-green-high
    with blue well below.
    """
    if img is None:
        return None
    W, H = img_size(img)
    x0, y0, x1, y1 = 0.0, H * 0.80, W * 0.12, float(H)
    cols, rows = 48, 40
    g = _rgb_grid(img, x0, y0, x1, y1, cols, rows)
    hits = [(c, r) for r, row in enumerate(g) for c, (red, green, blue) in enumerate(row)
            if red > 220 and green > 205 and blue < 190 and min(red, green) - blue > 60]
    if len(hits) < HUB_ICON_CELLS:
        return None
    cx = x0 + (sum(c for c, _ in hits) / len(hits) + 0.5) * (x1 - x0) / cols
    cy = y0 + (sum(r for _, r in hits) / len(hits) + 0.5) * (y1 - y0) / rows
    return Line("SHOP ICON", 1.0, cx - 1.0, cy - 1.0, 2.0, 2.0)


HUB_ICON_CELLS = 6      # yellow cells of 48 x 40 over the corner that make an icon


def _rgb_grid(img, x0: float, y0: float, x1: float, y1: float, cols: int, rows: int):
    """One region of a capture as rows x cols of (r, g, b), the _luma_grid way."""
    W, H = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
    x1, y1 = min(float(x1), W), min(float(y1), H)
    if x1 <= x0 or y1 <= y0:
        return []
    with objc.autorelease_pool():
        sub = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(x0, y0, x1 - x0, y1 - y0))
        ctx = Quartz.CGBitmapContextCreate(
            None, cols, rows, 8, cols * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        if sub is None or ctx is None:
            return []
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, cols, rows), sub)
        small = Quartz.CGBitmapContextCreateImage(ctx)
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(small)) if small else None
        if data is None:
            return []
        mv = memoryview(data)
        bpr = Quartz.CGImageGetBytesPerRow(small)
        out = [[(mv[r * bpr + c * 4 + 2], mv[r * bpr + c * 4 + 1], mv[r * bpr + c * 4])
                for c in range(cols)] for r in range(rows)]
        del mv
    return out


def mean_brightness(img) -> float:
    """Average brightness of the grid area. The loading overlay dims it to about 11."""
    W, H = img_size(img)
    r, g, b = _sample(img, W * 0.20, H * 0.25, W * 0.98, H * 0.85)
    return (r + g + b) / 3.0


def mean_rgb(img, x0: float, y0: float, x1: float, y1: float):
    """Average of the three colour channels over a pixel box.

    Careful: a live window capture and a PNG loaded off disk do not agree on channel
    order, so the same lit button reads (58, 98.7, 33.6) from the screen and
    (33.6, 98.7, 58) from a fixture. Green sits in the middle either way, so callers
    must lean on green and treat the outer two symmetrically.
    """
    return _sample(img, x0, y0, x1, y1)


DIM_THRESHOLD = 22.0

# A full read slower than this (RapidOCR on Windows: about 1.8 s on a laptop's graphics
# card, 3 to 4 s on a CPU) looks at the screen cheaply after a click before reading it.
SLOW_READ = 0.6
SETTLE_POLL = 0.05      # seconds between those cheap looks
SETTLE_MAX = 0.8        # and the most spent on them before reading anyway


def frame_key(img) -> List[float]:
    """A capture as 32 x 18 cells of brightness: cheap, and enough to see a window open."""
    W, H = img_size(img)
    return [v for row in _luma_grid(img, 0, 0, W, H, 32, 18) for v in row]


def cells_changed(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    """The share of cells whose brightness moved by more than 12 between two frame_keys."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) > 12 for x, y in zip(a, b)) / float(len(a))


# Measured live 2026-09-19 over a full refresh cycle: the pill reads green 98.7 when
# it is ready and 33.9 to 42 while counting down, every frame, so the gap is wide
# enough to read the button's own state instead of guessing at a timer. The countdown
# ran about 7 s, where refresh_cooldown had been waiting 12.
READY_GREEN = 65.0


def button_ready(img, line: Line) -> bool:
    """True when the APPLY FILTER pill is lit rather than counting down.

    The OCR text is not enough on its own: mid-countdown Vision reads the pill as
    '(FILTER', "' FILTER" or '7 APP5 FILTER', and a garbled frame of a lit button
    would look the same. The colour is unambiguous.
    """
    if img is None or line is None:
        return False
    r, g, b = mean_rgb(img, line.x - 70, line.y - 20, line.x + line.w + 70, line.y + line.h + 20)
    return g >= READY_GREEN and g > r and g > b


def to_global(bounds: dict, scale: float, line: Line) -> Tuple[float, float]:
    """Pixel box in the capture -> global screen point at the box centre."""
    return (
        bounds["X"] + line.cx / scale,
        bounds["Y"] + line.cy / scale,
    )


def bounds_differ(a: Optional[dict], b: Optional[dict], tol: float = 2.0) -> bool:
    """True when the window really moved or resized, ignoring 1-point jitter."""
    if a is None or b is None:
        return False
    return any(abs(a[k] - b[k]) > tol for k in ("X", "Y", "Width", "Height"))


# --------------------------------------------------------------------------- parsing


# RapidOCR's older models drop the space after the bar ('Case|Anime Case')
CASE_RE = re.compile(r"^Case\s*(?:\|\s*|\s+)(.+)$")
GENERIC_RE = re.compile(r"^(.+?)\s+\|\s+(.+)$")
INT_RE = re.compile(r"^\d{1,3}$")
PRICE_RE = re.compile(r"^\d{1,6}$")     # a listing price can pass 999
BALANCE_RE = re.compile(r"^\d{1,6}$")
SERIAL_RE = re.compile(r"#[\d,]{3,}")
RAP_RE = re.compile(r"RAP:?\s*([\d,]+)")
# The wear grades a card prints directly beneath its title.
CONDITION_RE = re.compile(
    r"^(Mint ?Condition|Near-?Mint|Standard-?Grade|Combat-?Worn|War-?Torn|Factory ?New|Field-?Tested|Battle-?Scarred|Well-?Worn)",
    re.I,
)


def _squash(text: str) -> str:
    """Upper-cased with spaces removed, for labels Vision runs together."""
    return "".join(text.upper().split())


def find_loose(lines: List[Line], needle: str) -> Optional[Line]:
    """find_text that survives a missing space, e.g. 'SELLITEMS' for 'SELL ITEMS'."""
    want = _squash(needle)
    for l in lines:
        if want in _squash(l.text):
            return l
    return None


def find_confirm(lines: List[Line]) -> Optional[Line]:
    """The green CONFIRM button, never the 'Confirm Purchase' heading above it.

    Vision garbles this button badly: it has come back as CONFIRM, CONFIRIG and
    CONFIEM on three different frames, so the match is a prefix and the heading is
    excluded by its second word rather than by spelling.
    """
    return find_button(lines, "CONFI", avoid="PURCHASE")


def _tab_word(text: str) -> Optional[str]:
    """A top-bar label's word, its icon read alongside or not: 'MARKET G', 'SELL¥', 'PROFILE 2'.

    None when anything else on the line is a word of its own: 'SELL ITEMS' is the
    basket's button, not the tab.
    """
    m = re.match(r"\s*([A-Za-z]+)(.*)$", text or "")
    if m is None or re.search(r"[A-Za-z]{2,}", m.group(2)):
        return None
    return m.group(1).upper()


def find_tab(lines: List[Line], label: str) -> Optional[Line]:
    """The MARKET or SELL tab to click: on the top bar, beside PROFILE.

    Since the game update of 2026-10-07 the bottom bar has a MARKET of its own, and
    from the SELL tab it does not switch back: clicked from there, the bot pressed it
    over and over. The top tab reads loosely too, Vision gluing its icon on ('MARKET
    G'). The main menu has no top bar and no PROFILE, and there the bar's MARKET is
    the way in.
    """
    want = label.upper()
    named = [l for l in lines if _tab_word(l.text) == want]
    profiles = [l for l in lines if _tab_word(l.text) == "PROFILE"]
    if profiles:
        p = min(profiles, key=lambda l: l.y)
        named = [l for l in named if abs(l.cy - p.cy) < 1.5 * p.h]
    else:
        named = [l for l in named if l.text.strip().upper() == want]
    return min(named, key=lambda l: (l.y, l.x), default=None)


def on_market(lines: List[Line]) -> bool:
    """The MARKET tab: only it carries the category row across the top."""
    texts = {l.text.strip().lower() for l in lines}
    return "rifles" in texts and "charms" in texts


def on_disconnected(lines: List[Line]) -> bool:
    """Roblox's own Disconnected box (lost connection, server restart, AFK kick).

    The button and the box's own words, so a stray 'Reconnect' in chat is not enough.
    """
    return any(l.text.strip().upper() == "RECONNECT" for l in lines) and (
        find_text(lines, "Disconnected") is not None or find_text(lines, "Error Code") is not None
    )


def on_lobby(lines: List[Line]) -> bool:
    """The game's main menu, which is where Roblox drops us on join and after a kick.

    Its bottom bar is PLAY, MARKET, INVENTORY and friends. Since the game update
    of 2026-10-07 the market and SELL show that bar too, so the lobby is the bar
    without the market's top tabs: no PROFILE there.
    """
    texts = {l.text.strip().upper() for l in lines}
    return (len(texts & {"PLAY", "INVENTORY", "GAMEMODES"}) >= 2 and "MARKET" in texts
            and "PROFILE" not in texts)


def on_sell(lines: List[Line]) -> bool:
    """The SELL tab: our own inventory.

    The basket button alone is not enough. Vision misses 'SELL ITEMS' on some frames
    (it did on the cart fixture), so any of the tab's own controls will do, and the
    market is excluded first because it shows DELIST on our own listings too.
    """
    if on_market(lines):
        return False
    return any(
        find_loose(lines, k) is not None for k in ("SELL ITEMS", "SELL NOW", "REMOVE")
    )


def option_key(text: str) -> str:
    """A dropdown name as compared: Vision reads l as I after a letter ('AlI', 'BIue')."""
    return re.sub(r"(?<=[A-Za-z])I", "l", (text or "").strip()).lower()


SORT_OPTIONS = {"quality", "rap", "newest", "alphabetical", "collection",
                "equipped", "type", "float", "serial"}
# Seen in the list live 2026-09-23 (All to Equipment, plus Miscellaneous), and the
# market's own categories. Only a known name counts, because Roblox chat draws over
# this row and a chat line would otherwise be taken for the dropdown and clicked.
CATEGORY_OPTIONS = {"all", "pistol", "smg", "rifle", "heavy", "equipment", "miscellaneous",
                    "knife", "knives", "glove", "gloves", "container", "containers",
                    "charm", "charms"}


def parse_sell_filters(lines: List[Line]) -> dict:
    """The SELL tab's filter row: category dropdown, sort dropdown, search box.

    Read off the row the search box sits on. The sort control is whichever line
    there names a sort order, the category is what sits left of it.
    """
    search = next((l for l in lines if l.text.strip().lower().startswith("search")), None)
    out = {"category": None, "sort": None, "search": search}
    if search is None:
        return out
    def words(l):
        # Each dropdown's chevron may come with its label, as a symbol or a lone letter
        # ('Newest ^', 'Quality v'): only the words count.
        m = re.match(r"^([A-Za-z][A-Za-z ]{2,}?)[\s\W\d_]*$", l.text.strip())
        if m is None:
            return None
        text = re.sub(r"(\s+[A-Za-z])+$", "", m.group(1).strip())
        # the line itself when nothing came off, so it is still the same line to a caller
        return l if text == l.text else replace(l, text=text)

    row = sorted(
        (w for w in (words(l) for l in lines if abs(l.cy - search.cy) < search.h and l.x < search.x)
         if w is not None),
        key=lambda l: l.x,
    )
    sorts = [l for l in row if l.text.strip().lower() in SORT_OPTIONS]
    if sorts:
        out["sort"] = sorts[-1]
        # Vision reads All as 'AlI': a capital I after a lowercase letter is an l
        left = [l for l in row if l.x < out["sort"].x and option_key(l.text) in CATEGORY_OPTIONS]
        out["category"] = left[-1] if left else None
    return out


# Which way the SELL tab's sort arrow is set. Up was the setting from the start
# (2026-09-23, read as newest on top), and fresh crates kept turning up below the
# first screen. infinite, 2026-10-02: "I think we have been using the wrong
# direction", so down. If newest turns out to be at the bottom after all, flip it.
SELL_ARROW = "down"
# Extra frames, 0.7 s apart, to find the sort control on a SELL tab still drawing.
SORT_RETRIES = 4


def sort_direction(img, sort: Optional[Line], search: Optional[Line]):
    """Which way the sort arrow next to the sort dropdown points: 'up', 'down' or None.

    The icon is not text, and Vision only reads it at all in one of its two states,
    so it is found by brightness in the gap between the sort box and the search box.
    Up has its shaft right of the lines and the head at the top; down is the
    mirror image. Both have to agree, or the answer is None. Which one the SELL
    view wants is SELL_ARROW.

    Also returns a Line over the icon's centre, for clicking it.
    """
    if img is None or sort is None or search is None or search.x <= sort.x:
        return None, None
    d = search.x - sort.x
    x0, x1 = sort.x + 0.45 * d, sort.x + 0.75 * d
    y0, y1 = sort.cy - 2.5 * sort.h, sort.cy + 2.5 * sort.h
    # About 4 capture px at the Mac's 2x. Windows captures at 1x, where that rule
    # gave 3 px cells over a 2 px shaft: the shaft blurred under the threshold and a
    # bar was taken for it, so the arrow read None at most window sizes (measured
    # 2026-10-21: 17 of 26 right). Every pixel there.
    cell = 1.0 if WINDOWS else max(1.0, sort.h / 9.0)
    g = _luma_grid(img, x0, y0, x1, y1, (x1 - x0) / cell, (y1 - y0) / cell)
    lit = [[v > 90 for v in row] for row in g]
    if not lit:
        return None, None
    cols = [sum(row[c] for row in lit) for c in range(len(lit[0]))]
    on = [c for c, n in enumerate(cols) if n]
    if not on:
        return None, None
    shaft = max(range(len(cols)), key=lambda c: cols[c])
    rows = [r for r, row in enumerate(lit) if any(row)]
    span = [r for r, row in enumerate(lit) if row[shaft]]
    near = lambda r: sum(lit[r][c] for c in range(max(0, shaft - 3), min(len(cols), shaft + 4)))
    head = max(span, key=near)
    right = shaft > (on[0] + on[-1]) / 2.0
    top = head < (span[0] + span[-1]) / 2.0
    centre = Line(
        "SORT ARROW", 1.0,
        x0 + (on[0] + on[-1] + 1) / 2.0 * cell - 1, y0 + (rows[0] + rows[-1] + 1) / 2.0 * cell - 1,
        2.0, 2.0,
    )
    if right and top:
        return "up", centre
    if not right and not top:
        return "down", centre
    return None, centre


@dataclass
class Card:
    name: str
    price: Optional[int]
    button: Optional[str]
    title_line: Line
    price_line: Optional[Line]
    button_line: Optional[Line]
    seller: Optional[str]
    serial: Optional[str]
    is_case: bool


# Letters Vision reads in place of a digit. Only ever applied after a '#', where a
# crate's name holds a number: infinite, 2026-10-02, 'Charm Pack #1' read as
# 'Charm Pack #T', which matched no crate, so it was never sold and the dashboard
# listed it as a crate of its own.
_DIGIT_LOOKALIKES = str.maketrans({"T": "1", "I": "1", "l": "1", "i": "1", "|": "1", "!": "1",
                                   "O": "0", "o": "0"})


def fix_number(name: str) -> str:
    """A crate name with the number after its '#' read back as digits: '#T' is '#1'."""
    head, sep, tail = name.rpartition("#")
    if not sep or not tail:
        return name
    fixed = tail.translate(_DIGIT_LOOKALIKES)
    return head + sep + fixed if fixed.isdigit() else name


def _title_name(text: str) -> Optional[Tuple[str, bool]]:
    """(name, is_case) for a card title line, else None."""
    m = CASE_RE.match(text)
    if m:
        # Vision sometimes doubles the separator ("Case | |Apex Case"), which left a
        # leading pipe in the name and then mismatched the modal title check.
        return fix_number(m.group(1).strip().lstrip("|").strip()), True
    m = GENERIC_RE.match(text)
    if m:
        return text.strip(), False
    return None


def _overlap(a: Line, b: Line) -> float:
    return min(a.x + a.w, b.x + b.w) - max(a.x, b.x)


def parse_cards(lines: List[Line], img_w: int) -> List[Card]:
    """Group OCR lines into cards by x-overlap with each title box, row by row."""
    if not lines:
        return []
    img_h = max(l.y + l.h for l in lines)
    # A title is only a title if it sits on top of a card. The Roblox chat overlay
    # draws across the grid, and a chat line reading "Case | Apex Case" otherwise
    # became a card whose NAME came from the chat and whose serial and SELL NOW
    # button came from the real, unrelated item underneath it. That is how an AWP
    # worth thousands could have been listed for 45.
    #
    # Every genuine card prints its condition directly under the title, sharing the
    # same left edge to within a few pixels. Chat does not.
    tol = max(8.0, img_h * 0.012)
    conditions = [l for l in lines if CONDITION_RE.match(l.text.strip())]
    titles = []
    for l in lines:
        got = _title_name(l.text)
        if not got:
            continue
        under = [
            c
            for c in conditions
            if abs(c.x - l.x) <= tol and l.y < c.y <= l.y + l.h + tol * 4
        ]
        if not under:
            continue
        titles.append((l, got[0], got[1]))
    if not titles:
        return []

    # cluster titles into rows by y
    titles.sort(key=lambda t: t[0].y)
    row_tol = img_h * 0.035
    rows: List[List[tuple]] = []
    for t in titles:
        if rows and t[0].y - rows[-1][0][0].y <= row_tol:
            rows[-1].append(t)
        else:
            rows.append([t])

    cards: List[Card] = []
    for i, row in enumerate(rows):
        top = min(t[0].y for t in row)
        bottom = min(t[0].y for t in rows[i + 1]) if i + 1 < len(rows) else img_h + 1
        band = [l for l in lines if top < l.y < bottom]
        row = sorted(row, key=lambda t: t[0].x)
        for tl, name, is_case in row:
            # Vision reads a serial's commas as full stops now and then ('#3.821.642'
            # on the live SELL grid), which matched nothing and left that card with
            # no serial at all. Only a serial carries a '#', so the swap is safe.
            mine = [replace(l, text=l.text.replace(".", ",")) if "#" in l.text else l
                    for l in band if _overlap(l, tl) > 0]
            mine.sort(key=lambda l: l.y)
            serial_line = next((l for l in mine if SERIAL_RE.search(l.text)), None)
            price_line = None
            if serial_line is not None:
                price_line = next(
                    (
                        l
                        for l in mine
                        if l.y > serial_line.y and INT_RE.match(l.text.strip())
                    ),
                    None,
                )
            button_line = next(
                (
                    l
                    for l in mine
                    if "VIEW" in l.text or "DELIST" in l.text or "SELL NOW" in l.text
                ),
                None,
            )
            button = None
            if button_line is not None:
                if "DELIST" in button_line.text:
                    button = "DELIST"
                elif "SELL NOW" in button_line.text:
                    button = "SELL NOW"     # the SELL tab: ours, not yet listed
                else:
                    button = "VIEW"
            seller_line = next((l for l in mine if "eller:" in l.text), None)
            cards.append(
                Card(
                    name=name,
                    price=int(price_line.text.strip()) if price_line else None,
                    button=button,
                    title_line=tl,
                    price_line=price_line,
                    button_line=button_line,
                    seller=seller_line.text.split(":", 1)[1].strip()
                    if seller_line
                    else None,
                    serial=SERIAL_RE.search(serial_line.text).group(0)
                    if serial_line
                    else None,
                    is_case=is_case,
                )
            )
    return cards


def _counter_row(lines: List[Line]):
    """(PROFILE on the top bar, the x the coin counter ends before), or None.

    The counter sits right of PROFILE, up to the top bar's CLOSE. The game update of
    2026-10-07 took that CLOSE away (icon buttons, no text, in its place), and the
    balance read as None for every frame. With no CLOSE on PROFILE's row the bound
    is five PROFILE widths, which reaches past the counter in both layouts: measured
    2,978 ending at x=2567 against 1934 + 5 x 176 = 2814, and the old 2793..2878
    against 2186 + 5 x 191 = 3141, the old CLOSE's own x.
    """
    profiles = [l for l in lines if l.text.strip().upper() == "PROFILE"]
    if not profiles:
        return None
    left = min(profiles, key=lambda l: l.y)
    # a dialog brings its own CLOSE button: the one that matters shares PROFILE's row
    closes = [l for l in lines if l.text.strip().upper() == "CLOSE" and l.x > left.x
              and abs(l.cy - left.cy) < 1.5 * left.h]
    right = min(closes, key=lambda l: l.x).x if closes else left.x + 5 * left.w
    return left, right


def parse_balance(lines: List[Line]) -> Optional[int]:
    """The coin counter: the bare integer on the tab row, between PROFILE and CLOSE.

    Only there. It used to take the topmost integer anywhere on screen, and Vision
    misses a lone small digit: live 2026-09-24 the counter showed 2, some frames
    read it and others missed it, so the topmost integer became a card price (43,
    62, 77). The bot kept buying with 2 coins, the game refused every CONFIRM, and
    five refusals ended the run. No counter read means no balance, never a guess.
    """
    found = _counter_row(lines)
    if found is None:
        return None
    left, right = found
    ints = [
        l for l in lines
        if BALANCE_RE.match(l.text.strip().replace(",", ""))
        and abs(l.cy - left.cy) < left.h and left.x + left.w < l.cx < right
    ]
    if not ints:
        return None
    return int(max(ints, key=lambda l: l.x).text.strip().replace(",", ""))


def counter_strip(lines: List[Line]):
    """The coin counter's strip on the tab row: (x0, y0, x1, y1) or None."""
    found = _counter_row(lines)
    if found is None:
        return None
    left, right = found
    return (left.x + left.w, left.cy - 1.5 * left.h, right, left.cy + 1.5 * left.h)


def _scaled_crop(img, box, k: float):
    """One region of a capture redrawn at scale k, as an image we own (no bytes())."""
    x0, y0, x1, y1 = box
    w, h = max(1, int((x1 - x0) * k)), max(1, int((y1 - y0) * k))
    with objc.autorelease_pool():
        sub = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(x0, y0, x1 - x0, y1 - y0))
        ctx = Quartz.CGBitmapContextCreate(
            None, w, h, 8, w * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
        )
        if sub is None or ctx is None:
            return None
        Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), sub)
        return Quartz.CGBitmapContextCreateImage(ctx)


# Measured live 2026-09-24 on a 2x capture of a counter showing 111: the full frame
# read '11' ten times in twelve (the box started one glyph late), and the strip alone
# read 111 at 0.35x, nothing at 0.5x, 111 at 0.75x, 117 at 1x, 111 at 1.5x, LLL at 2x.
# No single read is trustworthy; three of these agreeing with the frame is.
# 2026-10-02, a counter showing a lone 4: those three scales all read nothing, the
# vote had only the frame's 4, and the balance was "unreadable" for good. Single
# glyphs read at 0.5x, 2x and 3x (and 111 still wins at 0.35x, 0.75x, 1.5x, with
# 2x and 3x reading LLL), so all six vote: ~0.15 s, only when the coins change.
COUNTER_SCALES = (0.35, 0.5, 0.75, 1.5, 2.0, 3.0)


def vote_counter(img, lines: List[Line]) -> Optional[str]:
    """The coin counter by majority of the frame's read and three re-reads of its strip.

    Returns the digits at least two reads agree on, or None when they do not agree
    (an unknown balance buys nothing, which is the safe side of a misread).
    """
    box = counter_strip(lines)
    if box is None:
        return None
    votes = []
    first = parse_balance(lines)
    if first is not None:
        votes.append(str(first))
    for k in COUNTER_SCALES:
        crop = _scaled_crop(img, box, k)
        if crop is None:
            continue
        # the longest, and of those the rightmost: the strip starts at the coin icon,
        # which RapidOCR (Windows) reads as an 8 now and then
        digits = [(l.text.strip().replace(",", ""), l.x) for l in ocr(crop)]
        digits = [(d, x) for d, x in digits if BALANCE_RE.match(d)]
        if digits:
            votes.append(max(digits, key=lambda dx: (len(dx[0]), dx[1]))[0])
    if not votes:
        return None
    best = max(set(votes), key=lambda v: (votes.count(v), len(v)))
    return best if votes.count(best) >= 2 else None


def fix_counter_line(lines: List[Line], digits: Optional[str]) -> List[Line]:
    """The frame's lines with the counter set to the voted digits, or removed."""
    box = counter_strip(lines)
    if box is None:
        return lines
    x0, y0, x1, y1 = box
    on_row = lambda l: (BALANCE_RE.match(l.text.strip().replace(",", ""))
                        and y0 <= l.cy <= y1 and x0 < l.cx < x1)
    keep = [l for l in lines if not on_row(l)]
    if digits is not None:
        keep.append(Line(digits, 1.0, x0 + (x1 - x0) * 0.5, (y0 + y1) / 2 - 10, 20, 20))
    keep.sort(key=lambda l: (l.y, l.x))
    return keep


def find_text(lines: List[Line], needle: str) -> Optional[Line]:
    needle = needle.upper()
    for l in lines:
        if needle in l.text.upper():
            return l
    # Then whatever the spaces: RapidOCR (Windows) puts one inside a word now and
    # then ('Search for Item s...'), the way Vision runs two together.
    return find_loose(lines, needle)


def find_button(lines: List[Line], needle: str, avoid: Optional[str] = None) -> Optional[Line]:
    """Lowest line containing `needle`, skipping ones containing `avoid`.

    Needed because 'Confirm Purchase' (the modal header) also contains both
    'PURCHASE' and 'CONFIRM', and the real buttons sit below it.
    """
    needle = needle.upper()
    bad = avoid.upper() if avoid else None
    cands = [
        l
        for l in lines
        if needle in l.text.upper() and not (bad and bad in l.text.upper())
    ]
    if not cands:
        return None
    return max(cands, key=lambda l: l.y)


def ui_state(lines: List[Line], img=None) -> str:
    """Which screen are we looking at.

    'market_ready' | 'market_nofilter' | 'cooldown' | 'loading' | 'modal' |
    'modal_scrolled' | 'confirm' | 'sell_confirm' | 'setprice' | 'sell' | 'lobby' |
    'trade_rules' | 'hub' | 'disconnected' | 'unknown'
    """
    # Roblox draws this box over whatever the game was showing, so it goes first.
    if on_disconnected(lines):
        return "disconnected"
    # The game's own loading screen, joining either the main game or a trading
    # server (2026-10-07): bright orange, so the dim test below never sees it.
    if find_text(lines, "Loading Assets") is not None:
        return "loading"
    # A trading server greets every join with its rules, ACCEPT to go on.
    if find_text(lines, "TRADE RULES") is not None and find_button(lines, "ACCEPT") is not None:
        return "trade_rules"
    # Both confirmation dialogs are headed "Confirm Purchase", even the one that
    # sells, so the sentence underneath is what tells them apart.
    if find_text(lines, "sell your selected") is not None:
        return "sell_confirm"
    # Set Price carries its own CONFIRM and CANCEL, so it has to be recognised before
    # the generic confirm test or it reads as a confirmation dialog.
    if find_text(lines, "Set Price") is not None and find_text(lines, "Listing Price") is not None:
        return "setprice"
    confirm_btn = find_button(lines, "CONFIRM", avoid="PURCHASE")
    if find_text(lines, "Are you sure") or (confirm_btn and find_button(lines, "CANCEL")):
        return "confirm"
    if find_text(lines, "Confirm Purchase") and find_button(lines, "RCHASE", avoid="CONFIRM"):
        return "modal"
    # The same window scrolled down inside, its heading and the crate's name gone off
    # the top, which reads as nothing at all (2026-09-30, stuck for minutes) or even
    # as the market behind it. Its sales panel under PURCHASE is still there.
    if find_button(lines, "RCHASE", avoid="CONFIRM") and any(
            find_text(lines, k) for k in ("SALES GRAPH", "LATEST SALES", "SIMILAR LISTINGS")):
        return "modal_scrolled"
    if img is not None and mean_brightness(img) < DIM_THRESHOLD:
        return "loading"
    if on_sell(lines):
        return "sell"
    cards = parse_cards(lines, 0)
    if find_text(lines, "APPLY FILTER") and cards:
        return "market_ready"
    if cards and on_market(lines):
        # the market with its filter panel scrolled up, so APPLY FILTER is off-screen.
        # Coming back from the SELL tab lands here every time.
        return "cooldown" if find_text(lines, "Search for Items") is None else "market_nofilter"
    if cards:
        return "cooldown"
    if on_lobby(lines):
        return "lobby"
    # The trading hub: the world, and the shop icon that opens the market.
    if find_hub_shop(img) is not None:
        return "hub"
    return "unknown"


def parse_rap(lines: List[Line], card: "Card") -> Optional[int]:
    """The RAP printed on a card, which is the market's own reference price.

    Bounded below by the next title in the same column: without that, a card whose
    own RAP line was missed silently borrows the RAP of the card in the row beneath
    it, and prices the listing off a completely different item.
    """
    below = [
        l.y
        for l in lines
        if _overlap(l, card.title_line) > 0
        and l.y > card.title_line.y + card.title_line.h
        and _title_name(l.text)
    ]
    limit = min(below) if below else float("inf")
    best = None
    for l in lines:
        m = RAP_RE.match(l.text.strip())
        if not m or _overlap(l, card.title_line) <= 0:
            continue
        if l.y < card.title_line.y or l.y >= limit:
            continue
        if best is None or l.y < best.y:
            best = l
    if best is None:
        return None
    try:
        return int(RAP_RE.match(best.text.strip()).group(1).replace(",", ""))
    except (AttributeError, ValueError):
        return None


def crate_rank(name: str, s: Settings) -> int:
    """Where this crate sits in the table, 0 at the top: its priority, first bought and
    listed (infinite drags the rows into order). Anything off the table comes last."""
    want = (name or "").strip().lower()
    names = [n.strip().lower() for n in (s.crates or {})]
    return names.index(want) if want in names else len(names)


def crate_row(name: str, s: Settings) -> Optional[Dict[str, Optional[int]]]:
    """This crate's {"buy", "sell"} row in the crate table, or None when it has none.

    The whole name, never a substring: 'Glove Case' is a different crate from 'Glove
    Case 2', and a substring rule would also match anything else containing a listed
    name. Case and outer spaces do not count; Settings.clean keeps one row per name.
    """
    want = (name or "").strip().lower()
    for n, row in (s.crates or {}).items():
        if n.strip().lower() == want:
            return row
    return None


def crate_on(name: str, s: Settings) -> bool:
    """In the crate table and not switched off."""
    row = crate_row(name, s)
    return row is not None and row.get("on", True) is not False


def buy_price_for(name: str, s: Settings) -> Optional[int]:
    """The most the bot pays for this crate, inclusive; None when it never buys it."""
    row = crate_row(name, s)
    return (row.get("buy") or None) if row and crate_on(name, s) else None


def sell_price_for(name: str, s: Settings) -> Optional[int]:
    """What the bot lists this crate at; None when it never lists it (or it is off)."""
    row = crate_row(name, s)
    return (row.get("sell") or None) if row and crate_on(name, s) else None


def table_sell_price(name: str, s: Settings) -> Optional[int]:
    """The Sell at price in the table whether or not the crate is switched on.

    For pricing what is already held or already listed: switching a crate off stops
    new buys and listings, not the worth of the ones owned or the sales of the ones
    on the market.
    """
    row = crate_row(name, s)
    return (row.get("sell") or None) if row else None


def is_sellable(name: str, s: Settings) -> bool:
    """Whether the bot may list this item at all: in the crate table, with a sell price.

    Everything else in the inventory, the gloves, the packages, the AWPs worth
    thousands, is off limits by name.
    """
    return sell_price_for(name, s) is not None


def sale_clears_cost(price: int, paid: int, s: Settings) -> bool:
    """Does the payout, after the sale tax, actually beat what we paid.

    The tax comes off the listing price, so asking exactly what we paid is a small
    loss. This does not veto anything: the asking price is infinite's call and the
    bot uses it as given. It exists so a losing price is said out loud in the log
    rather than discovered later in the ledger.
    """
    return payout(price, s.sell_tax) > paid


def under_sell_floor(price: int, s: Settings, name: str, paid: int = 0) -> bool:
    """Would this listing pay out no more than the bot pays for that crate?

    Unlike sale_clears_cost this one vetoes: nothing is ever listed under it. A
    blank GUI price box wheeled to 1 became a 1-coin listing, and Settings.clean
    refuses such rows, but a hand-edited file could still get here. Each crate
    against its OWN buy price: one floor for all let a Nebula (bought at up to 58)
    be listed at 50 and paid 47. At infinite's 44 / 64 / 46 against 40 / 58 / 40
    with 5% tax the payouts are 41, 60 and 43, so real prices pass; 62 against 58
    pays 58, which is no profit at all. A crate the bot never buys is floored by
    what that serial cost when the ledger knows (paid): a blank Buy box plus a typo
    of 6 in Sell at would otherwise have listed every Viridian, bought at 55, for 6.
    A sell-only crate nobody paid the bot's coins for keeps the price it was given.
    """
    buy = buy_price_for(name, s)
    cap = buy if buy is not None else (paid or 0)
    return cap > 0 and payout(price, floor_tax(s.sell_tax)) < cap   # break-even is allowed


def floor_complaint(price: int, s: Settings, name: str, paid: int = 0) -> str:
    """Why under_sell_floor refused this listing: '42: pays 39 after the 5% tax, ...'."""
    tax = floor_tax(s.sell_tax)
    buy = buy_price_for(name, s)
    cap, what = (buy, "it buys at") if buy is not None else (paid, "this one cost")
    return "%d: pays %d after the %g%% tax, under the %d %s, fix the crate prices" % (
        price, payout(price, tax), tax * 100, cap, what)


def parse_setprice(lines: List[Line]) -> dict:
    """The Set Price sheet: its input field, what it currently reads, its buttons."""
    header = find_text(lines, "Set Price")
    listing = find_text(lines, "Listing Price")
    prompt = find_text(lines, "How much")
    price = None
    if listing is not None:
        # Bounded on the right by the sheet itself. The SELL basket panel sits beside
        # it, and once it holds three crates its third price lines up with this row:
        # when Vision missed a lone '3' here, that basket's 39 was read as the listing
        # price and a 3 was confirmed (live, 2026-09-28, twice, both the 4th crate).
        right = prompt.x + prompt.w if prompt is not None else listing.x + listing.w * 4
        cands = [
            l
            for l in lines
            if PRICE_RE.match(l.text.strip()) and abs(l.cy - listing.cy) < listing.h * 1.2
            and listing.x < l.x and l.cx < right
        ]
        if cands:
            price = int(cands[0].text.strip())
    field = None
    typed = None
    empty = False
    if prompt is not None:
        # the input sits between the prompt and the Listing Price row; clicking the
        # prompt's own centre line, one row down, lands inside it
        field = Line("price field", 1.0, prompt.x, prompt.y + prompt.h * 1.6,
                     prompt.w, prompt.h * 1.6)
        # What the box itself shows. The Listing Price row keeps a stale number even
        # when the box is empty (the empty fixture reads 25), so confirming on that
        # row alone could list an item without anything having been typed.
        lo, hi = prompt.y, (listing.y if listing is not None else prompt.y + prompt.h * 5)
        # Horizontally as well as vertically: the Roblox chat overlay draws inside
        # this band on a live frame, and a chat message that happens to be digits
        # would otherwise read as the price someone typed.
        left, right = prompt.x, prompt.x + prompt.w
        box = [
            l
            for l in lines
            if lo < l.y < hi and left <= l.cx <= right and PRICE_RE.match(l.text.strip())
        ]
        if box:
            typed = int(box[0].text.strip())
        # The 'Price...' placeholder is drawn only while the box holds nothing, so it
        # is proof of an empty box, where a missing number is only an unread one.
        # Both fresh sheets on file show it, with a stale 25 in the summary row.
        empty = any(
            lo < l.y < hi and left <= l.cx <= right and re.match(r"^price\s*[.\u2026]", l.text.strip(), re.I)
            for l in lines
        )
    return {
        "header": header,
        "field": field,
        "typed": typed,
        "empty": empty,
        "listing_price": price,
        "confirm_line": find_confirm(lines),
        "cancel_line": find_button(lines, "CANCEL"),
    }


BASKET_READS = 3     # frames to get every basket row read back before refusing to sell
STILL_READS = 4      # reads to wait for a scrolled grid to stop before clicking on it
STILL_GAP = 0.15     # seconds between them
SELL_MISSES = 3      # SELL NOW clicks that open nothing, in one trip, before it gives up


def basket_rows(lines: List[Line]) -> List[Tuple[Line, Optional[int]]]:
    """The SELL tab's basket, top to bottom: each row's 'Price' heading and its price.

    Every staged crate's row heads itself 'Case' and 'Price' and prints the price
    under the second, full size, after the Set Price sheet has closed. It is what
    SELL ITEMS will list at, so it has the last word on what was typed. A price
    Vision missed on this frame is None, for a closer read (Bot._row_price), not a
    row gone: four rows of plain 44s read as three on 2026-10-02 and the basket
    was refused for it.
    """
    heads = sorted(
        (p for p in lines
         if p.text.strip().lower() == "price"
         and any(c.text.strip().lower() == "case" and c.x < p.x and abs(c.cy - p.cy) < p.h
                 for c in lines)),
        key=lambda p: p.y,
    )
    rows = []
    for i, head in enumerate(heads):
        below = heads[i + 1].y if i + 1 < len(heads) else float("inf")
        # One heading width either side and no more: a name such as 'Glove Case 2'
        # that Vision splits leaves its '2' further left, and that is not a price.
        found = [l for l in sorted(lines, key=lambda l: l.y)
                 if head.y < l.y < below and abs(l.cx - head.cx) < head.w
                 and PRICE_RE.match(l.text.strip())]
        rows.append((head, int(found[0].text.strip()) if found else None))
    return rows


def parse_basket(lines: List[Line]) -> Optional[List[int]]:
    """The basket prices this frame read, top to bottom; None with no basket panel."""
    rows = basket_rows(lines)
    return [p for _, p in rows if p is not None] if rows else None


def _digits_seen(sheet: dict) -> int:
    """How many digits the Set Price sheet shows, in the box or the summary row."""
    return max(
        (len(str(v)) for v in (sheet.get("typed"), sheet.get("listing_price")) if v is not None),
        default=0,
    )


def first_clear(sheet: dict) -> int:
    """Keys to empty the Set Price box before the first try at typing a price.

    Every key costs about 85 ms of events while Roblox is in front (measured live
    2026-09-24), and the old fixed clear of 8 spent 32 of them, about 2.7 s, on a
    box that a fresh sheet opens empty. So: nothing when the placeholder shows or
    nothing reads at all, and one more than the digits on show otherwise. The
    summary row counts too, because the box's own digits render faint and Vision
    misses them. Too few is caught by the read-back, and the retry clears wide.
    """
    if sheet.get("typed") is None and (sheet.get("empty") or sheet.get("listing_price") is None):
        return 0
    return _digits_seen(sheet) + 1


def retry_clear(sheet: dict) -> int:
    """Keys to empty the box after a first try did not take: never fewer than 8.

    A failed try may have left digits Vision did not read, so what it did read is
    only a floor.
    """
    return max(8, _digits_seen(sheet) + 2)


def filter_clear(box: Line, spare: int, unread: int) -> int:
    """Keys to empty a market filter price box: what it shows plus a spare, or `unread`.

    Commas count as characters. The box may well hold '100,000,000' as eleven of
    them, and one left behind would make 100 into 1100.
    """
    shown = "" if box.text == "PRICE FIELD" else re.sub(r"\s", "", box.text)
    return len(shown) + spare if re.search(r"\d", shown) else unread


def parse_modal(lines: List[Line]) -> dict:
    """Item name, price, and the PURCHASE / CLOSE lines of the purchase modal."""
    purchase = find_button(lines, "RCHASE", avoid="CONFIRM")
    confirm_hdr = find_text(lines, "Confirm Purchase")
    titles = [l for l in lines if _title_name(l.text)]
    if confirm_hdr is not None:
        # only titles drawn between the modal header and its PURCHASE button count:
        # the market grid behind the modal is still fully readable and its titles
        # are only a few pixels shorter than the modal's own.
        hi = purchase.y if purchase is not None else float("inf")
        inside = [l for l in titles if confirm_hdr.y <= l.y <= hi]
        titles = inside
    title = max(titles, key=lambda l: l.h) if titles else None
    name = None
    is_case = False
    if title is not None:
        got = _title_name(title.text)
        name = got[0] if got[1] else title.text.strip()
        is_case = got[1]
    price = None
    price_line = None
    if purchase is not None:
        span = purchase.w * 0.5
        lo, hi = purchase.x - span, purchase.x + purchase.w + span
        cands = [
            l
            for l in lines
            if l.y < purchase.y and INT_RE.match(l.text.strip()) and lo <= l.cx <= hi
        ]
        if cands:
            price_line = max(cands, key=lambda l: l.y)
            price = int(price_line.text.strip())
    close = None
    closes = [l for l in lines if "CLOSE" in l.text.upper()]
    if closes and confirm_hdr is not None:
        close = min(closes, key=lambda l: abs(l.y - confirm_hdr.y))
    # With no header to anchor on, the nearest CLOSE to the top of the frame is the
    # market's own CLOSE, which quits the whole market UI. Better to return nothing
    # and let the next cycle recover than to click that.
    return {
        "name": name,
        "price": price,
        "is_case": is_case,
        "title_line": title,
        "price_line": price_line,
        "purchase_line": purchase,
        "close_line": close,
    }


# --------------------------------------------------------------------------- decision


def decide(card: Card, s: Settings) -> Tuple[bool, str]:
    if s.cases_only and not card.is_case:
        return False, "not a case"
    if card.button in ("DELIST", "SELL NOW"):
        return False, "own item"
    if card.price is None:
        return False, "no price read"
    # The crate table is the allowlist: a crate it does not name is never bought,
    # whatever it costs, and one named without a buy price is only ever relisted.
    row = crate_row(card.name, s)
    if row is None:
        return False, "not in the crate list"
    if row.get("on", True) is False:
        return False, "switched off"
    buy = row.get("buy")
    if not buy:
        return False, "never bought"
    if card.price <= buy:
        return True, "%d <= %d" % (card.price, buy)
    return False, "%d > %d" % (card.price, buy)


# --------------------------------------------------------------------------- input


MIN_IDLE_SECONDS = 0.5
# Longest we wait for Roblox to actually become frontmost before clicking.
ACTIVATE_TIMEOUT = 2.0
# How long a tab click gets to show its screen before it is pressed once more.
TAB_RETRY_AFTER = 2.5
# Timing of a burst. These decide whether a bargain is still there when the click
# lands, so they are measured against the live game rather than guessed.
# Measured live 2026-09-20 against the APPLY FILTER button: 12 of 12 clicks still
# registered at 0.03 settle and 0.01 gap, so these keep a wide margin over what was
# proven to work while cutting a 0.68 s burst to under 0.2 s.
ACTIVATE_SETTLE = 0.10   # after Roblox comes forward, before the first event
CLICK_GAP = 0.02         # between the mouse events of one click
# Measured live 2026-09-23: a read taken straight after a scroll is within 10 px of
# where the grid settles, and a read takes 0.3 s on its own. 0.35 s on top was waste.
SETTLE_AFTER_SCROLL = 0.1
# One wheel notch moves the grid about 100 pt whatever its size, and notches sent
# 0.06 s apart all count (measured 2026-09-23: 1, 2 and 4 notches moved 200, 402 and
# 803 px). So a long scroll is several notches inside one focus grab, not one grab
# per notch, which is what made scrolling slow.
NOTCH_GAP = 0.06
# A text box is fussier than a button: it wants the cursor to have moved there and a
# moment to take focus. Measured live 2026-09-24 in the market filter's To box: five
# timing profiles all typed right 6 times out of 6, down to a 0.06 s focus wait and
# 0.004 s key holds and gaps. While Roblox is in front every posted event costs about
# 85 ms of wall time whatever we sleep (time.sleep itself is good to 1 ms), so the
# sleeps were never the cost: the old profile took 7.1 s a box and the fastest 2.35 s,
# 2.2 s of that the events. What is left to win is sending fewer of them, which is
# what the clear counts are for. 0.1 s of focus keeps a margin over the 0.06 proven.
FIELD_CLICK_GAP = 0.015
FIELD_FOCUS_WAIT = 0.1
# With no clear keys the first digit follows mouse-up by FIELD_FOCUS_WAIT alone, about
# 0.1 s. The 0.06 s that proved safe was measured in the filter's To box behind a
# clear, never on a Set Price box with none, so that one case waits 0.4 s. A sleep
# costs no events and is exact, so the margin is 0.3 s and nothing else.
FIRST_DIGIT_WAIT = 0.4
KEY_HOLD = 0.004         # between a key's down and its up
KEY_GAP = 0.004          # after the up, before the next key
# Between one digit and the next. Roblox drops a key that follows the last one too
# closely: a 39 went in as 3 and a 60 as 6 on about one first try in four, in focused
# mode as much as out of it, so it was never the window switching. Measured live
# 2026-09-28, 20 crates staged each way: 4 ms apart, 5 to 6 lost the second digit;
# 0.15 s apart, none did, and every first try took.
DIGIT_GAP = 0.15


def wait_idle(s: Settings, stop_event: Optional[threading.Event] = None) -> bool:
    """Block until the user has not touched the machine for idle_seconds."""
    # idle gating is the only thing keeping the bursts off the user's keyboard, so a
    # 0 or negative setting must not switch it off.
    target = max(MIN_IDLE_SECONDS, float(s.idle_seconds))
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if _session_idle() >= target:
            return True
        time.sleep(0.2)


def _session_idle() -> float:
    """Seconds since the last input from anywhere in this login session."""
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType
    )


def _peak_mb() -> float:
    """The most memory this process has held, in MB (ru_maxrss is bytes on a Mac)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 * 1024.0)


def _topmost_pid_at(point: Tuple[float, float], ignore_pid: Optional[int] = None) -> Optional[int]:
    """Owner pid of the front-most ordinary window covering a global point.

    This, not "is Roblox the frontmost app", is what decides where a click lands.
    Measured 2026-09-19 on infinite's two-screen setup, with the browser and chat on
    the external display and Roblox on the laptop panel:

        0 Claude    (external)      2 Roblox  <- the only window over the button
        1 WhatsApp  (external)      3 Helium

    Roblox sits third globally and is still the only thing under the cursor, so a
    global frontmost check called it "not in front" and refused every click, which is
    exactly what infinite saw: the pointer moved to the button and nothing happened.
    """
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    x, y = point
    with objc.autorelease_pool():
        windows = list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])
    for w in windows:
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        owner = w.get("kCGWindowOwnerPID")
        if ignore_pid is not None and owner == ignore_pid:
            continue
        b = w.get("kCGWindowBounds") or {}
        if (
            b.get("X", 0) <= x <= b.get("X", 0) + b.get("Width", 0)
            and b.get("Y", 0) <= y <= b.get("Y", 0) + b.get("Height", 0)
        ):
            return owner
    return None


def _frontmost_pid(ignore_pid: Optional[int] = None) -> Optional[int]:
    """Owner pid of the frontmost ordinary window, ignoring our own.

    NSWorkspace.frontmostApplication() and NSRunningApplication.isActive() are both
    fed by workspace notifications that need a spinning runloop, which a plain script
    does not have. Measured 2026-09-19: after activating Roblox, both kept reporting
    the app that happened to be in front when the process started, for as long as we
    asked, while the window list showed Roblox on top immediately. So ask the window
    list, which is a fresh syscall every time. Our own always-on-top overlay is
    skipped, or the bot would mistake it for another app stealing the front.
    """
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    with objc.autorelease_pool():
        windows = list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])
    for w in windows:
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        owner = w.get("kCGWindowOwnerPID")
        if ignore_pid is not None and owner == ignore_pid:
            continue
        return owner
    return None


class _PSN(ctypes.Structure):
    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]


try:
    _HISERVICES = None if WINDOWS else ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
except OSError:
    _HISERVICES = None


def _active_pid() -> Optional[int]:
    """The app the user is actually in, to give focus back to after a click.

    Neither window-list question answers this. Measured 2026-09-23 with infinite in
    Claude on the other screen: the window under the click point was Roblox, so the
    bot saved nobody and never switched back. GetFrontProcess is deprecated, but it
    asks the window server itself, so it is never stale in a thread with no runloop
    the way NSWorkspace is: it named Claude, in 0.4 ms.
    """
    if _HISERVICES is not None:
        psn, pid = _PSN(), ctypes.c_int()
        if (
            _HISERVICES.GetFrontProcess(ctypes.byref(psn)) == 0
            and _HISERVICES.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid)) == 0
            and pid.value > 0
        ):
            return pid.value
    return _frontmost_pid(ignore_pid=os.getpid())


def _cursor_at() -> Tuple[float, float]:
    """Where the pointer is, in the same top-left global points the events use."""
    with objc.autorelease_pool():
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (p.x, p.y)


def _cursor_point():
    with objc.autorelease_pool():
        cur = NSEvent.mouseLocation()
        scr_h = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height
        return Quartz.CGPointMake(cur.x, scr_h - cur.y)


def bring_forward(pid: int, timeout: float = ACTIVATE_TIMEOUT):
    """Put Roblox in front and wait until it really is. Returns the app to restore.

    Pooled: NSEvent and NSRunningApplication hand back autoreleased objects, and the
    bot thread has no runloop to drain the pool they land in. The returned app is
    safe to keep, because PyObjC retains anything it wraps.
    """
    with _pool():
        front = _active_pid()
        prev = _app_for(front) if front is not None and front != pid else None
        rb = _app_for(pid)
        if rb is not None:
            _activate(rb)
    # Activation lands when it lands, and a click before it goes to macOS as the
    # window's focusing click: focused mode's first SELL click was lost that way
    # every time (infinite, 2026-09-30). So wait for Roblox to be the front app.
    deadline = time.time() + timeout
    while _active_pid() != pid and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(ACTIVATE_SETTLE)
    return prev


def _app_for(pid: int):
    """The running app with that pid, as _activate takes it, or None."""
    return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)


def _activate(app) -> None:
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def _point(x: float, y: float):
    """A global point the event calls take, with .x and .y."""
    return Quartz.CGPointMake(x, y)


def _warp(pt) -> None:
    """Move the pointer there. No event: it is not input (see _input_idle)."""
    Quartz.CGWarpMouseCursorPosition(pt)


def hand_back(prev, cursor_point) -> None:
    if cursor_point is not None:
        _warp(cursor_point)
    if prev is not None:
        _activate(prev)


def _burst(pid: int, point: Tuple[float, float], post, prev_app=None, hold: bool = False) -> bool:
    """Activate Roblox, warp the real cursor, post real HID events, put it all back.

    The only sequence Roblox accepts (SPEC.md). Steals focus for ~0.5 s, so every
    caller gates on wait_idle first. Returns False when Roblox never came forward,
    in which case nothing was posted.

    The pool is the same story as read(): NSEvent and NSRunningApplication autorelease,
    the bot thread drains nothing by itself, and a night is thousands of bursts.
    """
    with _pool():
        return _burst_inner(pid, point, post, prev_app, hold)


# When the bot last posted an input event. Focused mode tells a person from the bot
# by input newer than this (Bot._person_is_back).
LAST_POST: List[float] = [0.0]
FOCUS_GRACE = 2.0    # seconds after a focused start in which the hand leaving the mouse is fine
POST_SLACK = 0.1     # seconds an event of the bot's own may register after its stamp


def _send(ev) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _post(ev) -> None:
    """Post one input event, and stamp when: every event the bot sends goes through here."""
    _send(ev)
    LAST_POST[0] = time.time()


def _mouse_ev(kind: str, pt):
    """A left-button 'move', 'down' or 'up' event at pt, for _post."""
    kinds = {"move": Quartz.kCGEventMouseMoved, "down": Quartz.kCGEventLeftMouseDown,
             "up": Quartz.kCGEventLeftMouseUp}
    return Quartz.CGEventCreateMouseEvent(None, kinds[kind], pt, Quartz.kCGMouseButtonLeft)


def _wheel_ev(dy: int):
    """A wheel event, dy pixels: positive scrolls the content down (see scroll_at)."""
    return Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 1, -int(dy))


def _input_idle() -> float:
    """Seconds since the last mouse or keyboard event, the bot's own posts included.

    Measured 2026-09-29: a posted event resets this, a pointer warp does not. So the
    pointer moving by itself, which Roblox does when its window reloads, is no input.
    """
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    )


def _burst_inner(pid: int, point: Tuple[float, float], post, prev_app=None, hold: bool = False) -> bool:
    self_pid = os.getpid()
    if hold:
        # Already inside a held sequence: no activation, no handing focus back
        # between clicks. Three round trips per purchase is what lost the bargains.
        pt = _point(point[0], point[1])
        _warp(pt)
        deadline = time.time() + ACTIVATE_TIMEOUT
        while time.time() < deadline:
            if _topmost_pid_at((point[0], point[1]), ignore_pid=self_pid) == pid:
                post(pt)
                return True
            time.sleep(0.02)
        return False
    prev = prev_app
    if prev is None:
        # The app the user is in, not the window under the click: on two screens the
        # window under the click is Roblox itself, so nothing was ever handed back.
        front = _active_pid()
        if front is not None and front != pid:
            prev = _app_for(front)
    rb = _app_for(pid)
    orig = _cursor_point()
    pt = _point(point[0], point[1])
    if rb is not None:
        _activate(rb)
    try:
        _warp(pt)
        # macOS gives an inactive window's first click to the window itself, so a
        # click posted while Roblox is still coming forward is spent on focus and
        # the button never sees it. Waiting a fixed 0.25 s was a guess that lost
        # clicks whenever activation ran slow, which is most of the time when the
        # machine has been idle. Wait for the switch to actually land instead.
        deadline = time.time() + ACTIVATE_TIMEOUT
        focused = False
        while time.time() < deadline:
            if _topmost_pid_at((point[0], point[1]), ignore_pid=self_pid) == pid:
                focused = True
                break
            time.sleep(0.05)
        if not focused:
            # Something else covers the spot we are about to click, so the click would
            # land in that app instead. Give up and let the caller try again later.
            return False
        time.sleep(ACTIVATE_SETTLE)   # Roblox ignores events posted sooner
        post(pt)
        return True
    finally:
        # even on Ctrl-C the cursor and the frontmost app go back where they were
        _warp(orig)
        if prev is not None:
            _activate(prev)


def click_at(point: Tuple[float, float], pid: int, prev_app=None, hold: bool = False) -> bool:
    def post(pt):
        for kind in ("move", "down", "up"):
            _post(_mouse_ev(kind, pt))
            time.sleep(CLICK_GAP)

    return _burst(pid, point, post, prev_app, hold)


# macOS virtual keycodes for the only characters we ever type: a price.
DIGIT_KEYS = {"0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
              "5": 23, "6": 22, "7": 26, "8": 28, "9": 25}
KEY_A = 0
KEY_DELETE = 51
KEY_RIGHT = 124


def _press(keycode: int, char: Optional[str] = None, flags: int = 0) -> None:
    """One key down and up. With char, the keystroke carries the character itself.

    A virtual keycode is interpreted through the current keyboard layout, and a game
    that reads text rather than key presses may ignore it. Attaching the unicode
    string is what a text field actually wants.

    Two events, about 0.17 s with Roblox in front: the holds are 4 ms because the
    events are the cost (see FIELD_CLICK_GAP), not the sleeps.
    """
    for down in (True, False):
        _post(_key_ev(keycode, down, char, flags))
        time.sleep(KEY_HOLD if down else KEY_GAP)


def _key_ev(keycode: int, down: bool, char: Optional[str] = None, flags: int = 0):
    ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    if char is not None:
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(char), char)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    return ev


def type_digits_at(
    point: Tuple[float, float],
    text: str,
    pid: int,
    prev_app=None,
    unicode_first: bool = True,
    hold_ok: bool = False,
    clear: int = 8,
) -> bool:
    """Click a price field, clear it, and type a number into it.

    Clearing is backspaces rather than select-all: a game's own text box need not
    implement cmd-A, and a stray select-all that lands elsewhere is worse than a few
    harmless backspaces on an empty field.

    `clear` is exact: that many RIGHTs, then that many DELETEs, and 0 sends none.
    Callers pass the fewest that are safe for what the box shows, because each key
    is two events at about 85 ms each with Roblox in front (measured live
    2026-09-24). The old fixed 8 and 12 sent 32 to 48 key events, 2.7 to 4 s, even
    into an empty box; a price into a fresh sheet is now the click and 4 events.
    """
    if not text or any(c not in DIGIT_KEYS for c in text):
        return False

    aborted = []

    def ours(pt) -> bool:
        # A typing burst is seconds long. If the user takes the mouse or switches app
        # part-way, the rest of the backspaces and digits would land in their app.
        x, y = _cursor_at()
        if abs(x - pt.x) > 2 or abs(y - pt.y) > 2 or _active_pid() != pid:
            aborted.append(True)
            return False
        return True

    def post(pt):
        # The move matters. Roblox tracks what is under the cursor from mouse-moved
        # events, and a down/up with no move before it does not land on the text box:
        # proved live, the same click focuses the field with a move and not without.
        time.sleep(FIELD_CLICK_GAP)
        for kind in ("move", "down", "up"):
            _post(_mouse_ev(kind, pt))
            time.sleep(FIELD_CLICK_GAP)
        # let the field take focus before typing; longer when a digit comes first
        time.sleep(FIELD_FOCUS_WAIT if clear else FIRST_DIGIT_WAIT)
        # A click lands the caret wherever it was pressed, which on a retry is in the
        # middle of what is already there: backspacing from there deleted one digit
        # and typing re-inserted, turning 65 into 655. Walk to the end first, then
        # clear leftwards, so the field is empty whatever the caret was doing.
        for _ in range(clear):
            if not ours(pt):
                return
            _press(KEY_RIGHT)
        for _ in range(clear):
            if not ours(pt):
                return
            _press(KEY_DELETE)
        for i, ch in enumerate(text):
            if not ours(pt):
                return
            if i:
                time.sleep(DIGIT_GAP)
            _press(DIGIT_KEYS[ch], ch if unicode_first else None)

    return _burst(pid, point, post, prev_app, hold_ok) and not aborted


def scroll_at(point: Tuple[float, float], dy: int, pid: int, prev_app=None, hold: bool = False,
              notches: int = 1) -> bool:
    def post(pt):
        _post(_mouse_ev("move", pt))
        time.sleep(CLICK_GAP)
        for i in range(max(1, notches)):
            if i:
                time.sleep(NOTCH_GAP)
                # A wheel event goes to whatever is under the pointer. If the user has
                # taken the mouse, the rest of the burst would scroll their app.
                x, y = _cursor_at()
                if abs(x - pt.x) > 2 or abs(y - pt.y) > 2:
                    break
            _post(_wheel_ev(dy))
        time.sleep(CLICK_GAP)

    return _burst(pid, point, post, prev_app, hold)


# --------------------------------------------------------------------------- inventory


def read_inventory() -> Optional[dict]:
    """The last inventory snapshot on disk, or None when there is none or it is unreadable."""
    try:
        with open(INVENTORY_PATH) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("items"), list):
        return None
    for item in snap["items"]:
        # snapshots from before fix_number still hold names like 'Charm Pack #T'
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            item["name"] = fix_number(item["name"])
    return snap


def write_inventory(snap: dict) -> None:
    """Replace the snapshot on disk in one step, so a reader never sees half a file."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(INVENTORY_PATH) or ".", prefix=".inventory-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(snap, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, INVENTORY_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def inventory_stats(snapshot: Optional[dict], s: Settings) -> dict:
    """What the crates in a snapshot are worth, per crate name and in total.

    Worth is what they would pay out at the price the bot lists them for, after the
    sale tax. Crates only: the gloves and the rest are never listed by the bot, so
    it has no price of its own for them. A crate the table gives no sell price, or
    does not name at all, is still counted and shown, priced 0. Every total is 0
    before the first scan.
    """
    snap = snapshot or {}
    crates: Dict[str, dict] = {}
    for item in snap.get("items") or []:
        if not item.get("case"):
            continue
        row = crates.setdefault(item.get("name") or "?", {"held": 0, "listed": 0, "unlisted": 0})
        row["held"] += 1
        row["listed" if item.get("listed") else "unlisted"] += 1
    for name, row in crates.items():
        row["price"] = table_sell_price(name, s) or 0
        row["worth"] = row["held"] * payout(row["price"], s.sell_tax)
    return {
        "crates": crates,
        "totals": {
            "cases": sum(r["held"] for r in crates.values()),
            "listed": sum(r["listed"] for r in crates.values()),
            "unlisted": sum(r["unlisted"] for r in crates.values()),
            "worth_after_tax": sum(r["worth"] for r in crates.values()),
            "scanned_at": snap.get("time"),
            "complete": bool(snap.get("complete")),
            "unread": int(snap.get("unread") or 0),
            # when coins came in that no listed crate explained (Bot._note_sold)
            "unsure_since": snap.get("unsure_since"),
        },
    }


def grid_shift(before: List[Line], after: List[Line], most: float) -> Optional[float]:
    """How far the SELL grid moved up between two frames, in capture pixels.

    Everything on the grid moves by the same amount, so every line read with the
    same text at the same x on both frames votes for the gap between its two
    heights. The header holds still and votes about 0, which is ruled out. A grid of
    identical crates repeats its labels a row apart, but a page is at most the view
    less a whole card (_page_notches), 811 px against a 1180 px row pitch on the 2x
    fixtures, so inside 0..most only the real gap collects votes; the serials, which
    never repeat, vote for it as well. At the bottom the grid stops short of the
    page it was asked for, which is why this is measured and not assumed.
    None when nothing matched.
    """
    votes = [
        a.y - b.y
        for a in before
        for b in after
        if a.text == b.text and abs(a.x - b.x) <= 10 and 20 < a.y - b.y <= most
    ]
    if not votes:
        return None
    best = max(votes, key=lambda v: sum(abs(v - w) <= 20 for w in votes))
    near = sorted(w for w in votes if abs(w - best) <= 20)
    return near[len(near) // 2]


def _serials_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Two reads of a serial that could be the same one: equal, truncated, or missing.

    By digits, because Vision drops and swaps the punctuation: '#8982,700' and
    '#8,982,700' are one crate, and comparing the text counted it twice in a scan
    (infinite, 2026-10-02: three Case #1 for two).
    """
    da, db = _serial_digits(a), _serial_digits(b)
    return not da or not db or da.startswith(db) or db.startswith(da)


def same_serial(a: Optional[str], b: Optional[str]) -> bool:
    """Two reads of one serial, for looking it up in the ledger: equal digits, or one cut short.

    Digits only, because Vision reads the commas as full stops now and then
    ('#3.821.642' for '#3,821,642'), and a prefix either way, because it truncates
    (_serials_agree). Unlike _serials_agree a missing serial matches nothing: a
    crate with no serial read must never take some other crate's cost or price.
    """
    a, b = _serial_digits(a), _serial_digits(b)
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _serial_digits(serial: Optional[str]) -> str:
    return re.sub(r"\D", "", serial or "")


# Words on the game's bottom bar (LOADOUT, INVENTORY, GAMEMODES, PLAY, SHOP, MARKET,
# PROGRESSION), matched inside a line because Vision glues the icons on ('F LOADOUT').
BOTTOM_BAR = ("LOADOUT", "INVENTORY", "GAMEMODES", "PROGRESSION")
# The most crates one coin rise is booked as. The bot reads the coins every few
# seconds, so more than this at once is more likely coins from something else.
SURE_MIX = 3
# After a rise nothing on record explains, how long to let a burst of sales land
# before counting the inventory again to see which crates went (Bot.recount).
RECOUNT_WAIT = 120.0


def vanished(old: List[dict], new: List[dict]) -> List[dict]:
    """The listed crates of an old snapshot that a newer scan no longer holds.

    By serial, and never more of a crate than its count fell by: a serial Vision
    did not read this time is still a crate on the grid, not a sale.
    """
    gone = []
    for name in {i.get("name") for i in old}:
        now = [i for i in new if i.get("name") == name]
        room = sum(1 for i in old if i.get("name") == name) - len(now)
        missing = [i for i in old if i.get("name") == name and i.get("listed") and i.get("case")
                   and not any(same_serial(i.get("serial"), n.get("serial")) for n in now)]
        gone += missing[:max(0, room)]
    return gone


def fewest_crates(gained: int, have: Dict[int, int]) -> Optional[Dict[int, int]]:
    """How many crates of each payout add up to exactly gained: the mix with the fewest
    crates, or None when no mix does.

    have is {payout: how many listed crates pay it}. The payouts are few, a price or
    two per crate in the table, so it walks them one at a time keeping, for every sum
    it can reach, the fewest crates that make it: a bounded subset sum, and exact,
    because payout() is what the game pays. The greedy largest-first match it
    replaced took a Viridian's 58 out of a 72 rise, found nothing to fit the 14
    left, and called the rise unexplained when two Glove Cases at 36 had sold.
    """
    if gained <= 0:
        return None
    best: Dict[int, Tuple[int, Dict[int, int]]] = {0: (0, {})}     # sum: (crates, mix)
    for pay, n in sorted(have.items()):
        if pay <= 0:
            continue
        grown = dict(best)
        for total, (size, mix) in best.items():
            for k in range(1, min(n, (gained - total) // pay) + 1):
                reach = total + k * pay
                if reach not in grown or size + k < grown[reach][0]:
                    grown[reach] = (size + k, {**mix, pay: k})
        best = grown
    got = best.get(gained)
    return got[1] if got else None


# --------------------------------------------------------------------------- bot


BLOCK_SECONDS = 60.0
# Held by every pass that drives the Roblox window: a run's cycle(), a stopped-bot
# scan_once(). There is one window and one cursor, and two bots clicking it at once
# would each read frames the other had just changed.
WINDOW_LOCK = threading.Lock()
# The market in any of the shapes that carry the coin counter and the grid.
MARKET_STATES = ("market_ready", "market_nofilter", "cooldown")
# Measured live 2026-09-19: one scroll request of 350 moves the grid about 200 px of
# a 2168 px capture, a VIEW button sits about 860 px below its own title, and a row
# pitch is about 1000 px. So reaching a row-2 button took two scrolls from one
# position and can take five from the worst one. The old code scrolled once and gave
# up, which is why row 2 was never buyable.
SCROLL_STEP = 350
# Two notches a step: two is the fewest a row-2 button ever needed, and even the
# worst case (five) overshoots by only one notch, which still leaves the title on
# screen for the rematch.
SCROLL_NOTCHES = 2
SCROLL_TRIES = 4
# Live 2026-09-26 01:57-02:03: the one-go estimate sent up to 8 notches at once on
# the MARKET grid, and a second-row crate went off the top of the view every time
# (18 misses in a row on one Finishline Case), while the same buy worked whenever
# infinite moved the mouse, which cuts a burst short. One notch is ~200 px, but a
# run of them was only ever measured on the SELL grid; the market grid evidently
# gathers speed. So never more than two at a time there, and look after each.
MARKET_BURST_MAX = 2
# The filter column is taller than a grid row, so it gets its own budget. Measured
# live 2026-09-20 from the top of the column: 200 px per scroll and 8 scrolls before
# APPLY FILTER appears, which is why 6 kept falling one short.
# Overshooting is harmless there, the column stops at its bottom where the button is,
# so it takes the whole distance in one grab.
FILTER_NOTCHES = 10
FILTER_SCROLL_TRIES = 4
# The SELL grid pages down three notches at a time: a card is title-to-button about
# 800 px tall in a 1540 px view, so a bigger step could carry a row past unseen.
PAGE_NOTCHES = 3
SWEEP_SCROLLS = 60        # pages per basket before a sweep gives up looking
MAX_BASKETS = 40          # ponytail: 200 crates at 5 a basket; raise if the inventory is bigger
RECONNECT_EVERY = 15.0    # seconds between Reconnect presses while Roblox keeps failing
SWEEP_RETRY = 300.0       # a sweep that did not reach the bottom is tried again this often
# Roblox forgets the market filter on a rejoin; Settings.filter_* say what to put back.
FILTER_RETRY = 120.0      # at most one attempt at restoring it this often
# One-second reads to wait for the coin counter to catch up after CONFIRM.
VERIFY_READS = 8
# How long to sit between balance checks while waiting for listings to sell.
WAIT_FOR_SALES = 30.0
# Roblox kicks an idle player after about twenty minutes, so a waiting bot has to
# give it something. Well inside that, and a refresh click is harmless.
POKE_EVERY = 240.0
# Where across the window the market grid is scrolled from. The purchase window
# scrolls with any wheel over its footprint, even while it is closed, and then opens
# with its heading and name scrolled off the top: the bot's own row-2 scroll at the
# grid's centre did that (infinite, 2026-09-30). Measured live the same night: 3
# notches at 93% moved the grid and the next window opened whole, heading and all.
MARKET_WHEEL_X = 0.93
# Notches up that bring a scrolled purchase window back to its top: it scrolls at
# most ~600 px, 3 notches at 2x, and more is harmless against the top.
MODAL_UNSCROLL = 5
# Cards that make two full rows of the market grid: with fewer there is no third row
# to look at (Bot.peek_below).
PEEK_MIN_CARDS = 10
# Retries for a window capture that comes back empty even though the window is there.
GRAB_TRIES = 5
# Then how long to read the window's screen area instead before trying it again.
REGION_SPELL = 60.0
# What a refused window capture with no screen area either means, in words to act on.
# macOS 26 asks the app that started the bot, again every so often, to "bypass the
# system private window picker", on top of the Screen Recording list in Settings, and
# blocks every capture until that prompt is answered (infinite, 2026-10-02: Terminal
# on the list, the prompt up, and the log said only "Roblox window gone").
CAPTURE_BLOCKED = (
    "macOS is blocking screen capture. If it shows a prompt about Terminal recording your "
    "screen, click Allow: being on the list in System Settings is not enough on its own")
# How many purchases in a row may go unconfirmed before the bot stops itself. Losing
# a listing to another buyer is ordinary, so this is a failsafe against a broken bot,
# not a reaction to a normal miss.
UNVERIFIED_LIMIT = 5
# A CONFIRM that the dialog did not take is pressed again this many times, after
# waiting this long for the dialog to close (captures run ~0.3 s inside a held buy).
CONFIRM_RETRIES = 2
CONFIRM_SETTLE = 1.2
# Gap between polls while waiting for the pill to light.
POLL_SLEEP = 0.2
# How many of those polls may skip the OCR pass. During the countdown the only
# question asked several times a second is whether the pill has lit, and colour
# answers that from the capture alone: 113 ms against the 189 ms a full read costs
# (measured live 2026-09-20). At two colour polls between full reads the OCR pass
# still comes round about once a second, so a bargain listed mid-countdown is seen
# inside that second, which is what this must not trade away.
COLOUR_ONLY_POLLS = 2
# Ceilings on the three lists the bot keeps in memory. None of them is read further
# back than the last few entries: clicks is a debugging tail, history is what the
# window shows and what paid_for looks up (with purchases.jsonl behind it for
# anything older), and both grew without limit for as long as the bot ran.
MAX_CLICKS = 500
MAX_HISTORY = 500
# How often the loop writes its own memory use to the log. The night that died left
# no evidence at all, so a run now says every ten minutes how big it has got and how
# big the three bounded containers are.
HEARTBEAT_EVERY = 600.0


def _listing_key(card: "Card") -> tuple:
    return (card.name, card.price, card.serial)


class Bot:
    """One market cycle at a time. Every screen read is fresh; nothing is cached."""

    # Matched as patterns, not substrings. Adding a bare "SELL" for the tab made
    # every "Seller: someone" line clickable, which a test caught: a card's seller
    # name is not a button and must never be clicked.
    CLICKABLE = (
        r"\bFILTER\b",      # APPLY FILTER, including the garbled countdown variants
        r"\bVIEW\b",       # Vision glues neighbouring glyphs on, e.g. '-> VIEW'

        r"^\w*RCHASE$",     # PURCHASE and the truncated RCHASE, never 'Confirm Purchase'
        r"^CONFI\w*$",      # CONFIRM, and the CONFIRIG and CONFIEM it also returns
        r"^CLOSE$",
        r"^CANCEL$",
        r"^SELL$",
        r"^SELL NOW$",
        r"^SELL ?ITEMS$",   # Vision drops the space on this one
        r"^REMOVE$",        # takes a crate back out of the basket, never off the market
        r"^MARKET$",
        r"^NEWEST$",
        r"^QUALITY$",
        r"^PRICE FIELD$",
        r"^RECONNECT$",
        r"^ACCEPT$",        # a trading server's rules
        r"^SHOP ICON$",     # the trading hub's market icon, found by colour (find_hub_shop)
        r"^CATEGORY$",      # the SELL tab's dropdowns, relabelled: their text is whatever is picked
        r"^SORT$",
        r"^SORT ARROW$",
        r"^MISCELLANEOUS$",
        r"^RARITY$",        # the market filter's rarity dropdown, relabelled; its option
    )                       # (Settings.filter_rarity) is allowed only while picking it

    def __init__(self, settings: Settings, on_status=None, on_bounds=None):
        self.s = settings
        self.stop_event = threading.Event()
        self.on_status = on_status or (lambda msg: None)
        self.on_bounds = on_bounds or (lambda bounds: None)
        self.history: List[dict] = []
        # How many records have already been dropped off the front of history, so a
        # consumer that walks it by index (app.py) can tell a trim from a rewind.
        self.history_dropped = 0
        self.clicks: List[str] = []
        self.out_of_funds = False
        self._pending_sale = None
        self._unsold = 0
        self._nothing_to_sell = False
        self._waiting_since = None
        self._last_poke = 0.0
        self._last_reconnect = 0.0
        self._swept = False
        self._carried = False
        self._focused = False
        self._focus_since = 0.0
        self._idle_wait = None
        self._last_sweep = 0.0
        self._last_filter_fix = 0.0
        self._counter_key = None
        self._counter_digits = None
        self._filter_off_frames = 0
        self._filter_fails = 0
        self._picking = None
        self._sweep_complete = False
        self._page_failed = False
        self._last_heartbeat = time.time()
        self._unknown_frames = 0
        self._last_log = None
        self._log_repeats = 0
        self._hold = False
        # Where APPLY FILTER was on the last frame that could read it, so the
        # countdown can be watched by colour without an OCR pass. A Line of floats,
        # never an image: nothing on the bot may hold a CG object.
        self._pill_box = None
        self._colour_claim = None
        self._colour_only = True
        self.bought = 0
        self.spent = 0
        # What the GUI reads, all plain values the bot thread replaces rather than
        # edits in place, so a read from the Tk thread never sees one half-changed.
        # inventory is the snapshot (scan_inventory), balance the last trusted coin
        # count, sales_income the coins sales have brought in this run.
        self.inventory: Optional[dict] = None
        self.balance: Optional[int] = None
        self.sales_income = 0
        self.listed_count = 0
        # What the crates sold this run made: each one's payout after tax less what
        # was paid for it (see _book_sold). A sale whose cost is not on record counts
        # in sold_count and unknown_cost_count, and is left out of profit rather than
        # booked as all profit.
        self.profit = 0.0
        self.sold_count = 0
        self.unknown_cost_count = 0
        self._balance_seen: Optional[int] = None
        # Scan at the start of a run instead of carrying on from the last snapshot.
        self.scan_on_start = False
        self._scan_wanted = threading.Event()
        self._sell_wanted = threading.Event()      # the Sell all cases button, mid-run
        # Coins from rises no listed crate explained, and when to recount for them.
        self._unbooked = 0
        self._recount_at: Optional[float] = None
        self.scanning = False
        self._begun = False
        self.pid = None
        self.wid = None
        self.bounds = None
        self.bounds_moved = False
        self.scale = 1.0
        # seams for tests: swap these, never the module functions
        self.find_window = find_roblox
        self.grab = grab
        self.grab_region = grab_region
        self.topmost_at = _topmost_pid_at
        # Until when frames come from the screen area rather than the window (_grab).
        self._region_until = 0.0
        self._capture_refused = False      # the window capture was refused outright
        self._blocked_alerted = False      # the Mac has been told about this outage
        self.ocr = ocr
        self._ocr_said = None       # the chip reading the screen, as last logged (Windows)
        self._read_secs = 0.0       # time in full reads since the last heartbeat, and how many
        self._reads = 0
        self._read_avg = 0.0        # a running average of one full read, for _await_screen
        self._last_key = None       # frame_key of the last frame read, kept only when reads are slow
        self.click_at = click_at
        self.scroll_at = scroll_at
        self.button_ready = button_ready
        self.bring_forward = bring_forward
        self.type_digits_at = type_digits_at
        self.wait_idle = wait_idle
        self.sleep = time.sleep
        self.state_of = ui_state
        self._last_refresh = 0.0
        self._peek_due = False      # rows below the first screen not yet looked at this refresh
        self._blocked: dict = {}
        self._unverified = 0

    # -- plumbing

    def log(self, msg: str) -> None:
        """Emit a line, collapsing a run of identical ones into a single count.

        The refresh click repeats for as long as nothing qualifies, which buried
        everything worth reading under hundreds of identical lines.
        """
        if msg == self._last_log:
            self._log_repeats += 1
            return
        self._flush_repeats()
        self._last_log = msg
        self._emit(msg)

    def heartbeat(self) -> None:
        """Say how much memory the run is using, every HEARTBEAT_EVERY seconds.

        Peak rather than current, because ru_maxrss is free and monotonic: if the
        morning line reads the same as the evening one, nothing grew overnight.
        """
        if time.time() - self._last_heartbeat < HEARTBEAT_EVERY:
            return
        self._last_heartbeat = time.time()
        peak = _peak_mb()
        # The read time says whether a slow bot is a slow reader: about 0.2 s on a Mac,
        # 0.7 s on a Windows graphics card, 3 to 4 s on a Windows CPU.
        reads = ", reads %.1f s each" % (self._read_secs / self._reads) if self._reads else ""
        self._read_secs, self._reads = 0.0, 0
        self.log(
            "peak memory %.0f MB after %d buys: clicks %d, history %d, parked %d%s"
            % (peak, self.bought, len(self.clicks), len(self.history), len(self._blocked), reads)
        )

    def _flush_repeats(self) -> None:
        if self._log_repeats and self._last_log is not None:
            self._emit("%s  (x%d)" % (self._last_log, self._log_repeats + 1))
        self._log_repeats = 0

    def _emit(self, msg: str) -> None:
        # To a file as well as the window. An overnight run that ends badly takes its
        # window with it, and the morning after there was nothing at all to read:
        # no crash report, no log, only a purchase ledger that stopped.
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        self.on_status(msg)

    def attach(self) -> None:
        if not WINDOWS and not Quartz.CGPreflightScreenCaptureAccess():
            # macOS asks for Screen Recording again now and then, and until it is
            # allowed every capture fails or hangs: a scan sat stuck for minutes
            # (infinite, 2026-09-30). So ask now, at Start, and stop with a reason.
            Quartz.CGRequestScreenCaptureAccess()
            raise RuntimeError(
                "macOS has not allowed this app to record the screen. Allow it in the prompt, "
                "or in System Settings > Privacy & Security > Screen & System Audio Recording, "
                "then press Start again")
        self.pid, self.wid, self.bounds = self.find_window()
        self.log("roblox pid=%s window=%s bounds=%s" % (self.pid, self.wid, self.bounds))

    def begin(self) -> None:
        """What a run does once attached: pick up the last snapshot, and scan if asked.

        The snapshot is loaded either way, so a scan that fails still leaves the
        last good count on show. cycle() calls this itself on its first pass, so a
        loop that drives cycle() directly (app.py does) gets it too.
        """
        self._begun = True
        if WINDOWS:
            # here, not in run(): the window drives cycle() and never calls run()
            self.log("text reader: %s" % ocr_engine())
            self._ocr_said = _DEVICE[0]
        self.load_inventory()
        if self.scan_on_start:
            self.request_scan()
        if self.s.focus_mode:
            self.start_focus()

    def start_focus(self) -> None:
        """Roblox focused mode: bring Roblox forward once and keep it there.

        Every burst then takes the held path: no activation, no hand-back, no idle
        wait, and so none of the ~0.9 s a capture loses after each app switch
        (measured 2026-09-23). The price is that nobody else can use the machine, so
        the run watches for a person: the mouse moving off where the bot left it, or
        another app coming to the front, pauses it (focus_ok).
        """
        self.bring_forward(self.pid)
        self._hold = True
        self._focused = True
        self._focus_since = time.time()
        self._idle_wait = self.wait_idle
        self.wait_idle = lambda s, stop_event=None: self.focus_ok()
        self.log("Roblox focused mode: moving the mouse pauses the bot, Stop stops it")

    def focus_ok(self) -> bool:
        """In focused mode: pause while a person uses the machine, go on once they rest.

        infinite, 2026-09-29: moving the mouse used to stop the whole run, and a
        night ended that way when he came to look at a disconnect. Now it waits for
        the mouse and keys to rest for idle_seconds (the polite mode's own gate),
        brings Roblox back to the front and carries on. Only Stop stops it: False
        then, and only then.
        """
        if not self._focused:
            return not self.stop_event.is_set()
        why = self._person_is_back()
        if why:
            self.log("focused mode: %s, paused until the mouse and keys rest" % why)
            if not self._idle_wait(self.s, self.stop_event):
                return False
            self.bring_forward(self.pid)
            self._focus_since = time.time()      # a fresh grace, as at the start
            self.log("focused mode: resumed")
        return not self.stop_event.is_set()

    def _person_is_back(self) -> Optional[str]:
        """Why a person seems to be at the machine, or None.

        A hand on the mouse or keys, never where the pointer is. Roblox moves the
        pointer itself when its window reloads, and with nobody there that stopped
        two runs on 2026-09-29, at a relaunch (01:47) and after a disconnect (02:53).
        A warp is no input event and the bot's own posts are excused (LAST_POST), so
        input newer than both the bot's last post and the grace is a person.
        """
        if time.time() - self._focus_since < FOCUS_GRACE:
            return None                  # the hand that pressed Start is still leaving
        since = max(LAST_POST[0], self._focus_since + FOCUS_GRACE)
        if time.time() - _input_idle() > since + POST_SLACK:
            return "the mouse or keyboard was used"
        front = _active_pid()
        if front is not None and self.pid is not None and front != self.pid:
            return "another app came to the front"
        return None

    def end_focus(self) -> None:
        """Back to the polite mode: every burst gated, activated and handed back."""
        if not self._focused:
            return
        self._focused = False
        self._hold = False
        self.wait_idle = self._idle_wait

    def request_scan(self) -> bool:
        """Scan the inventory at the top of the next cycle. Safe from any thread.

        False, and nothing queued, while a scan is already running: a second press
        would only walk the whole grid again the moment the first one finished.
        """
        if self.scanning:
            self.log("a scan is already running, ignoring the request")
            return False
        self._scan_wanted.set()
        return True

    def request_sell_all(self) -> None:
        """List every sellable crate at the top of the next cycle. Safe from any thread."""
        self._sell_wanted.set()

    def read(self):
        """One frame: fresh bounds, a capture, and the OCR of it.

        The whole thing sits in an autorelease pool. This runs in a worker thread with
        no runloop, so nothing drains the pool these frameworks autorelease into, and
        an overnight run grew by gigabytes until the process died.
        """
        t = time.time()
        with _pool():
            frame = self._read_frame()
            took = time.time() - t
            # From 0, and each read capped at twice the line: the first read's model
            # load, or one capture that hung, can never mark a fast machine slow alone.
            self._read_avg = 0.8 * self._read_avg + 0.2 * min(took, 2 * SLOW_READ)
            # a key, never the image: nothing on the bot may hold a capture
            self._last_key = frame_key(frame[0]) if self._read_avg >= SLOW_READ else None
        self._read_secs += took
        self._reads += 1
        return frame

    def _read_frame(self):
        # the window list query costs ~2 ms: re-read the bounds every frame, otherwise
        # a window the user moved keeps capturing fine while every click goes stale.
        prev = self.bounds
        self.pid, self.wid, self.bounds = self.find_window()
        self.bounds_moved = bounds_differ(prev, self.bounds)
        if self.bounds_moved:
            self.log("roblox window moved to %s" % (self.bounds,))
            self.on_bounds(self.bounds)
            # a remembered pill box is in the old capture's pixels, so it is now a
            # box over whatever the window has been resized around
            self._pill_box = None
            self._colour_claim = None
        # The window server intermittently hands back nothing for a window that is
        # plainly on screen, measured at two failures in three calls while another
        # process was capturing the same window. One miss is not a missing window.
        img = self._grab()
        if img is None:
            if self._capture_refused and not self._blocked_alerted:
                # once an outage: the run retries by itself, a person may have to act
                self._blocked_alerted = True
                notify(CAPTURE_BLOCKED)
            raise RuntimeError(CAPTURE_BLOCKED if self._capture_refused
                               else "Roblox window gone (capture returned nothing)")
        self._blocked_alerted = False
        lines = self.ocr(img)
        # Only a read loads the models, so only now is it known whether the card really
        # took them: the text reader line at the start can say graphics card and still
        # be the CPU, which is said here as soon as it is known.
        if WINDOWS and _DEVICE[0] != self._ocr_said:
            self._ocr_said = _DEVICE[0]
            self.log("reading the screen on the %s" % self._ocr_said)
        lines = self._voted_counter(img, lines)
        if self.bounds and self.bounds["Width"]:
            self.scale = img_size(img)[0] / float(self.bounds["Width"])
        af = find_text(lines, "APPLY FILTER")
        if af is not None:
            # Only the frames that read the whole label. Mid-countdown Vision returns
            # the pill as 'AP', '10' or '(FILTER', whose boxes are a fraction of the
            # button, and sampling one of those would be sampling the wrong rectangle.
            self._pill_box = af
        self._audit_colour_claim(img, af)
        return img, lines

    def _grab(self):
        """This frame: the window's own capture, else the screen area it covers.

        screencapture -l has stopped returning a window that is plainly open and in
        the window list, for good until Roblox restarts (infinite, 2026-09-29, the
        fourth time). The screen area gives the same pixels while Roblox is the
        window on top there, so after GRAB_TRIES misses the bot reads that for
        REGION_SPELL, then tries the window again. Only while Roblox is on top at
        the window's centre: otherwise the area shows somebody else's window.
        """
        if time.time() >= self._region_until:
            self._capture_refused = False
            for _ in range(GRAB_TRIES):
                GRAB_ERROR[0] = ""          # this try's answer, never an older one
                img = self.grab(self.wid)
                if img is not None and img_size(img)[0] > 0:
                    return img
                if "could not create image" in GRAB_ERROR[0]:
                    self._capture_refused = True
                    break           # refused outright, ~1 s a try: no use asking again
                self.sleep(0.2)
            self.log("the Roblox window will not capture (%s): reading its screen area instead"
                     % (GRAB_ERROR[0] or "nothing came back"))
            self._region_until = time.time() + REGION_SPELL
        b = self.bounds
        centre = (b["X"] + b["Width"] / 2.0, b["Y"] + b["Height"] / 2.0)
        if self.topmost_at(centre, ignore_pid=os.getpid()) != self.pid:
            return None
        img = self.grab_region(b)
        return img if img is not None and img_size(img)[0] > 0 else None

    def _voted_counter(self, img, lines):
        """Replace the frame's coin-counter read with a majority vote (vote_counter).

        The vote costs three small OCR passes, about 0.14 s, so it is cached against
        a coarse brightness print of the strip and redone only when the counter's
        pixels change, which is only when coins move.
        """
        box = counter_strip(lines)
        if box is None or img is None:
            return lines
        try:
            g = _luma_grid(img, box[0], box[1], box[2], box[3], 48, 6)
            key = tuple(int(v) // 8 for row in g for v in row)
        except Exception:
            key = None
        if key is None or key != self._counter_key:
            self._counter_digits = vote_counter(img, lines)
            self._counter_key = key
        return fix_counter_line(lines, self._counter_digits)

    def _audit_colour_claim(self, img, af) -> None:
        """Hold the cheap poll to what a full read says, and drop it if it lies.

        Only one direction is evidence. A colour poll that said dark and a full read
        that says lit is the pill lighting in the 0.2 s between them, which is the
        whole point of watching it. A colour poll that said lit over a pill a full
        read finds dark cannot be time passing: the pill only goes dark when the
        refresh is clicked, and that happens after a full read. So that way round
        means the box is stale, most likely because the filter panel scrolled, and
        from then on every poll pays for the OCR again.
        """
        claim, self._colour_claim = self._colour_claim, None
        if not claim or af is None or not self._colour_only:
            return
        if not self.button_ready(img, af):
            self._colour_only = False
            self.log(
                "the colour poll called the pill lit and a full read says it is dark: "
                "back to a full read every poll"
            )

    def pill_lit(self) -> Optional[bool]:
        """Is APPLY FILTER lit, from its colour alone, with no OCR pass.

        None means 'ask a full read': there is no trustworthy box to sample, the
        window moved, or the capture came back empty. This never gates a click. The
        refresh still reads the screen and checks the colour again before clicking,
        so a wrong 'lit' costs one read and a wrong 'dark' costs at most the polls
        until the next full read.
        """
        if self._pill_box is None or not self._colour_only:
            return None
        with _pool():
            # Deliberately not assigning self.bounds: a full read owns that, and
            # quietly updating it here would swallow the move that bounds_moved
            # exists to announce, which is what stops a click going to a stale point.
            try:
                _, wid, bounds = self.find_window()
            except RuntimeError:
                return None
            if bounds_differ(self.bounds, bounds):
                return None
            img = self.grab(wid)
            if img is None or img_size(img)[0] <= 0:
                return None
            lit = bool(self.button_ready(img, self._pill_box))
            del img          # the 8 MB buffer goes now, inside the pool, not later
        self._colour_claim = lit
        return lit

    def cooldown_wait(self) -> None:
        """Sit out the countdown without paying for an OCR pass every poll.

        The pill is dark, the cards were read a moment ago, and the question asked
        several times a second is only whether the pill has lit. Colour answers that
        from the capture alone: 113 ms against the 189 ms a full read costs, measured
        live 2026-09-20. Over a 7 s countdown, CPU including the capture subprocess:

            png frame, a full read every poll     2.75 s   an OCR pass every 0.49 s
            tiff frame, a full read every poll    2.03 s   an OCR pass every 0.40 s
            tiff frame, colour-only polls         1.29 s   an OCR pass every 1.00 s

        That last row is the trade, stated plainly: half the CPU, and a listing that
        appears mid-countdown is seen within a second instead of within half of one.
        The refresh itself fires sooner than before, because a lit pill ends the wait
        rather than waiting out the sleep and then reading.
        """
        self.sleep(POLL_SLEEP)
        for _ in range(COLOUR_ONLY_POLLS):
            if self.stop_event.is_set():
                return
            lit = self.pill_lit()
            # Lit, or nothing worth trusting: either way the next full read decides.
            if lit is None or lit:
                return
            self.sleep(POLL_SLEEP)

    @contextlib.contextmanager
    def holding_focus(self):
        """Keep Roblox in front for a whole purchase.

        Every click used to activate Roblox and hand focus back, so buying cost three
        round trips between spotting the listing and confirming it, and a bargain can
        be taken by someone else in that time. Idleness is checked once here rather
        than before each click, because the sequence is already under way.
        """
        if self._hold:
            yield True
            return
        if not self.wait_idle(self.s, self.stop_event):
            yield False
            return
        cursor = _cursor_point()
        prev = self.bring_forward(self.pid)
        self._hold = True
        try:
            yield True
        finally:
            self._hold = False
            hand_back(prev, cursor)

    def click_line(self, line: Line, reason: str) -> bool:
        label = line.text.strip()
        rarity = (self.s.filter_rarity or "").strip().upper()
        if not any(re.search(k, label.upper()) for k in self.CLICKABLE) and not (
            rarity and label.upper() == rarity and self._picking == "RARITY"
        ):
            self.log("refusing to click %r" % label)
            return False
        if not self._hold and not self.wait_idle(self.s, self.stop_event):
            return False
        pt = to_global(self.bounds, self.scale, line)
        if self.click_at(pt, self.pid, hold=self._hold) is False:
            # Roblox never came forward, so nothing was clicked. Say so rather than
            # recording a click that the button never received.
            self.log("Roblox did not come forward, %r not clicked" % label)
            return False
        self.clicks.append(label)
        # A refresh click every few seconds is tens of thousands of labels over a
        # night, and nothing ever reads further back than the last handful. Keep a
        # tail, in place, so it stays the plain list the tests compare against.
        if len(self.clicks) > MAX_CLICKS:
            del self.clicks[: len(self.clicks) - MAX_CLICKS]
        self.log("click %r at %.0f,%.0f (%s)" % (label, pt[0], pt[1], reason))
        return True

    def _await_screen(self) -> None:
        """With a slow reader, let a click's change draw before the full read.

        A friend's Windows laptop read a screen in 1.8 s, and the first read after a
        click captured the screen from before it, the buy window still opening: thrown
        away, then read again. VIEW to PURCHASE took 4 s there against 1 s on a Mac.
        Only after VIEW (wait_for settle=True): after PURCHASE the first read already
        caught the confirm box, a dialog too small to count as a change here.
        Cheap looks (a capture and 576 brightness cells, no text) wait for the screen to
        change and stop moving, for at most SETTLE_MAX. A fast reader skips all this.
        """
        if self._read_avg < SLOW_READ or self._last_key is None:
            return
        deadline = time.time() + SETTLE_MAX
        prev, moved = self._last_key, False
        while time.time() < deadline and not self.stop_event.is_set():
            self.sleep(SETTLE_POLL)
            try:
                img = self._grab()
            except Exception:
                return
            if img is None:
                return
            key = frame_key(img)
            del img
            if moved and cells_changed(prev, key) < 0.02:
                return              # changed, and now holding still
            if cells_changed(self._last_key, key) >= 0.05:
                moved = True
            prev = key

    def wait_for(self, want, timeout=5.0, settle=False):
        """Poll fresh screens until ui_state is in `want`. Returns (state, img, lines).

        settle: let the screen change before the first read, for a slow reader.
        """
        if settle:
            self._await_screen()
        deadline = time.time() + timeout
        img = lines = None
        while True:
            img, lines = self.read()
            st = self.state_of(lines, img)
            if st in want:
                return st, img, lines
            if time.time() >= deadline or self.stop_event.is_set():
                return st, img, lines
            # A read already costs about a third of a second, which paces this loop
            # on its own. Any extra wait here is pure delay between the click and
            # noticing the dialog it opened.
            self.sleep(0.02)

    # -- steps

    def recover(self, lines, state) -> None:
        target = None
        if state == "confirm":
            target = find_button(lines, "CANCEL")
        if target is None:
            target = parse_modal(lines).get("close_line")
        if target is not None:
            self.click_line(target, "recover from unexpected %s" % state)
        else:
            self.log("stuck in %s, no CANCEL/CLOSE found" % state)

    def refresh(self, lines) -> None:
        if find_text(lines, "APPLY FILTER") is None:
            self.sleep(POLL_SLEEP)
            return
        since = time.time() - self._last_refresh
        if since < self.s.refresh_cooldown:
            self.sleep(min(1.0, self.s.refresh_cooldown - since))
            return
        # idle first, then read, then click: the wait can last many seconds and the
        # button position (and the whole panel) can scroll in the meantime.
        if not self.wait_idle(self.s, self.stop_event):
            return
        img, lines = self.read()
        af = find_text(lines, "APPLY FILTER")
        if af is None or self.bounds_moved:
            return
        if not self.button_ready(img, af):
            # still counting down: the pill is dark even though the text reads
            self.cooldown_wait()
            return
        if not self.click_line(af, "refresh listings"):
            return
        self._last_refresh = time.time()
        self._peek_due = True
        deadline = time.time() + 20.0
        while time.time() < deadline and not self.stop_event.is_set():
            self.sleep(0.5)
            img, lines = self.read()
            st = self.state_of(lines, img)
            if st in ("market_ready", "cooldown") and parse_cards(lines, 0):
                return
        self.log("refresh did not settle in 20 s")

    def _is_parked(self, card: Card, now: float) -> bool:
        """Whether this listing was parked in the last BLOCK_SECONDS.

        Same name and price, and a serial that matches allowing for truncation:
        Vision cuts the same serial short differently from frame to frame, and an
        exact match let a parked listing come straight back under a new spelling.
        """
        for (name, price, serial), at in self._blocked.items():
            if now - at >= BLOCK_SECONDS or name != card.name or price != card.price:
                continue
            if not serial or not card.serial:
                return True
            if serial.startswith(card.serial) or card.serial.startswith(serial):
                return True
        return False

    def _block(self, card: Card, why: str) -> None:
        """Park a listing we could not act on, so the next cycle tries something else."""
        now = time.time()
        # Drop the parks that have already expired. _pick only ever asks about the
        # last BLOCK_SECONDS, so an entry older than that is dead weight, and the
        # market turns over new listings all night: every one that got parked stayed
        # in this dict forever.
        for key in [k for k, at in self._blocked.items() if now - at >= BLOCK_SECONDS]:
            del self._blocked[key]
        self._blocked[_listing_key(card)] = now
        self.log("parking %s @%s for %ds: %s" % (card.name, card.price, BLOCK_SECONDS, why))

    def _pick(self, lines, balance) -> Optional[Card]:
        if balance is None:
            # an unreadable balance also disables the affordability check, so it is a
            # hard stop for buying rather than a reason to guess.
            self.log("balance unreadable, not buying this cycle")
            return None
        cards = parse_cards(lines, 0)
        now = time.time()
        # the crate higher in the table first, then the cheaper listing of it
        for card in sorted(cards, key=lambda c: (crate_rank(c.name, self.s), c.price is None, c.price or 0)):
            ok, why = decide(card, self.s)
            if not ok:
                continue
            if self._is_parked(card, now):
                continue
            if card.price is not None and balance < card.price:
                self.log("skip %s @%s: balance %s" % (card.name, card.price, balance))
                continue
            if (
                not self.s.dynamic_spend
                and self.spent + (card.price or 0) > self.s.max_spend
            ):
                self.log("skip %s @%s: would pass max_spend" % (card.name, card.price))
                continue
            self.log("candidate %s @%s (%s)" % (card.name, card.price, why))
            return card
        return None

    def _rematch(self, lines, card: Card) -> Optional[Card]:
        # Vision truncates serials differently between frames ('#3,606,883' one read,
        # '#3,606' the next), so match on the digits both reads agree on.
        # ponytail: name + serial prefix is the identity. Ceiling: two listings of the
        # same item whose serials share a prefix would be confused. Upgrade path:
        # add the seller, which we already parse.
        same_name = [c for c in parse_cards(lines, 0) if c.name == card.name]
        if card.serial:
            exact = [c for c in same_name if c.serial == card.serial]
            if len(exact) == 1:
                return exact[0]
            # Three Glove Cases on one fixture share the prefix '#3,784', so a
            # truncated read prefix-matches several and taking the first returns a
            # different listing at a different price. Only an unambiguous match, or
            # one pinned to the same column, is good enough to click.
            near = [
                c
                for c in same_name
                if c.serial
                and (c.serial.startswith(card.serial) or card.serial.startswith(c.serial))
            ]
            if len(near) == 1:
                return near[0]
            if near:
                in_column = [
                    c for c in near if _overlap(c.title_line, card.title_line) > 0
                ]
                return in_column[0] if len(in_column) == 1 else None
        # A serial that disagrees means a different listing, so only cards that do not
        # contradict ours may be matched on the weaker signals below.
        compatible = [c for c in same_name if not (c.serial and card.serial)]
        for c in compatible:
            if c.price is not None and c.price == card.price and _overlap(
                c.title_line, card.title_line
            ) > 0:
                return c
        # Measured 2026-09-19: right after a scroll, Vision sometimes drops a card's
        # serial AND its price for a frame while its title and column stay perfectly
        # readable. Both passes above then miss and the card reads as vanished, which
        # is what kept row-two listings from ever being bought. A unique name in the
        # same column is enough to click VIEW, because the modal check is the gate
        # that actually decides: it re-reads the title and price and backs out if
        # either moved.
        column = [c for c in compatible if _overlap(c.title_line, card.title_line) > 0]
        if len(column) == 1 and column[0].price in (None, card.price):
            return column[0]
        return None

    def _filter_panel_centre(self, lines) -> Optional[Line]:
        """A point inside the left filter column, to scroll it.

        Anchored on the panel's own labels rather than a fraction of the window, so a
        resized window still hits the panel and never the grid.
        """
        anchors = [
            find_text(lines, k)
            for k in ("Search for Items", "Price", "Wear", "Rarity", "Pattern", "Collection")
        ]
        anchors = [a for a in anchors if a is not None]
        if not anchors:
            return None
        cx = sum(a.cx for a in anchors) / len(anchors)
        cy = sum(a.cy for a in anchors) / len(anchors)
        return Line("filter panel", 1.0, cx, cy, 0.0, 0.0)

    def filter_is_off(self, lines) -> bool:
        """The market filter has been reset: a card priced over the cap is on screen.

        Seen live 2026-09-23 after a server restart: Rarity back to All, the price
        cap gone, and the grid full of knives and gloves with few crates left in it.
        """
        cards = parse_cards(lines, 0)
        if any(c.price is not None and c.price > self.s.filter_price_max for c in cards):
            return True
        # the rarity half: its dropdown shows above APPLY FILTER on a ready market
        ctl = self._filter_box(lines, "Rarity")
        if self.s.filter_rarity and ctl is not None and ctl.text != "PRICE FIELD":
            if option_key(ctl.text) != option_key(self.s.filter_rarity):
                return True
        if not cards:
            return False
        # A four-figure price is written '10,750', which the price reader does not
        # take, so look for one inside the grid: below the first title and right of
        # the filter column, which keeps the coin balance and '100,000,000' out.
        top = min(c.title_line.y for c in cards)
        left = min(c.title_line.x for c in cards)
        return any(re.match(r"^\d{1,3}(,\d{3})+$", l.text.strip()) and l.y > top and l.x >= left
                   and int(l.text.strip().replace(",", "")) > self.s.filter_price_max
                   for l in lines)

    def _filter_box(self, lines, label: str) -> Optional[Line]:
        """The input or dropdown under a filter label (From, To, Rarity).

        Its own text when Vision reads one, else the spot where the box sits:
        measured live, 2.55 label-heights below the label, whatever the scale.
        """
        col = min((c.title_line.x for c in parse_cards(lines, 0)), default=float("inf"))
        heads = [l for l in lines if l.text.strip() == label and l.x + l.w < col]
        if not heads:
            return None
        head = heads[0]
        below = [l for l in lines
                 if l is not head and l.x + l.w < col and _overlap(l, head) > 0
                 and head.y + head.h <= l.y <= head.y + 4 * head.h]
        if below:
            return min(below, key=lambda l: l.y)
        y = head.y + 2.55 * head.h
        # A number read in the box beside it (To's, for From) puts the row exactly:
        # 2.55 is in Vision's label heights, and RapidOCR's (Windows) boxes are a third
        # taller, which put the click at the box's bottom edge.
        twins = [l for l in lines if l.x + l.w < col and re.match(r"^[\d,]+$", l.text.strip())
                 and abs(l.cy - (y + head.h / 2)) < 1.5 * head.h]
        if twins:
            y = min(twins, key=lambda l: abs(l.cy - (y + head.h / 2))).cy - head.h / 2
        return Line("PRICE FIELD", 1.0, head.x, y, head.w, head.h)

    def check_market_filter(self, lines) -> bool:
        """Restore the filter if it looks reset. True if it acted (the cycle ends).

        Two frames in a row, because a dialog fading over a serial can make one
        frame read like a price. Three failed restores in a row and it stops trying
        and says so once, rather than typing into the panel every two minutes.
        """
        if not self.s.restore_filter or not self.filter_is_off(lines):
            self._filter_off_frames = 0
            return False
        self._filter_off_frames += 1
        if self._filter_off_frames < 2 or self._filter_fails >= 3:
            return False
        if time.time() - self._last_filter_fix < FILTER_RETRY:
            return False
        if self.set_market_filter():
            self._filter_fails = 0
        else:
            self._filter_fails += 1
            if self._filter_fails >= 3:
                self.log("could not restore the market filter three times running: "
                         "set Price 0-%d and Rarity %s by hand" % (self.s.filter_price_max, self.s.filter_rarity))
        self._filter_off_frames = 0
        return True

    def set_market_filter(self) -> bool:
        """Put the market filter back: price 0 to filter_price_max, rarity filter_rarity.

        The next refresh applies it, so this only sets the controls.
        """
        self._last_filter_fix = time.time()
        self.log("the market filter was reset: setting price 0-%d, rarity %s"
                 % (self.s.filter_price_max, self.s.filter_rarity))
        _, lines = self.read()
        centre = self._filter_panel_centre(lines)
        if centre is None or not self.wait_idle(self.s, self.stop_event):
            return False
        # the price boxes are at the top of the column
        self.scroll_at(to_global(self.bounds, self.scale, centre), -SCROLL_STEP, self.pid,
                       hold=self._hold, notches=FILTER_NOTCHES)
        self.sleep(SETTLE_AFTER_SCROLL)
        _, lines = self.read()
        ok = True
        for label, value in (("From", "0"), ("To", str(self.s.filter_price_max))):
            box = self._filter_box(lines, label)
            if box is None:
                self.log("the %s price box is not on screen" % label)
                ok = False
                continue
            if not self.wait_idle(self.s, self.stop_event):
                return False
            # What the box shows plus a spare, not a flat 12: Roblox resets From to
            # nothing and To to 100,000,000. Unread, From gets 3 and To the old 12.
            # Both boxes are read back below.
            clear = filter_clear(box, 2, 3) if label == "From" else filter_clear(box, 1, 12)
            if not self.type_digits_at(to_global(self.bounds, self.scale, box), value,
                                       self.pid, clear=clear, hold_ok=self._hold):
                self.log("typing into the %s box was cut short" % label)
                return False
            self.sleep(0.3)
        _, lines = self.read()
        to = self._filter_box(lines, "To")
        if to is None or re.sub(r"\D", "", to.text) != str(self.s.filter_price_max):
            self.log("the To box reads %r after typing" % (to.text if to else None))
            ok = False
        # From, off the same frame: unread it gets only 3 clear keys, so a box that
        # held 1234 would come out 10 and hide every crate under 10. Blank passes, as
        # the reset box on file reads blank; so does 0 and the unread placeholder.
        frm = self._filter_box(lines, "From")
        if frm is None or re.sub(r"\D", "", frm.text) not in ("", "0"):
            self.log("the From box reads %r after typing" % (frm.text if frm else None))
            ok = False
        # rarity sits at the bottom of the column, next to APPLY FILTER
        if not self.reveal_apply_filter(lines):
            return False
        _, lines = self.read()
        if not self.s.filter_rarity:
            return ok                      # blank: the rarity is infinite's to set
        ctl = self._filter_box(lines, "Rarity")
        if ctl is None or ctl.text == "PRICE FIELD":
            self.log("the rarity dropdown is not on screen")
            return False
        if option_key(ctl.text) != option_key(self.s.filter_rarity):
            if not self.pick_option(ctl, "RARITY", self.s.filter_rarity):
                return False
            _, lines = self.read()
            now = self._filter_box(lines, "Rarity")
            if now is None or option_key(now.text) != option_key(self.s.filter_rarity):
                self.log("rarity reads %r after picking" % (now.text if now else None))
                return False
        return ok

    def reveal_apply_filter(self, lines) -> bool:
        """Scroll the filter column down until APPLY FILTER is on screen.

        Coming back from the SELL tab leaves the column scrolled to the top, where
        the button sits below the fold and the bot would otherwise never refresh.
        """
        for attempt in range(FILTER_SCROLL_TRIES):
            centre = self._filter_panel_centre(lines)
            if centre is None:
                return False
            if not self.wait_idle(self.s, self.stop_event):
                return False
            self.log("scrolling the filter panel to reach APPLY FILTER (%d/%d)"
                     % (attempt + 1, FILTER_SCROLL_TRIES))
            self.scroll_at(to_global(self.bounds, self.scale, centre), SCROLL_STEP, self.pid,
                           hold=self._hold, notches=FILTER_NOTCHES)
            self.sleep(SETTLE_AFTER_SCROLL)
            _, lines = self.read()
            if find_text(lines, "APPLY FILTER") is not None:
                return True
        self.log("APPLY FILTER never came into view after %d scrolls"
                 % FILTER_SCROLL_TRIES)
        return False

    def _grid_centre(self, lines) -> Optional[Line]:
        cards = parse_cards(lines, 0)
        if not cards:
            return None
        cx = sum(c.title_line.cx for c in cards) / len(cards)
        cy = sum(c.title_line.cy for c in cards) / len(cards)
        return Line("grid", 1.0, cx, cy, 0.0, 0.0)

    def _notches_to_button(self, lines, card) -> int:
        """How far to scroll so the card's button shows, in one go if possible.

        A card on the top row shows its whole height, title to button, on the same
        frame, so the distance comes from the screen rather than a constant, and a
        notch is 100 pt. One notch over is deliberate: it still leaves the title on
        screen for the rematch, and one too few would cost a whole extra step.
        """
        whole = [c for c in parse_cards(lines, 0) if c.button_line is not None]
        if not whole or not self.bounds:
            return SCROLL_NOTCHES
        reach = max(c.button_line.y + c.button_line.h - c.title_line.y for c in whole)
        need = card.title_line.y + reach - self._view_bottom(lines)
        return max(1, min(8, int(math.ceil(need / (100.0 * self.scale))) + 1))

    def peek_below(self, lines, balance) -> bool:
        """Once a refresh, while the pill counts down: the rows under the first two.

        infinite, 2026-10-09: twelve crates in the table now, so a refresh can fill a
        third row, and no read of the first screen ever saw it. Two rows down, from
        the grid's right edge (MARKET_WHEEL_X: never over the purchase window), a
        look and a buy if one is there, then back to the top for the next refresh.
        Only when two full rows of five show: fewer, and there is nothing below.
        True when it went for a buy.
        """
        self._peek_due = False
        cards = parse_cards(lines, 0)
        tol = self._slot_tol()
        rows: List[float] = []
        for y in sorted(c.title_line.y for c in cards):
            if not rows or y - rows[-1] > tol:
                rows.append(y)
        centre = self._grid_centre(lines)
        if len(cards) < PEEK_MIN_CARDS or len(rows) < 2 or centre is None or not self.bounds:
            return False
        notches = max(1, int(round(2 * (rows[1] - rows[0]) / (100.0 * self.scale))))
        pt = to_global(self.bounds, self.scale,
                       replace(centre, x=self.bounds["Width"] * self.scale * MARKET_WHEEL_X))
        if not self.wait_idle(self.s, self.stop_event):
            return False
        self.scroll_at(pt, SCROLL_STEP, self.pid, hold=self._hold, notches=notches)
        self.sleep(SETTLE_AFTER_SCROLL)
        try:
            return self.try_buy(self._still_grid(), balance)
        finally:
            # back to the top whatever happened, so the next refresh shows row one
            if self.wait_idle(self.s, self.stop_event):
                self.scroll_at(pt, -SCROLL_STEP, self.pid, hold=self._hold, notches=notches + 3)
                self.sleep(SETTLE_AFTER_SCROLL)

    def _scroll_to_button(self, lines, card) -> Optional[Card]:
        """Scroll the grid until the card's VIEW button is on screen, or give up.

        One scroll is not enough. A card's VIEW button sits roughly a card-height
        below its title, so a second-row card whose title has just come into view
        still has its button past the bottom edge. The row pitch moves with the
        window size, so this asks the screen after every step instead of computing
        a distance once.
        """
        notches = min(self._notches_to_button(lines, card), MARKET_BURST_MAX)
        backed = 0
        for attempt in range(SCROLL_TRIES):
            centre = self._grid_centre(lines)
            if centre is None:
                return None
            # the grid's height, but its right edge: clear of the purchase window
            pt = to_global(self.bounds, self.scale,
                           replace(centre, x=self.bounds["Width"] * self.scale * MARKET_WHEEL_X))
            self.log(
                "scrolling grid to reach %s (%d/%d, %d notches)"
                % (card.name, attempt + 1, SCROLL_TRIES, notches)
            )
            before = lines
            self.scroll_at(pt, SCROLL_STEP, self.pid, hold=self._hold, notches=notches)
            notches = SCROLL_NOTCHES
            self.sleep(SETTLE_AFTER_SCROLL)
            img, lines = self.read()
            if self.bounds_moved:
                self.log("window moved while scrolling, skipping this cycle")
                return None
            live = self._rematch(lines, card)
            moved = grid_shift(before, lines, 4000.0)
            # Gone after a scroll most likely means gone past the top: come back a
            # notch at a time and look again before calling it lost. The frame is
            # kept, because the next miss should explain itself.
            while live is None and backed < 3:
                if backed == 0 and img is not None:
                    save_png(img, os.path.join(
                        FAILURES_DIR, "scroll-" + time.strftime("%Y%m%d-%H%M%S") + ".png"))
                    prune_failures()
                backed += 1
                self.log("%s @%s went past the top (grid moved %s px), back one notch"
                         % (card.name, card.price, None if moved is None else int(moved)))
                self.scroll_at(pt, -SCROLL_STEP, self.pid, hold=self._hold, notches=1)
                self.sleep(SETTLE_AFTER_SCROLL)
                img, lines = self.read()
                live = self._rematch(lines, card)
            if live is None:
                self.log("%s @%s scrolled out of view" % (card.name, card.price))
                return None
            if live.button_line is not None:
                return live
        self.log("no VIEW button reachable for %s after %d scrolls" % (card.name, SCROLL_TRIES))
        self._block(card, "no VIEW button reachable")
        return None

    def try_buy(self, lines, balance_before) -> bool:
        """True when an attempt was actually made (the cycle ends here).

        False means 'nothing I could do with this card', so the caller refreshes
        instead of coming straight back to the same blocked listing.
        """
        card = self._pick(lines, balance_before)
        if card is None:
            return False

        # idleness first, then the read. The wait routinely lasts seconds while the
        # user types on the other screen, and cards move and vanish in that time, so
        # the frame the click is aimed at has to be the last one we looked at.
        if not self.wait_idle(self.s, self.stop_event):
            return False
        _, lines = self.read()
        if self.bounds_moved:
            self.log("window moved between reads, skipping this cycle")
            return False
        live = self._rematch(lines, card)
        if live is None:
            self.log("%s @%s vanished before the click" % (card.name, card.price))
            return False
        if live.button_line is not None and live.button != "VIEW":
            self._block(card, "button reads %s" % live.button)
            return False
        # One focus grab for the whole purchase, the row-2 scroll included. Everything
        # from here to CONFIRM is a race against other buyers, and every switch of
        # the frontmost app makes the next screen capture wait about 0.9 s (measured
        # 2026-09-23: 0.12 s at rest, 0.85-0.92 s straight after an activation, the
        # same through every capture route).
        with self.holding_focus() as got:
            if not got:
                return False
            if live.button_line is None:
                live = self._scroll_to_button(lines, card)
                if live is None:
                    return False
            if live.button != "VIEW":
                self._block(card, "button reads %s" % live.button)
                return False
            return self._buy_sequence(live, card, balance_before)

    def _unscroll_modal(self):
        """Scroll a purchase window that is scrolled down inside back to its top.

        The bot's own wheel no longer does this (MARKET_WHEEL_X), but a person's can,
        and the window keeps it. Its heading and the crate's name are what the buy is
        checked against, so they have to be on screen first. The fresh frame.
        """
        b = self.bounds
        centre = Line("purchase window", 1.0, b["Width"] * self.scale / 2.0,
                      b["Height"] * self.scale / 2.0, 0.0, 0.0)
        self.log("the purchase window is scrolled down, scrolling it back to its top")
        self.scroll_at(to_global(self.bounds, self.scale, centre), -SCROLL_STEP, self.pid,
                       hold=self._hold, notches=MODAL_UNSCROLL)
        self.sleep(SETTLE_AFTER_SCROLL)
        return self.read()

    def _confirm_landed(self) -> bool:
        """After clicking CONFIRM, make sure the dialog took it; press again if not.

        Live 2026-09-24 03:00: three CONFIRM clicks in a row did nothing, the dialog
        was still up in every capture (most likely clicked while it was still
        animating in), and the misses ended the night through the failsafe. A click
        is only pressed again on a fresh frame that still shows the dialog and its
        button, so a dialog that closed in between is never clicked through.
        """
        for attempt in range(CONFIRM_RETRIES + 1):
            deadline = time.time() + CONFIRM_SETTLE
            while True:
                _, lines = self.read()
                if self.state_of(lines, None) != "confirm":
                    return True
                if time.time() >= deadline or self.stop_event.is_set():
                    break
                self.sleep(0.1)
            if attempt == CONFIRM_RETRIES or self.stop_event.is_set():
                break
            yes = find_confirm(lines)
            if yes is None:
                break
            self.log("CONFIRM did not land, pressing it again (%d/%d)" % (attempt + 1, CONFIRM_RETRIES))
            if not self.click_line(yes, "confirm purchase again"):
                break
        self.log("CONFIRM never landed, closing the dialog: nothing bought")
        cancel = find_button(lines, "CANCEL")
        if cancel is not None:
            self.click_line(cancel, "close the confirm that never took")
        return False

    def _buy_sequence(self, live, card, balance_before) -> bool:
        if not self.click_line(live.button_line, "open %s @%s" % (live.name, card.price)):
            return False

        st, img, lines = self.wait_for(("modal", "modal_scrolled"), 5.0, settle=True)
        if st == "modal_scrolled":
            img, lines = self._unscroll_modal()
            st = self.state_of(lines, img)
        if st != "modal":
            self.log("modal did not open (state %s)" % st)
            return False
        modal = parse_modal(lines)
        probe = replace(
            card,
            name=modal["name"] or "",
            price=modal["price"],
            button="VIEW",
            is_case=bool(modal["is_case"]),
        )
        ok, why = decide(probe, self.s)
        if modal["name"] != card.name or modal["price"] != card.price or not ok:
            self.log(
                "modal mismatch: %r @%s vs card %r @%s (%s), backing out"
                % (modal["name"], modal["price"], card.name, card.price, why)
            )
            self._block(card, "modal did not match")
            if modal["close_line"] is not None:
                self.click_line(modal["close_line"], "abort")
            return False

        if self.s.dry_run:
            # count the dry run against the caps too, otherwise nothing ever stops it:
            # a dry run never consumes the listing, so the same card qualifies forever.
            self.bought += 1
            self.spent += modal["price"] or 0
            self._block(card, "already evaluated this session")
            self.log("would buy %s @%s (dry run)" % (modal["name"], modal["price"]))
            self.record(card, modal, balance_before, balance_before, True, "dry run")
            if modal["close_line"] is not None:
                self.click_line(modal["close_line"], "dry run, close")
            if self.s.auto_sell:
                self._unsold += 1
            return True

        if modal["purchase_line"] is None or not self.click_line(
            modal["purchase_line"], "purchase"
        ):
            self.record(card, modal, balance_before, None, False, "purchase not clicked")
            return False
        st, img, lines = self.wait_for(("confirm",), 5.0)
        if st != "confirm":
            self.log("confirm dialog did not open (state %s)" % st)
            self.record(card, modal, balance_before, None, False, "confirm dialog missing")
            self.bail_out(lines, img)
            self._block(card, "confirm dialog missing")
            return True
        yes = find_confirm(lines)
        if yes is None or not self.click_line(yes, "confirm purchase"):
            self.record(card, modal, balance_before, None, False, "confirm not clicked")
            self._block(card, "confirm not clicked")
            return True
        if not self._confirm_landed():
            # Nothing was bought: the dialog never took the click, and it is closed now.
            self.record(card, modal, balance_before, None, False, "confirm never landed")
            self._block(card, "confirm never landed")
            self._unverified += 1
            if self._unverified >= UNVERIFIED_LIMIT:
                self.log("%d purchases in a row went unconfirmed, stopping" % self._unverified)
                self.stop_event.set()
            return True
        # the coins are gone the moment CONFIRM lands, so the caps count it here.
        # Over-counting a failed buy costs one skipped bargain; under-counting a real
        # one costs real money.
        self.bought += 1
        self.spent += modal["price"] or 0
        # The coin counter updates on a server round trip, so one read two seconds
        # later routinely still showed the old number and called a good purchase
        # unverified. Measured live 2026-09-19: Glove Case 2 at 25 really did debit
        # 466 to 441, and was recorded as 466 to 466. Wait for the number to move.
        img = lines = None
        balance_after = None
        for _ in range(VERIFY_READS):
            self.sleep(1.0)
            try:
                img, lines = self.read()
            except RuntimeError as exc:
                self.log("verify read failed: %s" % exc)
                continue
            if find_text(lines, "Confirm Purchase") is not None:
                # A dialog dims the top bar and Vision reads the dimmed coins short:
                # live 2026-09-24 it read 62 and 43 as '2' and called both buys
                # unverified. Only a clear frame's balance counts.
                balance_after = None
                continue
            balance_after = parse_balance(lines)
            if (
                balance_after is not None
                and balance_before is not None
                and balance_after < balance_before
            ):
                break
        verified = (
            lines is not None
            and find_text(lines, "Confirm Purchase") is None
            and balance_after is not None
            and balance_before is not None
            and balance_after < balance_before
        )
        outbid = (
            balance_after is not None
            and balance_before is not None
            and balance_after == balance_before
        )
        if verified:
            self._unverified = 0
            self.log("bought %s @%s" % (modal["name"], modal["price"]))
            self._note_bought(modal["name"] or card.name, card.serial, modal["price"] or 0,
                              bool(modal["is_case"]), balance_before, balance_after)
        elif outbid:
            # infinite worked this one out from the game: someone else takes the
            # listing in the moment between our CONFIRM and the server reading it.
            # The listing vanishes and no coins move, so this is not a purchase at
            # all and must not be counted, spent, or queued for selling.
            self.bought -= 1
            self.spent -= modal["price"] or 0
            self._unverified += 1
            self.log(
                "someone else took %s @%s first, balance still %s, nothing spent"
                % (modal["name"], modal["price"], balance_after)
            )
        else:
            # The balance could not be read at all, so whether the coins moved is
            # genuinely unknown. Keep the count: under-counting a real purchase
            # spends past the cap, over-counting only skips a bargain.
            self._unverified += 1
            self.log(
                "purchase counted but not verified (balance %s -> %s), saving capture"
                % (balance_before, balance_after)
            )
            if lines is not None and img is not None:
                self.bail_out(lines, img)
        # The grid keeps showing a listing until the next refresh, even one we just
        # bought. Live 2026-09-23: the bot bought a Glove Case 2, then tried that
        # same ghost card five times, ten seconds each, until the failsafe below
        # ended the run. Parked, the next pass refreshes instead.
        self._block(card, "bought" if verified else ("taken" if outbid else "unconfirmed"))
        if not verified and self._unverified >= UNVERIFIED_LIMIT:
            self.log(
                "%d purchases in a row went unconfirmed, stopping"
                % self._unverified
            )
            self.stop_event.set()
        self.record(
            card,
            modal,
            balance_before,
            balance_after,
            verified,
            ""
            if verified
            else ("outbid, balance unchanged" if outbid else "balance drop not confirmed"),
        )
        self.check_funds(balance_after)
        if self.s.auto_sell and verified:
            # Sell in waves rather than one crate at a time. Selling by name and
            # serial meant a serial Vision truncated differently between frames
            # simply failed; the grid already knows what we own and have not listed.
            # Batching also gives the server time to register the new item, which is
            # what the one-behind rule was doing.
            # Only flag it. The purchase runs inside a held focus, and a selling trip
            # is many clicks and a lot of typing: doing it here would keep the user's
            # cursor and keyboard for the whole batch with no idle check in sight.
            self._unsold += 1
        return True

    def note_balance(self, value: Optional[int]) -> None:
        """Follow the coin counter off market reads, and add up what sales bring in.

        A new value counts once two reads in a row agree on it: Vision drops a digit
        now and then, and 131, 31, 131 taken at face value would book the way back
        up as a 100-coin sale. Our own purchases never count as a drop that a sale
        later makes up, because a verified one takes its price off as it happens
        (see _note_bought). A rise takes the crates it paid for out of the snapshot
        and books what each one made (_book_sold).
        """
        if value is None:
            return
        if value != self._balance_seen:
            self._balance_seen = value
            return
        gained = value - self.balance if self.balance is not None and value > self.balance else 0
        # booked before the snapshot is touched, so a snapshot that will not parse
        # fails one pass rather than every pass after it
        self.balance = value
        if gained:
            self.sales_income += gained
            self.log("coins up %d to %d: listings sold (%d from sales this run)"
                     % (gained, value, self.sales_income))
            self._note_sold(gained)

    def _note_sold(self, gained: int) -> None:
        """Take the listed crates a coin rise paid for out of the snapshot, and book them.

        A sold crate leaves the SELL tab but stayed in the snapshot, so its worth
        was counted twice, as a crate and again in the coins it brought. Each listed
        crate pays exactly payout() of the price it was listed at, so the crates
        taken are the fewest whose payouts add up to the rise exactly (fewest_crates).
        A rise no set of them explains (a glove sold, coins from elsewhere, a crate
        relisted by hand) takes nothing, and the snapshot is marked as maybe stale
        from now on.
        """
        snap = self.inventory
        if not snap:
            return
        items = list(snap.get("items") or [])
        groups = self._listed_by_payout(items)
        mix = fewest_crates(gained, {pay: len(group) for pay, group in groups.items()})
        if mix and sum(mix.values()) > SURE_MIX:
            # With ~100 listed crates at four payouts almost any rise over ~150 has an
            # exact mix, so a big one proves little: a glove sold by hand for 500 was
            # booked as 10 crate sales and +33 profit. Leave it to a scan.
            self.log("coins up %d, %d crates' worth at once: not booked, scan to confirm"
                     % (gained, sum(mix.values())))
            mix = None
        if mix:
            taken = [pair for pay in sorted(mix, reverse=True) for pair in groups[pay][:mix[pay]]]
            kept = [i for i in items if all(i is not t for t, _ in taken)]
            self.log("%s sold, out of the inventory" % ", ".join(i.get("name") or "?" for i, _ in taken))
            self._put_inventory(dict(snap, items=kept))
            for item, price in taken:
                self._book_sold(item, price)
            return
        self.log("coins up %d that no listed crate explains: the inventory may be out of date, "
                 "recounting it in %d s" % (gained, RECOUNT_WAIT))
        self._unbooked += gained
        if self._recount_at is None:
            self._recount_at = time.time() + RECOUNT_WAIT
        self._put_inventory(dict(snap, complete=False,
                                 unsure_since=snap.get("unsure_since") or time.strftime("%Y-%m-%dT%H:%M:%S")))

    def recount(self) -> Optional[dict]:
        """Scan the inventory, and book the listed crates it no longer holds as sold.

        infinite, 2026-09-28: when a coin rise fits no listed crates, or is too many
        at once to trust (SURE_MIX), count again rather than guess. A listed crate
        gone from the grid was sold, at the price it was listed at. Booked only up to
        the coins no rise explained, so a crate matched to an earlier rise is never
        booked twice, and coins from crates listed by hand stay unbooked.
        """
        old = self.inventory
        snap = self.scan_inventory()
        if not self._unbooked:
            return snap
        if not (snap and snap.get("complete") and old):
            self._recount_at = time.time() + RECOUNT_WAIT       # a cut-short scan: again later
            return snap
        groups = self._listed_by_payout(vanished(old.get("items") or [], snap.get("items") or []))
        pairs = [pair for pay in sorted(groups, reverse=True) for pair in groups[pay]]
        if sum(pay * len(g) for pay, g in groups.items()) > self._unbooked:
            mix = fewest_crates(self._unbooked, {pay: len(g) for pay, g in groups.items()}) or {}
            pairs = [pair for pay in sorted(mix, reverse=True) for pair in groups[pay][:mix[pay]]]
        booked = sum(payout(price, self.s.sell_tax) for _, price in pairs)
        self.log("recount: %d crate%s gone since the last count, %d of the %d unexplained coins"
                 % (len(pairs), "" if len(pairs) == 1 else "s", booked, self._unbooked))
        for item, price in pairs:
            self._book_sold(item, price)
        self._unbooked = 0
        return snap

    def _listed_by_payout(self, items: List[dict]) -> Dict[int, List[Tuple[dict, int]]]:
        """The snapshot's listed crates as {payout: [(item, the price it was listed at)]}.

        A listed crate with no listing on record and no sell price in the table was
        listed by hand at a price the bot cannot know, so no rise is put down to it.
        """
        ledger = self._read_ledger()     # once a rise, not once a crate
        groups: Dict[int, List[Tuple[dict, int]]] = {}
        for item in items:
            if not (item.get("listed") and item.get("case")):
                continue
            price = self.listed_price_for(item.get("serial"), item.get("name") or "", ledger)
            if price is not None:
                groups.setdefault(payout(price, self.s.sell_tax), []).append((item, price))
        return groups

    def _book_sold(self, item: dict, price: int) -> None:
        """One crate a coin rise paid for: its profit, and a "sold" line in the ledger.

        price is what that crate was listed at (listed_price_for), which is what it
        paid out of, whatever the table says today. The profit is what the game paid,
        less what the ledger says was paid for that serial: bought at 35 and listed
        at 38 with 5% tax, 38 * 0.95 is 36.1, the game pays 36 (payout), and the flip
        made 1.00. The record is never marked dry: only a real rise of the coin
        counter books a sale, dry run or not.
        """
        name = item.get("name") or "?"
        serial = item.get("serial")
        paid = self.paid_for(serial)
        self.sold_count += 1
        profit = None
        if paid:
            profit = float(payout(price, self.s.sell_tax) - paid)
            self.profit = round(self.profit + profit, 2)
        else:
            self.unknown_cost_count += 1
        self.log("sold %s %s at %d: %s" % (
            name, serial or "", price,
            "profit %+.2f (paid %d), %+.2f this run" % (profit, paid, self.profit) if paid
            else "what it cost is not on record, left out of the profit"))
        self._remember({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "sold",
            "name": name,
            "serial": serial,
            "price": price,
            "paid": paid,
            "profit": profit,
            "ok": True,
            "dry_run": False,
        })

    def load_inventory(self) -> Optional[dict]:
        """The last snapshot from disk into self.inventory, None when there is none."""
        self.inventory = read_inventory()
        return self.inventory

    def _put_inventory(self, snap: dict) -> None:
        """Swap in a new snapshot and write it through; the GUI only ever sees whole ones."""
        self.inventory = snap
        try:
            write_inventory(snap)
        except OSError as exc:
            self.log("could not write %s: %s" % (INVENTORY_PATH, exc))

    def _snapshot_or_new(self) -> dict:
        # Before any scan, what the bot does itself still counts, marked incomplete.
        return self.inventory or {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "complete": False,
            "balance": None,
            "items": [],
            "unread": 0,
        }

    def _note_bought(self, name: str, serial: Optional[str], price: int, is_case: bool,
                     before: Optional[int], after: Optional[int]) -> None:
        """A verified purchase: the coins it took, and one more crate, not yet listed.

        The trusted balance loses the price only when the counter fell by exactly
        that. Any other fall means something else moved too, a sale in the same
        seconds or a misread modal price, and taking the price off regardless would
        book the difference later as income that never came. Unknown instead, the
        next two agreeing reads set it afresh and book nothing.
        """
        if self.balance is not None and before is not None and after is not None \
                and before - after == price:
            self.balance -= price
        else:
            self.balance = None
        self._balance_seen = None
        snap = self._snapshot_or_new()
        item = {"name": name, "serial": serial, "listed": False, "case": is_case}
        self._put_inventory(dict(snap, items=list(snap.get("items") or []) + [item]))

    def _note_listed(self, name: str, serial: Optional[str]) -> None:
        """A confirmed listing: count it, and mark that crate listed in the snapshot.

        A truncated serial can match several copies. Any unlisted one of them will
        do, because the snapshot is there to count crates, not to tell copies apart.
        """
        self.listed_count += 1
        snap = self._snapshot_or_new()
        items = [dict(i) for i in snap.get("items") or []]
        same = [i for i in items if i.get("name") == name and _serials_agree(i.get("serial"), serial)]
        hit = next((i for i in same if serial and i.get("serial") == serial), None)
        hit = hit or next((i for i in same if not i.get("listed")), None)
        if hit is None:
            items.append({"name": name, "serial": serial, "listed": True, "case": True})
        else:
            hit["listed"] = True
            hit["serial"] = max((hit.get("serial") or "", serial or ""), key=len) or None
        self._put_inventory(dict(snap, items=items))

    def check_funds(self, balance: Optional[int]) -> None:
        """The wallet is the limit in every mode, so wait when it runs dry.

        Running out is not the end of the run. The crates it bought are listed and
        will sell, and the coins come back, so it waits for that rather than stopping
        for the night after one dry spell. A static cap ends a run through done(),
        never through an empty wallet.
        """
        if balance is None:
            return
        floor = self.price_floor()
        if balance < floor and not self.out_of_funds:
            self.out_of_funds = True
            self._waiting_since = time.time()
            self.log(
                "balance %d is under the %d floor, waiting for listings to sell"
                % (balance, floor)
            )

    def rejoin(self, state, lines, img=None) -> bool:
        """Back to the market from a disconnect, a load or a menu. True if it acted.

        A dropped connection, a server restart and the AFK kick all end in Roblox's
        Disconnected box. Reconnect puts the game back where it was (infinite,
        2026-10-07): the main game's main menu, where MARKET is on the bottom bar, or
        a trading server, whose rules want ACCEPT and whose hub opens the market from
        the golden shop icon. Both load through the game's orange loading screen.
        """
        if state == "loading":
            self.sleep(1.0)
            return True
        if state == "trade_rules":
            btn = find_button(lines, "ACCEPT")
            self.click_line(replace(btn, text="ACCEPT"), "accept the trade rules")
            self.sleep(1.5)
            return True
        if state == "hub":
            icon = find_hub_shop(img)
            if icon is not None and self.click_line(icon, "open the market from the trading hub"):
                self.wait_for(MARKET_STATES, 6.0)
            return True
        if state == "disconnected":
            if time.time() - self._last_reconnect >= RECONNECT_EVERY:
                self._last_reconnect = time.time()
                btn = next(l for l in lines if l.text.strip().upper() == "RECONNECT")
                self.click_line(btn, "disconnected, reconnecting")
            self.sleep(2.0)
            return True
        if state == "lobby":
            # started from the main menu, or a reconnect sent us back there
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return True
        return False

    def wait_for_funds(self) -> None:
        """Sit out the dry spell, then pick up where it left off.

        While a fresh read is still short, everything sellable goes up first: that is
        what brings the coins back.
        """
        try:
            img, lines = self.read()
        except RuntimeError as exc:
            self.log("read failed while waiting: %s" % exc)
            self._wait_for_sales()
            return
        # the main menu has no wallet (its news date reads as a balance of 2026), and a
        # disconnect has none either: get back to the market before trusting a number
        state = self.state_of(lines, img)
        if self.rejoin(state, lines, img):
            return
        balance = parse_balance(lines)
        if state in MARKET_STATES:
            self.note_balance(balance)
        floor = self.price_floor()
        if balance is not None and balance >= floor:
            waited = time.time() - (self._waiting_since or time.time())
            self.out_of_funds = False
            self._nothing_to_sell = False
            self._swept = False
            self.log(
                "balance back to %d after %d min, buying again"
                % (balance, round(waited / 60))
            )
            return
        if (
            self.s.auto_sell
            and balance is not None
            and not self._swept
            and time.time() - self._last_sweep >= SWEEP_RETRY
        ):
            # Still short on a fresh read, so put up everything sellable in the
            # inventory, not only this session's buys: listings are what bring the
            # coins back. Only a sweep that reached the bottom counts as done; one
            # cut short by a disconnect, a bad frame or a missed scroll goes again.
            self._last_sweep = time.time()
            self._sweep_complete = False
            self.run_sell_batch(everything=True)
            self._swept = self._sweep_complete
            return
        mins = round((time.time() - (self._waiting_since or time.time())) / 60)
        self.log(
            "waiting for listings to sell: balance %s, need %d (%d min so far)"
            % (balance, floor, mins)
        )
        # Keep the session alive. Waiting quietly meant Roblox saw no input at all,
        # and it kicks an idle player after twenty minutes: the night of the 19th it
        # waited from 03:34, got kicked, and everything after that was lost. A
        # refresh is the harmless click we already know how to make.
        if time.time() - self._last_poke >= POKE_EVERY:
            self._last_poke = time.time()
            if self.state_of(lines, None) == "market_ready":
                pill = find_text(lines, "APPLY FILTER")
                if pill is not None:
                    self.click_line(pill, "keeping the session awake")
            else:
                self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
        self._wait_for_sales()

    def _wait_for_sales(self) -> None:
        """WAIT_FOR_SALES in 1 s naps, cut short by Stop or a Scan press.

        One 30 s sleep meant a Scan pressed while broke waited up to 30 s to start,
        and Stop as long to land. A nap costs nothing: no read, no event.
        """
        for _ in range(int(WAIT_FOR_SALES)):
            if self.stop_event.is_set() or self._scan_wanted.is_set() or self._sell_wanted.is_set():
                return
            self.sleep(1.0)

    # -- selling

    def goto_tab(self, label: str, want: Tuple[str, ...], timeout: float = 8.0) -> bool:
        """Click MARKET or SELL in the top bar and wait for that screen."""
        img, lines = self.read()
        if label != "MARKET" and on_lobby(lines):
            # SELL lives inside the market, so the main menu goes through MARKET first
            if not self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown")):
                return False
            img, lines = self.read()
        # The exact label, the only thing click_line will press. The first line merely
        # containing it could be a news banner ("Market Update!") or a seller name.
        tab = find_tab(lines, label)
        if tab is None:
            self.log("%s tab not visible (top of the screen read: %s)" % (
                label, ", ".join(repr(l.text) for l in lines if l.y < img_size(img)[1] * 0.12)))
            self.keep_frame(img, "no-%s-tab" % label.lower())
            return False
        if not self.click_line(replace(tab, text=label), "switch to %s" % label):
            return False
        st, _, lines = self.wait_for(want, TAB_RETRY_AFTER)
        if st not in want:
            # A click that never landed leaves the screen as it was, tab and all.
            # Once more: pressing a tab that did open late does nothing.
            again = find_tab(lines, label)
            if again is not None and self.click_line(replace(again, text=label), "switch to %s, again" % label):
                st, _, _ = self.wait_for(want, timeout)
        if st not in want:
            self.log("%s tab did not open (state %s)" % (label, st))
            self.keep_frame(self.read()[0], "%s-tab-did-not-open" % label.lower())
            return False
        return True

    def ensure_sell_view(self) -> bool:
        """Miscellaneous, Newest, arrow SELL_ARROW: the SELL grid set the way infinite uses it.

        Each dropdown hides the option already picked, so an option can only be looked
        for when something else is picked. Newest is required, because the regular
        batch only reads the first screen; the category and the arrow are best effort
        and say so in the log when they cannot be set.
        """
        img, lines = self.read()
        f = parse_sell_filters(lines)
        cat = f["category"]
        if cat is not None and cat.text.strip().lower() != "miscellaneous":
            self.pick_option(cat, "CATEGORY", "Miscellaneous")
            img, lines = self.read()
            f = parse_sell_filters(lines)
        for _ in range(SORT_RETRIES):
            if f["sort"] is not None:
                break
            # A big inventory can still be drawing its filter row when the tab opens:
            # one frame was all it got, and the whole trip failed on it.
            self.sleep(0.7)
            img, lines = self.read()
            f = parse_sell_filters(lines)
        if f["sort"] is None:
            self.log("sort control not found on the sell tab (read: %s)"
                     % ", ".join(repr(l.text) for l in lines if l.y < img_size(img)[1] * 0.25))
            self.keep_frame(img, "no-sort-control")
            return False
        if f["sort"].text.strip().lower() != "newest":
            if not self.pick_option(f["sort"], "SORT", "Newest"):
                return False
            img, lines = self.read()
            f = parse_sell_filters(lines)
            if f["sort"] is None or f["sort"].text.strip().lower() != "newest":
                self.log("the sort did not change to Newest")
                self.keep_frame(img, "sort-not-newest")
                return False
        way, arrow = sort_direction(img, f["sort"], f["search"])
        if way is not None and way != SELL_ARROW and self.click_line(arrow, "sort arrow %s" % SELL_ARROW):
            self.sleep(0.6)
            img, lines = self.read()
            f = parse_sell_filters(lines)
            way, _ = sort_direction(img, f["sort"], f["search"])
        if way != SELL_ARROW:
            self.log("sort arrow reads %s, not %s: newest may not come first" % (way, SELL_ARROW))
        return True

    def pick_option(self, control: Line, label: str, option: str) -> bool:
        """Open a dropdown and pick one of its options by exact name.

        Never a blind toggle. A lost click left a list open over APPLY FILTER in the
        review's scenario, and a blind close after a lost open-click opens it
        instead. So the list counts as open only when new lines appeared under the
        control after the open click, and a pick counts only once its option has
        gone from under the control.
        """
        # The list is as wide as the dropdown's box, not as its label: with All
        # picked the label is three letters and the options are indented past them,
        # so "under the label" found Miscellaneous only when Vision boxed All wide
        # (infinite, 2026-09-29: 'Miscellaneous is not in the category list' with it
        # on screen). Four label heights to the right covers the indent.
        band = replace(control, w=control.w + control.h * 4)
        under = lambda ls: {(l.text.strip(), round(l.y / 10)) for l in ls
                            if l.y > control.y + control.h and _overlap(l, band) > 0}
        _, before = self.read()
        if not self.click_line(replace(control, text=label), "open the %s list" % label.lower()):
            return False
        self.sleep(0.5)
        _, lines = self.read()
        find = lambda ls: next((l for l in ls if option_key(l.text) == option_key(option)
                                and l.y > control.y + control.h and _overlap(l, band) > 0), None)
        opt = find(lines)
        if opt is None:
            self.log("%s is not in the %s list" % (option, label.lower()))
            if under(lines) - under(before):
                self.click_line(replace(control, text=label), "close the list")
                self.sleep(0.5)
            return False
        self._picking = label
        try:
            if not self.click_line(replace(opt, text=option.upper()), "pick %s" % option):
                return False
        finally:
            self._picking = None
        self.sleep(0.8)
        _, lines = self.read()
        if find(lines) is not None:
            self.log("the %s pick did not land, closing the list" % label.lower())
            self.click_line(replace(control, text=label), "close the list")
            self.sleep(0.5)
            return False
        return True

    @staticmethod
    def _grid_moved(before, after) -> bool:
        """Did the grid scroll between two frames.

        Card positions decide it, because OCR reads the same serial differently from
        one frame to the next ('#3,821,642' then '#3.821.642'), and comparing text
        made a grid that sat still look like it was moving: measured live, a sweep
        paged sixteen times over a grid that only scrolls 680 px. Serials break the
        one tie positions cannot: a page of identical crates moved by exactly a row.
        """
        a = [c for c in parse_cards(before, 0)]
        b = [c for c in parse_cards(after, 0)]
        ya = sorted(c.title_line.y for c in a)
        yb = sorted(c.title_line.y for c in b)
        if len(ya) != len(yb) or any(abs(p - q) > 30 for p, q in zip(ya, yb)):
            return True
        digits = lambda cs: {re.sub(r"\D", "", c.serial) for c in cs if c.serial}
        sa, sb = digits(a), digits(b)
        return bool(sa and sb and not (sa & sb))

    def _view_bottom(self, lines) -> float:
        """Where the grid's view ends, in capture px: the game's bottom bar, else the window.

        Since 2026-10-07 the bar (LOADOUT .. PROGRESSION) sits over the bottom of the
        market and SELL grids, and a button behind it is not on screen.
        """
        limit = self.bounds["Height"] * self.scale * 0.97
        # the bar's own box starts about half a label above its text
        bar = [l.y - 1.5 * l.h for l in lines
               if l.y > limit * 0.6 and any(k in l.text.upper() for k in BOTTOM_BAR)]
        return min([limit] + bar)

    def _page_notches(self, lines) -> int:
        """The biggest page that cannot carry a row past unseen, from this frame.

        A card is only readable whole while its title is at least its own height
        above the bottom, so a page has to be shorter than the view minus a card.
        Roblox scales the cards with the window, but a notch stays 100 pt.
        """
        whole = [c for c in parse_cards(lines, 0) if c.button_line is not None]
        search = parse_sell_filters(lines)["search"]
        if not whole or search is None or not self.bounds:
            return 1
        reach = max(c.button_line.y + c.button_line.h - c.title_line.y for c in whole)
        view = self._view_bottom(lines) - (search.y + 3 * search.h)
        return max(1, min(PAGE_NOTCHES, int((view - reach) / (100.0 * self.scale))))

    def page_grid(self, lines, notches: int) -> Optional[list]:
        """Scroll the SELL grid, negative is up; the new lines, or None if it did not move.

        None also covers a scroll that never happened, and _page_failed says which,
        so a sweep does not mistake a missed focus grab for the end of the inventory.
        """
        self._page_failed = True
        centre = self._grid_centre(lines)
        if centre is None or not self.wait_idle(self.s, self.stop_event):
            return None
        if self.scroll_at(to_global(self.bounds, self.scale, centre),
                          SCROLL_STEP if notches > 0 else -SCROLL_STEP, self.pid,
                          hold=self._hold, notches=abs(notches)) is False:
            self.log("the sell grid did not scroll: Roblox did not come forward")
            return None
        self._page_failed = False
        self.sleep(SETTLE_AFTER_SCROLL)
        # Settled, not the first frame: at either end Roblox bounces the grid, and a
        # frame caught mid-bounce looked like movement, so the top was never "reached"
        # and scans came out INCOMPLETE after paging the whole grid (2026-10-02).
        after = self._still_grid()
        return after if self._grid_moved(lines, after) else None

    def scan_inventory(self) -> Optional[dict]:
        """Count what the SELL tab holds and keep it as the inventory snapshot.

        Reads only. The clicks are the tabs and the SELL view's own dropdowns and
        arrow, and the rest is the wheel: never SELL NOW, DELIST or REMOVE. The grid
        (Miscellaneous, Newest) holds still unless we buy or list, so paging it top
        to bottom sees every card. Pages overlap, so each card is kept once however
        many pages show it (see _scan_page). While it runs, scanning is True and a
        second request is turned away (request_scan).
        """
        self.scanning = True
        try:
            return self._scan_inventory()
        finally:
            self.scanning = False

    def _scan_inventory(self) -> Optional[dict]:
        self.log("scanning the inventory")
        if not self.goto_tab("SELL", ("sell",)):
            return None
        if not self.ensure_sell_view():
            self.goto_tab("MARKET", MARKET_STATES)
            return None
        # A tab switch already lands at the top; this is for a grid that did not.
        _, lines = self.read()
        homed = False
        for _ in range(6):
            nxt = self.page_grid(lines, -15)
            if nxt is None:
                homed = not self._page_failed
                break
            lines = nxt
        _, lines = self.read()
        # An empty inventory has no card to scroll by, so every page "fails" and the
        # scan read as cut short: the last count, crates and all, stayed on show
        # (infinite, 2026-09-30). A grid still empty a second later is empty.
        empty = not parse_cards(lines, 0)
        for _ in range(2 if empty else 0):
            self.sleep(0.5)
            _, lines = self.read()
            if parse_cards(lines, 0):
                empty = False
                break
        homed = homed or empty
        found: List[dict] = []
        balances = []
        offset, pages, bottom = 0.0, 0, False
        while True:
            balances.append(parse_balance(lines))
            self._scan_page(found, lines, offset)
            if self.stop_event.is_set() or pages >= SWEEP_SCROLLS:
                break
            notches = self._page_notches(lines)
            nxt = self.page_grid(lines, notches)
            if nxt is None:
                bottom = empty or not self._page_failed
                break
            # Where the grid really went, which at the bottom is less than asked for.
            step = notches * 100.0 * self.scale
            shift = grid_shift(lines, nxt, step + self._slot_tol())
            offset += step if shift is None else shift
            pages += 1
            lines = nxt
        read = [b for b in balances if b is not None]
        # A card Vision never read a serial for is still a crate: _scan_page kept it
        # once by its slot, so it counts, with serial None. Dropping it undercounted
        # the inventory and its worth by every such card.
        items = [
            {"name": f["name"], "serial": f["serial"], "listed": f["button"] == "DELIST",
             "case": f["case"]}
            for f in found
        ]
        snap = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "complete": homed and bottom and not self.stop_event.is_set(),
            # the value most frames agree on, so one misread frame cannot set it
            "balance": max(set(read), key=read.count) if read else None,
            "items": items,
            "unread": sum(1 for f in found if not f["serial"]),
        }
        old = self.inventory if self.inventory is not None else read_inventory()
        if not snap["complete"] and old and old.get("complete"):
            # A scan cut short by Stop, a disconnect or the page limit knows less than
            # the whole one before it, so that one stays, on disk and on show.
            self.inventory = old
            self.log("partial scan not saved: keeping the complete one from %s" % old.get("time"))
        else:
            self._put_inventory(snap)
        stats = inventory_stats(snap, self.s)["totals"]
        self.log(
            "inventory: %d crates (%d listed, %d waiting), %d other, %d unread, balance %s, %s"
            % (stats["cases"], stats["listed"], stats["unlisted"],
               len(items) - stats["cases"], snap["unread"], snap["balance"],
               "whole grid" if snap["complete"] else "INCOMPLETE after %d pages" % pages)
        )
        self.goto_tab("MARKET", MARKET_STATES)
        return snap

    def _slot_tol(self) -> float:
        # The row tolerance parse_cards uses, 3.5% of the frame: 76 px on the 2x
        # fixtures, where a row pitch is 1180, so neighbouring rows never share a slot.
        return self.bounds["Height"] * self.scale * 0.035 if self.bounds else 40.0

    def _scan_page(self, found: List[dict], lines, offset: float) -> None:
        """Add one page of the SELL grid to a scan, each card once.

        A card is the one already found in the same slot: the same column, and the
        same height once the scroll so far (offset) is added back. That is what lets
        a truncated serial ('#3,784') join its full reading, where the serial alone
        would match three copies on one row, and it is the only identity a card has
        when Vision missed its serial. Failing a slot, an exactly equal serial is the
        same card.
        """
        tol = self._slot_tol()
        for c in parse_cards(lines, 0):
            y = c.title_line.y + offset
            same = next((f for f in found
                         if f["name"] == c.name and _overlap(f["title"], c.title_line) > 0
                         and abs(f["y"] - y) <= tol and _serials_agree(f["serial"], c.serial)), None)
            if same is None and c.serial:
                # out of its slot only the whole number will do: a truncated one would
                # match every copy on a row
                same = next((f for f in found if f["name"] == c.name
                             and _serial_digits(f["serial"]) == _serial_digits(c.serial)), None)
            if same is None:
                found.append({"name": c.name, "serial": c.serial, "button": c.button,
                              "case": c.is_case, "title": c.title_line, "y": y})
                continue
            if c.serial and len(c.serial) > len(same["serial"] or ""):
                same["serial"] = c.serial
            if c.button:
                # a card low on the page has its button cut off; a later page shows it
                same["button"] = c.button

    def find_owned(self, lines, serial: Optional[str], name: str) -> Optional[Card]:
        """The card we just bought, identified by serial first and name second.

        The serial is the only thing that distinguishes two copies of the same crate,
        and listing the wrong copy is not something a later check would catch.
        """
        cards = [c for c in parse_cards(lines, 0) if c.button == "SELL NOW"]
        if serial:
            exact = [c for c in cards if c.serial == serial]
            if len(exact) == 1:
                return exact[0]
            # A truncated read can prefix-match two copies of the same crate, and
            # picking either would list the wrong one, which no later check catches.
            near = [
                c
                for c in cards
                if c.serial
                and (c.serial.startswith(serial) or serial.startswith(c.serial))
            ]
            return near[0] if len(near) == 1 else None
        named = [c for c in cards if c.name == name]
        return named[0] if len(named) == 1 else None

    def stage_one(self, card, price: int) -> bool:
        """Put one crate in the basket at a price. No selling happens here."""
        if not self.click_line(card.button_line, "stage %s at %d" % (card.name, price)):
            return False
        st, _, lines = self.wait_for(("setprice",), 6.0)
        if st != "setprice":
            self.log("the price sheet did not open (state %s)" % st)
            return False
        sheet = parse_setprice(lines)
        if sheet["field"] is None or sheet["confirm_line"] is None:
            self.log("price sheet did not parse, backing out")
            self.cancel_setprice(lines)
            return False
        pt = to_global(self.bounds, self.scale, sheet["field"])
        for unicode_first in (True, False):
            # Typing is a long burst of keystrokes. Never start one while the user is
            # at the keyboard, and give Stop a chance between attempts.
            if not self.wait_idle(self.s, self.stop_event):
                self.cancel_setprice(lines)
                return False
            # The first try clears only what the sheet shows, nothing on a fresh one;
            # the retry clears wide, over whatever the first try may have left.
            clear = first_clear(sheet) if unicode_first else retry_clear(sheet)
            if not self.type_digits_at(
                pt, str(price), self.pid, unicode_first=unicode_first, hold_ok=self._hold,
                clear=clear,
            ):
                self.log("could not reach the price field")
                self.cancel_setprice(lines)
                return False
            self.sleep(0.5)
            _, lines = self.read()
            sheet = parse_setprice(lines)
            # The placeholder is proof the box is empty, and the summary row keeps a
            # stale number under it (25 on both fresh sheets on file): that stale
            # number equalling the price must never pass for a typed one.
            if (sheet["listing_price"] == price and sheet["typed"] in (None, price)
                    and not sheet["empty"] and sheet["field"] is not None):
                break
            self.log(
                "price did not take (%s): box %s, summary %s, wanted %d"
                % ("character" if unicode_first else "keycode",
                   sheet["typed"], sheet["listing_price"], price)
            )
        else:
            self.log("neither way of typing took, backing out")
            self.cancel_setprice(lines)
            return False
        if sheet["confirm_line"] is None or not self.click_line(
            sheet["confirm_line"], "confirm the price"
        ):
            return False
        st, _, _ = self.wait_for(("sell",), 6.0)
        if st != "sell":
            self.log("did not return to the sell grid (state %s)" % st)
            return False
        return True

    def sell_batch(self, sweep: bool = False, want: int = 0) -> int:
        """List every unlisted crate we are allowed to sell, in one basket.

        A regular trip reads the first screen, where Newest puts the fresh buys, and
        pages down only when that screen holds fewer than the `want` crates it came
        for: infinite, 2026-09-28, the game's sort is sometimes wrong and a fresh crate
        sits further down, where a first-screen trip never saw it. Once it pages it
        keeps going to the bottom or a full basket, so crates left out by earlier
        trips go up too. A sweep starts at the top and pages down the whole grid.

        Selling one named crate at a time meant finding one exact serial, and a
        serial Vision truncated differently between frames meant the sale simply did
        not happen. Nothing in the inventory shows SELL NOW unless we own it and have
        not listed it, so the grid already knows what needs selling; the allowlist is
        what keeps that from meaning the gloves and the packages.
        """
        if not self.goto_tab("SELL", ("sell",)):
            return 0
        if not self.ensure_sell_view():
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        # Two reads, because selling a basket that already holds infinite's own items
        # would sell them at whatever price he gave them. One frame where Vision
        # drops the REMOVE label is not evidence that the basket is empty.
        empty = 0
        for _ in range(2):
            _, lines = self.read()
            if find_loose(lines, "REMOVE") is not None:
                self.log("the sell basket is not empty, leaving it alone")
                self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
                return 0
            empty += 1
            self.sleep(0.3)
        staged = []
        pages = 0
        reached_end = False
        self._sweep_complete = False
        if sweep:
            _, lines = self.read()
            for _ in range(6):
                lines = self.page_grid(lines, -15)
                if lines is None:
                    break
        lines = None
        refused = set()          # under the price floor: skipped, and never picked again
        misses: Dict[str, int] = {}   # SELL NOW clicks that opened no price sheet, by serial
        paging = sweep
        settle = True            # the grid was just scrolled, or the tab just opened
        while len(staged) < max(1, self.s.sell_batch_max):
            if self.stop_event.is_set():
                break
            if lines is None or settle:
                lines = self._still_grid(lines)
                settle = False
            done = {c["serial"] for c in staged} | refused
            waiting = [
                c
                for c in parse_cards(lines, 0)
                if c.button == "SELL NOW"
                and c.is_case                      # cases only, whatever the table holds
                and is_sellable(c.name, self.s)
                and c.button_line is not None
                and c.serial                       # no serial means no way to confirm it later
                and c.serial not in done
            ]
            if not waiting:
                nxt = None
                if not paging and len(staged) < want:
                    self.log("%d of the %d crates this trip is for are not on the first screen, "
                             "scrolling for them" % (want - len(staged), want))
                    paging = True
                if paging and pages < SWEEP_SCROLLS:
                    nxt = self.page_grid(lines, self._page_notches(lines))
                    reached_end = nxt is None and not self._page_failed
                if nxt is None:
                    break
                pages += 1
                lines = nxt          # page_grid waited for it to settle
                continue
            # higher in the crate table lists first; the grid's own order within one crate
            card = min(waiting, key=lambda c: crate_rank(c.name, self.s))
            price = sell_price_for(card.name, self.s)     # is_sellable: never None here
            paid = self.paid_for(card.serial)
            if under_sell_floor(price, self.s, card.name, paid):
                self.log("REFUSED to list %s %s at %s"
                         % (card.name, card.serial, floor_complaint(price, self.s, card.name, paid)))
                refused.add(card.serial)
                continue             # nothing was clicked, so this frame still holds
            lines = None             # staging changes the grid, so the next pass reads it
            if self.s.dry_run:
                # Walk the whole batch without touching anything, so the multi-item
                # path can be rehearsed rather than only ever showing the first one.
                self.log("would list %s %s at %d (dry run)" % (card.name, card.serial, price))
                staged.append({"serial": card.serial, "name": card.name, "price": price})
                continue
            if not self.stage_one(card, price):
                _, lines = self.read()
                state = self.state_of(lines, None)
                if state == "sell" and sum(misses.values()) < SELL_MISSES:
                    # Back on the grid with nothing open: the click missed, or the sheet
                    # was backed out of. Ending the trip on that ended whole sweeps
                    # (infinite, 2026-10-02). Look again; a crate missed twice waits
                    # for the next trip.
                    misses[card.serial] = misses.get(card.serial, 0) + 1
                    if misses[card.serial] >= 2:
                        refused.add(card.serial)
                    self.log("%s %s did not stage, looking again" % (card.name, card.serial))
                    lines, settle = None, True
                    continue
                # Whatever is already in the basket is ours and correctly priced, so
                # stop adding and sell those rather than abandoning them: a basket
                # left full blocks every later batch.
                self.log("could not stage %s, selling what is already in" % card.name)
                if state == "setprice":
                    self.cancel_setprice(lines)
                break
            staged.append({"serial": card.serial, "name": card.name, "price": price})
            self.log("staged %s %s at %d" % (card.name, card.serial or "", price))
            if not sale_clears_cost(price, self.paid_for(card.serial), self.s):
                self.log(
                    "note: %d after tax does not beat the %d paid for this one"
                    % (price, self.paid_for(card.serial))
                )
        if not staged:
            # a sweep is finished only when it saw the bottom with nothing left to stage
            self._sweep_complete = sweep and reached_end
            self.log("nothing of ours is waiting to be listed")
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        if self.s.dry_run:
            self.log("would sell a basket of %d (dry run)" % len(staged))
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return len(staged)
        lines = self.check_basket(staged)
        if lines is None:
            self._empty_basket()
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        basket = find_loose(lines, "SELL ITEMS")
        if basket is None or not self.click_line(basket, "sell the basket of %d" % len(staged)):
            self.log("SELL ITEMS not found")
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        st, _, lines = self.wait_for(("sell_confirm",), 6.0)
        if st != "sell_confirm":
            self.log("sell confirmation did not open (state %s)" % st)
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        yes = find_confirm(lines)
        if yes is None or not self.click_line(yes, "confirm the sale"):
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return 0
        sold = self.confirm_listed(staged)
        self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
        return sold

    def check_basket(self, staged):
        """The frame to sell from once the basket reads back what was typed, else None.

        infinite, 2026-09-28: a 39 went in as 3 and still passed the Set Price check.
        The basket panel is what SELL ITEMS lists at (parse_basket), so every staged
        crate must show there at the price it was typed at before anything is sold.
        A row Vision missed gets another frame; a wrong price never sells.
        """
        want = sorted(i["price"] for i in staged)
        got = img = None
        for i in range(BASKET_READS):
            if i:
                self.sleep(0.3)
            img, lines = self.read()
            rows = basket_rows(lines)
            got = [p if p is not None else self._row_price(img, head) for head, p in rows] or None
            if got is not None and None not in got and sorted(got) == want:
                return lines
        self.log("REFUSED to sell the basket: it reads %s, the crates were typed at %s, "
                 "taking them back out to list again next trip" % (got, want))
        self.bail_out(None, img)        # keeps the frame in failures/
        return None

    @staticmethod
    def _row_price(img, head: Line) -> Optional[int]:
        """A basket price the frame's read missed, from its own box at a few scales."""
        if img is None:
            return None
        box = (head.x - head.w * 0.5, head.y + head.h, head.x + head.w * 2.5, head.y + head.h * 4)
        for k in (1.0, 2.0, 0.5):
            crop = _scaled_crop(img, box, k)
            digits = [l.text.strip() for l in ocr(crop)] if crop is not None else []
            digits = [d for d in digits if PRICE_RE.match(d)]
            if digits:
                return int(digits[0])
        return None

    def _empty_basket(self) -> None:
        """Take everything back out of the basket, so the next trip stages it afresh.

        A basket still holding anything turns every later trip away ('the sell basket
        is not empty'), so this goes on until two reads running show no REMOVE.
        """
        clear = 0
        for _ in range(2 * max(1, self.s.sell_batch_max) + 2):
            _, lines = self.read()
            remove = find_loose(lines, "REMOVE")
            if remove is None:
                clear += 1
                if clear == 2:
                    return
                self.sleep(0.3)
                continue
            clear = 0
            if not self.click_line(remove, "take a crate back out of the basket"):
                break
            self.sleep(0.5)
        self.log("the sell basket would not empty, it needs clearing by hand")

    def _still_grid(self, lines=None):
        """A frame of the grid once it has stopped moving: two reads in a row agree.

        A scroll, and the bounce Roblox puts on a scroll past either end, keeps the
        grid gliding for a moment after the wheel stops, and a button read off a
        moving grid is clicked where it was: a sweep's SELL NOW landed beside its
        button and the sweep ended there (infinite, 2026-10-02).
        """
        if lines is None:
            _, lines = self.read()
        for _ in range(STILL_READS):
            self.sleep(STILL_GAP)
            _, now = self.read()
            if not self._grid_moved(lines, now):
                return now
            lines = now
        return lines

    def confirm_listed(self, staged) -> int:
        """Poll until the staged crates read DELIST with an empty basket."""
        listed = set()
        for _ in range(VERIFY_READS):
            self.sleep(0.5)
            try:
                _, lines = self.read()
            except RuntimeError as exc:
                self.log("verify read failed: %s" % exc)
                continue
            if find_loose(lines, "REMOVE") is not None:
                continue          # still sitting in the basket, not sold yet
            on_sale = [
                c.serial for c in parse_cards(lines, 0) if c.button == "DELIST" and c.serial
            ]
            for item in staged:
                # Accumulate: a crate confirmed on an earlier frame stays confirmed,
                # rather than being forgotten because a later frame read it badly.
                # Prefix either way, because Vision truncates serials between frames.
                if item["serial"] in listed:
                    continue
                if any(
                    seen == item["serial"]
                    or seen.startswith(item["serial"])
                    or item["serial"].startswith(seen)
                    for seen in on_sale
                ):
                    listed.add(item["serial"])
            if len(listed) == len(staged):
                break
            # A sweep stages crates from pages it has since scrolled past, so they
            # cannot be seen to read DELIST. An emptied basket is the sale going
            # through; only a crate still showing SELL NOW is evidence it did not.
            waiting = [c.serial for c in parse_cards(lines, 0) if c.button == "SELL NOW" and c.serial]
            for item in staged:
                if item["serial"] not in listed and not any(
                    w == item["serial"] or w.startswith(item["serial"]) or item["serial"].startswith(w)
                    for w in waiting
                ):
                    listed.add(item["serial"])
            break
        for item in staged:
            ok = item["serial"] in listed
            self.log(
                "%s %s at %d" % ("listed" if ok else "NOT confirmed",
                                 item["name"], item["price"])
            )
            if ok:
                self._note_listed(item["name"], item["serial"])
            self.record_sale(
                Card(item["name"], None, None, Line(item["name"], 1.0, 0, 0, 0, 0),
                     None, None, None, item["serial"], True),
                item["price"], self.paid_for(item["serial"]), None, ok,
            )
        return len(listed)

    def paid_for(self, serial: Optional[str]) -> int:
        """What we paid for a serial, from this session or the purchase log.

        Reading the file as well matters after a restart: crates bought in an earlier
        session are still in the inventory waiting to be listed, and without it every
        one of them records as paid 0 and the loss warning never fires. The newest
        record wins, and a serial Vision read as '#3.821.642' still finds the
        '#3,821,642' it was bought as, which an exact match never did.
        """
        # A "sell" or "sold" line carries the listing price under the same key, and
        # read as the cost it would make every crate look bought at what it sold for.
        rec = self._latest(serial, lambda r: r.get("kind") not in ("sell", "sold"))
        return (rec.get("price") or 0) if rec else 0

    def listed_price_for(self, serial: Optional[str], name: str,
                         ledger: Optional[List[dict]] = None) -> Optional[int]:
        """What this crate was listed at: its newest confirmed listing, else the table's price.

        A sale pays out of the price the crate was listed at, not the table's price
        today: listed at 44 and the table changed to 38 since, it still pays 41. So
        the sale matcher and the profit go by the listing, and by the table only for
        a crate with none on record. ledger is purchases.jsonl already read, for a
        caller asking about many crates at once.
        """
        rec = self._latest(serial, lambda r: r.get("kind") == "sell" and r.get("ok") and not r.get("dry_run"),
                           ledger)
        try:
            price = _as_price(rec.get("price")) if rec else None
        except ValueError:
            price = None
        return price if price is not None else table_sell_price(name, self.s)

    def _latest(self, serial: Optional[str], want, ledger: Optional[List[dict]] = None) -> Optional[dict]:
        """The newest record for this serial that want(rec) accepts, or None.

        This session's history first, then the file, read here unless ledger is it
        already. Serials compare by same_serial, so '#3.821.642' finds '#3,821,642'.
        """
        def newest(rows):
            return next((r for r in reversed(rows)
                         if isinstance(r, dict) and same_serial(r.get("serial"), serial) and want(r)), None)

        if not _serial_digits(serial):
            return None                  # nothing to look it up by
        hit = newest(self.history)
        return hit if hit is not None else newest(self._read_ledger() if ledger is None else ledger)

    def _read_ledger(self) -> List[dict]:
        """purchases.jsonl, oldest first; [] when it is missing or will not parse."""
        try:
            with open(PURCHASES_PATH) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except (OSError, ValueError):
            return []

    def sell_item(self, name: str, serial: Optional[str], paid: int) -> bool:
        """List the item we just bought, then come back to the market."""
        self.log("selling %s %s (paid %s)" % (name, serial or "", paid))
        # Before a single click: the price is fixed by the name, and card.name is
        # checked equal to name below, so this is the price it would be listed at.
        price = sell_price_for(name, self.s)
        if price is None:
            self.log("not listing %s: the crate list gives it no sell price" % name)
            return False
        if under_sell_floor(price, self.s, name, paid):
            self.log("REFUSED to list %s at %s" % (name, floor_complaint(price, self.s, name, paid)))
            return False
        if not self.goto_tab("SELL", ("sell",)):
            return False
        if not self.ensure_sell_view():
            return False
        _, lines = self.read()
        if find_loose(lines, "REMOVE") is not None:
            # Something is already staged in the basket, from an aborted run or from
            # infinite himself. Selling the basket would sell that too, at whatever
            # price it was given, so leave it alone and say so.
            self.log("the sell basket is not empty, leaving it alone")
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return False
        card = self.find_owned(lines, serial, name)
        if card is None:
            self.log("could not find %s %s in the inventory" % (name, serial or ""))
            return False
        if card.name != name:
            self.log("serial %s is a %s, not a %s, leaving it" % (serial, card.name, name))
            return False
        if not card.is_case:
            # whatever cases_only says: that one is about buying, and the bot never
            # lists anything but a case, however the crate table came to name it
            self.log("%s %s is not a case, leaving it" % (card.name, serial or ""))
            return False
        rap = parse_rap(lines, card)
        self.log("listing %s at %d (rap %s, paid %d)" % (card.name, price, rap, paid))
        if not sale_clears_cost(price, paid, self.s):
            self.log(
                "note: %d after %d%% tax does not beat the %d paid"
                % (price, round(self.s.sell_tax * 100), paid)
            )
        if not self.click_line(card.button_line, "list %s" % card.name):
            return False
        st, _, lines = self.wait_for(("setprice",), 6.0)
        if st != "setprice":
            self.log("the price sheet did not open (state %s)" % st)
            return False
        sheet = parse_setprice(lines)
        if sheet["field"] is None or sheet["confirm_line"] is None:
            self.log("price sheet did not parse, backing out")
            self.cancel_setprice(lines)
            return False
        if self.s.dry_run:
            self.log("would list %s at %d (dry run)" % (card.name, price))
            self.cancel_setprice(lines)
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return True
        pt = to_global(self.bounds, self.scale, sheet["field"])
        # Two ways to type: the keystroke carrying the character, and the bare virtual
        # keycode. Which one a game's text box listens to is not knowable from here,
        # so try one, read the box back, and try the other before giving up.
        for unicode_first in (True, False):
            if not self.wait_idle(self.s, self.stop_event):
                return False
            if not self.type_digits_at(
                pt, str(price), self.pid, unicode_first=unicode_first, hold_ok=self._hold
            ):
                self.log("could not reach the price field")
                self.cancel_setprice(lines)
                return False
            self.sleep(0.5)
            _, lines = self.read()
            sheet = parse_setprice(lines)
            # The summary row is what the game computed from the field, so it is the
            # number that gets listed. The box's own digits render small and faint
            # and Vision misses them on some frames: requiring both called a correct
            # 65 a failure, and the retry then typed into the middle of it. Treat the
            # box as a contradiction check only, and its placeholder as proof it is
            # empty, whatever stale number the summary row still shows.
            if (sheet["listing_price"] == price and sheet["typed"] in (None, price)
                    and not sheet["empty"] and sheet["field"] is not None):
                break
            self.log(
                "price did not take (%s): box %s, summary %s, wanted %d"
                % ("character" if unicode_first else "keycode",
                   sheet["typed"], sheet["listing_price"], price)
            )

        else:
            self.log("neither way of typing took, backing out")
            self.cancel_setprice(lines)
            return False
        if sheet["confirm_line"] is None or not self.click_line(
            sheet["confirm_line"], "confirm the price"
        ):
            return False
        st, _, lines = self.wait_for(("sell",), 6.0)
        if st != "sell":
            self.log("did not return to the sell grid (state %s)" % st)
            return False
        basket = find_loose(lines, "SELL ITEMS")
        if basket is None or not self.click_line(basket, "sell the basket"):
            self.log("SELL ITEMS not found")
            return False
        st, _, lines = self.wait_for(("sell_confirm",), 6.0)
        if st != "sell_confirm":
            self.log("sell confirmation did not open (state %s)" % st)
            return False
        yes = find_confirm(lines)
        if yes is None or not self.click_line(yes, "confirm the sale"):
            return False
        # The grid takes a variable moment to redraw the sold copy as DELIST, so poll
        # for it rather than reading once. A single read 1.5 s after the click called
        # a real listing a failure: the Viridian Case at 65 was on the market the
        # whole time. Same shape as the coin counter lagging after a purchase.
        listed = False
        for _ in range(VERIFY_READS):
            self.sleep(0.5)
            try:
                _, lines = self.read()
            except RuntimeError as exc:
                self.log("verify read failed: %s" % exc)
                continue
            # Being gone from SELL NOW is not proof: an item in the basket has already
            # left that state, so an abandoned sale would look like a success. A
            # listed item shows DELIST and leaves the basket empty.
            listed = find_loose(lines, "REMOVE") is None and any(
                c.button == "DELIST"
                and c.serial
                and card.serial
                and (c.serial == card.serial or c.serial.startswith(card.serial))
                for c in parse_cards(lines, 0)
            )
            if listed:
                break
        self.log(
            "listed %s at %d" % (card.name, price)
            if listed
            else "sale not confirmed, %s is not showing DELIST" % card.name
        )
        self.record_sale(card, price, paid, rap, listed)
        if listed:
            self._note_listed(card.name, card.serial)
        self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
        return listed

    def flush_pending_sale(self) -> None:
        """List whatever was bought before this one, if anything."""
        pending, self._pending_sale = self._pending_sale, None
        if not pending:
            return
        try:
            self.sell_item(pending["name"], pending["serial"], pending["paid"])
        except Exception as exc:     # a failed listing must not end the session
            self.log("sell failed: %s" % exc)
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))

    def cancel_setprice(self, lines) -> None:
        sheet = parse_setprice(lines)
        if sheet["cancel_line"] is not None:
            self.click_line(sheet["cancel_line"], "cancel the price sheet")

    def record_sale(self, card, price, paid, rap, ok) -> None:
        rec = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "sell",
            "name": card.name,
            "price": price,
            "paid": paid,
            "rap": rap,
            "serial": card.serial,
            "ok": bool(ok),
            "dry_run": bool(self.s.dry_run),
        }
        self._remember(rec)

    def _remember(self, rec: dict) -> None:
        """Keep the record in memory and on disk, with the in-memory list bounded.

        purchases.jsonl is the permanent ledger and it keeps everything. history is
        only the recent tail: the window renders it and paid_for reads it, and
        paid_for already falls back to the file for anything older.
        """
        self.history.append(rec)
        if len(self.history) > MAX_HISTORY:
            drop = len(self.history) - MAX_HISTORY
            del self.history[:drop]
            self.history_dropped += drop
        try:
            with open(PURCHASES_PATH, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            self.log("could not write purchases.jsonl: %s" % exc)

    def keep_frame(self, img, why: str) -> None:
        """Save a frame the bot could not make sense of to failures/, at most one a minute.

        Not finding a tab or the sort control saved nothing, so a friend's report came
        with a log and no picture of what the bot had seen.
        """
        if img is None or time.time() - getattr(self, "_kept_at", 0.0) < 60:
            return
        self._kept_at = time.time()
        path = os.path.join(FAILURES_DIR, "%s-%s.png" % (time.strftime("%Y%m%d-%H%M%S"), why))
        try:
            save_png(img, path)
            prune_failures()
            self.log("saved what it saw to %s" % path)
        except (OSError, RuntimeError) as exc:
            self.log("could not save %s: %s" % (path, exc))

    def bail_out(self, lines, img) -> None:
        if img is not None:
            save_png(
                img, os.path.join(FAILURES_DIR, time.strftime("%Y%m%d-%H%M%S") + ".png")
            )
            prune_failures()
        if lines is not None and find_text(lines, "Confirm Purchase") is not None:
            close = parse_modal(lines).get("close_line")
            if close is not None:
                self.click_line(close, "close after failure")

    def record(self, card, modal, before, after, ok, reason="") -> None:
        rec = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "name": modal["name"] or card.name,
            "price": modal["price"],
            "serial": card.serial,
            "seller": card.seller,
            "balance_before": before,
            "balance_after": after,
            "ok": bool(ok),
            "reason": reason,
            "dry_run": bool(self.s.dry_run),
        }
        self._remember(rec)

    # -- loop

    def cycle(self) -> None:
        # One pass at a time on the window, whichever bot runs it (see WINDOW_LOCK).
        with WINDOW_LOCK:
            self._cycle()

    def _cycle(self) -> None:
        self.heartbeat()
        if not self._begun:
            self.begin()
        if self._focused and not self.focus_ok():
            return
        if self._recount_at is not None and time.time() >= self._recount_at:
            self._recount_at = None
            self._scan_wanted.set()
        if self._scan_wanted.is_set():
            # Ahead of the wait for funds: a dry wallet is exactly when infinite
            # wants to see what is still sitting in the inventory.
            self._scan_wanted.clear()
            try:
                self.recount()
            except Exception as exc:      # a failed scan must not end the session
                self.log("inventory scan failed: %s" % exc)
            return
        if self._sell_wanted.is_set():
            # infinite's Sell all cases button: the out-of-coins sweep, on demand
            self._sell_wanted.clear()
            self.log("selling every case in the inventory, as asked")
            self.run_sell_batch(everything=True)
            return
        if self.out_of_funds:
            self.wait_for_funds()
            return
        if (
            self.s.auto_sell
            and self._unsold >= max(1, self.s.sell_batch_size)
            and not self.stop_event.is_set()
        ):
            self.run_sell_batch()
            return
        img, lines = self.read()
        state = self.state_of(lines, img)
        if state == "modal_scrolled":
            self._unscroll_modal()          # then it is an ordinary stray window
            return
        if state in ("modal", "confirm"):
            self.recover(lines, state)
            return
        if state == "loading":
            self.sleep(1.0)
            return
        if state == "market_nofilter":
            # the filter column is scrolled up, so evaluate what is on screen first
            # and then go looking for the button rather than stalling
            if self.check_market_filter(lines):
                return
            balance = parse_balance(lines)
            self.note_balance(balance)
            self.check_funds(balance)
            if self.out_of_funds or self.try_buy(lines, balance):
                return
            self.reveal_apply_filter(lines)
            return
        if state in ("sell", "setprice", "sell_confirm"):
            self.log("left on the sell tab, going back to the market")
            if state == "setprice":
                self.cancel_setprice(lines)
            elif state == "sell_confirm":
                # MARKET sits behind this dialog, so dismiss it before reaching for
                # the tab, or the bot loops here forever.
                cancel = find_button(lines, "CANCEL")
                if cancel is not None:
                    self.click_line(cancel, "dismiss the sell confirmation")
                self.sleep(0.6)
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return
        if self.rejoin(state, lines, img):
            self._unknown_frames = 0
            return
        if state in ("market_ready", "cooldown", "market_nofilter", "sell"):
            self._unknown_frames = 0
        if state in ("market_ready", "cooldown"):
            # cards are readable during the ~12 s cooldown too, and the filter panel
            # can be scrolled so APPLY FILTER is off-screen for the whole session:
            # only the refresh click needs that button, evaluating does not.
            # The wallet is checked before every buying pass, not just after a
            # purchase: a run that starts poor, or one whose coins went elsewhere,
            # used to refresh and OCR all night instead of waiting for a sale.
            if self.check_market_filter(lines):
                return
            balance = parse_balance(lines)
            self.note_balance(balance)
            self.check_funds(balance)
            if self.out_of_funds or self.try_buy(lines, balance):
                return
            if state == "market_ready":
                self.refresh(lines)
            else:
                # counting down: once a refresh, the rows under the first screen
                if self._peek_due and self.peek_below(lines, balance):
                    return
                # A full read every pass is what lets a bargain listed mid-countdown
                # be caught, so the OCR pass still comes round about once a second;
                # in between, the pill is watched by colour alone.
                self.cooldown_wait()
            return
        # Nothing readable. Most often a transient frame, but it is also what an AFK
        # kick or a disconnect looks like, so say so rather than spinning quietly.
        self._unknown_frames += 1
        if self._unknown_frames % 60 == 0:
            self.log(
                "the market has been unreadable for %d frames: kicked, disconnected, "
                "or the window moved?" % self._unknown_frames
            )
        if self._unknown_frames in (60, 600):
            # 2026-09-29: 15 minutes stuck after a disconnect, and nothing on file to
            # say what the screen showed. Keep what it looks like, so it can be taught.
            self.bail_out(None, img)
        self.sleep(1.0)

    def price_floor(self) -> int:
        """The lowest buy price in the crate table: below it the wallet cannot fund a buy.

        0 when the table buys nothing at all, the same as the old caps at 0: there is
        no sum worth waiting for, and nothing on the market will pass decide().
        """
        buys = [row.get("buy") for row in (self.s.crates or {}).values() if row.get("buy")]
        return min(buys) if buys else 0

    def sell_everything(self) -> int:
        """Every sellable crate in the inventory, a basket at a time, until none are left."""
        total = 0
        for _ in range(MAX_BASKETS):
            # a Scan pressed mid-sweep runs next cycle, not after up to 40 baskets
            if self.stop_event.is_set() or self._scan_wanted.is_set():
                break
            sold = self.sell_batch(sweep=True)
            total += sold
            # a dry run lists nothing, so the same crates would come round forever
            if not sold or self.s.dry_run:
                break
        self.log("listed %d from the whole inventory" % total)
        return total

    def run_sell_batch(self, everything: bool = False) -> None:
        """A selling trip, outside any held focus, with its own idle gating.

        The counter only clears when a batch actually lists something, so a trip that
        bails leaves the crates queued for the next one instead of forgetting them.
        """
        want = 0 if everything else self._unsold
        try:
            sold = self.sell_everything() if everything else self.sell_batch(want=want)
        except Exception as exc:     # a failed sale must not end the session
            self.log("sell batch failed: %s" % exc)
            self.goto_tab("MARKET", ("market_ready", "market_nofilter", "cooldown"))
            return
        if sold:
            # A crate bought seconds ago can take a while to show on the SELL tab, so
            # a shortfall is looked for once more on the next trip, then left to the
            # out-of-coins sweep rather than making every trip page the whole grid.
            short = max(0, want - sold)
            if short and not self._carried:
                self.log("%d crate(s) of this trip not found yet, looking again next trip" % short)
                self._unsold, self._carried = short, True
            else:
                self._unsold, self._carried = 0, False
        else:
            # nothing of ours was waiting, so stop asking every cycle
            self._unsold = 0 if self._nothing_to_sell else self._unsold
            self._nothing_to_sell = True

    def done(self) -> bool:
        # Being out of coins is a pause, not an ending: see wait_for_funds.
        if not self.s.dynamic_purchases and self.bought >= self.s.max_purchases:
            return True
        if not self.s.dynamic_spend and self.spent >= self.s.max_spend:
            return True
        return False

    def run(self) -> None:
        self.attach()
        self.begin()
        self.log("running (dry_run=%s)" % self.s.dry_run)
        while not self.stop_event.is_set() and not self.done():
            try:
                self.cycle()
            except Exception as exc:  # a bad frame must not kill the session
                # The whole traceback, not just the message. "cycle error: None" told
                # us nothing the morning after.
                self.log("cycle error: %s\n%s" % (exc, traceback.format_exc()))
                self.sleep(1.0)
        if self.s.auto_sell and self._unsold and not self.stop_event.is_set():
            self.log("listing what is left before stopping")
            with WINDOW_LOCK:
                self.run_sell_batch()
        self.log(
            "stopped: bought %d, spent %d, sold %d for %+.2f profit%s%s"
            % (self.bought, self.spent, self.sold_count, self.profit,
               " (%d at a cost not on record)" % self.unknown_cost_count if self.unknown_cost_count else "",
               " (waiting on sales)" if self.out_of_funds else "")
        )
        self.end_focus()
        self._flush_repeats()

    def start(self) -> threading.Thread:
        self.stop_event.clear()
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.stop_event.set()


def scan_once(settings: Settings, on_status=None, bot: Optional[Bot] = None) -> Optional[dict]:
    """Scan the inventory with no run going, for a Scan button while stopped.

    Call it from a worker thread, like a run: it reads the screen and clicks. The
    snapshot, or None when Roblox is not there, the scan could not start, or a run
    already holds the window. Pass `bot` to keep a handle that can stop() it.
    """
    bot = bot or Bot(settings, on_status=on_status)
    return _alone_on_window(bot, "inventory scan", bot.scan_inventory, None)


def sell_all_once(settings: Settings, on_status=None, bot: Optional[Bot] = None) -> int:
    """List every sellable crate with no run going, for Sell all cases while stopped.

    The same sweep a run makes when it is out of coins: named crates with a sell
    price only, a basket at a time, each basket read back before it sells. How many
    were listed; 0 when Roblox is not there or a run already holds the window.
    """
    bot = bot or Bot(settings, on_status=on_status)

    def job():
        # the saved snapshot first, so each listing marks it rather than a blank one
        bot.load_inventory()
        return bot.sell_everything()

    return _alone_on_window(bot, "selling everything", job, 0)


def _alone_on_window(bot: Bot, what: str, job, failed):
    """Run job() on a stopped bot that holds the window alone; `failed` if it cannot."""
    if not WINDOW_LOCK.acquire(blocking=False):
        bot.log("%s not started: another bot is driving the window" % what)
        return failed
    try:
        bot.attach()
        if bot.s.focus_mode:
            # the tick box counts for these too, not only for a run's begin()
            bot.start_focus()
        return job()
    except Exception as exc:      # not found, gone midway: say so, never raise into a GUI
        bot.log("%s failed: %s" % (what, exc))
        return failed
    finally:
        bot.end_focus()
        WINDOW_LOCK.release()
        bot._flush_repeats()


# --------------------------------------------------------------------------- cli


def report(lines, img, s: Settings) -> None:
    print("ui_state: %s" % ui_state(lines, img))
    print("balance: %s" % parse_balance(lines))
    af = find_text(lines, "APPLY FILTER")
    print("apply filter: %s" % (("%r at x=%.0f y=%.0f" % (af.text, af.cx, af.cy)) if af else "not readable"))
    cards = parse_cards(lines, img_size(img)[0] if img is not None else 0)
    print("cards: %d" % len(cards))
    for c in cards:
        buy, why = decide(c, s)
        print(
            "  %-26s price=%-5s button=%-7s serial=%-12s seller=%-18s -> %s (%s)"
            % (
                c.name[:26],
                c.price,
                c.button,
                c.serial,
                (c.seller or "")[:18],
                "BUY" if buy else "no",
                why,
            )
        )
    modal = parse_modal(lines)
    if modal["purchase_line"] is not None:
        print("modal: %r price=%s" % (modal["name"], modal["price"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="casebot")
    ap.add_argument("--probe", action="store_true", help="read the live window, no input events")
    ap.add_argument("--parse", metavar="PNG", help="read a fixture instead of the screen")
    ap.add_argument("--run", action="store_true", help="headless loop with the active config")
    ap.add_argument("--config", metavar="NAME", help="use this config from configs\\ instead")
    ap.add_argument(
        "--typetest",
        action="store_true",
        help="type into the market search box to see which keystroke style Roblox takes",
    )
    args = ap.parse_args(argv)
    active_config()         # an old settings.json moves in first, so it can be named
    if args.config:
        match = next((n for n in list_configs() if n.lower() == args.config.lower()), None)
        if match is None:
            print("no config %r in %s: have %s" % (args.config, CONFIGS_DIR, ", ".join(list_configs())))
            return 1
        args.config = match
    s = Settings.load(config_path(args.config) if args.config else None)

    if args.parse:
        img = load_png(args.parse)
        print("%s  %dx%d  brightness=%.1f" % ((args.parse,) + img_size(img) + (mean_brightness(img),)))
        report(ocr(img), img, s)
        return 0

    if args.probe:
        pid, wid, b = find_roblox()
        print("roblox pid=%s window=%s bounds=%s" % (pid, wid, b))
        img = grab(wid)
        W, H = img_size(img)
        print("capture %dx%d scale=%.2f brightness=%.1f" % (W, H, W / b["Width"], mean_brightness(img)))
        report(ocr(img), img, s)
        return 0

    if args.typetest:
        # The sell flow's one unverifiable step is whether synthetic keystrokes reach
        # a Roblox text box at all. The market's own search box is the safe place to
        # find out: typing in it buys nothing and sells nothing.
        bot = Bot(s, on_status=lambda m: print(m, flush=True))
        bot.attach()
        img, lines = bot.read()
        box = find_text(lines, "Search for Items")
        if box is None:
            print("open the MARKET tab with the filter panel scrolled to the top first")
            return 1
        pt = to_global(bot.bounds, bot.scale, box)
        for style, unicode_first in (("character", True), ("keycode", False)):
            type_digits_at(pt, "4321", bot.pid, unicode_first=unicode_first)
            time.sleep(0.6)
            _, after = bot.read()
            got = find_text(after, "4321") is not None
            print("%-10s keystrokes: %s" % (style, "WORKS" if got else "ignored"))
            type_digits_at(pt, "1", bot.pid, unicode_first=unicode_first)
            time.sleep(0.3)
        # leave the box as we found it
        def clear(pt):
            for _ in range(8):
                _press(KEY_DELETE)
        _burst(bot.pid, pt, clear)
        print("search box cleared")
        return 0

    if args.run:
        bot = Bot(s, on_status=lambda m: print(m, flush=True))
        try:
            bot.run()
        except KeyboardInterrupt:
            bot.stop()
        return 0

    ap.print_help()
    return 1


# --------------------------------------------------------------------------- windows


if WINDOWS:
    # The same bot on Windows: winplat.py stands in for every macOS call above.
    from winplat import (  # noqa: F401  (rebinds the macOS versions)
        GRAB_ERROR, find_roblox, grab, grab_region, notify, load_png, save_png, img_size,
        _sample, _luma_grid, _rgb_grid, _scaled_crop, _session_idle, _input_idle, _peak_mb,
        _topmost_pid_at, _active_pid, _cursor_at, _cursor_point,
        _app_for, _activate, _point, _warp, _send, _mouse_ev, _wheel_ev, _key_ev,
        DIGIT_KEYS, KEY_A, KEY_DELETE, KEY_RIGHT, read_text, ocr_engine, _DEVICE,
    )

    # RapidOCR's model is Chinese and English, and it reads the game's icons as CJK
    # glyphs: the menu as '口' and '三', the MARKET tab's shop icon as '司'. Glued
    # onto a word ('MARKET司') one stops an exact match. The game writes English.
    _CJK = re.compile("[⺀-〿぀-鿿가-힯豈-﫿＀-￯]+")

    def ocr(img) -> List[Line]:
        """RapidOCR in place of Vision (winplat.read_text), as the same Lines."""
        out = []
        for t, c, x, y, w, h in read_text(img):
            t = " ".join(_CJK.sub(" ", t.translate(_LATIN)).split())
            if t:
                out.append(Line(t, c, x, y, w, h))
        out.sort(key=lambda l: (l.y, l.x))
        return out


if __name__ == "__main__":
    sys.exit(main())
