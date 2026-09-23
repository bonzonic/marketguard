"""Convert recorded JSONL into Parquet for querying.

The recordings are append-only and immutable, and every useful query is an
aggregation over a time range rather than a point lookup. That is the
analytical access pattern, so the data wants a columnar store - not a row
store with transaction and index machinery it will never use.

Two datasets are produced under parquet/:

  book/    one row per order book snapshot
  trades/  one row per aggregated trade

The book table keeps levels as list columns rather than exploding each
snapshot into 40 rows - exploding inflates storage badly for no gain, since
most queries only need the precomputed scalars (best bid/ask, spread, mid,
total depth). Use UNNEST when per-level detail is actually needed.

Usage:
    python etl.py [data_dir] [--force]
"""
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from datafile import files_by_hour, read_all, timestamp

LEVEL = pa.struct([("price", pa.float64()), ("qty", pa.float64())])

BOOK_SCHEMA = pa.schema([
    ("t", pa.int64()),
    ("symbol", pa.string()),
    ("last_update_id", pa.int64()),
    ("best_bid", pa.float64()),
    ("best_bid_qty", pa.float64()),
    ("best_ask", pa.float64()),
    ("best_ask_qty", pa.float64()),
    ("mid", pa.float64()),
    ("spread", pa.float64()),
    ("bid_depth", pa.float64()),
    ("ask_depth", pa.float64()),
    ("bids", pa.list_(LEVEL)),
    ("asks", pa.list_(LEVEL)),
    ("est", pa.bool_()),
])

TRADE_SCHEMA = pa.schema([
    ("t", pa.int64()),
    ("event_time", pa.int64()),
    ("symbol", pa.string()),
    ("agg_id", pa.int64()),
    ("price", pa.float64()),
    ("qty", pa.float64()),
    ("is_buyer_maker", pa.bool_()),
    ("aggressive_buy", pa.bool_()),
])


def _levels(raw: list) -> tuple[list[dict], float]:
    """Parse [[price, qty], ...] into structs, returning total quantity."""
    out = []
    total = 0.0
    for price, qty in raw:
        q = float(qty)
        out.append({"price": float(price), "qty": q})
        total += q
    return out, total


def convert_hour(paths: list[Path]) -> tuple[pa.Table, pa.Table]:
    """Build book and trade tables for one hour's files (deduplicated)."""
    book = defaultdict(list)
    trades = defaultdict(list)

    for msg in read_all_paths(paths):
        stream = msg["stream"]
        symbol = stream.split("@", 1)[0]
        data = msg["data"]
        ts = timestamp(msg)

        if "aggTrade" in stream:
            trades["t"].append(msg.get("t"))
            trades["event_time"].append(data.get("E"))
            trades["symbol"].append(symbol)
            trades["agg_id"].append(data.get("a"))
            trades["price"].append(float(data["p"]))
            trades["qty"].append(float(data["q"]))
            maker = bool(data["m"])
            trades["is_buyer_maker"].append(maker)
            # m is "was the BUYER the maker". So m=False means the buyer
            # crossed the spread - an aggressive buy. Order flow imbalance
            # depends entirely on getting this inversion right.
            trades["aggressive_buy"].append(not maker)
            continue

        bids, bid_depth = _levels(data["bids"])
        asks, ask_depth = _levels(data["asks"])
        if not bids or not asks:
            continue

        book["t"].append(ts)
        book["symbol"].append(symbol)
        book["last_update_id"].append(data.get("lastUpdateId"))
        book["best_bid"].append(bids[0]["price"])
        book["best_bid_qty"].append(bids[0]["qty"])
        book["best_ask"].append(asks[0]["price"])
        book["best_ask_qty"].append(asks[0]["qty"])
        book["mid"].append((bids[0]["price"] + asks[0]["price"]) / 2)
        book["spread"].append(asks[0]["price"] - bids[0]["price"])
        book["bid_depth"].append(bid_depth)
        book["ask_depth"].append(ask_depth)
        book["bids"].append(bids)
        book["asks"].append(asks)
        book["est"].append(bool(msg.get("est")))

    return (
        pa.table({f.name: book.get(f.name, []) for f in BOOK_SCHEMA}, schema=BOOK_SCHEMA),
        pa.table({f.name: trades.get(f.name, []) for f in TRADE_SCHEMA}, schema=TRADE_SCHEMA),
    )


def read_all_paths(paths: list[Path]):
    """read_all() scoped to an explicit file list, preserving deduplication."""
    from datafile import message_id, read_messages

    seen = set()
    multi = len(paths) > 1
    for path in paths:
        for msg in read_messages(path):
            if multi:
                key = message_id(msg)
                if key in seen:
                    continue
                seen.add(key)
            yield msg


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    data_dir = Path(args[0]) if args else Path("data")
    out_dir = data_dir.parent / "parquet"

    (out_dir / "book").mkdir(parents=True, exist_ok=True)
    (out_dir / "trades").mkdir(parents=True, exist_ok=True)

    groups = files_by_hour(data_dir)
    if not groups:
        print(f"no data files in {data_dir}/")
        return 1

    converted = skipped = 0
    for hour in sorted(groups):
        book_path = out_dir / "book" / f"{hour}.parquet"
        trade_path = out_dir / "trades" / f"{hour}.parquet"

        # The current hour is still being written, so always redo it.
        is_latest = hour == max(groups)
        if book_path.exists() and not force and not is_latest:
            skipped += 1
            continue

        book, trades = convert_hour(groups[hour])
        # zstd beats snappy here by a wide margin: prices and timestamps in a
        # sorted-ish column repeat heavily.
        pq.write_table(book, book_path, compression="zstd")
        pq.write_table(trades, trade_path, compression="zstd")
        converted += 1
        print(f"  {hour}  book {book.num_rows:>7,}  trades {trades.num_rows:>7,}")

    print(f"\nconverted {converted} hour(s), skipped {skipped} already done")
    if converted:
        size = sum(p.stat().st_size for p in out_dir.rglob("*.parquet")) / 1024**2
        print(f"parquet total: {size:,.1f} MB  ->  {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
