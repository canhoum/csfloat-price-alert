from pathlib import Path
import zipfile, json

base = Path("/mnt/data/csfloat_github_actions")
base.mkdir(exist_ok=True)
(base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)

script = r'''import json
import os
import statistics
import time
from pathlib import Path
from urllib.parse import quote

import requests

CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state.json")
BASE_URL = "https://csfloat.com/api/v1/history/{}/sales"


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def extract_sales(payload):
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = next(
            (payload[k] for k in ("sales", "data", "results")
             if isinstance(payload.get(k), list)),
            []
        )
    else:
        raw = []

    sales = []
    for s in raw:
        if not isinstance(s, dict):
            continue

        price = s.get("price", s.get("sale_price", s.get("amount")))
        if price is None:
            continue

        try:
            price_eur = float(price) / 100.0
        except (TypeError, ValueError):
            continue

        sale_id = (
            s.get("id") or s.get("sale_id") or s.get("contract_id")
            or s.get("listing_id")
            or f"{s.get('created_at','')}|{price}"
        )

        sales.append({
            "id": str(sale_id),
            "price_eur": price_eur,
            "created_at": s.get("created_at") or s.get("sold_at")
        })

    return sales


def fetch_sales(name):
    url = BASE_URL.format(quote(name, safe=""))
    response = requests.get(
        url,
        headers={"User-Agent": "CSFloatPriceAlert-GitHubActions/1.0"},
        timeout=30
    )
    response.raise_for_status()
    return extract_sales(response.json())


def send_discord(webhook, name, sale, baseline, multiplier):
    content = (
        "**CSFloat PRICE SPIKE**\n"
        f"Item: `{name}`\n"
        f"Sale: **€{sale['price_eur']:.2f}**\n"
        f"Median baseline: €{baseline:.2f}\n"
        f"Multiple: **{multiplier:.2f}×**\n"
        f"CSFloat: https://csfloat.com/search?market_hash_name={quote(name)}"
    )
    response = requests.post(
        webhook,
        json={"content": content},
        timeout=30
    )
    response.raise_for_status()


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK não está configurado no GitHub Secrets.")

    config = load_json(CONFIG_FILE, {
        "default_rule": {
            "absolute_price_eur": 0.80,
            "multiple_of_median": 2.0,
            "baseline_sales": 100
        },
        "items": []
    })

    state_exists = STATE_FILE.exists()
    state = load_json(STATE_FILE, {"seen": {}})

    for item in config.get("items", []):
        if not item.get("enabled", True):
            continue

        name = item["market_hash_name"]
        rule = {**config.get("default_rule", {}), **item}

        try:
            sales = fetch_sales(name)
        except Exception as exc:
            print(f"[ERROR] {name}: {exc}")
            continue

        if not sales:
            print(f"[OK] {name}: API não devolveu vendas.")
            continue

        # On the first run, establish a baseline without sending old alerts.
        if not state_exists or name not in state["seen"]:
            state["seen"][name] = [s["id"] for s in sales][:500]
            print(f"[INIT] {name}: {len(sales)} vendas registadas sem alertas.")
            continue

        seen = set(state["seen"][name])
        new_sales = [s for s in reversed(sales) if s["id"] not in seen]

        for sale in new_sales:
            # Use other recent sales as the baseline, excluding this sale.
            comparison = [
                s["price_eur"] for s in sales
                if s["id"] != sale["id"]
            ][:int(rule.get("baseline_sales", 100))]

            if not comparison:
                continue

            baseline = statistics.median(comparison)
            multiplier = sale["price_eur"] / baseline if baseline else 0

            absolute_hit = sale["price_eur"] >= float(
                rule.get("absolute_price_eur", 0.80)
            )
            multiple_hit = multiplier >= float(
                rule.get("multiple_of_median", 2.0)
            )

            if absolute_hit or multiple_hit:
                try:
                    send_discord(
                        webhook, name, sale, baseline, multiplier
                    )
                    print(
                        f"[ALERT] {name}: €{sale['price_eur']:.2f} "
                        f"({multiplier:.2f}x)"
                    )
                except Exception as exc:
                    print(f"[DISCORD ERROR] {exc}")

        state["seen"][name] = list(
            dict.fromkeys(
                [s["id"] for s in sales] + state["seen"][name]
            )
        )[:500]

        print(
            f"[OK] {name}: {len(sales)} vendas, "
            f"{len(new_sales)} novas."
        )

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
'''

