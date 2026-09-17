import json
import os
import statistics
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
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def extract_sales(payload):
    if isinstance(payload, list):
        raw = payload

    elif isinstance(payload, dict):
        raw = next(
            (
                payload[key]
                for key in ("sales", "data", "results")
                if isinstance(payload.get(key), list)
            ),
            []
        )

    else:
        raw = []

    sales = []

    for sale in raw:
        if not isinstance(sale, dict):
            continue

        price = sale.get(
            "price",
            sale.get(
                "sale_price",
                sale.get("amount")
            )
        )

        if price is None:
            continue

        try:
            price_eur = float(price) / 100.0
        except (TypeError, ValueError):
            continue

        sale_id = (
            sale.get("id")
            or sale.get("sale_id")
            or sale.get("contract_id")
            or sale.get("listing_id")
            or f"{sale.get('created_at', '')}|{price}"
        )

        sales.append({
            "id": str(sale_id),
            "price_eur": price_eur,
            "created_at": (
                sale.get("created_at")
                or sale.get("sold_at")
            )
        })

    return sales


def fetch_sales(name):
    url = BASE_URL.format(
        quote(name, safe="")
    )

    response = requests.get(
        url,
        headers={
            "User-Agent": "CSFloatPriceAlert-GitHubActions/1.0"
        },
        timeout=30
    )

    response.raise_for_status()

    return extract_sales(response.json())


def send_discord(
    webhook,
    name,
    sale,
    baseline,
    multiplier
):
    above_median = sale["price_eur"] > baseline

    mention = "@everyone\n" if above_median else ""

    content = (
        f"{mention}"
        "**CSFloat SALE**\n"
        f"Item: `{name}`\n"
        f"Sale: **€{sale['price_eur']:.2f}**\n"
        f"Median baseline: €{baseline:.2f}\n"
        f"Difference: **{multiplier:.2f}×**\n"
        f"CSFloat: https://csfloat.com/search?"
        f"market_hash_name={quote(name)}"
    )

    response = requests.post(
        webhook,
        json={
            "content": content,
            "allowed_mentions": {
                "parse": ["everyone"] if above_median else []
            }
        },
        timeout=30
    )

    response.raise_for_status()


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK")

    if not webhook:
        raise SystemExit(
            "DISCORD_WEBHOOK não está configurado "
            "no GitHub Secrets."
        )

    config = load_json(
        CONFIG_FILE,
        {
            "default_rule": {
                "baseline_sales": 100
            },
            "items": []
        }
    )

    state_exists = STATE_FILE.exists()

    state = load_json(
        STATE_FILE,
        {"seen": {}}
    )

    for item in config.get("items", []):

        if not item.get("enabled", True):
            continue

        name = item["market_hash_name"]

        rule = {
            **config.get("default_rule", {}),
            **item
        }

        try:
            sales = fetch_sales(name)

        except Exception as exc:
            print(
                f"[ERROR] {name}: {exc}"
            )
            continue

        if not sales:
            print(
                f"[OK] {name}: "
                "API não devolveu vendas."
            )
            continue

        # Primeira execução:
        # guardar as vendas existentes sem notificações.
        if (
            not state_exists
            or name not in state["seen"]
        ):
            state["seen"][name] = [
                sale["id"]
                for sale in sales
            ][:500]

            print(
                f"[INIT] {name}: "
                f"{len(sales)} vendas registadas "
                "sem alertas."
            )

            continue

        seen = set(
            state["seen"][name]
        )

        new_sales = [
            sale
            for sale in reversed(sales)
            if sale["id"] not in seen
        ]

        for sale in new_sales:

            comparison = [
                other["price_eur"]
                for other in sales
                if other["id"] != sale["id"]
            ][:int(
                rule.get(
                    "baseline_sales",
                    100
                )
            )]

            if not comparison:
                continue

            baseline = statistics.median(
                comparison
            )

            multiplier = (
                sale["price_eur"] / baseline
                if baseline
                else 0
            )

            try:
                send_discord(
                    webhook,
                    name,
                    sale,
                    baseline,
                    multiplier
                )

                print(
                    f"[SALE] {name}: "
                    f"€{sale['price_eur']:.2f} "
                    f"({multiplier:.2f}x)"
                    + (
                        " @everyone"
                        if sale["price_eur"] > baseline
                        else ""
                    )
                )

            except Exception as exc:
                print(
                    f"[DISCORD ERROR] {exc}"
                )

        state["seen"][name] = list(
            dict.fromkeys(
                [
                    sale["id"]
                    for sale in sales
                ]
                + state["seen"][name]
            )
        )[:500]

        print(
            f"[OK] {name}: "
            f"{len(sales)} vendas, "
            f"{len(new_sales)} novas."
        )

    save_json(
        STATE_FILE,
        state
    )


if __name__ == "__main__":
    main()