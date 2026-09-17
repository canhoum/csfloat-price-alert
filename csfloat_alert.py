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

# Discord permite um número limitado de mensagens por minuto.
# Mantemos um intervalo seguro entre mensagens.
DISCORD_DELAY_SECONDS = 2.2

# Se houver muitas vendas pendentes, não tentamos enviar todas
# na mesma execução.
MAX_SALES_PER_RUN = 20


def load_json(path, default):
    if not path.exists():
        return default

    return json.loads(
        path.read_text(encoding="utf-8")
    )


def save_json(path, data):
    path.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def get_sale_id(sale):
    """
    Cria um identificador estável para uma venda.
    Preferimos o ID oficial da venda.
    """

    for key in (
        "id",
        "sale_id",
        "contract_id",
        "listing_id"
    ):
        value = sale.get(key)

        if value:
            return str(value)

    created_at = (
        sale.get("created_at")
        or sale.get("sold_at")
        or ""
    )

    price = sale.get(
        "price",
        sale.get(
            "sale_price",
            sale.get("amount", "")
        )
    )

    item = sale.get("item")

    asset_id = ""

    if isinstance(item, dict):
        asset_id = item.get(
            "asset_id",
            ""
        )

    return (
        f"{created_at}|"
        f"{price}|"
        f"{asset_id}"
    )


def extract_sales(payload):
    if isinstance(payload, list):
        raw = payload

    elif isinstance(payload, dict):
        raw = next(
            (
                payload[key]
                for key in (
                    "sales",
                    "data",
                    "results"
                )
                if isinstance(
                    payload.get(key),
                    list
                )
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

        except (
            TypeError,
            ValueError
        ):
            continue

        sale_id = get_sale_id(sale)

        sales.append({
            "id": sale_id,
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
            "User-Agent":
                "CSFloatPriceAlert-GitHubActions/1.0"
        },
        timeout=30
    )

    response.raise_for_status()

    return extract_sales(
        response.json()
    )


def send_discord(
    webhook,
    name,
    sale,
    baseline,
    multiplier
):
    above_median = (
        sale["price_eur"] > baseline
    )

    mention = (
        "@everyone\n"
        if above_median
        else ""
    )

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

    for attempt in range(5):

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

        if response.status_code == 204:
            return True

        if response.status_code == 200:
            return True

        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After"
            )

            try:
                wait_seconds = float(
                    retry_after
                )

            except (
                TypeError,
                ValueError
            ):
                wait_seconds = 5.0

            wait_seconds = max(
                wait_seconds,
                2.5
            )

            print(
                f"[DISCORD] Rate limit. "
                f"A aguardar {wait_seconds:.1f}s..."
            )

            time.sleep(
                wait_seconds
            )

            continue

        print(
            f"[DISCORD ERROR] "
            f"HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

        return False

    print(
        "[DISCORD ERROR] "
        "Não foi possível enviar após "
        "várias tentativas."
    )

    return False


def main():

    webhook = os.environ.get(
        "DISCORD_WEBHOOK"
    )

    if not webhook:
        raise SystemExit(
            "DISCORD_WEBHOOK não está "
            "configurado no GitHub Secrets."
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

    state = load_json(
    STATE_FILE,
    {
        "sent": {}
    }
)

# Compatibilidade com o estado antigo.
# A versão anterior guardava as vendas em "seen".
# Mantemos essas vendas para não as voltar a notificar.
if "sent" not in state:

    if "seen" in state:
        state["sent"] = state["seen"]

        print(
            "[MIGRATE] Estado antigo encontrado. "
            "A converter 'seen' para 'sent'."
        )

    else:
        state["sent"] = {}
    for item in config.get(
        "items",
        []
    ):

        if not item.get(
            "enabled",
            True
        ):
            continue

        name = item[
            "market_hash_name"
        ]

        rule = {
            **config.get(
                "default_rule",
                {}
            ),
            **item
        }

        try:
            sales = fetch_sales(
                name
            )

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

        sent_ids = set(
            state["sent"].get(
                name,
                []
            )
        )

        # Primeira inicialização.
        #
        # Se nunca tivermos estado para este item,
        # guardamos as vendas atuais sem enviar
        # notificações antigas.
        if name not in state["sent"]:

            state["sent"][name] = [
                sale["id"]
                for sale in sales
            ][:500]

            print(
                f"[INIT] {name}: "
                f"{len(sales)} vendas registadas "
                "sem alertas."
            )

            continue

        # Só processamos vendas que ainda não
        # foram enviadas para o Discord.
        pending_sales = [
            sale
            for sale in reversed(sales)
            if sale["id"] not in sent_ids
        ]

        print(
            f"[CHECK] {name}: "
            f"{len(sales)} vendas, "
            f"{len(pending_sales)} pendentes."
        )

        if not pending_sales:
            continue

        # Limite de segurança por execução.
        sales_to_process = (
            pending_sales[
                :MAX_SALES_PER_RUN
            ]
        )

        for sale in sales_to_process:

            # A mediana é calculada usando as outras
            # vendas disponíveis.
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
                print(
                    f"[SKIP] {name}: "
                    "sem dados suficientes."
                )
                continue

            baseline = statistics.median(
                comparison
            )

            multiplier = (
                sale["price_eur"]
                / baseline
                if baseline
                else 0
            )

            print(
                f"[SALE] {name}: "
                f"€{sale['price_eur']:.2f} "
                f"({multiplier:.2f}x)"
            )

            success = send_discord(
                webhook,
                name,
                sale,
                baseline,
                multiplier
            )

            if success:

                sent_ids.add(
                    sale["id"]
                )

                state["sent"][name] = list(
                    sent_ids
                )[-500:]

                save_json(
                    STATE_FILE,
                    state
                )

                if sale["price_eur"] > baseline:
                    print(
                        "[SENT] "
                        "@everyone enviado."
                    )
                else:
                    print(
                        "[SENT] "
                        "Notificação normal enviada."
                    )

            else:
                print(
                    "[PENDING] "
                    "Venda não enviada. "
                    "Será tentada novamente."
                )

            # Evita atingir o rate limit.
            time.sleep(
                DISCORD_DELAY_SECONDS
            )

    save_json(
        STATE_FILE,
        state
    )


if __name__ == "__main__":
    main()