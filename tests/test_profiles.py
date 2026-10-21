import unittest
from pathlib import Path
from unittest.mock import patch

import common
from common import default_profile_id, list_profiles, load_config


class ProfileConfigTests(unittest.TestCase):
    def test_list_profiles_finds_quentin(self):
        self.assertIn("quentin", list_profiles())

    def test_default_profile_is_unique_active(self):
        self.assertEqual(default_profile_id(), "quentin")

    def test_load_config_merges_profile_rules_with_shared_config(self):
        config = load_config()
        # Partagé (config.yaml)
        self.assertIn("sources", config)
        self.assertIn("general", config)
        # Profil (profiles/quentin/profile.yaml)
        self.assertEqual(config["profile_id"], "quentin")
        self.assertIn("keywords", config)
        self.assertEqual(config["scoring"]["min_score"], 55)
        self.assertIn("{count}", config["report"]["subject"])

    def test_cv_file_resolves_inside_profile_dir(self):
        cv = Path(load_config()["cv_file"])
        self.assertTrue(cv.is_absolute())
        self.assertEqual(cv.parent.name, "quentin")
        self.assertTrue(cv.exists())

    def test_explicit_profile_selects_it(self):
        self.assertEqual(load_config("quentin")["profile_id"], "quentin")

    def test_unknown_profile_fails_clearly(self):
        with self.assertRaises(SystemExit) as ctx:
            load_config("inconnu")
        self.assertIn("inconnu", str(ctx.exception))

    def test_multiple_active_profiles_require_choice(self):
        with patch.object(common, "list_profiles",
                          return_value=["quentin", "gabriela"]), \
                patch.object(common, "_load_profile_raw",
                             return_value={"active": True}):
            with self.assertRaises(SystemExit) as ctx:
                default_profile_id()
        self.assertIn("--profile", str(ctx.exception))

    def test_env_var_overrides_default_profile(self):
        with patch.dict("os.environ", {"VIGIE_PROFILE": "gabriela"}):
            self.assertEqual(default_profile_id(), "gabriela")


if __name__ == "__main__":
    unittest.main()
