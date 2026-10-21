import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from common import keyword_match, load_config
from collect import (
    _french_date,
    _relative_date,
    fetch_egu,
    fetch_france_travail,
    fetch_hellowork,
    fetch_lhotellerie,
    fetch_michaelpage,
    fetch_next_json,
)
from score import ask_llm, load_cv


class SourceParserTests(unittest.TestCase):
    def test_relative_french_dates_are_sortable(self):
        posted = datetime.fromisoformat(_relative_date("Publié il y a plus de 30 jours"))
        age = datetime.now(timezone.utc) - posted
        self.assertGreaterEqual(age, timedelta(days=30))

    def test_reed_next_data_maps_nested_job_fields(self):
        payload = {
            "props": {"pageProps": {"searchResults": {"jobs": [{
                "url": "/jobs/graduate-geologist/123",
                "jobDetail": {
                    "jobTitle": "Graduate Geologist",
                    "ouName": "Penguin Recruitment",
                    "displayLocationName": "Glasgow",
                    "jobDescriptionSnippet": "Entry-level geology role",
                    "dateCreated": "2026-10-05T10:00:00",
                },
            }]}}},
        }
        response = Mock(status_code=200, text=(
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script></html>"
        ))
        source = {
            "url": "https://www.reed.co.uk/jobs/geologist-jobs",
            "url_prefix": "https://www.reed.co.uk",
            "items_path": "props.pageProps.searchResults.jobs",
            "fields": {
                "title": "jobDetail.jobTitle",
                "company": "jobDetail.ouName",
                "location": "jobDetail.displayLocationName",
                "url": "url",
                "description": "jobDetail.jobDescriptionSnippet",
                "posted_at": "jobDetail.dateCreated",
            },
        }
        with patch("collect.requests.get", return_value=response):
            rows = fetch_next_json(source, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Graduate Geologist")
        self.assertEqual(rows[0]["company"], "Penguin Recruitment")
        self.assertEqual(rows[0]["location"], "Glasgow")
        self.assertEqual(
            rows[0]["url"],
            "https://www.reed.co.uk/jobs/graduate-geologist/123",
        )

    def test_egu_parser_extracts_listing_fields(self):
        response = Mock(status_code=200, text="""
            <article class="media"><div class="media-body">
              <h2><a href="/jobs/123/exploration-geologist/">Exploration Geologist</a></h2>
              <ul class="list-inline">
                <li class="list-inline-item"><i class="fas fa-university"></i>
                  <a href="https://example.test">Geo Co</a></li>
                <li class="list-inline-item"><i class="fas fa-globe"></i>
                  Cardiff, UK</li>
                <li class="list-inline-item"><i class="far fa-calendar-plus"></i>
                  <time datetime="2026-10-05T10:00:00+02:00">5 October</time></li>
              </ul>
              <p>Mineral exploration role</p>
              <p class="more-link">Read more</p>
            </div></article>
        """)
        with patch("collect.requests.get", return_value=response):
            rows = fetch_egu({"url": "https://www.egu.eu/jobs/search/"}, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Exploration Geologist")
        self.assertEqual(rows[0]["company"], "Geo Co")
        self.assertEqual(rows[0]["location"], "Cardiff, UK")
        self.assertEqual(rows[0]["posted_at"], "2026-10-05T10:00:00+02:00")
        self.assertEqual(rows[0]["description"], "Mineral exploration role")
        self.assertEqual(rows[0]["url"],
                         "https://www.egu.eu/jobs/123/exploration-geologist/")

    def test_hellowork_parser_extracts_french_offer_card(self):
        response = Mock(status_code=200, text="""
            <ul><li data-id-storage-target="item">
              <a data-cy="offerTitle" href="/fr-fr/emplois/123.html">
                <h3><p>Géologue junior H/F</p><p>Cabinet Géo</p></h3>
              </a>
              <div data-cy="localisationCard">Lyon - 69</div>
              <div data-cy="contractCard">CDI</div>
              <div>il y a 2 jours</div>
            </li></ul>
        """)
        with patch("collect.requests.get", return_value=response):
            rows = fetch_hellowork({
                "url": "https://www.hellowork.com/fr-fr/emploi/metier_geologue.html"
            }, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Géologue junior H/F")
        self.assertEqual(rows[0]["company"], "Cabinet Géo")
        self.assertEqual(rows[0]["location"], "Lyon - 69")
        self.assertIn("CDI", rows[0]["description"])
        self.assertTrue(rows[0]["posted_at"].startswith("20"))

    def test_france_travail_parser_preserves_hyphenated_company_name(self):
        response = Mock(status_code=200, text="""
            <ul><li class="result" data-id-offre="213TMFD">
              <a class="media with-fav" href="/offres/recherche/detail/213TMFD">
                <div class="media-body">
                  <h2><span class="media-heading-title">Technicien Géologue</span></h2>
                  <p class="subtext">ABO-ERG GEOTECHNIQUE - <span>13 - Vitrolles</span></p>
                  <p class="description">Études géologiques et géotechniques.</p>
                  <p class="date">Publié il y a 3 jours</p>
                </div>
              </a>
            </li></ul>
        """)
        with patch("collect.requests.get", return_value=response):
            rows = fetch_france_travail({
                "url": "https://candidat.francetravail.fr/offres/emploi/geologue/s19m4"
            }, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "ABO-ERG GEOTECHNIQUE")
        self.assertEqual(rows[0]["location"], "13 - Vitrolles")
        self.assertRegex(rows[0]["posted_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_keyword_filter_keeps_geologist_junior_roles_and_drops_noise(self):
        config = load_config("quentin")
        self.assertNotIn("junior", config["keywords"]["exclude"])
        self.assertTrue(keyword_match(
            config, "Graduate Geologist", "Junior entry-level role", "Glasgow"
        )[0])
        self.assertFalse(keyword_match(
            config, "HR Generalist - EMEA", "Mining company hiring", "Remote"
        )[0])
        self.assertFalse(keyword_match(
            config, "Business Development Representative", "commercial prospecting", "Paris"
        )[0])
        self.assertFalse(keyword_match(
            config, "Science-Dialog Manager", "communicating with geologists", "Germany"
        )[0])

    def test_cv_contact_details_are_redacted_before_scoring(self):
        cv = load_cv(load_config("quentin"))
        self.assertIn("BRGM", cv)
        self.assertIn("France", cv)
        self.assertNotIn("Contact", cv)
        self.assertNotIn("@", cv)
        self.assertNotRegex(cv, r"\b\d{5}\b")


class NewFetcherTests(unittest.TestCase):
    def test_lhotellerie_card_maps_title_location_date_and_skips_bare_links(self):
        response = Mock(status_code=200, text="""
        <div class="multiple-items-3 mb-4">
          <a id="1" href="/emploi/a4bb3f04/femme-valet-de-chambre/_NQQ3lj?UrlPart=x">
            <div class="card mx-2 p-3">
              <h3 class="text-primary fw-bold">Femme/Valet de chambre</h3>
              <p class="location mb-1"><i class="bi"></i>78 - Rambouillet</p>
              <p class="card-text text-truncate--two-lines mb-1">Best Western Amarys.</p>
              <p class="date mb-0">21/10/2026</p>
            </div>
          </a>
          <a class="sticky-job-link" href="/emploi/a4bb3f04/x/y?UrlPart=z">
            <img src="/logo.jpg"/>
          </a>
        </div>
        """)
        with patch("collect.requests.get", return_value=response):
            rows = fetch_lhotellerie({
                "url": "https://www.lhotellerie-restauration.fr/emploi/emploi-hotel-restaurant"
            }, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Femme/Valet de chambre")
        self.assertEqual(rows[0]["location"], "78 - Rambouillet")
        self.assertTrue(rows[0]["url"].endswith("/emploi/a4bb3f04/femme-valet-de-chambre/_NQQ3lj"))
        self.assertNotIn("UrlPart", rows[0]["url"])
        self.assertTrue(rows[0]["posted_at"].startswith("2026-10-21"))

    def test_michaelpage_card_carries_salary_and_remote_into_description(self):
        response = Mock(status_code=200, text="""
        <div class="job-tile search-job-tile">
          <div class="job-title"><h3><a href="/job-detail/directrice-hotel-fh/ref/jn-1">
            Directrice d'hôtel (H/F)</a></h3></div>
          <div class="job-properties">
            <div class="job-location"><i></i> Boulogne-Billancourt</div>
            <div class="job-contract-type"><i></i> CDI</div>
            <div class="job-salary"><i></i> 32 000€ - 36 000€ par an</div>
            <div class="job-nature"><i></i> Télétravail possible</div>
          </div>
          <div class="job-summary"><div><p>Pilotage de l'établissement.</p></div></div>
        </div>
        """)
        with patch("collect.requests.get", return_value=response):
            rows = fetch_michaelpage({"url": "https://www.michaelpage.fr/jobs"}, {})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["title"], "Directrice d'hôtel (H/F)")
        self.assertEqual(row["location"], "Boulogne-Billancourt")
        self.assertTrue(row["url"].endswith("/job-detail/directrice-hotel-fh/ref/jn-1"))
        self.assertIn("Pilotage de l'établissement", row["description"])
        self.assertIn("Salaire : 32 000€ - 36 000€ par an", row["description"])
        self.assertIn("Télétravail possible", row["description"])

    def test_french_absolute_dates_become_sortable_iso(self):
        self.assertTrue(_french_date("21/10/2026").startswith("2026-10-21T"))
        self.assertEqual(_french_date("non date"), "non date")

    def test_rss_max_items_keeps_most_recent(self):
        from collect import fetch_rss
        response = Mock(status_code=200, content=b"<rss/>")
        old = (2020, 1, 1, 0, 0, 0, 0, 1, 0)
        new = (2026, 10, 21, 0, 0, 0, 0, 294, 0)
        entries = [
            {"title": "vieille", "link": "http://a/1", "published_parsed": old},
            {"title": "récente", "link": "http://a/2", "published_parsed": new},
            {"title": "moyenne", "link": "http://a/3",
             "published_parsed": (2025, 5, 1, 0, 0, 0, 0, 121, 0)},
        ]
        with patch("collect.requests.get", return_value=response), \
                patch("collect.feedparser.parse",
                      return_value=Mock(entries=entries)):
            rows = fetch_rss({"url": "http://x/feed", "max_items": 2,
                              "fields": {"title": "title", "url": "link"}}, {})
        self.assertEqual([r["title"] for r in rows], ["récente", "moyenne"])


class LlmRetryTests(unittest.TestCase):
    def test_retries_rate_limit_then_returns_json(self):
        throttled = Mock(status_code=429, headers={"Retry-After": "0"})
        success = Mock(
            status_code=200,
            headers={},
        )
        success.json.return_value = {"choices": [{"message": {
            "content": '{"score": 72, "reason": "Bon profil.", "verdict": "pertinent"}'
        }}]}
        env = {
            "LLM_BASE_URL": "https://openrouter.ai/api/v1",
            "LLM_API_KEY": "test-key",
        }
        with patch("score.requests.post", side_effect=[throttled, success]), \
                patch("score.time.sleep") as sleep:
            result = ask_llm("prompt", env, "test/model", 0)
        self.assertEqual(result["score"], 72)
        sleep.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
