import sqlite3
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from report import barometer, gather, recipients, render, send


def _make_conn() -> sqlite3.Connection:
    """Schéma multiprofil en mémoire : offers (sans score) + scores."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY, source TEXT, keyword_hit INTEGER,
            posted_at TEXT, first_seen TEXT, location TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE scores (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            score_reason TEXT DEFAULT '',
            scored_at TEXT,
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.execute("""
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.execute("""
        CREATE TABLE matches (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            term TEXT DEFAULT '',
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.executemany(
        "INSERT INTO offers (id, source, keyword_hit, posted_at, first_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, "reed", 1, "2026-10-01", "2026-10-01"),
            (2, "egu", 1, "2026-10-05", "2026-10-05"),
            (3, "inactive-old-news", 1, "2026-10-06", "2026-10-06"),
            (4, "egu", 0, "2026-10-07", "2026-10-07"),
            (5, "egu", 1, "2026-10-04", "2026-10-04"),
            (6, "reed", 1, "2026-10-03", "2026-10-03"),
        ],
    )
    conn.executemany(
        "INSERT INTO scores (offer_id, profile_id, score) VALUES (?, 'quentin', ?)",
        [(1, 15), (2, 90), (3, 100), (4, 100), (5, 40), (6, 60)],
    )
    return conn


CONFIG = {
    "profile_id": "quentin",
    "scoring": {"min_score": 45, "top_n": 15},
    "sources": [
        {"name": "reed", "enabled": True},
        {"name": "egu", "enabled": True},
        {"name": "inactive-old-news", "enabled": False},
    ],
}


class RecentReportTests(unittest.TestCase):
    def test_latest_mode_uses_active_sources_and_posted_date(self):
        conn = _make_conn()
        result = gather(CONFIG, conn, latest=2)
        self.assertEqual([row["id"] for row in result["top"]], [2, 6])
        self.assertEqual(result["mode"], "latest")
        conn.close()

    def test_daily_mode_is_profile_scoped_and_skips_already_sent(self):
        conn = _make_conn()
        # Offres au-dessus du seuil (45) pour quentin : 2 (90), 3 (100),
        # 4 (100), 6 (60). Comportement historique conservé : un envoi du JOUR
        # reste inclus dans le relevé ; seul un envois antérieur exclut.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (2, 'quentin', '2026-10-01T00:00:00+00:00')")
        result = gather(CONFIG, conn)
        self.assertEqual(result["profile"], "quentin")
        self.assertEqual([row["id"] for row in result["top"]], [4, 3, 6])
        # Un envoi fait pour un AUTRE profil ne masque rien pour quentin.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (6, 'gabriela', '2026-10-01T00:00:00+00:00')")
        result = gather(CONFIG, conn)
        self.assertEqual([row["id"] for row in result["top"]], [4, 3, 6])
        # Envoi aujourd'hui pour quentin : l'offre reste dans le relevé du jour.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (6, 'quentin', ?)",
                     (gather(CONFIG, conn)["today"],))
        self.assertIn(6, [row["id"] for row in gather(CONFIG, conn)["top"]])
        conn.close()

    def test_geo_filter_drops_out_of_zone_offers(self):
        conn = _make_conn()
        conn.execute("UPDATE offers SET location = 'Lyon 69' WHERE id = 2")
        conn.execute("UPDATE offers SET location = '75 - PARIS' WHERE id = 6")
        config = {**CONFIG, "geo_filter": {
            "pattern": r"(?i)\b(?:75|77|78|9[1-5])\b|paris"}}
        # L'offre 2 (score 90) est hors zone : écartée des deux modes.
        result = gather(config, conn, latest=2)
        self.assertEqual([row["id"] for row in result["top"]], [6])
        daily = gather(config, conn)
        self.assertEqual([row["id"] for row in daily["top"]], [4, 3, 6])
        conn.close()

    def test_smtp_sends_to_recipient_and_cc(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_USER": "smtp-login",
            "SMTP_PASS": "smtp-secret",
            "MAIL_FROM": "sender@example.test",
            "MAIL_TO": "quentin@example.test",
            "MAIL_CC": "camille@example.test",
        }
        with patch("report.smtplib.SMTP", return_value=smtp):
            send(env, "Subject", "Text", "<p>HTML</p>")
        kwargs = smtp.send_message.call_args.kwargs
        self.assertEqual(kwargs["to_addrs"], [
            "quentin@example.test", "camille@example.test"
        ])
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "quentin@example.test")
        self.assertEqual(msg["Cc"], "camille@example.test")


class RecipientsTests(unittest.TestCase):
    ENV = {"MAIL_TO": "env-to@example.test", "MAIL_CC": "env-cc@example.test"}

    def test_profile_mailer_wins_over_env(self):
        config = {"mailer": {"to": "gabrielascavo@gmail.com",
                             "cc": "aubertcam@gmail.com"}}
        self.assertEqual(recipients(config, self.ENV),
                         (["gabrielascavo@gmail.com"], ["aubertcam@gmail.com"]))

    def test_env_is_the_fallback(self):
        self.assertEqual(recipients({}, self.ENV),
                         (["env-to@example.test"], ["env-cc@example.test"]))

    def test_send_honours_profile_mailer(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        env = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587",
               "SMTP_SECURITY": "starttls", "SMTP_USER": "u", "SMTP_PASS": "p",
               "MAIL_FROM": "sender@example.test",
               "MAIL_TO": "env-to@example.test",
               "MAIL_CC": "env-cc@example.test"}
        config = {"mailer": {"to": "gabrielascavo@gmail.com",
                             "cc": "aubertcam@gmail.com"}}
        with patch("report.smtplib.SMTP", return_value=smtp):
            send(env, "Subject", "Text", "<p>HTML</p>", config)
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "gabrielascavo@gmail.com")
        self.assertEqual(msg["Cc"], "aubertcam@gmail.com")
        self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], [
            "gabrielascavo@gmail.com", "aubertcam@gmail.com"
        ])


def _baro_conn() -> sqlite3.Connection:
    """Offres/scores datés RELATIVEMENT à aujourd'hui pour le baromètre."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in (
        "CREATE TABLE offers (id INTEGER PRIMARY KEY, source TEXT,"
        " keyword_hit INTEGER, posted_at TEXT, first_seen TEXT,"
        " location TEXT DEFAULT '')",
        "CREATE TABLE matches (id INTEGER PRIMARY KEY, offer_id INTEGER,"
        " profile_id TEXT, term TEXT DEFAULT '', UNIQUE (offer_id, profile_id))",
        "CREATE TABLE scores (id INTEGER PRIMARY KEY, offer_id INTEGER,"
        " profile_id TEXT, score INTEGER, score_reason TEXT DEFAULT '',"
        " scored_at TEXT, UNIQUE (offer_id, profile_id))",
    ):
        conn.execute(ddl)
    day = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731
    # Semaine actuelle : 3 offres (first_seen = hier) ; précédente : 2 (J-10)
    conn.executemany(
        "INSERT INTO offers (id, source, first_seen) VALUES (?, 'x', ?)",
        [(1, day(1)), (2, day(1)), (3, day(1)), (4, day(10)), (5, day(10))],
    )
    conn.executemany(
        "INSERT INTO matches (offer_id, profile_id) VALUES (?, 'quentin')",
        [(1,), (2,), (3,), (4,), (5,)],
    )
    # Notations : 2 récentes (60, 80) + 1 vieille (40)
    conn.executemany(
        "INSERT INTO scores (offer_id, profile_id, score, scored_at) "
        "VALUES (?, 'quentin', ?, ?)",
        [(1, 60, day(1)), (2, 80, day(1)), (4, 40, day(10))],
    )
    return conn


