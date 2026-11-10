#!/usr/bin/env python3
"""geo-vigie — construit et envoie le rapport du jour par email (SMTP).

Usage :
    python report.py            # envoie le rapport du jour
    python report.py --preview  # affiche sans envoyer
    python report.py --force    # envoie même si 0 offre
"""

from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from common import (
    db,
    end_run,
    geo_ok,
    load_config,
    load_env,
    log_run,
    now,
    profile_of,
)


def recipients(config: dict, env: dict) -> tuple[list[str], list[str]]:
    """Destinataires du mail : `mailer.to` / `mailer.cc` du profil d'abord,
    sinon les variables globales MAIL_TO / MAIL_CC du .env."""
    mailer = config.get("mailer") or {}
    raw_to = mailer.get("to") or env.get("MAIL_TO", "")
    raw_cc = mailer.get("cc") or env.get("MAIL_CC", "")
    to = [value.strip() for value in str(raw_to).split(",") if value.strip()]
    cc = [value.strip() for value in str(raw_cc).split(",") if value.strip()]
    return to, cc


def barometer(conn, config: dict) -> dict:
    """Volumes d'offres et santé des scores — posture « sondage du marché ».

    - offres matchant le profil par jour (`first_seen`, entrée en base) sur
      les 14 derniers jours, agrégées en 7 j vs 7 j précédents ;
    - scores des 7 derniers jours (moyenne, nb au-dessus du seuil) et cumul.

    Comparaisons lexicographiques sur les dates ISO — sûres pour `YYYY-MM-DD`.
    """
    profile = profile_of(config)
    min_score = int(config.get("scoring", {}).get("min_score", 45))
    today = date.today().isoformat()
    d7 = (date.today() - timedelta(days=7)).isoformat()
    d14 = (date.today() - timedelta(days=14)).isoformat()

    # Hors périmètre geo = pas un relevé pertinent du sondage : le filtre
    # dur empêche la notation, il doit aussi tenir les volumes du baromètre.
    counts: dict[str, int] = {}
    for r in conn.execute(
            "SELECT date(o.first_seen) AS d, o.location "
            "FROM offers o "
            "JOIN matches m ON m.offer_id = o.id AND m.profile_id = ? "
            "WHERE o.first_seen >= ?",
            (profile, d14)).fetchall():
        if not geo_ok(r["location"] or "", config):
            continue
        counts[r["d"]] = counts.get(r["d"], 0) + 1
    per_day = sorted(counts.items())
    last7 = sum(c for d, c in per_day if d7 <= d <= today)
    prev7 = sum(c for d, c in per_day if d < d7)
    recent_days = [(d, c) for d, c in per_day if d7 <= d <= today]

    if prev7 == 0:
        pct = None
        trend = ("démarrage" if last7 else "aucun relevé")
    else:
        pct = round((last7 - prev7) / prev7 * 100)
        trend = ("en hausse" if pct > 10
                 else "en baisse" if pct < -10 else "stable")

    def _stats(where: str, params: tuple) -> dict:
        row = conn.execute(
            "SELECT COUNT(*) n, AVG(score) avg, "
            "SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) ok "
            f"FROM scores {where}",
            (min_score, *params),
        ).fetchone()
        return {"n": row["n"], "avg": row["avg"], "ok": row["ok"] or 0}

    week = _stats("WHERE profile_id = ? AND scored_at >= ?",
                  (profile, d7))
    total = _stats("WHERE profile_id = ?", (profile,))
    tracked = sum(
        1 for r in conn.execute(
            "SELECT o.location FROM matches m "
            "JOIN offers o ON o.id = m.offer_id WHERE m.profile_id = ?",
            (profile,)).fetchall()
        if geo_ok(r["location"] or "", config))

    return {
        "last7": last7, "prev7": prev7, "pct": pct, "trend": trend,
        "days": recent_days, "min_score": min_score,
        "week": week, "total": total, "tracked": tracked,
    }


