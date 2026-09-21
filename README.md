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
| Feature extractor | 🔜 |
| Spoofing detector | 🔜 |
| Pump & dump detector | 🔜 |
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
| **Pump & dump** | planned | Volume z-score + one-sided flow + thin book + retracement |
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

One JSON object per line, exactly as received from Binance:

```json
{"stream":"solusdt@depth20@100ms","data":{"lastUpdateId":123,"bids":[["1.23","45.6"]],"asks":[["1.24","78.9"]]}}
{"stream":"solusdt@aggTrade","data":{"e":"aggTrade","E":1234567890123,"s":"SOLUSDT","p":"1.235","q":"10.0","m":false}}
```

All analysis uses Binance's server-side timestamp (`E`), not arrival time, so
network latency does not distort any downstream feature.

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
