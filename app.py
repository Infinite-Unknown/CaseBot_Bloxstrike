"""casebot GUI: a dashboard with the live log, the crate table, the other settings in
tabs, start/stop, history, and an overlay.

Tk owns the main thread. Every Quartz / Vision / AppKit call happens in a worker
thread (the bot, or an inventory scan while it is stopped) and reaches the GUI only
as a message on a queue.
"""

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import asdict, replace
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Dict, Optional, Tuple

import casebot as cb

POLL_MS = 200
PAD = 12
DEFAULTS = cb.Settings()
ANY = "(any)"       # how the rarity box shows a blank filter_rarity: leave the rarity alone
# The game takes 5% of every sale (cb.GAME_TAX), so the Sale tax box refuses anything
# less, which would pass losing prices; anything past half is a typo, not a tax.
MAX_TAX = 0.5
# Every way Tk turns a scroll or an arrow into a new value. On Tk 9.0.3 one wheel or
# trackpad pass, or an arrow click, over a blank ttk.Spinbox set it to 1, and a crate
# price of 1 gives the crate away: a box holding a number changes only by typing.
NUDGES = ("<MouseWheel>", "<TouchpadScroll>", "<Button-4>", "<Button-5>", "<<Increment>>", "<<Decrement>>")
TILES = (
    ("coins", "Coins now"),
    ("bought", "Cases bought"),
    ("listed", "Cases listed"),
    ("sold", "Cases sold"),
    ("profit", "Profit this run"),
    ("worth", "Inventory worth"),
    ("cases", "Cases held"),
    ("scan", "Last scan"),
)
# A tone as a label style: the Profit tile and the Earn per flip column go green or red.
TILE_STYLE = {"": "TLabel", "good": "Good.TLabel", "bad": "Bad.TLabel"}
EARN_STYLE = {"": "Hint.TLabel", "good": "Good.TLabel", "bad": "Bad.TLabel"}
# The crate table's columns, in characters, and how tall its scrolling view is: the
# ten default crates and the header fit without a scroll, and a long list cannot
# push the window past a 1080p screen.
NAME_CHARS, PRICE_CHARS, EARN_CHARS = 24, 8, 28
CRATE_VIEW_PX = 380


def coins(n) -> str:
    """1234 as '1,234', and a dash for a number nobody has read yet."""
    return "-" if n is None else "{:,}".format(n)


def signed(x: float) -> str:
    """1 as '+1.00', -0.4 as '-0.40', and nothing either way as '0.00'."""
    x = round(x, 2) + 0.0       # + 0.0 turns a -0.0 into 0.0, which would read '-0.00'
    return "%+.2f" % x if x else "0.00"


def tone(x: float) -> str:
    """'good' above 0, 'bad' below, '' at 0: the colour of a profit (TILE_STYLE)."""
    x = round(x, 2)
    return "good" if x > 0 else "bad" if x < 0 else ""


def earn(buy: str, sell: str, tax: float, lowest_buy: int = 0) -> Tuple[str, str]:
    """The Earn per flip cell for a crate's two price boxes as typed: (text, tone).

    One crate bought at buy and sold at sell earns what the game pays for the sale
    (cb.payout, floored) less buy: 35 and 38 at 5% make +1.00, since 38 * 0.95 is
    36.1 and the game pays 36. At or under 0 it is red, by the same test
    Settings.clean applies (at a tax never under the game's, cb.floor_tax), so a
    red row is one whose sell price is dropped and never listed.
    """
    try:
        b = cb._as_price(buy, zero_is_none=True)
    except ValueError:
        return "Buy up to is not a price", "bad"
    try:
        s = cb._as_price(sell)
    except ValueError:
        return "Sell at is not a price", "bad"
    if b is None:
        if s is not None and lowest_buy and cb.payout(s, cb.floor_tax(tax)) <= lowest_buy:
            # not refused (a sell-only crate may be worth little), but a typo of 6
            # for 60 reads exactly like this, so say so
            return "never bought, pays %d: check this price" % cb.payout(s, cb.floor_tax(tax)), "bad"
        return ("never bought" if s is not None else "never bought or listed"), ""
    if s is None:
        return "not listed", ""
    gain = cb.payout(s, cb.floor_tax(tax)) - b
    if gain < 0:
        return "%s: a loss, not listed" % signed(gain), "bad"
    if gain == 0:
        return "0.00: break-even", ""
    return signed(gain), "good"


def last_scan(totals: dict) -> Tuple[str, str]:
    """The Last scan tile: the clock time big, the day and any caveat small under it."""
    when = totals["scanned_at"]
    if not when:
        return "never", "no scan yet"
    day, _, clock = when.partition("T")
    notes = [day]
    if totals.get("unsure_since"):
        # coins came in that no listed crate explained (Bot._note_sold)
        notes.append("may have sold since %s" % totals["unsure_since"].partition("T")[2][:5])
    elif not totals["complete"]:
        notes.append("partial scan")
    if totals["unread"]:
        notes.append("%d unread" % totals["unread"])
    return clock[:5], ", ".join(notes)


def history_row(rec: dict) -> tuple:
    """A ledger record as a History row: time, event, crate, price, detail, mode.

    Three kinds share the ledger: a buy (no kind, with the wallet before and after),
    a "sell" (a listing, with what was paid) and a "sold" (a sale, with its profit).
    A sale has no wallet fields, and the old one-shape table read "None -> None".
    """
    kind, ok = rec.get("kind") or "buy", bool(rec.get("ok"))
    if kind == "sold":
        profit = rec.get("profit")
        event = "sold"
        detail = "profit %s" % signed(profit) if profit is not None else "cost not on record"
    elif kind == "sell":
        event = "listed" if ok else "list failed"
        detail = "paid %s" % rec["paid"] if rec.get("paid") else ""
    else:
        event = "bought" if ok else "buy failed"
        before, after = rec.get("balance_before"), rec.get("balance_after")
        detail = rec.get("reason") or ""
        if ok or not detail:
            detail = "wallet %s -> %s" % (coins(before), coins(after)) if before is not None else ""
    return (str(rec.get("time", "")).replace("T", " "), event, rec.get("name", ""), rec.get("price", ""),
            detail, "dry" if rec.get("dry_run") else "live")


def no_nudge(widget):
    """Make a Spinbox, Combobox or Entry ignore the wheel, the trackpad and its arrows (NUDGES)."""
    for event in NUDGES:
        try:
            widget.bind(event, lambda e: "break")
        except tk.TclError:
            pass        # an event this Tk does not have cannot nudge anything either
    return widget


