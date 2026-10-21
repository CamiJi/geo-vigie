#!/usr/bin/env python3
"""geo-vigie — bench « coût par tâche » : compare des modèles sur la vraie tâche.

La tâche de référence : scorer une offre (/100 + reason + verdict) à partir du
CV, avec le PROMPT strict de score.py. Pour chaque appel, on mesure :
  - coût exact ($ remonté par OpenRouter, paramètre `usage.include`),
  - latence, tokens in/out,
  - validité du JSON et score produit.

Usage :
    .venv/bin/python bench_cost.py                                # 3 modèles de référence
    .venv/bin/python bench_cost.py qwen/qwen3.7-flash             # ajoute un modèle
    .venv/bin/python bench_cost.py --models a/b,c/d --limit 5

Sortie : tableau résumé + détail complet dans db/bench-cost-<horodatage>.txt
Aucune écriture en base (les offres ne sont pas rescorées).
"""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime
from pathlib import Path

import requests

from common import ROOT, db, load_config, load_env
from score import PROMPT, load_cv, parse_json

DEFAULT_MODELS = [
    "google/gemini-3.7-flash",
    "xiaomi/mimo-v2.6-flash",
    "qwen/qwen3.7-flash",
]


def select_offers(config: dict, limit: int | None) -> list:
    """Même sélection que score.py (sources actives, tri chronologique)."""
    conn = db()
    active = [s["name"] for s in config.get("sources", []) if s.get("enabled", True)]
    if not active:
        raise SystemExit("Aucune source active.")
    placeholders = ",".join("?" for _ in active)
    sql = (
        f"SELECT * FROM offers WHERE source IN ({placeholders}) "
        "ORDER BY CASE WHEN posted_at != '' THEN posted_at ELSE first_seen END DESC, id DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, active).fetchall()
    conn.close()
    return rows


def ask(model: str, prompt: str, env: dict, temperature: int) -> dict:
    """Appel OpenRouter avec coût exact + backoff minimal (429/5xx)."""
    url = f"{env['LLM_BASE_URL'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {env['LLM_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "usage": {"include": True},  # coût exact dans usage.cost
    }
    last_exc: Exception | None = None
    for attempt in range(4):
        t0 = time.perf_counter()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
            continue
        latency = time.perf_counter() - t0
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        resp.raise_for_status()
        break
    else:
        raise RuntimeError(f"échec après retries : {last_exc}")

    body = resp.json()
    usage = body.get("usage", {})
    content = body["choices"][0]["message"]["content"]
    data = parse_json(content)  # ValueError si JSON invalide
    return {
        "latency": latency,
        "cost": float(usage.get("cost") or 0.0),
        "tokens_in": int(usage.get("prompt_tokens") or 0),
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "score": data["score"],
        "verdict": data.get("verdict", ""),
        "reason": data.get("reason", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    config = load_config()
    env = load_env()
    if not env.get("LLM_API_KEY"):
        raise SystemExit("LLM_API_KEY manquante (.env).")
    cv = load_cv(config)
    temperature = int(config.get("scoring", {}).get("temperature", 0))
    rows = select_offers(config, args.limit)
    if not rows:
        raise SystemExit("Aucune offre sélectionnée.")

    prompts = {
        row["id"]: PROMPT.format(
            cv=cv,
            title=row["title"],
            company=row["company"] or "—",
            location=row["location"] or "—",
            source=row["source"],
            description=row["description"] or "(aucune description fournie)",
        )
        for row in rows
    }
    titles = {row["id"]: row["title"] for row in rows}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = ROOT / "db" / f"bench-cost-{stamp}.txt"
    results: dict[str, dict[int, dict]] = {}
    lines = [f"Bench coût/tâche — {stamp} — offres : {len(rows)} — tâche : scoring geo-vigie"]

    for model in models:
        print(f"\n▶ {model} ({len(rows)} tâches)…")
        results[model] = {}
        for row in rows:
            try:
                res = ask(model, prompts[row["id"]], env, temperature)
                results[model][row["id"]] = res
                print(f"   {res['score']:>3}  {res['latency']:5.1f}s  "
                      f"${res['cost']:.5f}  {titles[row['id']][:50]!r}")
            except Exception as exc:  # noqa: BLE001 — banc : on enregistre l'échec
                results[model][row["id"]] = {"error": str(exc)}
                print(f"   KO   {titles[row['id']][:50]!r} → {exc}")
            lines.append(f"[{model}] offer={row['id']} "
                         + " ".join(f"{k}={v}" for k, v in results[model][row["id"]].items())
                         + f" title={titles[row['id']]!r}")

    # ── Résumé ──
    print("\n" + "=" * 78)
    print(f"{'MODÈLE':<30} {'OK':>4} {'$/TÂCHE':>10} {'LAT moy':>9} {'TOK in':>8} {'TOK out':>8}")
    summary = []
    for model in models:
        ok = [r for r in results[model].values() if "error" not in r]
        n = len(ok)
        cost = sum(r["cost"] for r in ok) / n if n else 0
        lat = statistics.mean(r["latency"] for r in ok) if n else 0
        tin = statistics.mean(r["tokens_in"] for r in ok) if n else 0
        tout = statistics.mean(r["tokens_out"] for r in ok) if n else 0
        summary.append((model, n, cost, lat, tin, tout))
        print(f"{model:<30} {n:>4} {cost:>10.5f} {lat:>8.1f}s {tin:>8.0f} {tout:>8.0f}")
        lines.append(f"SUMMARY {model} ok={n} cost_per_task={cost:.6f} "
                     f"latency_avg={lat:.1f} tokens_in_avg={tin:.0f} tokens_out_avg={tout:.0f}")

    # Scores côte à côte (même offre, tous modèles) + référence Gemini du backup.
    print(f"\n{'OFFRE':<6} " + " ".join(f"{m.split('/')[1][:14]:>15}" for m in models))
    for row in rows:
        cells = []
        for model in models:
            r = results[model].get(row["id"], {})
            cells.append(f"{r.get('score', 'KO'):>15}")
        print(f"{row['id']:<6} " + " ".join(cells))
        lines.append(f"SCORES offer={row['id']} "
                     + " ".join(f"{m}={results[m].get(row['id'], {}).get('score', 'KO')}" for m in models))

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nDétail écrit : {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
