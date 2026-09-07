import json
import os
import subprocess
import sys

import requests


CARDMARKET_PRICES_URL = (
    "https://raw.githubusercontent.com/michalkiral/optcg-data-cardmarket/main/"
    "data/prices/summary.json"
)
CARDMARKET_CARDS_URL = (
    "https://raw.githubusercontent.com/michalkiral/optcg-data-cardmarket/main/"
    "data/index/cards_by_id.json"
)
OFFICIAL_API_URL = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
PRICE_API_URL = "https://www.optcgapi.com/api/allSetCards/"
TEST_OUTPUT_FILE = "cards_multisource_test.json"
REPORT_OUTPUT_FILE = "cards_multisource_test_report.json"
RAW_OUTPUT_FILES = {
    "cardmarket_prices_raw.json": CARDMARKET_PRICES_URL,
    "cardmarket_cards_raw.json": CARDMARKET_CARDS_URL,
}
PUBLISH_FILES = [*RAW_OUTPUT_FILES, TEST_OUTPUT_FILE, REPORT_OUTPUT_FILE]


def publish_raw_data():
    branch_name = os.environ.get("GITHUB_REF_NAME")
    if not branch_name:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()

    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "add", "--", *PUBLISH_FILES], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en los RAW de Cardmarket; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar datos de cardmarket"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", f"HEAD:{branch_name}"], check=True)
        print(f"Archivos de prueba publicados: {', '.join(PUBLISH_FILES)}")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Error al publicar los RAW de Cardmarket: {error}")
        sys.exit(1)


def fetch_community_data():
    headers = {
        "User-Agent": "OPTCG-App-Data-Discovery/1.0",
    }

    downloaded_data = {}
    for output_file, source_url in RAW_OUTPUT_FILES.items():
        print(f"Descargando fuente Cardmarket: {source_url}")
        try:
            response = requests.get(source_url, headers=headers, timeout=60)
            response.raise_for_status()
            downloaded_data[output_file] = response.json()
        except requests.RequestException as error:
            print(f"Error de red descargando {output_file}: {error}")
            sys.exit(1)
        except ValueError as error:
            print(f"{output_file} no contiene un JSON válido: {error}")
            sys.exit(1)

    return downloaded_data


def save_raw_data(downloaded_data):
    for output_file, data in downloaded_data.items():
        try:
            with open(output_file, "w", encoding="utf-8") as output_handle:
                json.dump(data, output_handle, indent=2, ensure_ascii=False)
        except (OSError, TypeError) as error:
            print(f"Error al escribir {output_file}: {error}")
            sys.exit(1)

        print(f"Datos crudos guardados en {output_file}.")


def print_structure_summary(data):
    for output_file, source_data in data.items():
        if isinstance(source_data, list):
            print(f"{output_file}: {len(source_data)} registros.")
        elif isinstance(source_data, dict):
            print(f"{output_file}: objeto con {len(source_data)} claves principales.")
        else:
            print(f"{output_file}: tipo de raíz {type(source_data).__name__}.")


def normalize_text(value):
    return " ".join(str(value or "").casefold().split())


def get_number(*values):
    for value in values:
        if value is None or str(value).strip().upper() == "NULL":
            continue
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return value
    return None


def get_price(price_item):
    return get_number(
        price_item.get("usd"),
        price_item.get("market_price"),
        price_item.get("inventory_price"),
    )


