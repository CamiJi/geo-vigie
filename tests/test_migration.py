import os
import sqlite3
import unittest
from unittest.mock import patch

import common
from common import (LEGACY_OFFER_COLUMNS, SCHEMA, _migration_profile,
                    migrate_schema)

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


class MigrationProfileTests(unittest.TestCase):
    """Rattachement de l'historique mono-profil (incident du 2026-11-10).

    Deux profils actifs : l'ancien code retombait sur `default`, rendant
    les scores et rapports de Quentin invisibles → 7 offres déjà envoyées
    lui ont été renvoyées. On retrouve désormais le profil par le
    destinataire historique (MAIL_TO / mailer.to).
    """

    PROFILES = {
        "gabriela": {"mailer": {"to": "gabrielascavo@gmail.com"}},
        "quentin": {},          # repli sur MAIL_TO du .env
    }

    def _resolve(self, env=None, profiles=None, forced=None):
        profiles = profiles or self.PROFILES
        args = {"MAIL_TO": "quentin.charbonnier71@gmail.com"}
        args.update(env or {})

        def _raw(pid):
            return profiles.get(pid, {})

        with patch.object(common, "list_profiles", return_value=sorted(profiles)), \
             patch.object(common, "_load_profile_raw", side_effect=_raw), \
             patch.object(common, "load_env", return_value=args), \
             patch.object(common, "default_profile_id",
                          side_effect=SystemExit("deux profils actifs")), \
             patch.dict(os.environ, ({"VIGIE_LEGACY_PROFILE": forced}
                                     if forced else {}), clear=False):
            if not forced:
                os.environ.pop("VIGIE_LEGACY_PROFILE", None)
            return _migration_profile()

    def test_legacy_recipient_is_selected(self):
        self.assertEqual(self._resolve(), "quentin")

    def test_explicit_override_wins(self):
        self.assertEqual(self._resolve(forced="gabriela"), "gabriela")

    def test_two_profiles_on_global_mail_to_warns(self):
        # Deux profils sans `mailer` retombent tous deux sur MAIL_TO :
        # impossible de trancher → 'default' + avertissement explicite.
        profiles = {"a": {}, "b": {}}
        with patch("builtins.print") as fake_print:
            self.assertEqual(self._resolve(profiles=profiles), "default")
        self.assertIn("default", fake_print.call_args.args[0])

    def test_ambiguous_match_falls_back(self):
        profiles = {"a": {"mailer": {"to": "same@x.y"}},
                    "b": {"mailer": {"to": "same@x.y"}}}
        with patch("builtins.print"):
            self.assertEqual(self._resolve(env={"MAIL_TO": "same@x.y"},
                                           profiles=profiles), "default")


if __name__ == "__main__":
    unittest.main()
