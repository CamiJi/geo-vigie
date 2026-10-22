"""Prompt de scoring par profil (étape 5 — persona, geo, plancher salarial)."""

import unittest

from common import load_config
from score import build_prompt

KW = dict(cv="<CV>", title="Offre test", company="Société", location="Paris",
          source="test", description="Description.")


class QuentinDefaultPromptTests(unittest.TestCase):
    """Le profil de référence n'a ni persona ni plancher : prompt = l'ancien."""

    @classmethod
    def setUpClass(cls):
        cls.prompt = build_prompt(load_config("quentin"), **KW)

    def test_uses_default_geoscience_persona_and_weights(self):
        self.assertIn("recruteur spécialisé en géosciences", self.prompt)
        self.assertIn("- 45 % adéquation des compétences métier", self.prompt)
        self.assertIn("- 0 si l'offre n'a rien à voir avec les géosciences.",
                      self.prompt)

    def test_has_no_geo_block_nor_salary_floor(self):
        self.assertNotIn("Localisation : base", self.prompt)
        self.assertNotIn("Plancher salarial", self.prompt)
        self.assertNotIn("Salaire non mentionné", self.prompt)


class GabrielaPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompt = build_prompt(load_config("gabriela"), **KW)

    def test_persona_location_and_zone_injected(self):
        self.assertIn("hôtellerie-restauration", self.prompt)
        self.assertIn("sondage du marché", self.prompt)
        self.assertIn("Localisation : base Fontenay-le-Fleury (78)", self.prompt)
        self.assertIn("zones visées : Île-de-France", self.prompt)
        self.assertIn("Télétravail : accepté", self.prompt)
        self.assertIn("Relocation : non", self.prompt)

    def test_salary_floor_rule_generated_from_config(self):
        self.assertIn("Plancher salarial : 2200 € net/mois", self.prompt)
        self.assertIn("plafonne le score à 30", self.prompt)
        # salary_signal: tail (recadrage 21/10) — l'absence de salaire n'est
        # pas un signal : mention neutre en fin de justification.
        self.assertIn("NE pénalise PAS le score", self.prompt)
        self.assertIn("« (salaire non mentionné) »", self.prompt)

    def test_profile_own_weights_and_rules_replace_defaults(self):
        self.assertIn("- 40 % adéquation des compétences métier", self.prompt)
        self.assertIn("Ouverture au changement de secteur", self.prompt)
        self.assertNotIn("géosciences", self.prompt)
        self.assertNotIn("- 45 %", self.prompt)

    def test_prompt_payload_still_contains_offer_fields(self):
        for marker in ("<CV>", "Offre test", "Société", "Description.",
                       '"score"'):
            self.assertIn(marker, self.prompt)


class FloorRuleTests(unittest.TestCase):
    def test_no_floor_config_no_rule(self):
        config = {"scoring": {"min_score": 55}}
        prompt = build_prompt(config, **KW)
        self.assertNotIn("Plancher salarial", prompt)

    def test_floor_without_persona_keeps_default_persona(self):
        config = {"scoring": {"min_salary_net_month": 2200}}
        prompt = build_prompt(config, **KW)
        self.assertIn("géosciences", prompt)
        self.assertIn("Plancher salarial : 2200 € net/mois", prompt)


if __name__ == "__main__":
    unittest.main()
