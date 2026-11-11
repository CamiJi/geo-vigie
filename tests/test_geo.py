"""Filtre géo dur (geo_filter), exclusions de titre et plancher parameterisé.

Recadrage produit du 2026-10-21 (Camille) : hors Île-de-France = éliminé
AVANT notation LLM ; aucun poste hiérarchiquement moindre que directeur/
directrice adjointe d'hôtel ; salaire absent = neutre (mention en fin).
"""

import unittest
from unittest.mock import patch

from collect import _match_profiles
from common import (geo_ok, geo_penalty, haversine, keyword_match)
from score import _floor_rule

GEO = {"geo_filter": {
    "pattern": r"(?i)\b(?:75|77|78|9[1-5])\b|paris|île-de-france"}}


class GeoOkTests(unittest.TestCase):
    def test_no_geo_filter_passes_everything(self):
        self.assertTrue(geo_ok("Lyon", {}))
        self.assertTrue(geo_ok("Lyon", {"geo_filter": {}}))

    def test_empty_or_ambiguous_location_passes(self):
        self.assertTrue(geo_ok("", GEO))
        self.assertTrue(geo_ok("   ", GEO))

    def test_idf_locations_pass(self):
        for loc in ("75 - PARIS 01", "92 - BOULOGNE-BILLANCOURT",
                    "78 - VERSAILLES", "95 - PONTOISE", "Île-de-France",
                    "77 - MARNE-LA-VALLÉE", "91 - MASSY", "94 - CRÉTEIL"):
            self.assertTrue(geo_ok(loc, GEO), loc)

    def test_out_of_zone_locations_fail(self):
        for loc in ("69 - LYON", "Nice (06)", "La Seyne-sur-Mer (83)",
                    "Londres", "Marseille 13008", "Pérols 34"):
            self.assertFalse(geo_ok(loc, GEO), loc)


class ExcludeTitleTests(unittest.TestCase):
    CONF = {"keywords": {
        "include_weak": ["directeur", "responsable"],
        "exclude_title": ["responsable de salle", "commis",
                           "maitre d hotel"],
    }}

    def test_lower_title_excluded(self):
        for title in ("Responsable de salle H/F", "Commis de cuisine",
                      "Maître d'hôtel (H/F)", "RESPONSABLE DE SALLE"):
            self.assertEqual(keyword_match(self.CONF, title, "", ""),
                             (False, ""), title)

    def test_mention_in_description_keeps_the_offer(self):
        kept, term = keyword_match(
            self.CONF, "Directeur d'hôtel - H/F",
            "Vous encadrez l'équipe : responsable de salle, commis…", "")
        self.assertTrue(kept)
        self.assertEqual(term, "directeur")

    def test_plain_exclude_still_matches_everywhere(self):
        conf = {"keywords": {"include_weak": ["directeur"],
                             "exclude": ["stage"]}}
        self.assertEqual(
            keyword_match(conf, "Directeur d'hôtel", "poste en stage", ""),
            (False, ""))


class MatchProfilesGeoTests(unittest.TestCase):
    PROFILES = {
        "gabriela": {**GEO,
                     "keywords": {"include_strong": ["hôtel", "hotel"]}},
        "quentin": {"keywords": {"include_strong": ["géolog"]}},
    }

    def test_out_of_zone_matched_for_nothing_and_counted(self):
        matched, geo_out = _match_profiles(
            self.PROFILES, "Hôtel de Lyon", "offre", "69 - LYON")
        self.assertEqual(matched, {})
        self.assertEqual(geo_out, 1)

    def test_in_zone_offer_kept_for_gabriela(self):
        matched, geo_out = _match_profiles(
            self.PROFILES, "Hôtel Paris 8e", "offre", "75 - PARIS")
        self.assertEqual(matched, {"gabriela": "hôtel"})
        self.assertEqual(geo_out, 0)

    def test_profile_without_geo_filter_unaffected(self):
        matched, geo_out = _match_profiles(
            self.PROFILES, "Géologue - Hôtel", "terrain", "Lyon")
        self.assertEqual(matched, {"quentin": "géolog"})
        self.assertEqual(geo_out, 1)


SOFT = {"geo_filter": {
    "soft": True,
    "pattern": r"(?i)\b(?:75|77|78|9[1-5]|28|27|60)\b|paris|île-de-france",
    "penalty": 25}}


