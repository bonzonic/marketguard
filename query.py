"""SQL over the recorded data, via DuckDB.

No server, no schema migrations - DuckDB reads the Parquet files directly.
Two views are registered:

    book    one row per order book snapshot
            t, symbol, best_bid, best_ask, mid, spread, bid_depth, ask_depth,
            bids, asks (lists of {price, qty}), est

    trades  one row per aggregated trade
            t, event_time, symbol, price, qty, is_buyer_maker, aggressive_buy

Usage:
    python query.py                      # summary of what has been recorded
    python query.py "SELECT ..."         # run a query
    python query.py --walls              # largest single-level size jumps
"""
import sys
from pathlib import Path

import duckdb

import config

PARQUET = config.PARQUET_DIR


def connect(parquet_dir: Path = PARQUET) -> duckdb.DuckDBPyConnection:
    """A connection with `book` and `trades` views registered."""
    con = duckdb.connect()
    for name in ("book", "trades"):
        glob = (parquet_dir / name / "*.parquet").as_posix()
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}')")
    return con


def summary(con: duckdb.DuckDBPyConnection) -> None:
    print("=== coverage ===")
    con.sql("""
        SELECT symbol,
               count(*)                                    AS snapshots,
               strftime(to_timestamp(min(t)/1000), '%m-%d %H:%M') AS first,
               strftime(to_timestamp(max(t)/1000), '%m-%d %H:%M') AS last,
               round(avg(spread / mid) * 10000, 2)         AS avg_spread_bps,
               round(100.0 * sum(est::int) / count(*), 1)  AS pct_est
        FROM book GROUP BY symbol ORDER BY snapshots DESC
    """).show()

    print("=== trade flow ===")
    con.sql("""
        SELECT symbol,
               count(*)                                          AS trades,
               round(sum(qty * price))                           AS notional_usd,
               round(100.0 * sum(aggressive_buy::int) / count(*), 1) AS pct_aggressive_buy
        FROM trades GROUP BY symbol ORDER BY notional_usd DESC
    """).show()


def walls(con: duckdb.DuckDBPyConnection, limit: int = 15) -> None:
    """Largest single-snapshot jumps in top-of-book size.

    A crude first look at spoofing candidates: size at the best bid or ask
    multiplying several-fold from one snapshot to the next. Not a detector -
    it ignores lifetime, fill ratio and repetition - but it shows whether
    anything dramatic is happening in the book at all.
    """
    print(f"=== largest top-of-book size jumps (top {limit}) ===")
    con.sql(f"""
        WITH d AS (
            SELECT t, symbol, best_bid_qty, best_ask_qty, mid,
                   lag(best_bid_qty) OVER w AS prev_bid_qty,
                   lag(best_ask_qty) OVER w AS prev_ask_qty,
                   lag(t)            OVER w AS prev_t
            FROM book
            WINDOW w AS (PARTITION BY symbol ORDER BY t)
        )
        SELECT strftime(to_timestamp(t/1000), '%m-%d %H:%M:%S') AS ts,
               symbol,
               CASE WHEN best_bid_qty / nullif(prev_bid_qty,0)
                       > best_ask_qty / nullif(prev_ask_qty,0)
                    THEN 'bid' ELSE 'ask' END                   AS side,
               round(greatest(best_bid_qty / nullif(prev_bid_qty,0),
                              best_ask_qty / nullif(prev_ask_qty,0)), 1) AS size_mult,
               round(greatest(best_bid_qty, best_ask_qty))       AS new_qty,
               round(greatest(best_bid_qty, best_ask_qty) * mid) AS notional_usd,
               t - prev_t                                        AS gap_ms
        FROM d
        WHERE prev_t IS NOT NULL
          AND t - prev_t < 500
          AND greatest(best_bid_qty, best_ask_qty) * mid > 50000
        ORDER BY size_mult DESC
        LIMIT {limit}
    """).show()


def main() -> int:
    if not PARQUET.exists():
        print("no parquet/ directory - run: python etl.py")
        return 1

    con = connect()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--walls" in sys.argv:
        walls(con)
    elif args:
        con.sql(args[0]).show(max_rows=60)
    else:
        summary(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
