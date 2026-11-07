"""Reversible repair for confirmed historical duplicate transcript rows.

The v0.15.2-and-earlier double-assistant-write defect left two identical
assistant rows per completed turn in some conversations; historical resume
paths may have done the same with a time gap. This tool classifies candidate
pairs against DURABLE TURN RECORDS (never text/timestamps alone), hides the
proven redundant row with the canonical `hidden` metadata flag already
honored by display/history readers and the model-context loader, and can
reverse the change.

Mechanics:
- `--inspect` prints the read-only classification report.
- `--dry-run` prints the exact row ids that would be hidden plus projected
  integrity counts; nothing is written.
- `--apply` flags the confirmed redundant rows (short parameterized UPDATE
  per row; metadata JSON edited in Python and stored back as text).
- `--unhide ROW_ID[,ROW_ID...]` restores flagged rows (the inverse op).

A row is only flagged when ALL of the following hold:
- same session, same role (`assistant`), byte-identical content;
- a durable `gateway_turns` record with `status='completed'` whose window
  [created_at, terminal_at] contains BOTH rows of the pair;
- the pair has exactly two rows inside that window;
- the later row is the hidden candidate (the earliest is kept).

Anything else is left untouched as ambiguous or legitimate.

NOTE: the running application keeps an in-memory session cache; a repair
applied while it serves must be followed by a service restart (or a deploy)
so the cache re-reads the repaired transcript. The canonical `hidden` flag
is honored by display readers and the model-context loader after v0.15.5.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from datetime import datetime

from sqlalchemy import create_engine, text


def _as_datetime(value):
    """Normalize a timestamp that may arrive as an ISO string (SQLite)."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _db_url(args) -> str:
    if args.database_url:
        return args.database_url
    from dotenv import load_dotenv

    load_dotenv("/home/ubuntu/.config/mnerys-preview/runtime.env", override=True)
    import os

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not configured for this host")
    return url


def _classify(engine) -> list[dict]:
    """Read-only classification of identical-content assistant pairs."""
    report = []
    with engine.connect() as conn:
        groups = conn.execute(text("""
            SELECT session_id, content, count(*) AS n,
                   min(timestamp) AS oldest, max(timestamp) AS newest,
                   min(id) AS first_id, max(id) AS last_id
            FROM chat_messages
            WHERE role = 'assistant'
            GROUP BY session_id, content
            HAVING count(*) = 2
        """)).mappings().all()
        for group in groups:
            session_id = group["session_id"]
            # A double-write leaves the two assistant rows CONSECUTIVE: any
            # user message between them means these are legitimate answers
            # to two separate asks, not duplicated persistence.
            user_between = conn.execute(text(
                "SELECT count(*) FROM chat_messages"
                " WHERE session_id = :s AND role = 'user'"
                " AND timestamp > :oldest AND timestamp <= :newest"
            ), {
                "s": session_id,
                "oldest": group["oldest"],
                "newest": group["newest"],
            }).scalar_one()
            window = None
            if user_between == 0:
                window = conn.execute(text(
                    "SELECT id FROM gateway_turns"
                    " WHERE session_id = :s AND status = 'completed'"
                      " AND created_at IS NOT NULL AND terminal_at IS NOT NULL"
                      " AND created_at <= :oldest AND terminal_at >= :newest"
                    " LIMIT 1"
                ), {
                    "s": session_id,
                    "oldest": group["oldest"],
                    "newest": group["newest"],
                }).scalar_one_or_none()
            rows = conn.execute(text("""
                SELECT id, timestamp FROM chat_messages
                WHERE session_id = :s AND role = 'assistant' AND content = :ct
                ORDER BY timestamp
            """), {"s": session_id, "ct": group["content"]}).all()
            gap = (_as_datetime(group["newest"]) - _as_datetime(group["oldest"])).total_seconds()
            item = {
                "session_label": "S-" + hashlib.sha1(
                    session_id.encode(),
                ).hexdigest()[:6],
                "content_hash": "sha1:" + hashlib.sha1(
                    str(group["content"]).encode(),
                ).hexdigest()[:10],
                "row_count": len(rows),
                "rows": [
                    {"id": r[0], "timestamp": _as_datetime(r[1]).isoformat()}
                    for r in rows
                ],
                "gap_seconds": round(gap, 3),
                "classification": "ambiguous",
                "confidence": "low",
            }
            if window is not None:
                item["turn_id"] = window
                item["keep_row_id"] = rows[0][0]
                item["hide_row_id"] = rows[-1][0]
                item["classification"] = "confirmed-redundant"
                item["confidence"] = "high" if gap < 2 else "medium"
            report.append(item)
    return report