def dashboard(snap: Optional[dict], s: cb.Settings, session=None, balance: Optional[int] = None):
    """What the Dashboard tab shows: {tile: (value, caption, tone)} and the crate rows.

    session is the running bot, or the last one to run, for this run's numbers.
    balance is the running bot's live coin count, which beats the snapshot's.
    """
    stats = cb.inventory_stats(snap, s)
    t = stats["totals"]
    if balance is None and snap:
        balance = snap.get("balance")

    def run(key):
        return getattr(session, key, 0) or 0

    # What the crates sold this run made after the tax, less what each cost: not the
    # coin counter's rises, which count the whole payout of every sale as a gain.
    profit, unknown = run("profit"), run("unknown_cost_count")
    tiles = {
        "coins": (coins(balance), "in the wallet", ""),
        "bought": (coins(run("bought")), "for %s coins" % coins(run("spent")), ""),
        "listed": (coins(run("listed_count")), "put on sale this run", ""),
        "sold": (coins(run("sold_count")), "bought by players this run", ""),
        "profit": (signed(profit),
                   "%d sold at unknown cost" % unknown if unknown else "after the %g%% sale tax" % (s.sell_tax * 100),
                   tone(profit)),
        "worth": (coins(t["worth_after_tax"]), "after the %g%% sale tax" % (s.sell_tax * 100), ""),
        "cases": (coins(t["cases"]), "%s listed / %s waiting" % (coins(t["listed"]), coins(t["unlisted"])), ""),
        "scan": last_scan(t) + ("",),
    }
    rows = sorted(
        ((name, r["held"], r["listed"], r["unlisted"], r["price"], r["worth"])
         for name, r in stats["crates"].items()),
        key=lambda r: (-r[5], r[0]),
    )
    return tiles, rows


