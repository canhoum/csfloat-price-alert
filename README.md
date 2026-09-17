# CSFloat Price Spike Alert

Monitoriza vendas recentes de um item na CSFloat e envia um alerta para o Discord quando uma venda ultrapassa um preço absoluto ou um múltiplo da mediana das vendas recentes.

## Instalação

No Windows, instala Python 3.10+ e, nesta pasta:

```powershell
py -m pip install -r requirements.txt
```

## Discord

Discord -> canal -> Editar Canal -> Integrações -> Webhooks -> Novo Webhook.

Coloca o URL em `config.json`:

```json
"discord_webhook": "https://discord.com/api/webhooks/..."
```

Não partilhes esse URL.

## Skin

No `config.json`, coloca o nome exato do item:

```json
"market_hash_name": "AK-47 | Redline (Field-Tested)"
```

## Regras

```json
"absolute_price_eur": 1.00,
"multiple_of_median": 3.0,
"baseline_sales": 100
```

Gera alerta se a venda for >= €1,00 OU >= 3x a mediana das últimas 100 vendas.

## Executar

```powershell
py csfloat_alert.py
```

O intervalo padrão é 120 segundos.

## Nota sobre a API

A documentação oficial do CSFloat documenta a API de mercado, mas o endpoint de histórico de vendas não aparece na documentação oficial principal. Um SDK comunitário atualizado em 2026 documenta `GET /history/{market_hash_name}/sales` como endpoint funcional.

Por isso, este monitor depende de uma superfície da API que pode mudar sem aviso.

O programa apenas consulta dados e envia notificações. Não compra, vende ou modifica itens.