def _row_counts(engine):
    with engine.connect() as conn:
        return {
            "assistant_rows": conn.execute(text(
                "SELECT count(*) FROM chat_messages WHERE role='assistant'",
            )).scalar_one(),
            "total_rows": conn.execute(text(
                "SELECT count(*) FROM chat_messages",
            )).scalar_one(),
        }


def _load_metadata(conn, row_id) -> dict:
    raw = conn.execute(text(
        "SELECT metadata FROM chat_messages WHERE id = :i",
    ), {"i": row_id}).scalar_one_or_none()
    if raw is None:
        raise RuntimeError(f"row {row_id} vanished before the repair")
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _hide_rows(engine, row_ids):
    """Flag rows hidden with short parameterized updates (idempotent)."""
    hidden = []
    with engine.begin() as conn:
        for row_id in row_ids:
            metadata = _load_metadata(conn, row_id)
            if metadata.get("hidden"):
                continue
            metadata["hidden"] = True
            metadata["hidden_reason"] = "duplicate-write-repair"
            conn.execute(text(
                "UPDATE chat_messages SET metadata = :m WHERE id = :i",
            ), {"m": json.dumps(metadata), "i": row_id})
            hidden.append(row_id)
    return hidden


def _unhide_rows(engine, row_ids):
    restored = []
    with engine.begin() as conn:
        for row_id in row_ids:
            metadata = _load_metadata(conn, row_id)
            if not metadata.get("hidden"):
                continue
            metadata.pop("hidden", None)
            metadata.pop("hidden_reason", None)
            conn.execute(text(
                "UPDATE chat_messages SET metadata = :m WHERE id = :i",
            ), {"m": json.dumps(metadata), "i": row_id})
            restored.append(row_id)
    return restored


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reversible duplicate transcript repair (hide/unhide).",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--unhide", default=None, help="comma-separated row ids")
    args = parser.parse_args(argv)

    engine = create_engine(
        _db_url(args), connect_args={"connect_timeout": 10},
    )

    if args.unhide:
        ids = [x.strip() for x in args.unhide.split(",") if x.strip()]
        restored = _unhide_rows(engine, ids)
        counts = _row_counts(engine)
        print(json.dumps({"restored": restored, **counts}, indent=2))
        return 0

    report = _classify(engine)
    targets = [
        item for item in report
        if item["classification"] == "confirmed-redundant"
    ]

    if args.inspect or args.dry_run:
        counts = _row_counts(engine)
        payload = {
            "pairs": report,
            "confirmed_redundant": len(targets),
            "would_hide": [item["hide_row_id"] for item in targets],
            "visible_assistant_rows_before": counts["assistant_rows"],
            "visible_assistant_rows_after": (
                counts["assistant_rows"] - len(targets)
                if args.dry_run else counts["assistant_rows"]
            ),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    if args.apply:
        ids = [item["hide_row_id"] for item in targets]
        counts_before = _row_counts(engine)
        hidden = _hide_rows(engine, ids)
        counts_after = _row_counts(engine)
        print(json.dumps({
            "hidden": hidden,
            "rows_before": counts_before["total_rows"],
            "rows_after": counts_after["total_rows"],
            "row_count_unchanged": (
                counts_before["total_rows"] == counts_after["total_rows"]
            ),
        }, indent=2))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
