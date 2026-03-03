"""Async SQLite database manager using aiosqlite."""

import aiosqlite
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("/data/fve.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  DATETIME NOT NULL,
    source      TEXT NOT NULL DEFAULT 'forecast_solar',
    for_date    DATE NOT NULL,
    slot_time   DATETIME NOT NULL,
    watts       REAL NOT NULL,
    wh_day      REAL,
    UNIQUE (fetched_at, slot_time)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_slot ON forecasts(slot_time);
CREATE INDEX IF NOT EXISTS idx_forecasts_date ON forecasts(for_date);

CREATE TABLE IF NOT EXISTS actuals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at  DATETIME NOT NULL UNIQUE,
    power_w     REAL NOT NULL,
    energy_kwh  REAL
);
CREATE INDEX IF NOT EXISTS idx_actuals_sampled ON actuals(sampled_at);

CREATE TABLE IF NOT EXISTS daily_summary (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date            DATE NOT NULL UNIQUE,
    forecast_wh_raw         REAL,
    forecast_wh_calibrated  REAL,
    actual_wh               REAL,
    ratio                   REAL,
    correction_factor       REAL,
    tod_morning_factor      REAL,
    tod_midday_factor       REAL,
    tod_afternoon_factor    REAL,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_daily_summary_date ON daily_summary(summary_date);

CREATE TABLE IF NOT EXISTS calibration_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    global_correction   REAL NOT NULL DEFAULT 1.0,
    tod_morning_factor  REAL NOT NULL DEFAULT 1.0,
    tod_midday_factor   REAL NOT NULL DEFAULT 1.0,
    tod_afternoon_factor REAL NOT NULL DEFAULT 1.0,
    days_of_data        INTEGER NOT NULL DEFAULT 0,
    phase               TEXT NOT NULL DEFAULT 'warmup',
    last_calibrated_at  DATETIME,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO calibration_state (id) VALUES (1);
"""


class DatabaseManager:

    async def initialize(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        logger.info("Database initialized at %s", DB_PATH)

    # ── Forecasts ──────────────────────────────────────────────────────────

    async def upsert_forecast_slots(
        self, fetched_at: datetime, slots: list[dict]
    ) -> int:
        """Bulk-insert forecast slots; skip duplicates. Returns inserted count."""
        if not slots:
            return 0
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO forecasts
                    (fetched_at, for_date, slot_time, watts, wh_day)
                VALUES (:fetched_at, :for_date, :slot_time, :watts, :wh_day)
                """,
                [
                    {
                        "fetched_at": fetched_at.isoformat(),
                        "for_date": s["for_date"],
                        "slot_time": s["slot_time"],
                        "watts": s["watts"],
                        "wh_day": s.get("wh_day"),
                    }
                    for s in slots
                ],
            )
            await db.commit()
            return len(slots)

    async def get_forecast_for_date(
        self, for_date: str, latest_only: bool = True
    ) -> list[dict]:
        """
        Return forecast slots for a given date.
        If latest_only=True, returns only the most recently fetched set.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if latest_only:
                cursor = await db.execute(
                    """
                    SELECT f.* FROM forecasts f
                    INNER JOIN (
                        SELECT MAX(fetched_at) AS max_fetch FROM forecasts WHERE for_date = ?
                    ) m ON f.fetched_at = m.max_fetch
                    WHERE f.for_date = ?
                    ORDER BY f.slot_time
                    """,
                    (for_date, for_date),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM forecasts WHERE for_date = ? ORDER BY slot_time",
                    (for_date,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_latest_forecast_wh_day(self, for_date: str) -> Optional[float]:
        """Return the latest forecasted daily total Wh for a date."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """
                SELECT wh_day FROM forecasts
                WHERE for_date = ? AND wh_day IS NOT NULL
                ORDER BY fetched_at DESC LIMIT 1
                """,
                (for_date,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    # ── Actuals ────────────────────────────────────────────────────────────

    async def insert_actual(
        self, sampled_at: str, power_w: float, energy_kwh: Optional[float]
    ) -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO actuals (sampled_at, power_w, energy_kwh)
                VALUES (?, ?, ?)
                """,
                (sampled_at, power_w, energy_kwh),
            )
            await db.commit()

    async def get_actuals_for_date(self, for_date: str) -> list[dict]:
        """Return all actual readings for a given calendar date, ordered by time."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM actuals
                WHERE date(sampled_at) = ?
                ORDER BY sampled_at
                """,
                (for_date,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Daily summary ──────────────────────────────────────────────────────

    async def upsert_daily_summary(self, data: dict) -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO daily_summary
                    (summary_date, forecast_wh_raw, forecast_wh_calibrated,
                     actual_wh, ratio, correction_factor,
                     tod_morning_factor, tod_midday_factor, tod_afternoon_factor,
                     updated_at)
                VALUES
                    (:summary_date, :forecast_wh_raw, :forecast_wh_calibrated,
                     :actual_wh, :ratio, :correction_factor,
                     :tod_morning_factor, :tod_midday_factor, :tod_afternoon_factor,
                     CURRENT_TIMESTAMP)
                ON CONFLICT(summary_date) DO UPDATE SET
                    forecast_wh_raw         = excluded.forecast_wh_raw,
                    forecast_wh_calibrated  = excluded.forecast_wh_calibrated,
                    actual_wh               = excluded.actual_wh,
                    ratio                   = excluded.ratio,
                    correction_factor       = excluded.correction_factor,
                    tod_morning_factor      = excluded.tod_morning_factor,
                    tod_midday_factor       = excluded.tod_midday_factor,
                    tod_afternoon_factor    = excluded.tod_afternoon_factor,
                    updated_at              = CURRENT_TIMESTAMP
                """,
                {
                    "summary_date": data.get("summary_date"),
                    "forecast_wh_raw": data.get("forecast_wh_raw"),
                    "forecast_wh_calibrated": data.get("forecast_wh_calibrated"),
                    "actual_wh": data.get("actual_wh"),
                    "ratio": data.get("ratio"),
                    "correction_factor": data.get("correction_factor"),
                    "tod_morning_factor": data.get("tod_morning_factor"),
                    "tod_midday_factor": data.get("tod_midday_factor"),
                    "tod_afternoon_factor": data.get("tod_afternoon_factor"),
                },
            )
            await db.commit()

    async def get_recent_summaries(self, days: int) -> list[dict]:
        """Return up to `days` most recent daily_summary rows, newest first."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM daily_summary ORDER BY summary_date DESC LIMIT ?",
                (days,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Calibration state ──────────────────────────────────────────────────

    async def get_calibration_state(self) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM calibration_state WHERE id = 1")
            row = await cursor.fetchone()
            return dict(row) if row else {}

    async def update_calibration_state(self, data: dict) -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                UPDATE calibration_state SET
                    global_correction    = :global_correction,
                    tod_morning_factor   = :tod_morning_factor,
                    tod_midday_factor    = :tod_midday_factor,
                    tod_afternoon_factor = :tod_afternoon_factor,
                    days_of_data         = :days_of_data,
                    phase                = :phase,
                    last_calibrated_at   = CURRENT_TIMESTAMP,
                    updated_at           = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                {
                    "global_correction": data.get("global_correction", 1.0),
                    "tod_morning_factor": data.get("tod_morning_factor", 1.0),
                    "tod_midday_factor": data.get("tod_midday_factor", 1.0),
                    "tod_afternoon_factor": data.get("tod_afternoon_factor", 1.0),
                    "days_of_data": data.get("days_of_data", 0),
                    "phase": data.get("phase", "warmup"),
                },
            )
            await db.commit()
