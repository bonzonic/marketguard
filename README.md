# MarketGuard

Real-time market manipulation surveillance for retail traders.

Institutional trade surveillance costs six figures and serves exchanges and
regulators. There is no consumer tier — so manipulation *is* detected, by
institutions, on behalf of institutions, and the retail trader losing money
never finds out.

MarketGuard is that capability, open source, pointed at the person being
fleeced.

> Built for the Nebius x NVIDIA Global AI Hackathon — Best Apps and Agents track.

---

## Status

| Component | State |
|---|---|
| Recorder | ✅ working |
| Feature extractor | ✅ batch path working |
| Spoofing detector | 🔜 |
| Pump & dump gate | ✅ statistical gate; no LLM stage yet |
| Nemotron cascade | 🔜 |
| Frontend | 🔜 |

---

## Architecture

```
Exchange WS (depth + trades)
        │
        ▼
  Book state              in-memory; @depth20 snapshots need no reconstruction
        │
        ▼
  Feature extractor       rolling windows: depth imbalance, level lifetime,
        │                 cancel/fill ratio, volume z-score, order flow imbalance
        ▼
  Statistical gate        NO LLM. Runs at stream rate. Free.
        │
     [escalate]
        ▼
  Nemotron Nano           is this window a candidate pattern?
        │
    [candidate]
        ▼
  Nemotron Super/Ultra    verdict + plain-English explanation with evidence
        │
        ▼
  Event store ──► WebSocket ──► Frontend (live chart + depth heatmap)
```

**The LLM never sees raw ticks.** It receives structured feature summaries —
*"order of 340 BTC placed 8bps from mid, cancelled after 420ms, 5th occurrence
in 90s, opposite-side fills totalling 12 BTC during the window."*

Cheap statistics filter to expensive reasoning. That is what makes continuous
surveillance across many symbols economically possible.

---

## Detectors

| Detector | Status | Notes |
|---|---|---|
| **Spoofing** | planned | Large orders near mid, cancelled unfilled, repeated |
| **Pump & dump** | gate built | Volume z-score + vol-normalised spike + thin book + one-sided flow; retracement confirms, retrospectively |
| Layering | stretch | Reuses spoofing machinery |
| ~~Wash trading~~ | **excluded** | Requires account identity — not available on public feeds |

