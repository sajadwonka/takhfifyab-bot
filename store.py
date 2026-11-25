"""SQLite storage for the price-hunter bot.

Two tables
-----------
watches: one row per (user, product) the user follows.
    user_id, product_id, title, first_price, last_price, lowest_price,
    target_price (NULL = no target), created_at, updated_at.
    Prices are Toman (integers).

price_history: one row per check, so the bot can show a trend and the
    user can see how the price moved.
    product_id, price, checked_at.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Watch:
    user_id: int
    product_id: int
    title: str
    first_price: int
    last_price: int
    lowest_price: int
    target_price: int | None
    created_at: int
    updated_at: int

    @property
    def change_percent(self) -> float:
        if not self.first_price:
            return 0.0
        return (self.last_price - self.first_price) / self.first_price * 100.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    user_id      INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    first_price  INTEGER NOT NULL,
    last_price   INTEGER NOT NULL,
    lowest_price INTEGER NOT NULL,
    target_price INTEGER,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    checked_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, checked_at);
"""


class Store:
    def __init__(self, path: str = "pricebot.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- watches ---------------------------------------------------------
    def count_watches(self, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM watches WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["n"])

    def get_watch(self, user_id: int, product_id: int) -> Watch | None:
        row = self.conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        ).fetchone()
        return self._row_to_watch(row) if row else None

    def list_watches(self, user_id: int) -> list[Watch]:
        rows = self.conn.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [self._row_to_watch(r) for r in rows]

    def all_watches(self) -> list[Watch]:
        rows = self.conn.execute("SELECT * FROM watches ORDER BY user_id, created_at").fetchall()
        return [self._row_to_watch(r) for r in rows]

    def add_watch(self, user_id: int, product_id: int, title: str, price: int) -> Watch:
        now = int(time.time())
        existing = self.get_watch(user_id, product_id)
        if existing:
            return existing
        self.conn.execute(
            """INSERT INTO watches
               (user_id, product_id, title, first_price, last_price,
                lowest_price, target_price, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (user_id, product_id, title, price, price, price, now, now),
        )
        self.record_price(product_id, price)
        self.conn.commit()
        return self.get_watch(user_id, product_id)  # type: ignore[return-value]

    def update_price(self, user_id: int, product_id: int, new_price: int) -> Watch | None:
        watch = self.get_watch(user_id, product_id)
        if watch is None:
            return None
        lowest = min(watch.lowest_price, new_price)
        self.conn.execute(
            """UPDATE watches SET last_price = ?, lowest_price = ?,
               updated_at = ? WHERE user_id = ? AND product_id = ?""",
            (new_price, lowest, int(time.time()), user_id, product_id),
        )
        self.record_price(product_id, new_price)
        self.conn.commit()
        return self.get_watch(user_id, product_id)

    def set_target(self, user_id: int, product_id: int, target: int) -> Watch | None:
        self.conn.execute(
            "UPDATE watches SET target_price = ? WHERE user_id = ? AND product_id = ?",
            (target, user_id, product_id),
        )
        self.conn.commit()
        return self.get_watch(user_id, product_id)

    def remove_watch(self, user_id: int, product_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM watches WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- price history ---------------------------------------------------
    def record_price(self, product_id: int, price: int) -> None:
        self.conn.execute(
            "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
            (product_id, price, int(time.time())),
        )

    def recent_history(self, product_id: int, limit: int = 10) -> list[tuple[int, int]]:
        rows = self.conn.execute(
            """SELECT price, checked_at FROM price_history
               WHERE product_id = ? ORDER BY checked_at DESC LIMIT ?""",
            (product_id, limit),
        ).fetchall()
        return [(r["price"], r["checked_at"]) for r in rows]

    @staticmethod
    def _row_to_watch(row: sqlite3.Row) -> Watch:
        return Watch(
            user_id=row["user_id"],
            product_id=row["product_id"],
            title=row["title"],
            first_price=row["first_price"],
            last_price=row["last_price"],
            lowest_price=row["lowest_price"],
            target_price=row["target_price"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
