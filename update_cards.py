import html
import json
import os
import subprocess
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
RAW_OUTPUT_FILE = "cards_api_raw.json"
PRICE_API_URL = "https://www.optcgapi.com/api/allSetCards/"
PRICE_RAW_OUTPUT_FILE = "cards_prices_raw.json"
# Cambiar a True solo para guardar y publicar las respuestas JSON originales.
SAVE_RAW_FILES = False


def publish_generated_files(include_cards_file=True):
    files_to_add = []
    if SAVE_RAW_FILES:
        files_to_add.extend([RAW_OUTPUT_FILE, PRICE_RAW_OUTPUT_FILE])
    if include_cards_file:
        files_to_add.append(OUTPUT_FILE)
    if not files_to_add:
        return

    branch_name = os.environ.get("GITHUB_REF_NAME")
    if not branch_name:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()

    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", "--", *files_to_add], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en los archivos generados; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar catálogo de cartas"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", f"HEAD:{branch_name}"], check=True)
        print(f"Archivos publicados en la rama {branch_name}: {', '.join(files_to_add)}")
    except (OSError, subprocess.CalledProcessError) as publish_err:
        print(f"Error al publicar los archivos generados en GitHub: {publish_err}")
        sys.exit(1)

def get_all_cards_from_api():
    print("Descargando el catálogo completo de optcgapi.com...")
    base_url = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
    per_page = 20
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, /"
    }
    
    all_cards = []
    page = 1
    
    while True:
        try:
            response = requests.get(
                base_url,
                params={"page": page, "per_page": per_page},
                headers=headers,
                timeout=30,
            )
            if response.status_code != 200:
                print(f"La API oficial respondió con HTTP {response.status_code}.")
                break
                
            data = response.json()
            if isinstance(data, dict):
                cards_chunk = data.get("data", data.get("results", []))
                total_pages = data.get("total_pages")
            else:
                cards_chunk = data
                total_pages = None
                
            if not isinstance(cards_chunk, list) or not cards_chunk:
                break
                
            all_cards.extend(cards_chunk)

            if total_pages is not None and page >= int(total_pages):
                break
            if total_pages is None and len(cards_chunk) < per_page:
                break
                
            page += 1
        except Exception as e:
            print(f"⚠️ Error al conectar con la API en la página {page}: {e}")
            break
            
    return all_cards


