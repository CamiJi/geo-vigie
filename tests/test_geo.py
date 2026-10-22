"""Filtre géo dur (geo_filter), exclusions de titre et plancher parameterisé.

Recadrage produit du 2026-10-21 (Camille) : hors Île-de-France = éliminé
AVANT notation LLM ; aucun poste hiérarchiquement moindre que directeur/
directrice adjointe d'hôtel ; salaire absent = neutre (mention en fin).
"""

import unittest

from collect import _match_profiles
from common import geo_ok, keyword_match
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
