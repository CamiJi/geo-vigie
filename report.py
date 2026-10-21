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
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from common import db, end_run, load_config, load_env, log_run, now


def gather(config: dict, conn, *, latest: int | None = None) -> dict:
    """Collect today's qualifying offers or the latest scored matches."""
    sc = config.get("scoring", {})
    min_score = int(sc.get("min_score", 45))
    top_n = int(sc.get("top_n", 15))

    today = date.today().isoformat()

    if latest is not None:
        active_sources = [
            source["name"] for source in config.get("sources", [])
            if source.get("enabled", True)
        ]
        if not active_sources:
            top = []
        else:
            placeholders = ",".join("?" for _ in active_sources)
            top = conn.execute(
                "SELECT * FROM offers WHERE keyword_hit = 1 AND score >= ? "
                f"AND source IN ({placeholders}) "
                "ORDER BY CASE WHEN posted_at != '' THEN posted_at "
                "ELSE first_seen END DESC, score DESC, id DESC LIMIT ?",
                (min_score, *active_sources, latest),
            ).fetchall()

        new_unscored = conn.execute(
            "SELECT COUNT(*) c FROM offers WHERE score IS NULL "
            "AND first_seen >= ?", (today,)).fetchone()["c"]
        return {"top": top, "min_score": min_score,
                "new_unscored": new_unscored, "today": today,
                "mode": "latest", "latest": latest}

    top = conn.execute(
        "SELECT * FROM offers "
        "WHERE score >= ? AND (reported_at IS NULL OR reported_at >= ?) "
        "ORDER BY score DESC, id DESC LIMIT ?",
        (min_score, today, top_n),
    ).fetchall()

    new_unscored = conn.execute(
        "SELECT COUNT(*) c FROM offers WHERE score IS NULL "
        "AND first_seen >= ?", (today,)).fetchone()["c"]

    return {"top": top, "min_score": min_score, "new_unscored": new_unscored,
            "today": today, "mode": "daily"}


def render(rows, config: dict, meta: dict, *, fmt: str) -> str:
    subject_cfg = config.get("report", {}).get(
        "subject", "[geo-vigie] {count} offre(s) — {date}")
    subject = subject_cfg.format(count=len(rows), date=meta["today"])
    if meta.get("mode") == "latest":
        subject = f"[geo-vigie] Extrait — {len(rows)} offres récentes ({meta['today']})"

    if not rows:
        if meta.get("mode") == "latest":
            body = ("Aucune offre récente ne dépasse le seuil d'adéquation "
                    f"de {meta['min_score']}/100.")
        else:
            body = ("Aucune nouvelle offre au-dessus du seuil aujourd'hui.\n"
                    f"Offres en attente de notation : {meta['new_unscored']}.")
        if fmt == "html":
            body = f"<p>{escape(body).replace(chr(10), '<br>')}</p>"
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
      <table style="border-collapse:collapse;width:100%">{''.join(tr)}</table>
      <p style="color:#9ca3af;font-size:12px">geo-vigie — relevé automatique.
      Vérifiez toujours l'offre d'origine avant de postuler.</p>
    </body></html>"""
    return subject, html


def send(env: dict, subject: str, text: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = env["MAIL_FROM"]
    to_addresses = [value.strip() for value in env["MAIL_TO"].split(",")
                    if value.strip()]
    cc_addresses = [value.strip() for value in env.get("MAIL_CC", "").split(",")
                    if value.strip()]
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
        end_run(conn, run_id, "preview")
        conn.close()
        return 0

    if (not rows and not force and latest is None
            and not config.get("report", {}).get("send_empty")):
        print("0 offre au-dessus du seuil — mail non envoyé "
              "(utiliser --force pour forcer).")
        end_run(conn, run_id, "vide, non envoyé")
        conn.close()
        return 0

    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO")
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise SystemExit(f"Configuration SMTP incomplète : {', '.join(missing)}")

    send(env, subject, text, html)

    ids = [r["id"] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE offers SET reported_at=? WHERE id IN ({placeholders})",
            (now(), *ids),
        )
        conn.commit()

    end_run(conn, run_id, f"envoyé, {len(rows)} offre(s)")
    conn.close()
    print(f"Mail envoyé — {len(rows)} offre(s)")
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
