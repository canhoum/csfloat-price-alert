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

STATE_VERSION = 3

MAX_STORED_SALES = 500


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


def get_asset_id(sale):
    item = sale.get("item")

    if isinstance(item, dict):
        return str(
            item.get("asset_id", "")
        )

    return ""


def get_sale_key(sale):
    """
    Cria um identificador estável para uma venda.

    Usamos:
        created_at
        sold_at
        price
        asset_id

    O ID da venda é usado apenas como fallback.
    """

    created_at = (
        sale.get("created_at")
        or ""
    )

    sold_at = (
        sale.get("sold_at")
        or ""
    )

    price = sale.get(
        "price",
        sale.get(
            "sale_price",
            sale.get("amount", "")
        )
    )

    asset_id = get_asset_id(
        sale
    )

    # Fingerprint estável
    if created_at or sold_at or asset_id:

        return (
            f"fp|"
            f"{created_at}|"
            f"{sold_at}|"
            f"{price}|"
            f"{asset_id}"
        )

    # Fallback
    for key in (
        "id",
        "sale_id",
        "contract_id",
        "listing_id"
    ):
        value = sale.get(key)

        if value:
            return f"id|{value}"

    return (
        f"fallback|"
        f"{price}"
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

        if not isinstance(
            sale,
            dict
        ):
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

            price_eur = (
                float(price) / 100.0
            )

        except (
            TypeError,
            ValueError
        ):
            continue

        sales.append(
            {
                "key": get_sale_key(
                    sale
                ),

                "id": str(
                    sale.get(
                        "id",
                        ""
                    )
                ),

                "price_eur": price_eur,

                "created_at": (
                    sale.get(
                        "created_at"
                    )
                    or sale.get(
                        "sold_at"
                    )
                )
            }
        )

    return sales


def fetch_sales(name):

    url = BASE_URL.format(
        quote(
            name,
            safe=""
        )
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
        sale["price_eur"]
        > baseline
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

        if response.status_code in (
            200,
            204
        ):
            return True

        if response.status_code == 429:

            retry_after = (
                response.headers.get(
                    "Retry-After"
                )
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
                f"A aguardar "
                f"{wait_seconds:.1f}s..."
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

    # ==================================================
    # DISCORD
    # ==================================================

    webhook = os.environ.get(
        "DISCORD_WEBHOOK"
    )

    if not webhook:

        raise SystemExit(
            "DISCORD_WEBHOOK não está "
            "configurado no GitHub Secrets."
        )

    # ==================================================
    # CONFIG
    # ==================================================

    config = load_json(
        CONFIG_FILE,
        {
            "default_rule": {
                "baseline_sales": 100
            },
            "items": []
        }
    )

    # ==================================================
    # STATE
    # ==================================================

    state = load_json(
        STATE_FILE,
        {}
    )

    old_state = state

    # ==================================================
    # DETETAR SE PRECISAMOS DE REINICIALIZAR
    # ==================================================

    needs_reinitialization = (
        state.get(
            "version"
        ) != STATE_VERSION
    )

    if needs_reinitialization:

        print(
            "[RESET] A criar novo estado "
            "com identificadores estáveis."
        )

        state = {
            "version": STATE_VERSION,
            "sent": {}
        }

    # ==================================================
    # ITEMS
    # ==================================================

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

        # ==================================================
        # OBTER VENDAS
        # ==================================================

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

        # ==================================================
        # PRIMEIRA EXECUÇÃO DA NOVA VERSÃO
        # ==================================================

        #
        # IMPORTANTE:
        #
        # As 40 vendas que estão atualmente na API
        # serão usadas como baseline.
        #
        # NÃO serão enviadas para o Discord.
        #

        if needs_reinitialization:

            state["sent"][name] = [
                sale["key"]
                for sale in sales
            ][
                :MAX_STORED_SALES
            ]

            print(
                f"[RESET] {name}: "
                f"{len(sales)} vendas "
                "registadas como existentes. "
                "Nenhum alerta enviado."
            )

            continue

        # ==================================================
        # VENDAS JÁ ENVIADAS
        # ==================================================

        sent_keys = set(
            state.get(
                "sent",
                {}
            ).get(
                name,
                []
            )
        )

        # ==================================================
        # NOVAS VENDAS
        # ==================================================

        pending_sales = [
            sale
            for sale in reversed(
                sales
            )
            if sale["key"]
            not in sent_keys
        ]

        print(
            f"[CHECK] {name}: "
            f"{len(sales)} vendas, "
            f"{len(pending_sales)} novas."
        )

        if not pending_sales:

            continue

        # ==================================================
        # PROCESSAR NOVAS VENDAS
        # ==================================================

        for sale in pending_sales:

            # --------------------------------------------------
            # MEDIANA
            # --------------------------------------------------

            comparison = [
                other["price_eur"]
                for other in sales
                if other["key"]
                != sale["key"]
            ][:int(
                rule.get(
                    "baseline_sales",
                    100
                )
            )]

            if not comparison:

                print(
                    f"[SKIP] {name}: "
                    "sem dados suficientes "
                    "para calcular a mediana."
                )

                continue

            baseline = statistics.median(
                comparison
            )

            # --------------------------------------------------
            # MULTIPLICADOR
            # --------------------------------------------------

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

            # --------------------------------------------------
            # DISCORD
            # --------------------------------------------------

            success = send_discord(
                webhook,
                name,
                sale,
                baseline,
                multiplier
            )

            if success:

                sent_keys.add(
                    sale["key"]
                )

                state["sent"][name] = list(
                    sent_keys
                )[
                    -MAX_STORED_SALES:
                ]

                save_json(
                    STATE_FILE,
                    state
                )

                if (
                    sale["price_eur"]
                    > baseline
                ):

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

            # --------------------------------------------------
            # DELAY
            # --------------------------------------------------

            time.sleep(
                DISCORD_DELAY_SECONDS
            )

    # ==================================================
    # GUARDAR ESTADO
    # ==================================================

    save_json(
        STATE_FILE,
        state
    )


if __name__ == "__main__":
    main()