class SoftGeoPenaltyTests(unittest.TestCase):
    """Recadrage 2026-11-10 : hors périmètre = malus, plus d'élimination."""

    def test_soft_filter_never_eliminates(self):
        for loc in ("69 - LYON", "64 - BAYONNE", "34 - MONTPELLIER", ""):
            self.assertTrue(geo_ok(loc, SOFT), loc)

    def test_no_penalty_inside_perimeter(self):
        for loc in ("75 - PARIS", "78 - VERSAILLES", "28 - DREUX",
                    "27 - ÉVREUX", "60 - BEAUVAIS", "Île-de-France"):
            self.assertEqual(geo_penalty(loc, SOFT), 0, loc)

    def test_penalty_outside_perimeter(self):
        for loc in ("69 - LYON", "64 - BAYONNE", "34 - MONTPELLIER"):
            self.assertEqual(geo_penalty(loc, SOFT), 25, loc)

    def test_empty_location_gets_no_penalty(self):
        self.assertEqual(geo_penalty("", SOFT), 0)

    def test_hard_filter_never_applies_penalty(self):
        self.assertEqual(geo_penalty("69 - LYON", GEO), 0)
        self.assertFalse(geo_ok("69 - LYON", GEO))

    def test_gabriela_profile_is_soft(self):
        from common import load_config
        conf = load_config("gabriela")
        self.assertTrue(conf["geo_filter"]["soft"])
        import common
        common._GEO_CACHE.clear()
        common._BASE_COORDS_CACHE.clear()
        # Au-delà de max_km : coupé (pas de malus, l'offre ne sera pas notée).
        # Le mock geocode renvoie None → repli motif → BAYONNE coupé.
        with patch("common.geocode", return_value=None):
            self.assertFalse(geo_ok("64 - BAYONNE", conf))
            self.assertEqual(geo_penalty("64 - BAYONNE", conf), 0)
        self.assertEqual(geo_penalty("78 - VERSAILLES", conf), 0)


class GeoCodingTests(unittest.TestCase):
    """Géocodage BAN + coupure dure à max_km (Gabriela, 2026-11-11).

    Quentin, lui, n'a pas de geo_filter : ces tests ne le concernent pas.
    """

    def setUp(self):
        import common
        common._GEO_CACHE.clear()
        common._BASE_COORDS_CACHE.clear()

    CONF = {"geo_filter": {"soft": True, "max_km": 45, "pattern": "x",
                           "penalty": 25},
            "location": {"base": "Fontenay-le-Fleury (78)"}}

    def test_haversine_paris_fontenay(self):
        # ~25 km à vol d'oiseau
        d = haversine(48.8566, 2.3522, 48.8109, 2.0416)
        self.assertGreater(d, 20)
        self.assertLess(d, 30)

    def test_max_km_cuts_beyond_perimeter(self):
        with patch("common.geocode", side_effect=lambda loc: {
            "Fontenay-le-Fleury (78)": (48.8109, 2.0416),
            "Versailles": (48.8014, 2.1301),
            "Chartres": (48.4439, 1.4890),
        }.get(loc.split(" - ")[-1].strip() if " - " in loc else loc) or
              ((48.8109, 2.0416) if "Fontenay" in loc else None)):
            # Versailles ~6 km → passe
            self.assertTrue(geo_ok("78 - Versailles", self.CONF))
            # Chartres ~57 km → coupé
            self.assertFalse(geo_ok("28 - Chartres", self.CONF))

    def test_remote_exception_keeps_far_offer(self):
        with patch("common.geocode", side_effect=lambda loc: (48.8109, 2.0416)
                   if "Fontenay" in loc else (48.4439, 1.4890)):
            self.assertTrue(geo_ok("Chartres — télétravail", self.CONF))

    def test_no_coords_falls_back_to_pattern(self):
        with patch("common.geocode", return_value=None):
            # motif présent → passe
            self.assertTrue(geo_ok("75 - Paris", {"geo_filter":
                {"soft": True, "max_km": 45,
                 "pattern": r"(?i)\b75\b|paris", "penalty": 25}}))
            # motif absent → coupé
            self.assertFalse(geo_ok("69 - Lyon", {"geo_filter":
                {"soft": True, "max_km": 45,
                 "pattern": r"(?i)\b75\b|paris", "penalty": 25}}))

    def test_graded_penalty_within_perimeter(self):
        with patch("common.geocode", side_effect=lambda loc: {
            "Fontenay-le-Fleury (78)": (48.8109, 2.0416),
            "Versailles": (48.8014, 2.1301),   # ~6 km
            "Haute-Isle": (49.12, 1.75),       # ~41 km
        }.get(loc.split(" - ")[-1].strip() if " - " in loc else loc) or
              ((48.8109, 2.0416) if "Fontenay" in loc else None)):
            self.assertEqual(geo_penalty("78 - Versailles", self.CONF), 0)
            self.assertEqual(geo_penalty("95 - Haute-Isle", self.CONF), 10)

    def test_gabriela_profile_has_max_km(self):
        from common import load_config
        conf = load_config("gabriela")
        self.assertEqual(conf["geo_filter"]["max_km"], 45)
        self.assertTrue(conf["geo_filter"]["soft"])

class FloorRuleTests(unittest.TestCase):
    def test_head_is_the_historical_behaviour(self):
        rule = _floor_rule({"scoring": {"min_salary_net_month": 2000}})
        self.assertIn("fais commencer `reason` par « Salaire non "
                      "mentionné : »", rule)

    def test_tail_is_neutral_and_ends_the_reason(self):
        rule = _floor_rule({"scoring": {"min_salary_net_month": 2200,
                                        "salary_signal": "tail"}})
        self.assertIn("NE pénalise PAS le score", rule)
        self.assertIn("termine `reason` par la mention neutre « (salaire "
                      "non mentionné) »", rule)
        self.assertNotIn("commencer `reason`", rule)

    def test_no_floor_no_rule(self):
        self.assertEqual(_floor_rule({"scoring": {}}), "")


if __name__ == "__main__":
    unittest.main()
