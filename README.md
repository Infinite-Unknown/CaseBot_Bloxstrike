# CaseBot (Bloxstrike)

A helper for the skin market in the Roblox game Bloxstrike, for **Mac and Windows**. It
watches the market, buys crates at or under the prices you set, and lists them again at your
sell prices.

It reads the game's text off the screen (Apple Vision on a Mac, RapidOCR on Windows) and
clicks the way a person would. It only ever trades **cases**: knives, guns and gloves are
never bought or sold.

> **Windows is new.** It is the same bot, but its clicks, typing and screen capture have not
> been tried on a Windows PC yet. Start with **Dry run** on, and if something goes wrong,
> send the log and the newest picture in `failures/`.

> **Warning:** automating a game client can break the game's rules and get your account
> banned, and it spends real in-game coins. Leave **Dry run** on until you trust your
> settings. Use it at your own risk.

## Windows

### What you need

- Windows 10 or 11, 64-bit.
- [uv](https://docs.astral.sh/uv/), which also fetches the right Python for you. Open
  **PowerShell** and run:

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

  Then close PowerShell and open it again.
- Roblox in a window (maximised is fine), not minimised. A screen of its own is best.

### Install

Download the bot: on this page press **Code > Download ZIP**, and unzip it. Or, with git:

```powershell
git clone https://github.com/Infinite-Unknown/CaseBot_Bloxstrike.git
```

Then, in PowerShell:

```powershell
cd CaseBot_Bloxstrike
uv venv --python 3.12
uv pip install -r requirements.txt
```

`cd` into the folder you unzipped if you used the ZIP (its name ends in `-main`). The install is
about 150 MB, most of it the text reader.

**Updating an older install?** The text reader changed: older versions read the market wrong,
and it now runs on your graphics card. Swap it over once, in the bot's folder:

```powershell
uv pip uninstall onnxruntime onnxruntime-directml
uv pip install -r requirements.txt
```

### Start it

1. Open Bloxstrike in Roblox and go to the **MARKET** tab.
2. In PowerShell, in the bot's folder:

   ```powershell
   .venv\Scripts\python app.py
   ```

   It keeps the PC and its screen awake while it is open.
3. Carry on from step 3 of the Mac's "Start it" below: set your crate prices, then Start.

Windows needs no permissions. Two things to know:

- If Roblox runs **as administrator**, Windows blocks the bot's clicks. Run both normally.
- Reading the screen takes under a second a frame with a graphics card (DirectX 12, which
  nearly every PC has), and about 3 to 4 seconds on the CPU alone, so without one the bot
  is slower to spot a cheap crate.

## Mac

### What you need

- A Mac. It was built and tested on macOS 26 with Apple Silicon.
- Python 3.12 with Tk. The easiest way is [uv](https://docs.astral.sh/uv/) (`brew install uv`)
  or the python.org installer. Homebrew's own Python also needs `brew install python-tk@3.12`.
- Roblox running **in a window**, not full screen, best on a screen of its own.

### Install

```bash
git clone https://github.com/Infinite-Unknown/CaseBot_Bloxstrike.git
cd CaseBot_Bloxstrike
uv venv --python 3.12
uv pip install -r requirements.txt
```

Without uv:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Allow it to see and click

macOS gives these permissions to the app you start the bot from (Terminal, iTerm, and so on).
Open **System Settings > Privacy & Security** and turn your terminal on under:

- **Screen & System Audio Recording**, so it can see the Roblox window.
- **Accessibility**, so it can click and type.

macOS asks again from time to time. If Start says it is not allowed to record the screen,
click **Allow** in the prompt and press Start again. Right after allowing, screen captures can
be slow for a few minutes. That passes on its own.

### Start it

1. Open Bloxstrike in Roblox, in a window, and go to the **MARKET** tab.
2. Start the bot:

   ```bash
   cd CaseBot_Bloxstrike
   caffeinate -dimsu .venv/bin/python app.py
   ```

   `caffeinate` keeps the Mac awake while it runs.
3. On the **Crates** tab, set the price you pay and the price you sell at for each crate,
   then press **Save settings**.
4. Press **Start**. It asks whether to scan your inventory first. Yes counts your crates on
   the SELL tab before it starts buying.

For the first run, leave **Dry run** on (Config tab). It opens listings and closes them
again without pressing PURCHASE, and the log says what it would have bought.

## The window

**Top bar**

- **Start / Stop**.
- **Scan inventory**: counts your crates on the SELL tab.
- **Sell all cases**: lists every case you own that has a sell price.
- **Roblox focused mode**: keeps Roblox in front the whole time, so everything runs about
  2.5 times faster. Using your mouse or keyboard pauses it, and it carries on once you stop
  for a moment. Only Stop stops it. With it off, the bot waits until you are idle before every
  click and gives your screen back after each one.

**Tabs**

- **Dashboard**: coins, cases bought, listed and sold, profit this run after the 5% sale tax,
  what your crates are worth, the crates you hold, and the live log.
- **Crates**: one row per crate, by its exact name (Glove Case is not Glove Case 2).
  - **Buy up to**: the most it pays, that price included. Blank means never buy it.
  - **Sell at**: the price it lists at. Blank means never list it.
  - **Earn per flip**: what one crate makes after the tax.
  - **On**: untick to pause a crate without losing its prices.
  - **Drag a row by its ≡ handle** to set priority: higher rows are bought and listed first.
  - **Add a crate**: type the name as the game shows it after "Case |", for example
    `Apex Case`.
  - **Selling**: relist what it buys, sell after N buys, and the most crates per basket.
- **Config**: every other setting, and your configs.
  - **Config**: pick which config is in use. Each one is a whole set of settings, crate
    prices included, kept as its own file in `configs\`. **Save as new…** copies what the
    window shows into a new config; **Delete** removes the one shown; **Undo unsaved
    changes** goes back to its file; **Open file location** shows the bot's folder.
    Switching with unsaved changes asks whether to save them first.
  - **Safety**: Dry run, cases only, max purchases and max spend (or let your wallet decide).
  - **Money**: the sale tax.
  - **Timing**: how long you must be idle before a click, and the time between refreshes.
  - **Market filter**: the max price and rarity for the market filter. Roblox forgets the
    filter when it reconnects, and the bot puts it back.
- **History**: this session's buys, listings and sales.

## What it does by itself

- Refreshes the market and buys crates at or under your price, never more than it can afford.
- Lists them on the SELL tab in baskets at your sell price. Before it presses SELL ITEMS it
  reads every price back from the basket. A wrong price is taken out, never sold.
- Never lists a crate for less than it pays for it, after the tax.
- When coins run out and relisting is on, it lists everything sellable and waits for sales.
- Looks past the first screen of the market too: once a refresh, it scrolls down to the third
  row of cards while the refresh button counts down.
- Gets back on its own: it presses MARKET from the main menu, presses Reconnect after a
  disconnect, and puts the market filter back. In a trading server it accepts the trade
  rules and opens the market from the gold shop icon at the bottom left.
- Works out which listings sold from how much your coins went up. When it cannot tell, it
  scans your inventory again.

## Files it writes

All in the bot's folder, and all left out of git:

| File | What it holds |
| --- | --- |
| `configs/` | one `.json` per config, written when you press Save, and `active.txt`, the one in use. An old `settings.json` moves in as `configs/default.json` the first time it starts |
| `starter_config.json` | the settings a fresh download starts from (Dry run on), copied in as `configs/default.json` the first time it starts. Shipped with the bot, not written by it |
| `purchases.jsonl` | every buy, listing and sale |
| `casebot.log` | everything it did |
| `inventory.json` | the last inventory scan |
| `failures/` | screenshots of anything that went wrong (the last 40) |

## Command line

```bash
.venv/bin/python casebot.py --probe      # read the Roblox window and print what it sees, no clicks
.venv/bin/python casebot.py --run        # run without the window, using the active config
.venv/bin/python casebot.py --run --config "night run"   # or a config of your choosing
.venv/bin/python app.py --autostart      # open the window and press Start right away
```

On Windows, write `.venv\Scripts\python` in place of `.venv/bin/python`.

## Problems

- **"Roblox window not found"**: Roblox has to run in a window, not full screen (Mac), and
  not minimised (Windows).
- **Start says it cannot record the screen** (Mac): see "Allow it to see and click" above.
- **Clicks do nothing**: on a Mac, turn on Accessibility for your terminal. On Windows,
  check Roblox is not running as administrator.
- **`uv` is not recognised** (Windows): close PowerShell and open a new one after installing uv.
- **Anything else**: read the log on the Dashboard, and look in `failures/` for a screenshot
  of what the bot saw.

## License

MIT, see [LICENSE](LICENSE).
