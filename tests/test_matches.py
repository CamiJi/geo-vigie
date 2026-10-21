import sqlite3
import unittest

from collect import _match_profiles
from common import SCHEMA, backfill_matches

HOTEL = {"keywords": {
    "include_strong": ["hôteli", "hotel", "revenue management"],
    "include_weak": ["directeur d'établissement"],
    "exclude": ["stage", "alternance"],
}}
GEO = {"keywords": {
    "include_strong": ["géolog"],
    "include_weak": ["mine"],
    "exclude": ["stage"],
}}


class MultiProfileMatchTests(unittest.TestCase):
    def test_offer_matches_only_the_profiles_whose_keywords_hit(self):
        profiles = {"gabriela": HOTEL, "quentin": GEO}
        hotel = _match_profiles(profiles, "Directrice adjointe d'hôtel",
                                "Gestion opérationnelle d'un établissement",
                                "Paris 14")
        self.assertEqual(set(hotel), {"gabriela"})
        self.assertTrue(hotel["gabriela"])

        geo = _match_profiles(profiles, "Géologue confirmé H/F",
                              "Prospection et cartographie", "Lyon")
        self.assertEqual(set(geo), {"quentin"})

    def test_offer_matching_no_profile_is_dropped(self):
        profiles = {"gabriela": HOTEL, "quentin": GEO}
        self.assertEqual(
            _match_profiles(profiles, "Commercial B2B", "Prospection client",
                            "Paris"),
            {},
        )

    def test_exclude_term_wins_even_with_include_hit(self):
        profiles = {"gabriela": HOTEL}
        self.assertEqual(
            _match_profiles(profiles, "Stage en hôtellerie", "", "Paris"),
            {},
        )

    def test_one_offer_survives_even_if_only_one_profile_matches(self):
        """Une offre hors-quentin reste en base : c'est le but de Gabriela."""
        profiles = {"gabriela": HOTEL, "quentin": GEO}
        matched = _match_profiles(
            profiles, "Revenue Manager H/F", "Optimisation tarifaire hôtelière",
            "Boulogne")
        self.assertEqual(list(matched), ["gabriela"])


class BackfillTests(unittest.TestCase):
    def test_existing_offers_are_attached_once_to_default_profile(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO offers (fingerprint, source, title, first_seen) "
            "VALUES ('fp1', 'reed', 'Géologue', '2026-10-01')"
        )
        self.assertTrue(backfill_matches(conn, default_profile="quentin"))
        rows = conn.execute("SELECT * FROM matches").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["profile_id"], "quentin")
        # Idempotent : jamais deux fois.
        self.assertFalse(backfill_matches(conn, default_profile="quentin"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM matches")
                         .fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