---

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/bonzonic/marketguard.git
cd marketguard
python -m venv venv
```

Windows:

```powershell
.\venv\Scripts\pip install -r requirements.txt
```

macOS / Linux:

```bash
./venv/bin/pip install -r requirements.txt
```

## Running the recorder

```powershell
.\venv\Scripts\python recorder.py
```

Writes hourly gzipped JSONL to `data/`. Stop with `Ctrl-C` — it closes the
current file cleanly.

That is the right way to run it for an afternoon. For a run that has to last
until the deadline without a human watching it, see
[Running unattended on Windows](#running-unattended-on-windows).

**Measured throughput:** ~30 msg/s and **~0.08 GB/day** across six symbols
(~3.3 GB for a 40-day run). `@depth20@100ms` only pushes when the book
actually changes, and order book JSON compresses extremely well — so storage
is far cheaper than a naive 10-updates-per-second estimate suggests. Expect
several times this during high-volatility periods.

The recorder logs a per-stream message breakdown every five minutes.

### Inspecting the data

```powershell
.\venv\Scripts\python datafile.py
```

Prints per-stream counts and sample records for the most recent file.
`datafile.py` also provides `read_lines` / `read_messages` helpers that
tolerate the truncated tail of a file still being written.

### Data format

One JSON object per line. `t` is the recorder's receive time in milliseconds.

```json
{"t":1790090601632,"stream":"solusdt@depth20@100ms","data":{"lastUpdateId":123,"bids":[["1.23","45.6"]],"asks":[["1.24","78.9"]]}}
{"t":1790090601640,"stream":"solusdt@aggTrade","data":{"e":"aggTrade","E":1790090601357,"s":"SOLUSDT","p":"1.235","q":"10.0","m":false}}
```

`m` is the field that matters most in the trade stream, and it inverts:
`m: false` means the **buyer** crossed the spread (aggressive buy), `m: true`
means the **seller** did. Order flow imbalance depends on getting this right.

#### Why `t` exists

Partial book depth streams carry **no timestamp** — only `lastUpdateId`,
`bids` and `asks`. Spoofing detection is entirely about order lifetime in
milliseconds, so without a clock on the depth stream the primary detector
cannot be built. The recorder stamps every message on receipt.

Measured inter-message gap for `solusdt@depth20@100ms` is a median of 100ms,
matching the stream cadence, so lifetimes down to ~200ms are measurable.

#### Reconstructed timestamps

Files recorded before stamping existed were backfilled by interpolating
between surrounding trades, which do carry an exchange event time:

```powershell
.\venv\Scripts\python backfill.py --dry-run
.\venv\Scripts\python backfill.py
```

Those messages carry `"est":1`. Accuracy was validated against files where the
true receive time is known:

| | error |
|---|---|
| median | 22 ms |
| p90 | 67 ms |
| p99 | 174 ms |
| max | 375 ms |

Fine for volume and pump-window analysis, marginal for sub-100ms spoof
lifetimes. Use `datafile.is_estimated(msg)` to filter them out where precision
matters.

> Note: exchange event time runs ~900ms behind local receive time on the
> recording machine. That is a constant offset, so it does not affect relative
> measurements like order lifetime — but it matters if you correlate against
> external event times.

---

## Running unattended on Windows

The recorder has to keep capturing until **30 Oct 2026** on a machine that is
also somebody's daily driver. Started from a PowerShell window it is a child
of that window: it dies on window close, logout, reboot and sleep, with no
restart and no notification. Four things fix that, and the fourth catches the
failure the other three cannot see.

| Failure | Mechanism |
|---|---|
| Window closed, logout, reboot | Task Scheduler, running as `SYSTEM` |
| Machine goes to sleep | `SetThreadExecutionState` held by the recorder (`keepawake.py`) |
| Process crashes or exits | Restart-on-failure **and** a repeating trigger |
| Process alive but receiving nothing | `watchdog.py` — checks the archive is growing |

That last row is the important one. A half-open websocket leaves the TCP
connection `ESTABLISHED` with no `FIN` and no `RST`: the process is alive,
connected and writing nothing, and every process-level check says it is fine.
The only honest measure is whether new records are actually landing.

### Install

One-time, from an **elevated** PowerShell in the repository:

```powershell
.\ops\install-tasks.ps1 -NtfyTopic 'marketguard-<something-random>'
```

Run it from the checkout you actually intend to run — it resolves the data
directory by asking that checkout's `config.py`, so installing from a worktree
would point the recorder at the worktree's `data\`. Pass `-DataDir` to be
explicit.

It does not start the recorder, because a recorder may still be running in a
console window and two of them writing the same hour is not what you want.
So:

```powershell
# stop the old one first, then
Start-ScheduledTask -TaskPath '\MarketGuard\' -TaskName 'Recorder'
```

Overlapping files are not a disaster if it happens — `datafile.py`
deduplicates on message identity — but it doubles the work and muddies
`coverage.py`.

Then set the restart window for Windows Update:

```powershell
.\ops\windows-update-policy.ps1            # active hours, pinned
.\ops\windows-update-policy.ps1 -Show      # just report
```

### What gets installed

Three tasks under `\MarketGuard\`:

| Task | Runs as | Triggers |
|---|---|---|
| `Recorder` | SYSTEM | at startup, at logon, and every 5 min |
| `Watchdog` | SYSTEM | at startup (+3 min), and every 5 min |
| `Alert Popup` | you | Application event 1001 |

The recorder has three triggers because each one alone has a hole. *At
startup* misses a fast-startup resume, where Windows restores a hibernated
kernel session instead of booting. *At logon* misses an unattended reboot.
*Restart-on-failure* only fires on a non-zero exit, and the recorder's own
out-of-disk abort exits zero. `MultipleInstances=IgnoreNew` makes the overlap
a no-op, so the worst case after any single failure is five minutes of lost
capture.

Everything that is not code lives in `C:\ProgramData\MarketGuard\`:

```
marketguard.env     data dir, state dir, alert channels
logs\               recorder-<timestamp>.log, watchdog-<timestamp>.log
alerts.log          one line per alert, what status.py reads
watchdog.json       restart history and alert cooldowns
```

### Alerting

Alerts go to whatever is configured, always including the Windows Application
log — which is what triggers the on-screen popup. That only reaches you while
you are at the machine, so configure **ntfy** as well: it needs no account at
all. Pick an unguessable topic, install the ntfy app, subscribe, and pass
`-NtfyTopic`. A Slack or Discord incoming webhook works too via
`-WebhookUrl` (`-WebhookField content` for Discord).

Test the whole path:

```powershell
.\venv\Scripts\python notify.py WARN
```

A popup and a phone notification should both arrive. The watchdog also sends
one **daily heartbeat** — not decoration: a silent monitor and a working one
look identical, and the heartbeat is the cheap proof that the channel still
reaches you.

### Verifying it is actually working

```powershell
.\venv\Scripts\python status.py
```

```
MarketGuard   2026-09-25 01:13:12 +0800   (2026-09-24 17:13 UTC)

  CAPTURING        newest record 2s old

  recorder       pid 18924             up 77s, Task Scheduler
  task recorder  Running               \MarketGuard\Recorder
  task watchdog  Ready                 \MarketGuard\Watchdog
  disk           293.4 GB free         C:  (recorder aborts below 5 GB)
  archive        531 MB                74 hours, 09-21 16:00 .. 09-24 17:00 UTC
  burn rate      0.17 GB/day           ~5.9 GB more to 30 Oct (35d), 293 GB free
  last 24h       [#######################>] 24/24 hours
  alerts         none in 7 days

  ALL GOOD
```

Exit code is 0 only when everything is fine, so it also works in a script.
`--hours 72` widens the strip, `--log` tails the recorder's own log.
`#` is a normal hour, `-` thin, `.` missing, `>` the hour in progress.
`coverage.py` remains the authoritative audit of the archive.

Three things that look wrong and are not:

- **Last Run Result `0x800710E0`** on the Recorder task. That is the repeating
  trigger being refused because an instance is already running. It is the
  `IgnoreNew` policy working, not a failure.
- **`Length 0`** on the file currently being written. On NTFS the directory
  entry for an open file is updated lazily — we have watched it report 0 bytes
  for a file holding 6.8 MB. Nothing in this project measures liveness with
  `stat()` for exactly that reason; `status.py` reads the newest record back
  instead.
- **Two `python.exe` per recorder.** A venv's `python.exe` on Windows is a
  launcher that re-execs the base interpreter. `status.py` collapses the pair.

### When it is not working

| Symptom | Where to look |
|---|---|
| `NOT CAPTURING` | `python status.py --log` — the recorder's own log has the disconnect reason |
| `recorder: no process` | `Get-ScheduledTaskInfo -TaskPath '\MarketGuard\' -TaskName 'Recorder'`, then `C:\ProgramData\MarketGuard\logs\` |
| `NOT scheduled` in the recorder row | something started it from a console; it will die with that window |
| `task ... NOT INSTALLED` | re-run `ops\install-tasks.ps1` elevated |
| Nothing in `logs\` at all | the task action failed before Python started — wrong `-RepoRoot` or `-Python` |
| Alerts never arrive | `python notify.py WARN`, then check `alerts.log` for which channels took it |
| Restarts every few minutes | `watchdog.json` restart history; after two failed restarts in an hour it stops trying and keeps alerting |

Stopping it by hand needs a note of caution: `Stop-ScheduledTask` ends the
task's action process but **leaves the Python process running** — the action
chain is `powershell -> cmd -> python` and only the top is terminated. To
actually stop capture, stop the processes:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*recorder.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

(The watchdog does exactly this before restarting, for the same reason.)

To remove everything: `.\ops\uninstall-tasks.ps1`. It never touches the
archive.

### Sleep, and why not just change the power settings

`powercfg /change standby-timeout-ac 0` would work and would keep working
after this project ends, on somebody's desktop, invisibly. Instead the
recorder holds `ES_SYSTEM_REQUIRED | ES_CONTINUOUS` for as long as it runs.
The kernel releases that when the process exits — including when it is killed
— so normal power behaviour returns the moment recording stops. Display sleep
is left alone; a dark monitor costs nothing.

This defeats the **idle** timer, which is what cost us the nine thin hours
between 09-21 17:00 and 09-22 13:00. It does not defeat a deliberate
Start → Sleep. `MARKETGUARD_AWAY_MODE=1` in `marketguard.env` makes it
intercept that too, at the cost of turning "sleep" into "runs with the screen
off".

### Disk

Measured burn is **~0.08 GB/day** across six symbols; the live archive is
running nearer 0.17 GB/day. Even at five times that — a sustained
high-volatility month — the whole run to 30 Oct is under 25 GB, against 293 GB
free. Disk is not the constraint.

It is still monitored, because the failure is silent: the recorder aborts
below 5 GB and stops. The watchdog warns at **25 GB** and escalates at 8 GB,
so an alert means *something else* is filling the drive, with weeks of
headroom to deal with it.

---

## Querying the data

The recordings are append-only and immutable, and every useful query is an
aggregation over a time range rather than a point lookup. That is the
analytical access pattern, so the data goes to **Parquet + DuckDB** rather
than a row store — full SQL, no server, no migrations.

```powershell
.\venv\Scripts\python etl.py      # JSONL -> parquet/ (incremental)
.\venv\Scripts\python query.py    # coverage + trade flow summary
```

```powershell
.\venv\Scripts\python query.py "SELECT symbol, count(*) FROM book GROUP BY 1"
```

Two views are registered:

| view | grain | columns |
|---|---|---|
| `book` | one row per snapshot | `t, symbol, best_bid, best_ask, mid, spread, bid_depth, ask_depth, bids, asks, est` |
| `trades` | one row per trade | `t, event_time, symbol, price, qty, is_buyer_maker, aggressive_buy` |

`bids` / `asks` are list columns of `{price, qty}`. Most queries only need the
precomputed scalars; `UNNEST` when per-level detail is required. Exploding
every snapshot into 40 level-rows would mean ~67M rows/day and inflates
storage for no gain.

---

## Features

`features.py` computes the rolling features every detector consumes, per
symbol, over configurable windows. Batch/historical only for now — over
Parquet via DuckDB, which is what the auto-labeller and threshold tuning
need and the only path that can be validated against real capture.
`FeatureExtractor` is the ABC a future streaming implementation must satisfy.

```powershell
.\venv\Scripts\python features.py              # diagnostic over every symbol
.\venv\Scripts\python features.py solusdt 51   # one symbol, 51 hours
.\venv\Scripts\python -m pytest test_features.py
```

| Feature | Grain | Fires |
|---|---|---|
| Volume z-score | bar | z > 5 notable, z > 10 extreme |
| Thin-book percentile | bar | below p20 of its own trailing distribution |
| Wall detection | level | above the p99 of its **price band** *and* within 20bps of mid |
| Order flow imbalance | window of bars | — |
| Retracement ratio | bar | above 0.7 |
| Level lifetime, cancel/fill | episode | — |

Median and MAD, never mean and stdev: a trailing window wide enough to be a
useful baseline also contains previous pumps, which inflate σ and mask the
next one.

### ⚠️ Missing history is a state, not a number

Only ~3 days of capture exist and the recorder stopped and restarted inside
it, so trailing windows are routinely short. Every value carries a `Status`
— `ok`, `partial_history` (computed, visibly degraded), `insufficient_history`
(`None`), `no_data`, `zero_scale`, `no_pump`. `Windowed.require()` raises
rather than let a caller read through a gap.

A bar counts as *covered* only if it saw a snapshot or a trade. Left-joining
a dense minute grid onto the trades table otherwise turns every un-recorded
minute into a `0.0`, and a window half full of fabricated zeros drives the
median to 0, the MAD to 0, and the z-score to nonsense.

---

## Pump & dump gate

`pumpdump.py` is the cheap filter in front of the LLM cascade. It consumes
`features.py` bars and emits a **candidate** when four things are
simultaneously true: volume z-score past the notable cut, a price spike large
*for that symbol*, depth within ±1% of mid below the p20 of its own trailing
distribution, and order flow imbalance approaching +1.0. A fifth leg — no news
catalyst — is a seam (`CatalystSource`), not an implementation; the default
`UncheckedCatalyst` returns `unknown`, never `absent`.

```powershell
.\venv\Scripts\python pumpdump.py                  # gate over every symbol, 24h
.\venv\Scripts\python pumpdump.py solusdt 72       # one symbol, 72 hours
.\venv\Scripts\python pumpdump.py --distributions  # what the features actually do
.\venv\Scripts\python -m pytest test_pumpdump.py
```

### Probabilistic and confirmed are different types

The retracement ratio is the *confirming* signal and it is **retrospective**:
it needs the trough, and at the moment an alert fires the trough has not
happened. A live alert is a probability; a retracement is a finding. Blurring
them is how a surveillance system ends up accusing someone.

So the distinction is structural rather than a comment or a boolean:

| | `Candidate` | `ConfirmedDump` |
|---|---|---|
| when | at the end of its own bar | only after `observation_bars` more |
| retracement field | **does not exist** | required |
| how constructed | `PumpDumpGate.evaluate()` | `confirm()` only |
| headline text | `CANDIDATE (probabilistic, unconfirmed)` | `CONFIRMED retracement` |

`ConfirmedDump` deliberately does not re-export the candidate's fields — reach
them through `.candidate` — so a renderer written for one raises
`AttributeError` on the other instead of quietly relabelling it. And
`confirm()` raises `PrematureConfirmation` unless it is handed bars extending
past the candidate's window, so there is no code path from a live alert to a
confirmed event without data from the future. `None` from `confirm()` means
"looked, and it did not retrace"; the exception means "cannot look yet".

### ⚠️ The archive contains no pump and dump

The largest 30-minute run-up on solusdt is 1.74% and the median is 0.74%.
There is no positive example anywhere in the capture, so the thresholds in
`GateConfig` **cannot be validated** and are frozen at their design values.
Correctness lives in `test_pumpdump.py`, against synthetic markets of known
shape — clean pump and dump, a pump that never dumps, a slow organic rally, a
volume spike with no price move, a crash with no preceding pump, the same pump
into a deep book, the same pump on two-sided flow.

Over the whole archive (6 symbols, 5.4 days, 39,372 covered bars) the gate
produces **4 candidates**, and all four are false positives — which is the
expected result, because nothing in this data is a pump. They are instructive
anyway:

| | opusdt 09-23 22:08 | arbusdt 09-27 00:33–00:35 |
|---|---|---|
| volume z | 17.7 | 65.0 |
| what that was, in dollars | **$14,588** | **$136,668** |
| that symbol's median minute | $2,973 | $12,882 |
| price move | +0.77% over 5 min | +0.77% over 5 min |

Both are ordinary market buys. Every leg of the conjunction is a *relative*
measure, so on a symbol whose median minute is three thousand dollars, a
fourteen-thousand-dollar order is a 17-MAD event — correctly, and
meaninglessly. **The missing leg is an absolute magnitude floor**, the same
gap `level_min_usd` already documents for resting size. It is deliberately not
added here: picking the number without a real event to calibrate against would
be guessing, and this is written down so the tuning pass starts from the
measurement rather than from a hunch.

The opusdt candidate also *confirmed*, retracing 267% of a **0.73%** run-up.
That is `min_runup_pct = 0.005` behaving exactly as `features.py` says it
will — deliberately low so the feature stays exercised on an archive with no
pumps in it. It is noise retracing noise, not a finding.

### Measured feature distributions

Per symbol, over the full archive, on the bars where each feature was
computable. This is what a later threshold pass should start from.

| feature | p50 | p90 | p99 | max | past the gate |
|---|---|---|---|---|---|
| volume z | ~0.0 | 4.3–6.1 | 17.7–28.6 | 132–264 | 8.6–12.2% of bars |
| price spike z | ~0.0 | 2.2 | 4.6–5.2 | 8.8–11.8 | 0.75–1.24% |
| thin-book pctile | 0.50 | 0.92–0.94 | 1.00 | 1.00 | 20.8–23.5% |
| OFI (5-bar) | ~0.0 | 0.38–0.50 | 0.64–0.79 | 0.86–1.00 | 0.49–2.70% |

Read together: volume z fires on roughly one bar in ten, and the spike, thin
book and OFI legs each fire on far fewer — the conjunction is doing the work,
not any single threshold. OFI is the binding constraint on this data; only
solusdt and avaxusdt ever reach ±0.9, and no symbol sustains it while the
other three legs hold.

---

### ⚠️ Comparing depth across snapshots

Quantity at "the best bid" is **not comparable between snapshots unless the
best bid price is unchanged.** Top of book flickers between adjacent ticks
with tiny resting sizes, so a naive `lag(best_bid_qty)` produces enormous
multipliers that are pure artifact — and they look exactly like dramatic
findings. Always constrain with `best_bid = prev_bid`.

---

## Why `@depth20@100ms`

Binance offers order book **diffs** (`@depth`) and **snapshots** (`@depth20`).
This project uses snapshots:

| | diffs | snapshots |
|---|---|---|
| Book maintenance | build and maintain locally | none — every message is complete |
| Sequence gaps | must detect and resync | n/a |
| Depth | full book | top 20 levels |
| Bug risk | high | near zero |

Spoofing happens near the top of book — a wall 5% away persuades nobody — so
20 levels is sufficient for the primary detector, and this removes the most
bug-prone component in the system.

---

## License

MIT — see [LICENSE](LICENSE).