class BarometerTests(unittest.TestCase):
    def test_last7_vs_prev7_trend_and_score_stats(self):
        conn = _baro_conn()
        b = barometer(conn, CONFIG)
        self.assertEqual(b["last7"], 3)
        self.assertEqual(b["prev7"], 2)
        self.assertEqual(b["pct"], 50)
        self.assertEqual(b["trend"], "en hausse")
        recent_day = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(b["days"], [(recent_day, 3)])
        # Notation 7 j : 60 et 80 (moyenne 70) ; 40 est hors semaine.
        self.assertEqual(b["week"]["n"], 2)
        self.assertEqual(b["week"]["avg"], 70)
        self.assertEqual(b["week"]["ok"], 2)
        # Cumul : 3 scores (60, 80, 40), moyenne 60, 2 ≥ 45.
        self.assertEqual(b["total"]["n"], 3)
        self.assertEqual(b["total"]["avg"], 60)
        self.assertEqual(b["total"]["ok"], 2)
        self.assertEqual(b["tracked"], 5)
        conn.close()

    def test_geo_filter_keeps_out_of_zone_out_of_the_barometer(self):
        conn = _baro_conn()
        conn.execute("UPDATE offers SET location = '69 - LYON' WHERE id = 2")
        config = {**CONFIG, "geo_filter": {
            "pattern": r"(?i)\b(?:75|77|78|9[1-5])\b|paris"}}
        b = barometer(conn, config)
        # L'offre 2 (J-1) est hors zone : sortie des volumes et du suivi.
        self.assertEqual(b["last7"], 2)
        self.assertEqual(b["prev7"], 2)
        self.assertEqual(b["tracked"], 4)
        conn.close()

    def test_first_days_without_history_report_demarrage(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for ddl in (
            "CREATE TABLE offers (id INTEGER PRIMARY KEY, source TEXT,"
            " keyword_hit INTEGER, posted_at TEXT, first_seen TEXT,"
            " location TEXT DEFAULT '')",
            "CREATE TABLE matches (id INTEGER PRIMARY KEY, offer_id INTEGER,"
            " profile_id TEXT, term TEXT DEFAULT '', UNIQUE (offer_id, profile_id))",
            "CREATE TABLE scores (id INTEGER PRIMARY KEY, offer_id INTEGER,"
            " profile_id TEXT, score INTEGER, score_reason TEXT DEFAULT '',"
            " scored_at TEXT, UNIQUE (offer_id, profile_id))",
        ):
            conn.execute(ddl)
        b = barometer(conn, CONFIG)
        self.assertEqual((b["last7"], b["prev7"], b["pct"]), (0, 0, None))
        self.assertEqual(b["trend"], "aucun relevé")
        conn.close()


class BarometerRenderTests(unittest.TestCase):
    META = {"min_score": 45, "new_unscored": 0, "today": "2026-10-21",
            "mode": "daily", "profile": "quentin",
            "baro": {"last7": 3, "prev7": 2, "pct": 50, "trend": "en hausse",
                     "days": [("2026-10-20", 3)], "min_score": 45,
                     "week": {"n": 2, "avg": 70.0, "ok": 2},
                     "total": {"n": 3, "avg": 60.0, "ok": 2},
                     "tracked": 5}}
    ROW = {"id": 1, "score": 80, "title": "Directrice d'hôtel",
           "company": "Hôtel Test", "location": "Paris", "source": "mp",
           "url": "https://example.test/1", "score_reason": "Très pertinent."}

    def test_barometer_appears_in_text_and_html_with_rows(self):
        _, text = render([self.ROW], CONFIG, self.META, fmt="text")
        self.assertIn("Baromètre", text)
        self.assertIn("en hausse (+50 %)", text)
        _, html = render([self.ROW], CONFIG, self.META, fmt="html")
        self.assertIn("Baromètre", html)
        self.assertIn("+50 %", html)

    def test_empty_report_still_carries_barometer(self):
        _, text = render([], CONFIG, self.META, fmt="text")
        self.assertIn("Aucune nouvelle offre", text)
        self.assertIn("Baromètre", text)
        _, html = render([], CONFIG, self.META, fmt="html")
        self.assertIn("Baromètre", html)

    def test_gather_attaches_barometer_meta(self):
        conn = _baro_conn()
        meta = barometer(conn, CONFIG)
        self.assertEqual(meta["last7"], 3)
        conn.close()


if __name__ == "__main__":
    unittest.main()