def get_all_cards_with_prices():
    print("Descargando catálogo y precios de optcgapi.com...")
    response = requests.get(PRICE_API_URL, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("La API de precios no devolvió una lista de cartas.")
    return data


def normalize_text(value):
    return " ".join(str(value or "").casefold().split())


def get_image_keys(*values):
    keys = set()
    for value in values:
        normalized = normalize_text(value)
        if normalized:
            keys.add(normalized.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    return keys


def get_price(item):
    for key in ("market_price", "marketPrice", "price", "inventory_price"):
        value = item.get(key)
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return None


def get_first_value(*values):
    for value in values:
        if value is not None and str(value).strip().upper() != "NULL":
            return value
    return None


def get_number(*values):
    value = get_first_value(*values)
    if value is None:
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return value


def match_score(official_item, price_item):
    official_name = normalize_text(official_item.get("name"))
    price_name = normalize_text(price_item.get("card_name"))
    score = 0
    if official_name and official_name == price_name:
        score += 100
    if get_image_keys(
        official_item.get("image"),
        official_item.get("id"),
    ) & get_image_keys(
        price_item.get("card_image"),
        price_item.get("card_image_id"),
    ):
        score += 80
    if normalize_text(official_item.get("rarity")) == normalize_text(price_item.get("rarity")):
        score += 10
    if normalize_text(official_item.get("type")) == normalize_text(price_item.get("card_type")):
        score += 5
    if normalize_text(official_item.get("color")) == normalize_text(price_item.get("card_color")):
        score += 5
    return score


def build_card_record(code, official_item=None, price_item=None):
    official_item = official_item or {}
    price_item = price_item or {}
    name = get_first_value(price_item.get("card_name"), official_item.get("name"), "")
    official_image = f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png"
    image_url = official_image
    if not official_item:
        image_url = price_item.get("card_image") or official_image

    rarity = get_first_value(price_item.get("rarity"), official_item.get("rarity"), "Common")
    return {
        "id": get_first_value(official_item.get("id"), price_item.get("card_image_id")),
        "code": code,
        "game": "One Piece",
        "name": html.unescape(str(name)).strip(),
        "imageUrl": image_url,
        "price": get_price(price_item),
        "inventoryPrice": get_number(price_item.get("inventory_price")),
        "marketPrice": get_number(price_item.get("market_price")),
        "currency": "USD",
        "rarity": rarity,
        "setName": get_first_value(price_item.get("set_name"), official_item.get("set")),
        "setId": get_first_value(price_item.get("set_id")),
        "type": get_first_value(price_item.get("card_type"), official_item.get("type")),
        "effect": get_first_value(price_item.get("card_text"), official_item.get("effect")),
        "cost": get_number(price_item.get("card_cost"), official_item.get("cost")),
        "power": get_number(price_item.get("card_power"), official_item.get("power")),
        "counter": get_number(price_item.get("counter_amount"), official_item.get("counter")),
        "life": get_number(price_item.get("life"), official_item.get("life")),
        "color": get_first_value(price_item.get("card_color"), official_item.get("color")),
        "attribute": get_first_value(price_item.get("attribute"), official_item.get("attribute")),
        "class": get_first_value(price_item.get("sub_types"), official_item.get("class")),
        "priceDate": price_item.get("date_scraped"),
    }

def main():
    cards_list = get_all_cards_from_api()

    if not cards_list or not isinstance(cards_list, list):
        print("ALERTA DE SEGURIDAD: No se pudo obtener la lista de cartas de la API.")
        sys.exit(1)

    print(f"Total de registros descargados de la API: {len(cards_list)}")

    try:
        price_cards = get_all_cards_with_prices()
    except (OSError, ValueError, requests.RequestException) as price_err:
        print(f"Error al descargar el catálogo de precios: {price_err}")
        sys.exit(1)

    print(f"Total de registros descargados de la API de precios: {len(price_cards)}")

    if SAVE_RAW_FILES:
        try:
            with open(RAW_OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(cards_list, f, indent=2, ensure_ascii=False)
            with open(PRICE_RAW_OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(price_cards, f, indent=2, ensure_ascii=False)
        except (OSError, TypeError) as write_err:
            print(f"Error al escribir los archivos raw: {write_err}")
            sys.exit(1)

    official_by_code = {}
    price_by_code = {}
    for item in cards_list:
        code = str(item.get("code") or item.get("card_number") or item.get("number") or "").strip().upper()
        if code and item.get("name") and "placeholder" not in normalize_text(item.get("name")):
            official_by_code.setdefault(code, []).append(item)
    for item in price_cards:
        code = str(item.get("card_set_id") or "").strip().upper()
        if code and item.get("card_name"):
            price_by_code.setdefault(code, []).append(item)

    grouped_cards = {}
    all_codes = list(official_by_code)
    all_codes.extend(code for code in price_by_code if code not in official_by_code)
    for code in all_codes:
        official_items = official_by_code.get(code, [])
        price_items = price_by_code.get(code, [])
        unmatched_official = list(official_items)
        merged_records = []

        for price_item in price_items:
            best_item = None
            best_score = 0
            for official_item in unmatched_official:
                score = match_score(official_item, price_item)
                if score > best_score:
                    best_item = official_item
                    best_score = score
            if best_item is not None and best_score >= 100:
                unmatched_official.remove(best_item)
                merged_records.append(build_card_record(code, best_item, price_item))
            else:
                merged_records.append(build_card_record(code, None, price_item))

        merged_records.extend(build_card_record(code, item) for item in unmatched_official)
        if not merged_records:
            continue

        base_record = merged_records[0]
        base_record["variants"] = []
        for index, variant in enumerate(merged_records[1:], start=1):
            suffix = f"_P{index}"
            variant["id"] = variant.get("id") or f"{code}{suffix}"
            variant["suffix"] = suffix
            base_record["variants"].append(variant)
        grouped_cards[code] = base_record

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base agrupadas correctamente: {total_base_cards}")

    if total_base_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_base_cards} cartas base. Abortando.")
        publish_generated_files(include_cards_file=False)
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(grouped_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_base_cards} cartas estructuradas correctamente en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

    publish_generated_files()

if __name__ == "__main__":
    main()