def fetch_official_cards():
    cards = []
    page = 1
    while True:
        response = requests.get(
            OFFICIAL_API_URL,
            params={"page": page, "per_page": 100},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        chunk = data.get("data", []) if isinstance(data, dict) else data
        if not isinstance(chunk, list) or not chunk:
            break
        cards.extend(chunk)
        total_pages = data.get("total_pages") if isinstance(data, dict) else None
        if total_pages is not None and page >= int(total_pages):
            break
        if total_pages is None and len(chunk) < 100:
            break
        page += 1
    return cards


def source_record(code, card=None, price=None, cardmarket=None):
    card = card or {}
    price = price or {}
    cardmarket = cardmarket or {}
    name = (
        cardmarket.get("name")
        or price.get("card_name")
        or card.get("name")
        or ""
    )
    image_url = (
        cardmarket.get("image")
        or price.get("card_image")
        or card.get("image")
        or f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png"
    )
    return {
        "id": cardmarket.get("id") or card.get("id") or price.get("id") or code,
        "code": code,
        "game": "One Piece",
        "name": str(name).strip(),
        "imageUrl": image_url,
        "price": get_price(price) if price else None,
        "priceEur": get_number(price.get("eur")),
        "inventoryPrice": get_number(price.get("inventory_price")),
        "marketPrice": get_number(price.get("market_price")),
        "currency": "USD",
        "rarity": cardmarket.get("rarity") or price.get("rarity") or card.get("rarity"),
        "setName": price.get("set_name") or cardmarket.get("set"),
        "setId": price.get("set_id"),
        "type": cardmarket.get("category") or price.get("card_type") or card.get("type"),
        "effect": cardmarket.get("effect") or price.get("card_text") or card.get("effect"),
        "cost": get_number(cardmarket.get("cost"), price.get("card_cost"), card.get("cost")),
        "power": get_number(cardmarket.get("power"), price.get("card_power"), card.get("power")),
        "counter": get_number(cardmarket.get("counter"), price.get("counter_amount"), card.get("counter")),
        "life": get_number(cardmarket.get("life"), price.get("life"), card.get("life")),
        "color": ", ".join(cardmarket.get("colors", [])) or price.get("card_color") or card.get("color"),
        "attribute": ", ".join(cardmarket.get("attributes", [])) or price.get("attribute") or card.get("attribute"),
        "class": ", ".join(cardmarket.get("types", [])) or price.get("sub_types") or card.get("class"),
        "priceDate": price.get("date_scraped"),
        "priceSource": price.get("src") or "optcgapi",
        "sources": [],
    }


def merge_sources(official_cards, price_cards, cardmarket_cards, cardmarket_prices):
    official_by_code = {}
    price_by_code = {}
    cardmarket_by_code = {}
    for item in official_cards:
        code = str(item.get("code") or "").strip().upper()
        if code:
            official_by_code.setdefault(code, []).append(item)
    for item in price_cards:
        code = str(item.get("card_set_id") or "").strip().upper()
        if code:
            price_by_code.setdefault(code, []).append(item)
    for code, item in cardmarket_cards.items():
        base_code = str(code).split("_p", 1)[0].upper()
        cardmarket_by_code.setdefault(base_code, []).append((code, item))

    all_codes = set(official_by_code) | set(price_by_code) | set(cardmarket_by_code)
    merged = {}
    stats = {"codes": len(all_codes), "new_variants": 0, "merged_variants": 0}

    for code in sorted(all_codes):
        candidates = []
        official_items = official_by_code.get(code, [])
        price_items = price_by_code.get(code, [])
        market_items = cardmarket_by_code.get(code, [])
        market_by_key = {key: item for key, item in market_items}
        keys = set(market_by_key)
        keys.update(
            item.get("card_set_id", code).upper()
            for item in price_items
            if item.get("card_set_id")
        )
        if not keys:
            keys = {code}
        for key in sorted(keys):
            market_item = market_by_key.get(key, {})
            price_item = next(
                (item for item in price_items if str(item.get("card_set_id", "")).upper() == key),
                {},
            )
            market_price_item = cardmarket_prices.get(key, {})
            combined_price = dict(market_price_item)
            combined_price.update(price_item)
            official_item = next(
                (item for item in official_items if normalize_text(item.get("name")) == normalize_text(market_item.get("name"))),
                official_items[0] if official_items and key == code else {},
            )
            record = source_record(key, official_item, combined_price, market_item)
            record["sources"] = [
                source for source, present in (
                    ("official", bool(official_item)),
                    ("optcgapi", bool(price_item)),
                    ("cardmarket", bool(market_item or market_price_item)),
                ) if present
            ]
            candidates.append(record)

        base = candidates[0]
        base["variants"] = []
        for candidate in candidates[1:]:
            if candidate["imageUrl"] == base["imageUrl"] and normalize_text(candidate["name"]) == normalize_text(base["name"]):
                stats["merged_variants"] += 1
                continue
            candidate["suffix"] = f"_P{len(base['variants']) + 1}"
            base["variants"].append(candidate)
            stats["new_variants"] += 1
        merged[code] = base
    return merged, stats


def main():
    community_data = fetch_community_data()
    save_raw_data(community_data)
    print("Descargando API oficial para la comparación...")
    official_cards = fetch_official_cards()
    print("Descargando API de precios para la comparación...")
    price_response = requests.get(PRICE_API_URL, timeout=60)
    price_response.raise_for_status()
    price_cards = price_response.json()
    cardmarket_cards = community_data["cardmarket_cards_raw.json"]
    cardmarket_prices = community_data["cardmarket_prices_raw.json"]
    merged_cards, report = merge_sources(
        official_cards,
        price_cards,
        cardmarket_cards,
        cardmarket_prices.get("cards", {}),
    )
    try:
        with open(TEST_OUTPUT_FILE, "w", encoding="utf-8") as output_file:
            json.dump(merged_cards, output_file, indent=2, ensure_ascii=False)
        with open(REPORT_OUTPUT_FILE, "w", encoding="utf-8") as report_file:
            json.dump(report, report_file, indent=2, ensure_ascii=False)
    except (OSError, TypeError) as error:
        print(f"Error al escribir los archivos de prueba: {error}")
        sys.exit(1)
    print(f"Catálogo de prueba generado: {TEST_OUTPUT_FILE}")
    print(f"Informe de fusión generado: {REPORT_OUTPUT_FILE}")
    publish_raw_data()
    print_structure_summary(community_data)


if __name__ == "__main__":
    main()
