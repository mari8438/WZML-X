import asyncio
import aiosqlite
import json
import logging
import os
import sys
from typing import Optional, List, Dict, Any

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import DB_PATH
else:
    from .config import DB_PATH

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s")
LOGGER = logging.getLogger("StarfallDB")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS anime_seasons (
    season_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_romaji TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    total_episodes INTEGER NOT NULL,
    nyaa_info_hash TEXT,
    nyaa_release_group TEXT,
    reserved_bytes INTEGER DEFAULT 0,
    season_state TEXT DEFAULT 'PENDING' CHECK(season_state IN ('PENDING', 'HARVESTING', 'DOWNLOADING', 'PROCESSING', 'MERGING', 'COMPLETED', 'FAILED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tamil_audio_library (
    audio_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    episode_number INTEGER NOT NULL,
    source_msg_id INTEGER NOT NULL,
    source_file_unique TEXT UNIQUE,
    local_m4a_path TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    audio_sha256 TEXT,
    file_size_bytes INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS episode_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    episode_number INTEGER NOT NULL,
    audio_id INTEGER REFERENCES tamil_audio_library(audio_id),
    bd_relative_path TEXT,
    sync_mode TEXT CHECK(sync_mode IN ('CONSTANT', 'EYECATCH_STEP', 'PAL_DRIFT', 'CENSOR_CUTS', 'EPISODE_SHIFT', 'FAILED')),
    calculated_delay_ms INTEGER,
    speed_ratio REAL DEFAULT 1.0,
    min_psr_score REAL,
    anchor_telemetry JSON,
    job_state TEXT DEFAULT 'QUEUED' CHECK(job_state IN ('QUEUED', 'SYNCING', 'MUXING', 'UPLOADING', 'COMPLETED', 'QUARANTINE')),
    tg_single_msg_id INTEGER,
    uploaded_by_bot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS season_merge_outputs (
    merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    part_index INTEGER DEFAULT 1,
    episode_range TEXT NOT NULL,
    file_size_bytes INTEGER,
    tg_season_msg_id INTEGER,
    upload_state TEXT DEFAULT 'PENDING' CHECK(upload_state IN ('PENDING', 'MERGING', 'UPLOADING', 'COMPLETED', 'FAILED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tg_client_pool (
    client_username TEXT PRIMARY KEY,
    client_role TEXT CHECK(client_role IN ('HELPER_BOT', 'USER_PREMIUM')),
    cooldown_until_ts REAL DEFAULT 0,
    uploads_last_min INTEGER DEFAULT 0,
    total_bytes_sent INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audio_ep ON tamil_audio_library(season_id, episode_number);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON episode_jobs(season_id, episode_number);
"""

async def init_db() -> None:
    """Initialize SQLite database with WAL mode and tables."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        async with db.execute("PRAGMA journal_mode;") as cursor:
            row = await cursor.fetchone()
            journal_mode = row[0] if row else "unknown"
        await db.commit()
    LOGGER.info(f"Initialized Starfall State DB at {DB_PATH} (journal_mode={journal_mode.upper()})")

def get_db_connection() -> aiosqlite.Connection:
    """Return aiosqlite connection context manager."""
    return aiosqlite.connect(DB_PATH)

async def get_or_create_season(
    title_romaji: str,
    season_number: int,
    total_episodes: int = 12,
    season_state: str = "HARVESTING",
) -> int:
    """Find existing anime season or create a new entry."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute(
            "SELECT season_id FROM anime_seasons WHERE title_romaji = ? AND season_number = ?",
            (title_romaji, season_number),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row["season_id"]

        cursor = await db.execute(
            """
            INSERT INTO anime_seasons (title_romaji, season_number, total_episodes, season_state)
            VALUES (?, ?, ?, ?)
            RETURNING season_id;
            """,
            (title_romaji, season_number, total_episodes, season_state),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0] if row else 0

async def register_audio_entry(
    season_id: Optional[int],
    episode_number: int,
    source_msg_id: int,
    source_file_unique: str,
    local_m4a_path: str,
    duration_ms: int,
    audio_sha256: str,
    file_size_bytes: int,
) -> int:
    """Insert or update a Tamil audio library record."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute(
            """
            INSERT INTO tamil_audio_library (
                season_id, episode_number, source_msg_id, source_file_unique,
                local_m4a_path, duration_ms, audio_sha256, file_size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_file_unique) DO UPDATE SET
                season_id = COALESCE(excluded.season_id, tamil_audio_library.season_id),
                local_m4a_path = excluded.local_m4a_path,
                duration_ms = excluded.duration_ms,
                file_size_bytes = excluded.file_size_bytes,
                audio_sha256 = excluded.audio_sha256
            RETURNING audio_id;
            """,
            (
                season_id,
                episode_number,
                source_msg_id,
                source_file_unique,
                local_m4a_path,
                duration_ms,
                audio_sha256,
                file_size_bytes,
            ),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0] if row else 0

async def get_all_audio_entries(limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve indexed audio records."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT a.*, s.title_romaji, s.season_number
            FROM tamil_audio_library a
            LEFT JOIN anime_seasons s ON a.season_id = s.season_id
            ORDER BY a.episode_number ASC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_database_summary() -> Dict[str, Any]:
    """Retrieve summary of all pipeline tables."""
    summary = {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for tbl in ["anime_seasons", "tamil_audio_library", "episode_jobs", "season_merge_outputs", "tg_client_pool"]:
            async with db.execute(f"SELECT COUNT(*) FROM {tbl}") as cursor:
                row = await cursor.fetchone()
                summary[tbl] = row[0] if row else 0
    return summary

if __name__ == "__main__":
    async def _main():
        print("=== Initializing Starfall SQLite WAL Database ===")
        await init_db()
        summary = await get_database_summary()
        print(f"Database located at: {DB_PATH}")
        print("Table Record Counts:")
        for tbl, count in summary.items():
            print(f"  • {tbl}: {count} records")
        print("=== Database Ready in WAL Mode ===")

    asyncio.run(_main())
