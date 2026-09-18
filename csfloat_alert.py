import json
import os
import statistics
import time
from pathlib import Path
from urllib.parse import quote

import requests


CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state.json")

BASE_URL = "https://csfloat.com/api/v1/history/{}/sales"

DISCORD_DELAY_SECONDS = 2.2
MAX_STORED_SALES = 200
STATE_VERSION = 4


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_sale_id(sale):
    """
    O ID da própria venda é o identificador mais seguro.
    """
    value = sale.get("id")

    if value is not None:
        return str(value)

    return ""


def extract_sales(payload):
    if isinstance(payload, list):
        raw_sales = payload

    elif isinstance(payload, dict):
        raw_sales = next(
            (
                payload[key]
                for key in ("sales", "data", "results")
                if isinstance(payload.get(key), list)
            ),
            []
        )

    else:
        raw_sales = []

    sales = []

    for sale in raw_sales:
        if not isinstance(sale, dict):
            continue

        sale_id = get_sale_id(sale)

        if not sale_id:
            continue

        price = sale.get("price")

        if price is None:
            continue

        try:
            price_usd = float(price) / 100.0
        except (TypeError, ValueError):
            continue

        sold_at = sale.get("sold_at") or sale.get("created_at") or ""

        sales.append(
            {
                "id": sale_id,
                "price_usd": price_usd,
                "sold_at": sold_at,
            }
        )

    return sales


def fetch_sales(name):
    url = BASE_URL.format(quote(name, safe=""))

    response = requests.get(
        url,
        headers={
            "User-Agent": "CSFloatPriceAlert-GitHubActions/1.0"
        },
        timeout=30
    )

    response.raise_for_status()

    return extract_sales(response.json())


def send_discord(webhook, name, sale, baseline):
    price = sale["price_usd"]

    above_median = price > baseline

    mention = "@everyone\n" if above_median else ""

    difference = (
        price / baseline
        if baseline > 0
        else 0
    )

    content = (
        f"{mention}"
        f"**CSFloat SALE**\n"
        f"Item: `{name}`\n"
        f"Sale: **${price:.2f}**\n"
        f"Median baseline: ${baseline:.2f}\n"
        f"Difference: **{difference:.2f}×**\n"
        f"CSFloat: https://csfloat.com/search?"
        f"market_hash_name={quote(name)}"
    )

    for attempt in range(5):

        response = requests.post(
            webhook,
            json={
                "content": content,
                "allowed_mentions": {
                    "parse": ["everyone"]
                    if above_median
                    else []
                }
            },
            timeout=30
        )

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:

            retry_after = response.headers.get("Retry-After")

            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = 5.0

            wait_seconds = max(wait_seconds, 2.5)

            print(
                f"[DISCORD] Rate limit. "
                f"A aguardar {wait_seconds:.1f}s..."
            )

            time.sleep(wait_seconds)
            continue

        print(
            f"[DISCORD ERROR] "
            f"HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

        return False

    print(
        "[DISCORD ERROR] "
        "Não foi possível enviar após várias tentativas."
    )

    return False


def main():

    webhook = os.environ.get("DISCORD_WEBHOOK")

    if not webhook:
        raise SystemExit(
            "DISCORD_WEBHOOK não está configurado "
            "nos GitHub Secrets."
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

    state = load_json(STATE_FILE, {})

    # ---------------------------------------------------------
    # PRIMEIRO ARRANQUE / ESTADO INVÁLIDO
    # ---------------------------------------------------------

    valid_state = (
        isinstance(state, dict)
        and state.get("version") == STATE_VERSION
        and isinstance(state.get("seen"), dict)
    )

    if not valid_state:

        print(
            "[RESET] Estado inexistente ou incompatível."
        )

        state = {
            "version": STATE_VERSION,
            "seen": {}
        }

        first_run = True

    else:

        first_run = False

    # ---------------------------------------------------------
    # PROCESSAR ITEMS
    # ---------------------------------------------------------

    for item in config.get("items", []):

        if not item.get("enabled", True):
            continue

        name = item["market_hash_name"]

        rule = {
            **config.get("default_rule", {}),
            **item
        }

        baseline_count = int(
            rule.get("baseline_sales", 100)
        )

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

        print(
            f"[API] {name}: "
            f"{len(sales)} vendas recebidas."
        )

        # -----------------------------------------------------
        # PRIMEIRO ARRANQUE
        # -----------------------------------------------------

        if first_run:

            state["seen"][name] = [
                sale["id"]
                for sale in sales[:MAX_STORED_SALES]
            ]

            print(
                f"[BASELINE] {name}: "
                f"{len(sales)} vendas registadas."
            )

            print(
                "[BASELINE] "
                "Nenhuma notificação enviada."
            )

            continue

        # -----------------------------------------------------
        # DETETAR NOVAS VENDAS
        # -----------------------------------------------------

        seen_ids = set(
            state["seen"].get(name, [])
        )

        new_sales = [
            sale
            for sale in reversed(sales)
            if sale["id"] not in seen_ids
        ]

        print(
            f"[CHECK] {name}: "
            f"{len(sales)} vendas, "
            f"{len(new_sales)} novas."
        )

        if not new_sales:
            continue

        # -----------------------------------------------------
        # MEDIANA
        # -----------------------------------------------------

        all_prices = [
            sale["price_usd"]
            for sale in sales
        ]

        # Excluir a nova venda da própria referência
        # para não influenciar a mediana.
        for sale in new_sales:

            comparison_prices = [
                other["price_usd"]
                for other in sales
                if other["id"] != sale["id"]
            ]

            comparison_prices = comparison_prices[
                :baseline_count
            ]

            if not comparison_prices:

                print(
                    f"[SKIP] {name}: "
                    "sem dados suficientes."
                )

                continue

            baseline = statistics.median(
                comparison_prices
            )

            print(
                f"[SALE] {name}: "
                f"${sale['price_usd']:.2f} "
                f"(mediana ${baseline:.2f})"
            )

            success = send_discord(
                webhook,
                name,
                sale,
                baseline
            )

            if success:

                seen_ids.add(sale["id"])

                print(
                    "[SENT] "
                    "Notificação enviada."
                )

            else:

                print(
                    "[PENDING] "
                    "Venda não enviada."
                )

            time.sleep(
                DISCORD_DELAY_SECONDS
            )

        # -----------------------------------------------------
        # GUARDAR ESTADO
        # -----------------------------------------------------

        state["seen"][name] = list(seen_ids)[
            -MAX_STORED_SALES:
        ]

    save_json(
        STATE_FILE,
        state
    )


if __name__ == "__main__":
    main()