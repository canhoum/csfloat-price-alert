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
EXCHANGE_URL = "https://csfloat.com/api/v1/meta/exchange-rates"

DISCORD_DELAY_SECONDS = 2.2

# Guardamos IDs suficientes para evitar duplicações.
MAX_STORED_SALES = 200

# Número de vendas usadas para calcular a mediana.
BASELINE_SALES = 100

STATE_VERSION = 5


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
    value = sale.get("id")

    if value is None:
        return ""

    return str(value)


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
            # CSFloat devolve o preço em cêntimos de USD.
            price_usd = float(price) / 100.0
        except (TypeError, ValueError):
            continue

        sold_at = (
            sale.get("sold_at")
            or sale.get("created_at")
            or ""
        )

        sales.append(
            {
                "id": sale_id,
                "price_usd": price_usd,
                "sold_at": sold_at,
            }
        )

    return sales


def fetch_sales(name):
    url = BASE_URL.format(
        quote(name, safe="")
    )

    print(f"[API URL] {url}")

    response = requests.get(
        url,
        headers={
            "User-Agent": "CSFloatPriceAlert-GitHubActions/1.0"
        },
        timeout=30
    )

    print(f"[API STATUS] {response.status_code}")

    response.raise_for_status()

    sales = extract_sales(response.json())

    print(f"[API] {name}: {len(sales)} vendas recebidas.")

    return sales


def fetch_usd_to_eur():
    """
    Obtém a taxa USD -> EUR através do endpoint
    público de exchange rates do CSFloat.
    """

    try:
        response = requests.get(
            EXCHANGE_URL,
            headers={
                "User-Agent": "CSFloatPriceAlert-GitHubActions/1.0"
            },
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        print(f"[EXCHANGE] Resposta: {data}")

        # O CSFloat devolve atualmente as taxas dentro de "data".
        rates = data.get("data", data)

        if isinstance(rates, dict):

            # EUR é a taxa que precisamos.
            for key in ("eur", "EUR"):

                value = rates.get(key)

                try:
                    rate = float(value)

                    if rate > 0:
                        print(
                            f"[EXCHANGE] USD -> EUR: {rate}"
                        )

                        return rate

                except (TypeError, ValueError):
                    continue

        print(
            "[EXCHANGE] "
            "Taxa EUR não encontrada. Usar 1.0."
        )

    except Exception as exc:

        print(
            f"[EXCHANGE ERROR] {exc}"
        )

    return 1.0    except Exception as exc:
        print(f"[EXCHANGE ERROR] {exc}")

    print("[EXCHANGE] Não foi possível obter a taxa. Usar 1.0.")
    return 1.0


def send_discord(
    webhook,
    name,
    sale,
    baseline_usd,
    usd_to_eur
):
    price_usd = sale["price_usd"]

    baseline_eur = baseline_usd * usd_to_eur
    price_eur = price_usd * usd_to_eur

    above_median = price_usd > baseline_usd

    difference = (
        price_usd / baseline_usd
        if baseline_usd > 0
        else 0
    )

    mention = "@everyone\n" if above_median else ""

    content = (
        f"{mention}"
        f"**CSFloat SALE**\n"
        f"Item: `{name}`\n"
        f"Sale: **€{price_eur:.2f}**\n"
        f"Median baseline: €{baseline_eur:.2f}\n"
        f"Difference: **{difference:.2f}×**\n"
        f"CSFloat: https://csfloat.com/search?"
        f"market_hash_name={quote(name)}"
    )

    for attempt in range(5):

        try:
            response = requests.post(
                webhook,
                json={
                    "content": content,
                    "allowed_mentions": {
                        "parse": (
                            ["everyone"]
                            if above_median
                            else []
                        )
                    }
                },
                timeout=30
            )

        except Exception as exc:
            print(
                f"[DISCORD ERROR] Tentativa {attempt + 1}: {exc}"
            )

            time.sleep(5)
            continue

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After"
            )

            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = 5.0

            wait_seconds = max(
                wait_seconds,
                2.5
            )

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


def add_seen_id(seen_list, sale_id):
    """
    Adiciona o ID mantendo a ordem.

    IMPORTANTE:
    Não usamos set() para guardar o estado final,
    porque isso destrói a ordem dos IDs.
    """

    if sale_id in seen_list:
        return seen_list

    seen_list.append(sale_id)

    if len(seen_list) > MAX_STORED_SALES:
        seen_list = seen_list[-MAX_STORED_SALES:]

    return seen_list


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
                "baseline_sales": BASELINE_SALES
            },
            "items": []
        }
    )

    state = load_json(
        STATE_FILE,
        {}
    )

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

    usd_to_eur = fetch_usd_to_eur()

    for item in config.get("items", []):

        if not item.get("enabled", True):
            continue

        name = item["market_hash_name"]

        rule = {
            **config.get("default_rule", {}),
            **item
        }

        baseline_count = int(
            rule.get(
                "baseline_sales",
                BASELINE_SALES
            )
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
                f"API não devolveu vendas."
            )

            continue

        # --------------------------------------------------
        # PRIMEIRA EXECUÇÃO
        # --------------------------------------------------

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

        # --------------------------------------------------
        # ESTADO ANTERIOR
        # --------------------------------------------------

        seen_ids = state["seen"].get(
            name,
            []
        )

        if not isinstance(seen_ids, list):
            seen_ids = []

        # Garantir que não existem duplicados
        # mas preservar a ordem.
        seen_ids = list(
            dict.fromkeys(
                str(value)
                for value in seen_ids
            )
        )

        # --------------------------------------------------
        # DETETAR NOVAS VENDAS
        # --------------------------------------------------

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

        # --------------------------------------------------
        # PROCESSAR CADA VENDA NOVA
        # --------------------------------------------------

        for sale in new_sales:

            comparison_prices = [
                other["price_usd"]
                for other in sales
                if other["id"] != sale["id"]
            ]

            comparison_prices = (
                comparison_prices[:baseline_count]
            )

            if not comparison_prices:

                print(
                    f"[SKIP] {name}: "
                    "sem dados suficientes."
                )

                # Mesmo assim registamos o ID para
                # não o processar repetidamente.
                seen_ids = add_seen_id(
                    seen_ids,
                    sale["id"]
                )

                continue

            baseline_usd = statistics.median(
                comparison_prices
            )

            price_eur = (
                sale["price_usd"]
                * usd_to_eur
            )

            baseline_eur = (
                baseline_usd
                * usd_to_eur
            )

            print(
                f"[SALE] {name}: "
                f"€{price_eur:.2f} "
                f"(mediana €{baseline_eur:.2f})"
            )

            success = send_discord(
                webhook,
                name,
                sale,
                baseline_usd,
                usd_to_eur
            )

            if success:

                seen_ids = add_seen_id(
                    seen_ids,
                    sale["id"]
                )

                print(
                    f"[SENT] "
                    f"Venda {sale['id']} enviada."
                )

            else:

                print(
                    f"[PENDING] "
                    f"Venda {sale['id']} "
                    f"não enviada."
                )

            time.sleep(
                DISCORD_DELAY_SECONDS
            )

        # --------------------------------------------------
        # GUARDAR ESTADO
        # --------------------------------------------------

        state["seen"][name] = seen_ids[
            -MAX_STORED_SALES:
        ]

    save_json(
        STATE_FILE,
        state
    )

    print("[STATE] Estado guardado.")


if __name__ == "__main__":
    main()