def gather(config: dict, conn, *, latest: int | None = None) -> dict:
    """Collect today's qualifying offers or the latest scored matches."""
    sc = config.get("scoring", {})
    min_score = int(sc.get("min_score", 45))
    top_n = int(sc.get("top_n", 15))
    profile = profile_of(config)

    today = date.today().isoformat()

    def _unscored() -> int:
        """Offres du jour en attente — hors périmètre geo non comptées.

        (Des offres hors zone restent « non notées » à vie : les annoncer
        comme en attente serait trompeur.)
        """
        rows = conn.execute(
            "SELECT o.location FROM offers o "
            "JOIN matches m ON m.offer_id = o.id AND m.profile_id = ? "
            "LEFT JOIN scores s ON s.offer_id = o.id AND s.profile_id = ? "
            "WHERE s.id IS NULL AND o.first_seen >= ?",
            (profile, profile, today),
        ).fetchall()
        return sum(1 for r in rows if geo_ok(r["location"] or "", config))

    if latest is not None:
        active_sources = [
            source["name"] for source in config.get("sources", [])
            if source.get("enabled", True)
        ]
        if not active_sources:
            top = []
        else:
            placeholders = ",".join("?" for _ in active_sources)
            # +100 de marge : le filtre géo s'applique après coup, ne pas
            # gaspiller les places du top sur des offres hors périmètre.
            top = [r for r in conn.execute(
                "SELECT o.*, s.score, s.score_reason "
                "FROM offers o "
                "JOIN scores s ON s.offer_id = o.id AND s.profile_id = ? "
                "WHERE o.keyword_hit = 1 AND s.score >= ? "
                f"AND o.source IN ({placeholders}) "
                "ORDER BY CASE WHEN o.posted_at != '' THEN o.posted_at "
                "ELSE o.first_seen END DESC, s.score DESC, o.id DESC LIMIT ?",
                (profile, min_score, *active_sources, latest + 100),
            ).fetchall() if geo_ok(r["location"] or "", config)][:latest]

        new_unscored = _unscored()
        return {"top": top, "min_score": min_score,
                "new_unscored": new_unscored, "today": today,
                "mode": "latest", "latest": latest, "profile": profile,
                "baro": barometer(conn, config)}

    # Une offre déjà signalée au profil ne revient JAMAIS, même le jour
    # même : la clause historique `OR r.sent_at >= today` renvoyait les
    # offres du relevé précédent lors d'un second envoi dans la journée
    # (doublons du 2026-11-10). Pour rejouer un relevé, `--force`.
    top = [r for r in conn.execute(
        "SELECT o.*, s.score, s.score_reason "
        "FROM offers o "
        "JOIN scores s ON s.offer_id = o.id AND s.profile_id = ? "
        "LEFT JOIN reports r ON r.offer_id = o.id AND r.profile_id = ? "
        "WHERE s.score >= ? AND r.sent_at IS NULL "
        "ORDER BY s.score DESC, o.id DESC LIMIT ?",
        (profile, profile, min_score, top_n + 100),
    ).fetchall() if geo_ok(r["location"] or "", config)][:top_n]

    new_unscored = _unscored()

    return {"top": top, "min_score": min_score, "new_unscored": new_unscored,
            "today": today, "mode": "daily", "profile": profile,
            "baro": barometer(conn, config)}


def _baro_facts(meta: dict) -> dict | None:
    """Valeurs formatées du baromètre (None si absent du meta)."""
    b = meta.get("baro")
    if not b:
        return None
    if b["pct"] is None:
        evo = f"semaine précédente : {b['prev7']} → {b['trend']}"
    else:
        evo = (f"semaine précédente : {b['prev7']} → {b['trend']} "
               f"({b['pct']:+d} %)")
    days = " · ".join(f"{d[5:]}: {c}" for d, c in b["days"]) or "aucun jour"
    week_avg = ("—" if b["week"]["avg"] is None
                else f"{b['week']['avg']:.0f}")
    total_avg = ("—" if b["total"]["avg"] is None
                 else f"{b['total']['avg']:.0f}")
    return {"b": b, "evo": evo, "days": days,
            "week_avg": week_avg, "total_avg": total_avg}


def _baro_text(meta: dict) -> list[str]:
    """Bloc baromètre en texte (vide si pas de meta baro)."""
    f = _baro_facts(meta)
    if not f:
        return []
    b = f["b"]
    return [
        "📊 Baromètre — sondage du marché (7 jours)",
        f"  Offres pertinentes relevées : {b['last7']} ({f['evo']})",
        f"  Par jour (MM-JJ) : {f['days']}",
        f"  Notation 7 j : {b['week']['n']} notée(s), moyenne "
        f"{f['week_avg']}/100, {b['week']['ok']} ≥ seuil {b['min_score']}",
        f"  Cumul : {b['tracked']} offre(s) suivie(s), moyenne "
        f"{f['total_avg']}/100, {b['total']['ok']} au-dessus du seuil",
        "",
    ]