config = {
    "default_rule": {
        "absolute_price_eur": 0.80,
        "multiple_of_median": 2.0,
        "baseline_sales": 100
    },
    "items": [
        {
            "market_hash_name": "Fracture Case",
            "absolute_price_eur": 0.80,
            "multiple_of_median": 2.0,
            "enabled": True
        }
    ]
}

workflow = r'''name: CSFloat Price Alerts

on:
  schedule:
    # Every 5 minutes, at minute 2, 7, 12, 17, ...
    - cron: "2-59/5 * * * *"
  workflow_dispatch:

concurrency:
  group: csfloat-price-alert
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  monitor:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Download previous state
        uses: actions/download-artifact@v4
        with:
          name: csfloat-state
          path: .
        continue-on-error: true

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: python -m pip install -r requirements.txt

      - name: Check CSFloat
        env:
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
        run: python csfloat_alert.py

      - name: Save state
        uses: actions/upload-artifact@v4
        with:
          name: csfloat-state
          path: state.json
          overwrite: true
          retention-days: 90
'''

readme = r'''# CSFloat Price Alerts — GitHub Actions

Esta versão corre automaticamente no GitHub Actions, sem precisar de deixar o PC ligado.

## Ficheiros

- `csfloat_alert.py` — monitor
- `config.json` — skins e regras
- `requirements.txt` — dependência Python
- `.github/workflows/csfloat.yml` — execução automática

## Configuração

O webhook NÃO deve ser colocado no `config.json`.

No GitHub, cria um Repository Secret chamado:

`DISCORD_WEBHOOK`

e cola lá o URL do webhook.

## Fracture Case

A configuração incluída é:

- alerta se uma venda >= €0,80
- OU se uma venda >= 2x a mediana das 100 vendas anteriores
- verificação a cada 5 minutos

## Adicionar skins

No `config.json`, acrescenta objetos dentro de `items`.

Exemplo:

{
  "market_hash_name": "Kilowatt Case",
  "absolute_price_eur": 0.50,
  "multiple_of_median": 2.0,
  "enabled": true
}

## Primeiro arranque

Na primeira execução o programa apenas cria o estado inicial e NÃO envia alertas para vendas antigas. A partir da execução seguinte, apenas vendas ainda não vistas podem gerar alertas.

## Nota

O workflow usa o endpoint de histórico de vendas do CSFloat. A API oficial do CSFloat requer API key para os endpoints que explicitamente a exigem; o endpoint de histórico de vendas é atualmente documentado como uma superfície funcional por um SDK comunitário. Se a CSFloat alterar essa superfície, o monitor poderá precisar de atualização.

O agendamento do GitHub Actions tem intervalo mínimo de 5 minutos e pode sofrer algum atraso em períodos de carga elevada.
'''

(base / "csfloat_alert.py").write_text(script, encoding="utf-8")
(base / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
(base / "requirements.txt").write_text("requests>=2.31,<3\n", encoding="utf-8")
(base / "README.md").write_text(readme, encoding="utf-8")
(base / ".github" / "workflows" / "csfloat.yml").write_text(workflow, encoding="utf-8")

zip_path = Path("/mnt/data/csfloat_github_actions.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for p in [base / "csfloat_alert.py", base / "config.json",
              base / "requirements.txt", base / "README.md",
              base / ".github" / "workflows" / "csfloat.yml"]:
        z.write(p, p.relative_to(base))

print(zip_path)