class App:
    def __init__(self, root: tk.Tk, settings: Optional[cb.Settings] = None):
        self.root = root
        self.q = queue.Queue()
        # The config the window shows and Save writes: one file in configs\.
        self.config_name = cb.active_config()
        self.settings = settings or cb.Settings.load()
        # Each Ignore box's grey-out, rerun when a config is loaded into the window.
        self._greys = []
        self._ranges = {}       # each slider's own range, widened per config to hold its value
        self.bot = None
        # The run that last finished, so its numbers stay on the dashboard.
        self.last_bot = None
        self.thread = None
        self.scanning = False
        # The bot a scan runs on while stopped, kept so Stop scan and quitting can stop it.
        self.scan_bot = None
        self.overlay = None
        self.bounds = None
        self.seen = 0
        # How many values the last collect() turned away, for the status line.
        self.rejected = 0
        # The dashboard's snapshot while no bot is running. Reading it is only file
        # IO, so the main thread may; a scan itself always runs in a worker.
        self.snapshot = cb.read_inventory()
        self._bad_snap = None
        self._build()
        self._title()
        self._mark_saved()
        self._refresh_dashboard()
        # The table shows this session only. purchases.jsonl keeps every record, so
        # the permanent ledger survives; it is just not replayed into the window.
        self._after = root.after(POLL_MS, self._tick)
        root.protocol("WM_DELETE_WINDOW", self._quit)

    def _spawn(self, target) -> threading.Thread:
        # A seam: tests swap it, so nothing they press starts a thread that reads the screen.
        t = threading.Thread(target=target, daemon=True)
        t.start()
        return t

    # -- ui

    def _build(self):
        base = tkfont.nametofont("TkDefaultFont", root=self.root)
        size = abs(base.actual("size")) or 13
        self.big = base.copy()
        self.big.configure(size=size + 11, weight="bold")
        self.bold = base.copy()
        self.bold.configure(size=size + 2, weight="bold")
        self.head = base.copy()
        self.head.configure(weight="bold")
        style = ttk.Style(self.root)
        style.configure("Big.TButton", font=self.bold, padding=(14, 4))
        style.configure("Hint.TLabel", foreground="gray50")
        style.configure("Good.TLabel", foreground="#1f8a3b")
        style.configure("Bad.TLabel", foreground="#d0342c")

        top = ttk.Frame(self.root, padding=(PAD, PAD, PAD, 0))
        top.grid(row=0, column=0, sticky="ew")
        self.run_btn = ttk.Button(top, text="Start", style="Big.TButton", width=9, command=self.toggle)
        self.run_btn.grid(row=0, column=0)
        self.scan_btn = ttk.Button(top, text="Scan inventory", command=self.scan)
        self.scan_btn.grid(row=0, column=1, padx=(8, 0))
        self.sell_btn = ttk.Button(top, text="Sell all cases", command=self.sell_all)
        self.sell_btn.grid(row=0, column=2, padx=(8, 0))
        # Roblox focused mode: fastest, but the machine is Roblox's until the mouse moves.
        self.focus_mode = tk.BooleanVar(master=self.root, value=bool(self.settings.focus_mode))
        ttk.Checkbutton(top, text="Roblox focused mode (moving the mouse pauses it)",
                        variable=self.focus_mode).grid(row=0, column=3, padx=(PAD, 0))
        self.status = tk.StringVar(master=self.root, value="idle")
        ttk.Label(top, textvariable=self.status, width=64).grid(row=0, column=4, sticky="w", padx=PAD)
        top.columnconfigure(4, weight=1)

        self.vars: Dict[str, tk.Variable] = {}
        self.vars["focus_mode"] = self.focus_mode     # the top bar's tick box, saved like the rest
        self.widgets: Dict[str, tk.Widget] = {}
        self.book = ttk.Notebook(self.root)
        self.book.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)
        for title, build in (
            ("Dashboard", self._build_dashboard),
            ("Crates", self._build_crates),
            ("Config", self._build_config),
            ("History", self._build_history),
        ):
            tab = ttk.Frame(self.book, padding=PAD)
            self.book.add(tab, text=title)
            build(tab)

        bottom = ttk.Frame(self.root, padding=(PAD, 0, PAD, PAD))
        bottom.grid(row=2, column=0, sticky="ew")
        ttk.Button(bottom, text="Save settings", command=self.save).grid(row=0, column=0)
        ttk.Button(bottom, text="Open file location", command=self.open_folder
                   ).grid(row=0, column=1, padx=(8, 0))
        self.save_note = tk.StringVar(master=self.root)
        ttk.Label(bottom, textvariable=self.save_note, style="Hint.TLabel"
                  ).grid(row=0, column=2, sticky="w", padx=PAD)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

    def _build_dashboard(self, tab):
        self.tile_vars = {}
        self.tile_labels = {}
        for i, (key, title) in enumerate(TILES):
            box = ttk.LabelFrame(tab, text=title, padding=(10, 0, 10, 6))
            box.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            value = tk.StringVar(master=self.root, value="-")
            caption = tk.StringVar(master=self.root)
            label = self.tile_labels[key] = ttk.Label(box, textvariable=value, font=self.big)
            label.grid(row=0, column=0, sticky="w")
            ttk.Label(box, textvariable=caption, style="Hint.TLabel").grid(row=1, column=0, sticky="w")
            self.tile_vars[key] = (value, caption)
        for c in range(4):
            tab.columnconfigure(c, weight=1, uniform="tile")
        # What it holds on the left and what it is doing on the right, under the run's
        # numbers: the one tab to watch while it runs.
        held = ttk.LabelFrame(tab, text="Crates held", padding=6)
        held.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=4, pady=(8, 0))
        cols = ("crate", "held", "listed", "not listed", "sell price", "worth after tax")
        self.stock = ttk.Treeview(held, columns=cols, show="headings", height=14)
        for c, w in zip(cols, (180, 50, 55, 75, 70, 105)):
            self.stock.heading(c, text=c)
            self.stock.column(c, width=w, anchor="w" if c == "crate" else "e")
        self.stock.grid(row=0, column=0, sticky="nsew")
        held.rowconfigure(0, weight=1)
        held.columnconfigure(0, weight=1)
        live = ttk.LabelFrame(tab, text="Log", padding=6)
        live.grid(row=2, column=2, columnspan=2, sticky="nsew", padx=4, pady=(8, 0))
        self.log = tk.Text(live, height=16, width=60, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(live, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)
        live.rowconfigure(0, weight=1)
        live.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self._stock_rows = None

    def _build_crates(self, tab):
        """One row per crate, by its whole name: what it pays, what it lists at, what a flip earns."""
        table = ttk.LabelFrame(tab, text="Crates it buys and lists", padding=8)
        table.grid(row=0, column=0, sticky="nsew")
        # A canvas, so a long list scrolls inside the tab instead of growing the window.
        canvas = self.crate_canvas = tk.Canvas(
            table, height=CRATE_VIEW_PX, highlightthickness=0, borderwidth=0, yscrollincrement=10
        )
        bar = ttk.Scrollbar(table, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        inner = self.crate_table = ttk.Frame(canvas)
        canvas.create_window(0, 0, window=inner, anchor="nw")

        def fit(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"), width=inner.winfo_reqwidth())

        inner.bind("<Configure>", fit)
        self._wheel_scrolls(canvas)
        self._wheel_scrolls(inner)
        self.earn_head = tk.StringVar(master=self.root)
        # column 0 is each row's drag handle: the order of the rows is the priority
        for c, text in enumerate(("", "On", "Crate", "Buy up to", "Sell at")):
            head = ttk.Label(inner, text=text, font=self.head)
            head.grid(row=0, column=c, sticky="w", padx=(0, 8), pady=(0, 4))
            self._wheel_scrolls(head)
        head = ttk.Label(inner, textvariable=self.earn_head, font=self.head)
        head.grid(row=0, column=5, sticky="w", padx=(0, 8), pady=(0, 4))
        self._wheel_scrolls(head)
        self.crate_rows: Dict[str, dict] = {}
        for name, row in self.settings.crates.items():
            self._crate_row(name, *("" if row.get(k) is None else str(row[k]) for k in ("buy", "sell")),
                            on=row.get("on", True) is not False)
        ttk.Label(
            table,
            text="Only these crates are ever bought or listed, by the whole name: Glove Case is not "
                 "Glove Case 2.\nDrag a row by its \u2261 to set its priority: higher is bought and "
                 "listed first.\nUntick On to pause a crate: it keeps its prices. Blank Buy up to: never "
                 "bought. Blank Sell at: never listed.\nEarn per flip is what one crate makes after the "
                 "sale tax (Config).",
            style="Hint.TLabel", justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        add = ttk.LabelFrame(tab, text="Add a crate", padding=8)
        add.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        for c, text in enumerate(("Name, as shown after Case |, e.g. Apex Case", "Buy up to", "Sell at",
                                  "Earn per flip")):
            ttk.Label(add, text=text, style="Hint.TLabel").grid(row=0, column=c, sticky="w", padx=(0, 8))
        self.new_name = tk.StringVar(master=self.root)
        new = self.new_row = {k: tk.StringVar(master=self.root) for k in ("buy", "sell", "earn")}
        boxes = (
            no_nudge(ttk.Entry(add, textvariable=self.new_name, width=NAME_CHARS)),
            no_nudge(ttk.Entry(add, textvariable=new["buy"], width=PRICE_CHARS, justify="right")),
            no_nudge(ttk.Entry(add, textvariable=new["sell"], width=PRICE_CHARS, justify="right")),
        )
        new["earn_label"] = ttk.Label(add, textvariable=new["earn"], width=EARN_CHARS)
        self.add_btn = ttk.Button(add, text="Add", width=7, command=self._add_crate)
        for c, w in enumerate(boxes + (new["earn_label"], self.add_btn)):
            w.grid(row=1, column=c, sticky="w", padx=(0, 8))
        for box in boxes:
            box.bind("<Return>", lambda e: self._add_crate())
        for key in ("buy", "sell"):
            new[key].trace_add("write", lambda *_: self._show_earn(new))

        sell = ttk.LabelFrame(tab, text="Selling", padding=8)
        sell.grid(row=0, column=1, rowspan=2, sticky="nw", padx=(PAD * 2, 0))
        self._check(sell, "auto_sell", "Relist what it buys (SELL tab, sorted Newest)")
        self._spin(sell, "sell_batch_size", "Sell after N buys", 1, 50)
        self._spin(sell, "sell_batch_max", "Max per basket", 1, 50)
        tab.rowconfigure(0, weight=1)
        self._show_all_earn()
        # The canvas is as wide as its rows. Its <Configure> only fires once it is on
        # screen, so size it now too: a hidden window was measured 10 cm wide.
        inner.update_idletasks()
        fit()

    def _build_config(self, tab):
        """Every setting outside the crate table, in sections, with Save and the bot's folder."""
        def section(title, row, col):
            box = ttk.LabelFrame(tab, text=title, padding=8)
            box.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            return box

        # Which config: every setting on every tab, the crate table included, is one
        # file in configs\, and this picks which one the window shows and Save writes.
        configs = section("Config", 0, 0)
        configs.grid(columnspan=2, sticky="ew")
        ttk.Label(configs, text="Config").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.config_var = tk.StringVar(master=self.root, value=self.config_name)
        # guarded like every other box: a wheel or trackpad pass over it switched configs
        self.config_box = no_nudge(ttk.Combobox(configs, textvariable=self.config_var, state="readonly",
                                                width=24, values=cb.list_configs() or [self.config_name]))
        self.config_box.grid(row=0, column=1, sticky="w")
        self.config_box.bind("<<ComboboxSelected>>", lambda e: self._switch_config(self.config_var.get()))
        for c, (text, command) in enumerate((
            ("Save", self.save),
            ("Save as new…", self._save_config_as),
            ("Undo unsaved changes", self._reload_settings),
            ("Delete", self._delete_config),
            ("Open file location", self.open_folder),
        ), start=2):
            ttk.Button(configs, text=text, command=command).grid(row=0, column=c, sticky="w", padx=(8, 0))
        ttk.Label(configs, text="Each config is its own file in %s, crate prices included. "
                                "Pick one to switch; Start uses the one shown." % cb.CONFIGS_DIR,
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))

        safety = section("Safety", 1, 0)
        self._check(safety, "dry_run", "Dry run (never presses PURCHASE)")
        self._check(safety, "cases_only", "Cases only: never buy a knife, a gun or gloves")
        self._cap(safety, "max_purchases", "dynamic_purchases", "Max purchases")
        self._cap(safety, "max_spend", "dynamic_spend", "Max spend")

        money = section("Money", 1, 1)
        self._entry(money, "sell_tax", "Sale tax", "0.05 is the game's 5%, and the least it takes:\n"
                                                   "Earn per flip and the dashboard use it")
        self.vars["sell_tax"].trace_add("write", lambda *_: self._tax_typed())

        timing = section("Timing", 2, 0)
        # 0.05 steps: a Scale snaps its variable to its resolution as it is built, so
        # at 0.5 a saved 2.3 s cooldown opened as 2.5 and Save wrote that back.
        self._slider(timing, "idle_seconds", "Idle seconds before a click", 0.5, 5, 0.05,
                     "how long your mouse and keys\nmust rest first")
        self._slider(timing, "refresh_cooldown", "Min seconds between refreshes", 0.5, 10, 0.05,
                     "a floor: the real countdown\nis about 7 s")

        market = section("Market filter", 2, 1)
        self._spin(market, "filter_price_max", "Max price", 1, 99_999_999, "typed into the filter's To box")
        var = tk.StringVar(master=self.root, value=self.settings.filter_rarity or ANY)
        self.vars["filter_rarity"] = var
        self.widgets["filter_rarity"] = self._row(
            market, "Rarity", no_nudge(ttk.Combobox(market, textvariable=var, values=("Red", ANY), width=12)),
            "(any) leaves the rarity alone\nand checks the price only",
        )
        self._check(market, "restore_filter", "Put the market filter back when Roblox resets it")
        for c in range(2):
            tab.columnconfigure(c, weight=1, uniform="config")

    def _build_history(self, tab):
        # The log itself lives on the Dashboard: one Text growing all night, not two.
        cols = ("time", "event", "crate", "price", "detail", "mode")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=20)
        for c, w in zip(cols, (150, 90, 190, 60, 220, 60)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(tab, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        ttk.Label(tab, text="This session's buys, listings and sales. purchases.jsonl keeps every one.",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

    # -- the crate table

    def _crate_row(self, name: str, buy: str, sell: str, on: bool = True) -> None:
        """One crate on the Crates tab: On, name, Buy up to, Sell at, Earn per flip, Remove."""
        t = self.crate_table
        r = len(self.crate_rows) + 1       # under the headings, in priority order
        row = {k: tk.StringVar(master=self.root, value=v) for k, v in (("buy", buy), ("sell", sell), ("earn", ""))}
        row["on"] = tk.BooleanVar(master=self.root, value=on)
        # Entries, never Spinboxes: a blank Spinbox took one wheel pass as 1, and that
        # became a 1-coin listing (see NUDGES).
        widgets = (
            ttk.Checkbutton(t, variable=row["on"], command=lambda: self._show_earn(row)),
            ttk.Label(t, text=name, width=NAME_CHARS),
            no_nudge(ttk.Entry(t, textvariable=row["buy"], width=PRICE_CHARS, justify="right")),
            no_nudge(ttk.Entry(t, textvariable=row["sell"], width=PRICE_CHARS, justify="right")),
            ttk.Label(t, textvariable=row["earn"], width=EARN_CHARS),
            ttk.Button(t, text="Remove", width=7, command=lambda: self._remove_crate(name)),
        )
        handle = ttk.Label(t, text="\u2261", cursor="fleur", padding=(4, 0))
        handle.bind("<B1-Motion>", lambda e: self._drag_crate(name, e.y_root))
        handle.bind("<ButtonRelease-1>", lambda e: self._dropped_crate(name))
        handle.grid(row=r, column=0, sticky="w", padx=(0, 4), pady=1)
        self._wheel_scrolls(handle)
        for c, w in enumerate(widgets, start=1):
            w.grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
            self._wheel_scrolls(w)
        row["widgets"], row["earn_label"], row["handle"] = widgets, widgets[4], handle
        for key in ("buy", "sell"):
            # every keystroke, so the row says what a flip earns before anything is saved
            row[key].trace_add("write", lambda *_: self._show_earn(row))
        self.crate_rows[name] = row
        self._show_earn(row)

    def _add_crate(self) -> None:
        """The Add a crate row into the table, unless its name is blank, a skin's, or listed already.

        A pasted whole title, 'Case | Apex Case', goes in as Apex Case, the name the
        bot matches (cb.typed_crate_name).
        """
        name, refused = cb.typed_crate_name(self.new_name.get())
        if not name:
            self._say("type the crate's name first, as shown after Case |, e.g. Apex Case")
            return
        if refused:
            self._say(refused)
            return
        # names match without case or outer spaces (cb.crate_row), so two spellings
        # of one crate would leave only the first in charge
        have = next((n for n in self.crate_rows if n.strip().lower() == name.lower()), None)
        if have is not None:
            self._say("%s is already in the list: change its prices in its row" % have)
            return
        self._crate_row(name, self.new_row["buy"].get().strip(), self.new_row["sell"].get().strip())
        for var in (self.new_name, self.new_row["buy"], self.new_row["sell"]):
            var.set("")
        self.crate_table.update_idletasks()
        self.crate_canvas.yview_moveto(1.0)
        self._say("added %s, not saved yet" % name)

    def _remove_crate(self, name: str) -> None:
        row = self.crate_rows.pop(name, None)
        if row is None:
            return
        for w in row["widgets"] + (row["handle"],):
            w.destroy()
        self._regrid_crates()
        self._say("removed %s, not saved yet: a crate not in the list is never bought or listed" % name)

    def _drag_crate(self, name: str, y_root: int) -> None:
        """A row's handle dragged: the row follows the pointer to the slot under it."""
        t = self.crate_table
        slot = t.grid_location(0, y_root - t.winfo_rooty())[1] - 1      # row 0 is the headings
        self._move_crate(name, slot)

    def _dropped_crate(self, name: str) -> None:
        order = list(self.crate_rows)
        if name in order:
            self._say("%s is priority %d of %d, not saved yet" % (name, order.index(name) + 1, len(order)))

    def _move_crate(self, name: str, slot: int) -> None:
        """Put a crate at this place in the list (0 is the top, the first bought and listed)."""
        order = [n for n in self.crate_rows if n != name]
        if name not in self.crate_rows:
            return
        slot = max(0, min(slot, len(order)))
        order.insert(slot, name)
        if order == list(self.crate_rows):
            return
        self.crate_rows = {n: self.crate_rows[n] for n in order}
        self._regrid_crates()

    def _regrid_crates(self) -> None:
        """Every crate row back on the grid in the table's order, with no gaps."""
        for r, row in enumerate(self.crate_rows.values(), start=1):
            for w in (row["handle"],) + row["widgets"]:
                w.grid_configure(row=r)

    def _tax(self) -> float:
        """The Sale tax box as typed, or the saved tax while the box holds no usable one."""
        var = self.vars.get("sell_tax")
        if var is None:
            return self.settings.sell_tax       # the Config tab is built after this one
        try:
            tax = float(var.get().strip())
        except ValueError:
            return self.settings.sell_tax
        return tax if cb.GAME_TAX <= tax <= MAX_TAX else self.settings.sell_tax

    def _tax_typed(self) -> None:
        """Every keystroke in the Sale tax box: the Earn per flip column follows it, and
        a tax under the game's own is refused out loud on the status line."""
        self._show_all_earn()
        try:
            tax = float(self.vars["sell_tax"].get().strip())
        except ValueError:
            return
        if tax < cb.GAME_TAX:
            self._say("sale tax %g is under the game's %g, refused: it would pass losing prices"
                      % (tax, cb.GAME_TAX))

    def _show_earn(self, row: dict) -> None:
        text, how = earn(row["buy"].get(), row["sell"].get(), self._tax(), self._lowest_buy())
        if "on" in row and not row["on"].get():
            text, how = "off: not bought or listed", ""
        if row["earn"].get() != text:
            row["earn"].set(text)
        row["earn_label"].configure(style=EARN_STYLE[how])

    def _lowest_buy(self) -> int:
        """The cheapest Buy up to in the table as typed, 0 when none reads as a price."""
        buys = []
        for row in (getattr(self, "crate_rows", None) or {}).values():
            if "on" in row and not row["on"].get():
                continue
            try:
                b = cb._as_price(row["buy"].get(), zero_is_none=True)
            except (ValueError, KeyError, AttributeError):
                continue
            if b:
                buys.append(b)
        return min(buys) if buys else 0

    def _show_all_earn(self) -> None:
        self.earn_head.set("Earn per flip, %g%% tax" % (cb.floor_tax(self._tax()) * 100))
        for row in list(self.crate_rows.values()) + [self.new_row]:
            self._show_earn(row)

    def _wheel_scrolls(self, widget) -> None:
        """The wheel or trackpad over any part of the crate table scrolls the table.

        Over a price box too, where this takes the place of no_nudge's plain "break":
        still nothing reaches the box itself, so a price changes only by typing.
        """
        canvas = self.crate_canvas

        def wheel(e):
            if e.delta:
                canvas.yview_scroll(-3 if e.delta > 0 else 3, "units")
            return "break"

        def touch(e):
            # Tk 9 packs a trackpad's x and y movement into the one delta
            dy = float(self.root.tk.splitlist(self.root.tk.call("tk::PreciseScrollDeltas", e.delta))[1])
            if dy:
                canvas.yview_scroll(-1 if dy > 0 else 1, "units")
            return "break"

        widget.bind("<MouseWheel>", wheel)
        try:
            widget.bind("<TouchpadScroll>", touch)
        except tk.TclError:
            pass        # a Tk before 9 has no such event, and sends the trackpad as a wheel

    # -- form rows: every control is registered under its Settings field

    def _row(self, tab, label, widget, hint=""):
        r = tab.grid_size()[1]
        ttk.Label(tab, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, PAD))
        widget.grid(row=r, column=1, sticky="w", pady=3)
        if hint:
            ttk.Label(tab, text=hint, style="Hint.TLabel").grid(row=r, column=2, sticky="w", padx=(PAD, 0))
        return widget

    def _slider(self, tab, key, label, lo, hi, step, hint=""):
        value = getattr(self.settings, key)
        kind = tk.DoubleVar if isinstance(getattr(DEFAULTS, key), float) else tk.IntVar
        var = self.vars[key] = kind(master=self.root, value=value)
        self._ranges[key] = (lo, hi)
        # A range that holds the saved value, or the scale would quietly clamp it.
        scale = tk.Scale(tab, variable=var, from_=min(lo, value), to=max(hi, value), resolution=step,
                         orient="horizontal", length=260, showvalue=True, highlightthickness=0)
        self.widgets[key] = self._row(tab, label, scale, hint)

    def _spin(self, tab, key, label, lo, hi, hint=""):
        var = self.vars[key] = tk.StringVar(master=self.root, value=str(getattr(self.settings, key)))
        box = no_nudge(ttk.Spinbox(tab, textvariable=var, from_=lo, to=hi, increment=1, width=12))
        self.widgets[key] = self._row(tab, label, box, hint)
        return box

    def _entry(self, tab, key, label, hint=""):
        var = self.vars[key] = tk.StringVar(master=self.root, value=str(getattr(self.settings, key)))
        box = no_nudge(ttk.Entry(tab, textvariable=var, width=8))
        self.widgets[key] = self._row(tab, label, box, hint)
        return box

    def _check(self, tab, key, label, command=None):
        var = self.vars[key] = tk.BooleanVar(master=self.root, value=bool(getattr(self.settings, key)))
        box = self.widgets[key] = ttk.Checkbutton(tab, text=label, variable=var, command=command)
        box.grid(row=tab.grid_size()[1], column=0, columnspan=3, sticky="w", pady=3)

    def _cap(self, tab, key, flag, label):
        """A cap with its Ignore box beside it; the cap greys out while it is ignored."""
        box = self._spin(tab, key, label, 1, 9_999_999)
        var = self.vars[flag] = tk.BooleanVar(master=self.root, value=bool(getattr(self.settings, flag)))

        def grey():
            box.state(["disabled"] if var.get() else ["!disabled"])

        self._greys.append(grey)
        tick = self.widgets[flag] = ttk.Checkbutton(
            tab, text="Ignore: buy while the balance allows", variable=var, command=grey
        )
        tick.grid(row=box.grid_info()["row"], column=2, sticky="w", padx=(PAD, 0))
        grey()

    MAX_TREE_ROWS = 500

    def _add_row(self, rec):
        # The table grew for as long as the run did, and a Treeview holds every row it
        # is given. Drop the oldest, the same way the log widget drops its oldest lines.
        kids = self.tree.get_children("")
        if len(kids) >= self.MAX_TREE_ROWS:
            self.tree.delete(*kids[: len(kids) - self.MAX_TREE_ROWS + 1])
        self.tree.insert("", "end", values=history_row(rec))
        self.tree.yview_moveto(1.0)

    def _write_log(self, msg):
        self.log.insert("end", msg + "\n")
        # An overnight run writes tens of thousands of lines, and a Text widget keeps
        # every one of them in memory. Trim the top rather than grow all night.
        if int(self.log.index("end-1c").split(".")[0]) > self.MAX_LOG_LINES:
            self.log.delete("1.0", "%d.0" % (self.MAX_LOG_LINES // 4))
        self.log.see("end")

    def _say(self, msg):
        # The status line is one line: a traceback's first line, not the whole thing.
        self.status.set(str(msg).splitlines()[0][:120] if msg else "")

    # -- settings

    def _value(self, key, var):
        """A control's value as its setting's type; ValueError when the text will not parse."""
        kind = type(getattr(DEFAULTS, key))
        raw = var.get()     # a slider's IntVar or DoubleVar is a number already
        if kind is bool:
            return bool(raw)
        if kind is str:
            return "" if raw.strip() == ANY else raw.strip()
        if isinstance(raw, str):
            raw = raw.strip().replace(",", "")
        # 4 places: enough to shed a slider's float noise (2.3000000000000003) and
        # still keep a sale tax such as 0.025, which 2 places turned into 0.03.
        return round(float(raw), 4) if kind is float else int(raw)

    def collect(self, quiet: bool = False) -> cb.Settings:
        """The window's settings. quiet: only to compare, so nothing is logged or counted."""
        # From the last settings rather than Settings(): a field with no control here
        # (the old tier fields) keeps its value instead of quietly going back to the default.
        s = replace(self.settings)
        said = []
        for key, var in self.vars.items():
            try:
                setattr(s, key, self._value(key, var))
            except (ValueError, tk.TclError):
                said.append("bad value for %s: %r, keeping %r"
                            % (key, self.root.getvar(str(var)), getattr(s, key)))
        if not cb.GAME_TAX <= s.sell_tax <= MAX_TAX:
            said.append("sale tax %r is outside %g to %g, keeping %g"
                        % (s.sell_tax, cb.GAME_TAX, MAX_TAX, self.settings.sell_tax))
            s.sell_tax = self.settings.sell_tax
        # The rows exactly as typed. clean() parses every box, and a value it cannot
        # use becomes None with a complaint: never bought or never listed, so a typo
        # can only make the bot do less. The tax is set first: clean() checks each
        # sell price against it.
        s.crates = {name: {"buy": row["buy"].get().strip(), "sell": row["sell"].get().strip(),
                           "on": bool(row["on"].get())}
                    for name, row in self.crate_rows.items()}
        said += s.clean()
        if s.idle_seconds < cb.MIN_IDLE_SECONDS:
            said.append(
                "idle seconds %s is below the %.1f floor, using the floor"
                % (s.idle_seconds, cb.MIN_IDLE_SECONDS)
            )
            s.idle_seconds = cb.MIN_IDLE_SECONDS
        if quiet:
            return s
        for complaint in said:
            self._write_log(complaint)
        # Counted for the status line (_rejected_note): a complaint that only reached
        # the log went unseen while the old value quietly stayed.
        self.rejected = len(said)
        # a row whose flip earns nothing goes red, against the tax just collected
        self._show_all_earn()
        return s

    def _rejected_note(self) -> str:
        """', 1 value rejected: see the log' when the last collect() turned any away, else ''."""
        n = self.rejected
        return ", %d value%s rejected: see the log" % (n, "" if n == 1 else "s") if n else ""

    def save(self):
        self.settings = self.collect()
        path = cb.config_path(self.config_name)
        self.settings.save(path)
        self._mark_saved()
        cb.set_active_config(self.config_name)
        self._refresh_configs()
        self._write_log("saved %s" % path)
        self._say("saved config %s" % self.config_name + self._rejected_note())

    # -- configs: named settings files in configs\

    def _title(self):
        self.root.title("casebot: %s" % self.config_name)
        self.save_note.set("Saves to %s. A running bot keeps the settings it started with."
                           % os.path.join("configs", self.config_name + ".json"))

    def _refresh_configs(self):
        self.config_box.configure(values=cb.list_configs() or [self.config_name])
        self.config_var.set(self.config_name)
        self._title()

    def _unsaved(self) -> bool:
        """True when the window holds edits its config's file does not."""
        return self._view() != self._saved_view

    def _view(self):
        # the crate order too: asdict compares dicts without it, and a dragged
        # priority was dropped on a switch with no prompt
        s = self.collect(quiet=True)
        return asdict(s), list(s.crates)

    def _ask_save_first(self, doing: str) -> bool:
        """Offer to save unsaved edits before they are dropped. False: the person cancelled."""
        if not self._unsaved():
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            "Config %s has unsaved changes.\n\nSave them before %s?" % (self.config_name, doing),
            parent=self.root)
        if answer is None:
            return False
        if answer:
            self.save()
        return True

    def _apply_settings(self, s: cb.Settings) -> None:
        """Show a config's settings in every control, the crate table rebuilt from it."""
        self.settings = s
        for key, var in self.vars.items():
            value = getattr(s, key)
            if key == "filter_rarity":
                value = value or ANY
            if key in self._ranges:
                # widen first, or the Scale clamps a value outside the startup config's range
                lo, hi = self._ranges[key]
                self.widgets[key].configure(from_=min(lo, value), to=max(hi, value))
            if isinstance(var, tk.BooleanVar):
                value = bool(value)
            var.set(value if isinstance(var, (tk.BooleanVar, tk.IntVar, tk.DoubleVar)) else str(value))
        for grey in self._greys:
            grey()
        for name in list(self.crate_rows):
            row = self.crate_rows.pop(name)
            for w in row["widgets"] + (row["handle"],):
                w.destroy()
        for name, row in s.crates.items():
            self._crate_row(name, *("" if row.get(k) is None else str(row[k]) for k in ("buy", "sell")),
                            on=row.get("on", True) is not False)
        self._show_all_earn()
        self._mark_saved()

    def _mark_saved(self) -> None:
        # What the window reads as now, the yardstick for _unsaved: comparing with the
        # file itself flagged a fresh load, whose crate rows read back with an "on" added.
        self._saved_view = self._view()

    def _switch_config(self, name: str) -> None:
        if name == self.config_name:
            return
        if not self._ask_save_first("switching to %s" % name):
            self.config_var.set(self.config_name)
            return
        self.config_name = name
        cb.set_active_config(name)
        self._apply_settings(cb.Settings.load(cb.config_path(name)))
        self._refresh_configs()
        self._write_log("switched to config %s" % name)
        self._say("using config %s" % name)

    def _save_config_as(self) -> None:
        """The window's settings as a new config, which then becomes the one in use."""
        from tkinter import simpledialog
        typed = simpledialog.askstring("Save as new config", "Name for the new config:",
                                       initialvalue="%s copy" % self.config_name, parent=self.root)
        if typed is None:
            return
        name, refused = cb.config_name(typed)
        if refused:
            messagebox.showerror("Save as new config", refused, parent=self.root)
            return
        same = next((n for n in cb.list_configs() if n.lower() == name.lower()), None)
        if same is not None and not messagebox.askyesno(
                "Save as new config", "A config called %s exists. Replace it?" % same, parent=self.root):
            return
        # the file keeps its own spelling on a Mac or Windows disk, so the name does too
        self.config_name = same or name
        self.save()
        self._say("saved as new config %s, now in use" % name)

    def _reload_settings(self) -> None:
        """Back to what the config's file says, dropping every edit since the last Save."""
        self._apply_settings(cb.Settings.load(cb.config_path(self.config_name)))
        self._say("config %s reloaded from its file" % self.config_name)

    def _delete_config(self) -> None:
        others = [n for n in cb.list_configs() if n.lower() != self.config_name.lower()]
        if not others:
            messagebox.showinfo("Delete config", "This is the only config: make another with "
                                "Save as new first.", parent=self.root)
            return
        if not messagebox.askyesno("Delete config", "Delete config %s? Its file is removed for good."
                                   % self.config_name, parent=self.root):
            return
        gone = self.config_name
        try:
            cb.delete_config(gone)
        except OSError as exc:
            self._say("could not delete %s: %s" % (gone, exc))
            return
        self.config_name = others[0]
        cb.set_active_config(self.config_name)
        self._apply_settings(cb.Settings.load(cb.config_path(self.config_name)))
        self._refresh_configs()
        self._write_log("deleted config %s, now using %s" % (gone, self.config_name))
        self._say("deleted %s, now using %s" % (gone, self.config_name))

    def open_folder(self) -> None:
        """Show the bot's folder in Explorer (Finder on a Mac), the active config picked out."""
        import subprocess
        target = cb.config_path(self.config_name)
        try:
            if cb.WINDOWS:
                if os.path.exists(target):
                    subprocess.Popen(["explorer", "/select,", target])
                else:
                    os.startfile(cb.HERE)
            else:
                subprocess.Popen(["open", "-R", target] if os.path.exists(target) else ["open", cb.HERE])
        except OSError as exc:
            self._say("could not open %s: %s" % (cb.HERE, exc))

    # -- run

    def toggle(self):
        if self.scan_bot is not None:
            # The run button reads Stop scan (or Stop selling) while a stopped-bot job
            # drives the window.
            self.scan_bot.stop()
            self.run_btn.config(text="Stopping")
            self.run_btn.state(["disabled"])
            self._say("stopping")
            return
        if self.bot is not None:
            self.bot.stop()
            self.run_btn.config(text="Stopping")
            for b in (self.run_btn, self.scan_btn, self.sell_btn):
                b.state(["disabled"])
            self._say("stopping")
            return
        if self.scanning:
            return
        clock, notes = last_scan(cb.inventory_stats(self.snapshot, self.settings)["totals"])
        answer = messagebox.askyesnocancel(
            "Scan inventory first?",
            "Last scan: %s (%s)\n\n"
            "Yes: count the crates on the SELL tab first, then start.\n"
            "No: start now and carry on from the saved inventory." % (clock, notes),
            parent=self.root,
        )
        if answer is None:
            return
        self.start(scan=bool(answer))

    def start(self, scan: bool) -> None:
        """Start a run, scanning the inventory first or not. No dialog: a launcher
        calls this straight (main's --autostart flags), toggle() after asking."""
        if self.bot is not None or self.scanning:
            return
        self.settings = self.collect()
        self.bot = cb.Bot(
            self.settings,
            on_status=lambda m: self.q.put(("log", m)),
            on_bounds=lambda b: self.q.put(("bounds", b)),
        )
        # The bot loads the saved snapshot either way; this only adds a scan up front.
        self.bot.scan_on_start = scan
        self.seen = 0
        self.run_btn.config(text="Stop")
        self._say(("starting: scanning the inventory first" if scan else "starting from the saved inventory")
                  + self._rejected_note())
        self.thread = self._spawn(self._run)

    def scan(self):
        if self.bot is not None:
            # The running bot owns the window, so the scan joins its work instead of
            # a second thread driving the same clicks.
            if self.bot.request_scan():
                self._say("inventory scan queued: it runs at the top of the next cycle")
            else:
                self._say("a scan is already running")
            return
        if self.scanning:
            return
        self.settings = self.collect()
        self.scanning = True
        # A bot of the GUI's own, not one hidden inside scan_once, so that Stop scan
        # and quitting can stop() it: a whole-grid scan runs for minutes.
        bot = self.scan_bot = cb.Bot(self.settings, on_status=lambda m: self.q.put(("log", m)))
        self.run_btn.config(text="Stop scan")
        for b in (self.scan_btn, self.sell_btn):
            b.state(["disabled"])
        self._say("scanning the inventory" + self._rejected_note())

        def work():
            snap = None
            try:
                # attach() and scan_inventory(), with scan_once's error handling and lock
                snap = cb.scan_once(bot.s, bot=bot)
            finally:
                self.q.put(("scanned", snap))

        self._spawn(work)

    def _scanned(self, snap):
        self.scanning = False
        bot, self.scan_bot = self.scan_bot, None
        self.run_btn.config(text="Start")
        for b in (self.run_btn, self.scan_btn, self.sell_btn):
            b.state(["!disabled"])
        if snap is None:
            self._say("inventory scan failed, see the log")
            return
        # A partial scan never replaces a complete one (Bot.scan_inventory keeps the
        # complete one), so show what the bot kept; the status line reports the scan.
        self.snapshot = bot.inventory if bot is not None and bot.inventory is not None else snap
        t = cb.inventory_stats(snap, self.settings)["totals"]
        self._say("inventory: %d crates, %d listed%s"
                  % (t["cases"], t["listed"], "" if t["complete"] else ", partial scan"))

    def sell_all(self):
        """infinite, 2026-09-28: one button to list every case in the inventory.

        The same sweep a run makes when out of coins: crates on the Crates tab with a
        sell price, never knives, guns or gloves. A running bot does it next cycle;
        a stopped one gets a bot of its own, like Scan inventory, that Stop can stop.
        """
        if self.scanning:
            return
        if not messagebox.askyesno(
            "Sell all cases?",
            "List every case in the inventory at its sell price on the Crates tab?\n\n"
            "Only crates with a sell price go up. Knives, guns and gloves are never touched.",
            parent=self.root,
        ):
            return
        if self.bot is not None:
            self.bot.request_sell_all()
            self._say("selling every case: it starts at the top of the next cycle")
            return
        self.settings = self.collect()
        self.scanning = True
        bot = self.scan_bot = cb.Bot(self.settings, on_status=lambda m: self.q.put(("log", m)))
        self.run_btn.config(text="Stop selling")
        for b in (self.scan_btn, self.sell_btn):
            b.state(["disabled"])
        self._say("selling every case in the inventory" + self._rejected_note())

        def work():
            n = 0
            try:
                n = cb.sell_all_once(bot.s, bot=bot)
            finally:
                self.q.put(("sold_all", n))

        self._spawn(work)

    def _sold_all(self, n):
        self.scanning = False
        bot, self.scan_bot = self.scan_bot, None
        self.run_btn.config(text="Start")
        for b in (self.run_btn, self.scan_btn, self.sell_btn):
            b.state(["!disabled"])
        if bot is not None and bot.inventory is not None:
            self.snapshot = bot.inventory
        self._say("listed %d case%s from the inventory" % (n, "" if n == 1 else "s"))

    MAX_LOG_LINES = 2000

    def _run(self):
        bot = self.bot
        try:
            bot.attach()
            self.q.put(("bounds", bot.bounds))
            bot.log("running (dry_run=%s)" % bot.s.dry_run)
            while not bot.stop_event.is_set() and not bot.done():
                try:
                    bot.cycle()
                except Exception as exc:
                    bot.log("cycle error: %s" % exc)
                    bot.sleep(1.0)
            bot.end_focus()      # a focused run gives the machine back when it stops
        except RuntimeError as exc:
            # attach() says why it cannot start (no Roblox window, no Screen Recording
            # permission) in words, and a traceback under them only buries the reason.
            self.q.put(("log", "could not start: %s" % exc))
        except BaseException as exc:
            # BaseException, not Exception: whatever takes the thread down should say
            # so in the log rather than vanishing with the window.
            self.q.put(("log", "fatal: %s\n%s" % (exc, traceback.format_exc())))
        self.q.put(("done", None))

    def _tick(self):
        # Re-armed whatever happens: one exception here used to end the polling for
        # good, and with it every log line, every table row and the end of a run.
        try:
            self._drain()
            if self.bot is not None:
                self._sync_history()
                self._update_overlay()
            self._refresh_dashboard()
        finally:
            self._after = self.root.after(POLL_MS, self._tick)

    def _drain(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "log":
                self._write_log(payload)
                self._say(payload)
            elif kind == "bounds":
                self.bounds = payload
                self._show_overlay()
            elif kind == "done":
                self._finish()
            elif kind == "scanned":
                self._scanned(payload)
            elif kind == "sold_all":
                self._sold_all(payload)

    def _sync_history(self):
        # seen counts records, not list slots. The bot trims the front of history
        # once it is long enough, so anything already dropped is skipped rather
        # than read off the wrong index.
        dropped = getattr(self.bot, "history_dropped", 0)
        if self.seen < dropped:
            self.seen = dropped
        while self.seen - dropped < len(self.bot.history):
            self._add_row(self.bot.history[self.seen - dropped])
            self.seen += 1

    def _refresh_dashboard(self):
        live = self.bot
        snap = self.snapshot
        if live is not None and live.inventory is not None:
            snap = live.inventory
        args = (live.s if live is not None else self.settings, live or self.last_bot,
                live.balance if live is not None else None)
        try:
            tiles, rows = dashboard(snap, *args)
        except Exception as exc:
            # A hand-edited inventory.json that is valid JSON but not a snapshot
            # raised here every POLL_MS. Said once per snapshot, shown as no scan.
            if self._bad_snap is not snap:
                self._bad_snap = snap
                self._write_log("the inventory snapshot does not read (%r), showing none" % (exc,))
            if snap is self.snapshot:
                self.snapshot = None      # so Start's question does not trip on it either
            tiles, rows = dashboard(None, *args)
        # Set only what changed: this runs every POLL_MS, and a Treeview rebuilt that
        # often flickers and loses its scroll position.
        for key, (value, caption, how) in tiles.items():
            v, c = self.tile_vars[key]
            if v.get() != value:
                v.set(value)
            if c.get() != caption:
                c.set(caption)
            label = self.tile_labels[key]
            if str(label.cget("style")) != TILE_STYLE[how]:
                label.configure(style=TILE_STYLE[how])
        if rows != self._stock_rows:
            self._stock_rows = rows
            self.stock.delete(*self.stock.get_children())
            for name, held, listed, unlisted, price, worth in rows:
                # price 0: the crate table gives it no sell price, so it is never listed
                self.stock.insert("", "end", values=(name, held, listed, unlisted, price or "-", coins(worth)))

    def _finish(self):
        bot = self.bot
        if bot is not None:
            # the records that landed after the last tick, before the bot is let go
            self._sync_history()
            self._say("stopped: bought %d, spent %d, sold %d for %s profit"
                      % (bot.bought, bot.spent, bot.sold_count, signed(bot.profit)))
            # What the run knew stays on the dashboard: its snapshot and its last coin count.
            snap = bot.inventory if bot.inventory is not None else self.snapshot
            if bot.balance is not None:
                snap = dict(snap or {}, balance=bot.balance)
            self.snapshot, self.last_bot = snap, bot
        self.bot = None
        self.run_btn.config(text="Start")
        for b in (self.run_btn, self.scan_btn, self.sell_btn):
            b.state(["!disabled"])
        self._hide_overlay()

    # -- overlay

    def _show_overlay(self):
        self._hide_overlay()
        b = self.bounds
        if not b:
            return
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.wm_attributes("-topmost", True)
        # bottom-left of the Roblox window, below the filter panel so it never
        # sits on APPLY FILTER or any card button
        x = int(b["X"]) + 12
        y = int(b["Y"]) + int(b["Height"]) - 44
        top.geometry("%dx%d+%d+%d" % (320, 32, x, y))
        self.overlay_text = tk.StringVar(value="casebot: starting")
        tk.Label(
            top,
            textvariable=self.overlay_text,
            bg="#101014",
            fg="#6ee7a8",
            anchor="w",
            padx=8,
        ).pack(fill="both", expand=True)
        if cb.WINDOWS:
            self._click_through(top)
        self.overlay = top

    @staticmethod
    def _click_through(top):
        """Let clicks pass the overlay to the game under it (Windows).

        It sits over Roblox's own bottom-left buttons, which it used to swallow.
        Layered and transparent, Windows hands every click to the window beneath,
        and winplat._windows skips it, so the bot never takes it for a cover either.
        """
        import ctypes
        # first: Tk rewrites the extended style when it sets alpha
        top.wm_attributes("-alpha", 0.85)
        top.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        hwnd = user32.GetParent(top.winfo_id()) or top.winfo_id()
        GWL_EXSTYLE = -20
        # layered, transparent, never activated, and no taskbar button
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | 0x80000 | 0x20 | 0x8000000 | 0x80
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)

    def _update_overlay(self):
        if self.overlay is None or self.bot is None:
            return
        self.overlay_text.set(
            "casebot: %s | bought %d | spent %d%s"
            % (
                self.status.get()[:28],
                self.bot.bought,
                self.bot.spent,
                " | dry run" if self.bot.s.dry_run else "",
            )
        )

    def _hide_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def _quit(self):
        if not self._ask_save_first("closing"):
            return
        for bot in (self.bot, self.scan_bot):
            if bot is not None:
                bot.stop()
        self._hide_overlay()
        self.root.destroy()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = tk.Tk()
    app = App(root)
    if "--smoke" in argv:
        root.withdraw()
        root.after(1000, root.destroy)
    # For a launcher that starts the bot unattended: Start with the scan question
    # answered, 2 s in so the window has drawn before the bot takes the mouse.
    for flag, scan in (("--autostart-scan", True), ("--autostart", False)):
        if flag in argv:
            root.after(2000, lambda scan=scan: app.start(scan=scan))
            break
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
