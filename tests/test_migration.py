import sqlite3
import unittest

from common import LEGACY_OFFER_COLUMNS, SCHEMA, migrate_schema

LEGACY_SCHEMA = """
CREATE TABLE offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT UNIQUE NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    company      TEXT DEFAULT '',
    location     TEXT DEFAULT '',
    url          TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    posted_at    TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    keyword_hit  INTEGER DEFAULT 0,
    score        INTEGER,
    score_reason TEXT,
    score_model  TEXT,
    scored_at    TEXT,
    reported_at  TEXT
);
"""


def _legacy_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO offers (fingerprint, source, title, first_seen, score, "
        "score_reason, score_model, scored_at, reported_at) VALUES "
        "('fp1', 'reed', 'Géologue confirmé', '2026-10-01', 80, 'Bon match', "
        "'m', '2026-10-02T10:00:00+00:00', '2026-10-03T10:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO offers (fingerprint, source, title, first_seen) "
        "VALUES ('fp2', 'egu', 'Jamais notée', '2026-10-04')"
    )
    return conn


class MigrationTests(unittest.TestCase):
    def test_migration_moves_legacy_data_to_profile(self):
        conn = _legacy_conn()
        self.assertTrue(migrate_schema(conn, default_profile="quentin"))

        scores = conn.execute("SELECT * FROM scores").fetchall()
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["profile_id"], "quentin")
        self.assertEqual(scores[0]["score"], 80)

        reports = conn.execute("SELECT * FROM reports").fetchall()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["profile_id"], "quentin")
        self.assertEqual(reports[0]["sent_at"], "2026-10-03T10:00:00+00:00")

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(offers)")}
        for legacy in LEGACY_OFFER_COLUMNS:
            self.assertNotIn(legacy, cols)
        conn.close()

    def test_migration_is_idempotent(self):
        conn = _legacy_conn()
        self.assertTrue(migrate_schema(conn, default_profile="quentin"))
        self.assertFalse(migrate_schema(conn, default_profile="quentin"))
        conn.close()

    def test_new_schema_needs_no_migration(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        self.assertFalse(migrate_schema(conn, default_profile="quentin"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