def _baro_html(meta: dict) -> str:
    """Bloc baromètre en HTML (chaîne vide si absent)."""
    f = _baro_facts(meta)
    if not f:
        return ""
    b = f["b"]
    return f"""
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;
                  padding:10px 14px;margin:12px 0;font-size:14px;color:#374151">
        <strong>📊 Baromètre — sondage du marché (7 jours)</strong><br>
        Offres pertinentes relevées : <strong>{b['last7']}</strong>
        ({escape(f['evo'])})<br>
        Par jour (MM-JJ) : {escape(f['days'])}<br>
        Notation 7 j : {b['week']['n']} notée(s) · moyenne
        {f['week_avg']}/100 · {b['week']['ok']} ≥ seuil {b['min_score']}<br>
        Cumul : {b['tracked']} offre(s) suivie(s) · moyenne
        {f['total_avg']}/100 · {b['total']['ok']} au-dessus du seuil
      </div>"""


def render(rows, config: dict, meta: dict, *, fmt: str) -> str:
    subject_cfg = config.get("report", {}).get(
        "subject", "[geo-vigie] {count} offre(s) — {date}")
    subject = subject_cfg.format(count=len(rows), date=meta["today"])
    if meta.get("mode") == "latest":
        subject = f"[geo-vigie] Extrait — {len(rows)} offres récentes ({meta['today']})"

    if not rows:
        # Relevé vide : objet dédié optionnel (`report.subject_empty`), pour
        # que le mail informatif ne ressemble pas à un rapport à 0 offre.
        empty_cfg = config.get("report", {}).get("subject_empty")
        if empty_cfg and meta.get("mode") != "latest":
            subject = empty_cfg.format(count=0, date=meta["today"])
        if meta.get("mode") == "latest":
            body = ("Aucune offre récente ne dépasse le seuil d'adéquation "
                    f"de {meta['min_score']}/100.")
        else:
            body = ("Aucune nouvelle offre au-dessus du seuil "
                    f"({meta['min_score']}/100) sur ce relevé.\n"
                    f"Offres en attente de notation : {meta['new_unscored']}.")
        if fmt == "html":
            body = f"<p>{escape(body).replace(chr(10), '<br>')}</p>"
            return subject, body + _baro_html(meta)
        baro = _baro_text(meta)
        if baro:
            body = f"{body}\n\n" + "\n".join(baro)
        return subject, body

    if fmt == "text":
        lines = [subject, "=" * len(subject), ""]
        if meta.get("mode") == "latest":
            lines.append(
                "Extrait des offres les plus récemment publiées parmi les "
                f"sources suivies, avec un score d'au moins {meta['min_score']}/100. "
                "Score = adéquation au CV, pas probabilité d'embauche."
            )
            lines.append("")
        lines += _baro_text(meta)
        for r in rows:
            lines += [
                f"[{r['score']}/100] {r['title']}",
                f"  {r['company'] or '—'} | {r['location'] or '—'} "
                f"| source : {r['source']}",
                f"  {r['url']}",
                f"  → {r['score_reason'] or '(pas de justification)'}",
                "",
            ]
        if meta.get("mode") == "latest":
            lines.append("Tri : date de publication, puis score d'adéquation.")
        else:
            lines.append(f"Seuil d'affichage : {meta['min_score']}/100")
        lines.append(f"Offres en attente de notation : {meta['new_unscored']}")
        return subject, "\n".join(lines)

    # HTML
    tr = []
    for r in rows:
        tr.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb;
                    font-size:20px;font-weight:700;
                    color:{'#15803d' if r['score'] >= 70 else '#b45309'};
                    white-space:nowrap">{r['score']}</td>
          <td style="padding:8px;border-bottom:1px solid #e5e7eb">
            <a href="{escape(r['url'], quote=True)}"
               style="color:#1d4ed8;text-decoration:none;font-weight:600">
              {escape(r['title'])}</a><br>
            <span style="color:#6b7280;font-size:13px">
              {escape(r['company'] or '—')} ·
              {escape(r['location'] or '—')} ·
              {escape(r['source'])}</span>
            <div style="color:#374151;font-size:14px;margin-top:4px">
              {escape(r['score_reason'] or '')}</div>
          </td>
        </tr>""")

    intro = (
        "Extrait des offres les plus récemment publiées parmi les sources suivies. "
        f"Score minimal : {meta['min_score']}/100. Le score mesure l'adéquation "
        "au CV, pas la probabilité d'embauche."
        if meta.get("mode") == "latest"
        else f"Seuil {meta['min_score']}/100 · "
             f"{meta['new_unscored']} offre(s) en attente de notation"
    )
    html = f"""<!doctype html><html><body style="font-family:system-ui,
      -apple-system,Segoe UI,Roboto,sans-serif;color:#111827">
      <h2 style="margin-bottom:4px">🔎 {escape(subject)}</h2>
      <p style="color:#6b7280;margin-top:0">{escape(intro)}</p>
      {_baro_html(meta)}
      <table style="border-collapse:collapse;width:100%">{''.join(tr)}</table>
      <p style="color:#9ca3af;font-size:12px">geo-vigie — relevé automatique.
      Vérifiez toujours l'offre d'origine avant de postuler.</p>
    </body></html>"""
    return subject, html


def send(env: dict, subject: str, text: str, html: str,
         config: dict | None = None, *, cc_only: bool = False) -> None:
    to_addresses, cc_addresses = recipients(config or {}, env)
    if cc_only:
        # Relevé vide : le mail informatif ne part qu'aux adresses en copie
        # (le propriétaire du robot), jamais au candidat.
        to_addresses, cc_addresses = cc_addresses, []
    if not to_addresses:
        raise SystemExit("Aucun destinataire : `mailer.to` (profile.yaml) "
                         "ou MAIL_TO (.env) manquant.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["MAIL_FROM"]
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    host, port = env["SMTP_HOST"], int(env.get("SMTP_PORT", 587))
    security = env.get("SMTP_SECURITY", "starttls").lower()

    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as srv:
            srv.login(env["SMTP_USER"], env["SMTP_PASS"])
            srv.send_message(msg, to_addrs=to_addresses + cc_addresses)
    else:
        with smtplib.SMTP(host, port, timeout=60) as srv:
            srv.ehlo()
            if security == "starttls":
                srv.starttls(context=context)
                srv.ehlo()
            srv.login(env["SMTP_USER"], env["SMTP_PASS"])
            srv.send_message(msg, to_addrs=to_addresses + cc_addresses)


def report(config: dict, *, preview: bool = False, force: bool = False,
           latest: int | None = None) -> int:
    env = load_env()
    conn = db()
    run_id = log_run(conn, "report")

    meta = gather(config, conn, latest=latest)
    rows = meta["top"]
    subject, text = render(rows, config, meta, fmt="text")
    _, html = render(rows, config, meta, fmt="html")

    if preview:
        print(text)
        end_run(conn, run_id, f"profile={meta['profile']} preview")
        conn.close()
        return 0

    send_empty = config.get("report", {}).get("send_empty")
    if not rows and not force and latest is None and not send_empty:
        print("0 offre au-dessus du seuil — mail non envoyé "
              "(utiliser --force pour forcer).")
        end_run(conn, run_id, f"profile={meta['profile']} vide, non envoyé")
        conn.close()
        return 0

    # `send_empty: "cc"` = relevé vide envoyé aux seules adresses en copie
    # (test/monitoring) ; `true` = à tout le monde ; `false` = silence.
    cc_only = (not rows) and str(send_empty).lower() in ("cc", "cc-only")
    if cc_only and not recipients(config, env)[1]:
        print("0 offre — mail informatif non envoyé : aucune adresse en "
              "copie (mailer.cc / MAIL_CC).")
        end_run(conn, run_id, f"profile={meta['profile']} vide, pas de cc")
        conn.close()
        return 0

    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM")
    missing = [k for k in required if not env.get(k)]
    if not recipients(config, env)[0]:
        missing.append("MAIL_TO (ou `mailer.to` du profil)")
    if missing:
        raise SystemExit(f"Configuration SMTP incomplète : {', '.join(missing)}")

    send(env, subject, text, html, config, cc_only=cc_only)

    ids = [r["id"] for r in rows]
    if ids:
        sent_at = now()
        conn.executemany(
            "INSERT INTO reports (offer_id, profile_id, sent_at) "
            "VALUES (?,?,?) "
            "ON CONFLICT(offer_id, profile_id) DO UPDATE SET "
            "sent_at=excluded.sent_at",
            [(offer_id, meta["profile"], sent_at) for offer_id in ids],
        )
        conn.commit()

    if cc_only:
        detail = f"profile={meta['profile']} informatif vide, copie seule"
        message = "Mail informatif envoyé — 0 offre (copie seule)"
    else:
        detail = f"profile={meta['profile']} envoyé, {len(rows)} offre(s)"
        message = f"Mail envoyé — {len(rows)} offre(s)"
    end_run(conn, run_id, detail)
    conn.close()
    print(message)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true",
                        help="affiche le rapport sans envoyer")
    parser.add_argument("--force", action="store_true",
                        help="envoie même sans offre")
    parser.add_argument("--latest", type=int, default=None,
                        help="envoie les N offres les plus récentes, sans seuil de score")
    parser.add_argument("--profile", default=None,
                        help="profil à utiliser (défaut : profil unique actif)")
    args = parser.parse_args()
    return report(load_config(args.profile), preview=args.preview, force=args.force,
                  latest=args.latest)


if __name__ == "__main__":
    sys.exit(main())
