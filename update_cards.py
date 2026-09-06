import html
import json
import re
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
DEFAULT_PRICE_USD = 0.05

def get_all_cards_from_api():
    print("Descargando catálogo completo y precios de optcgapi.com...")
    base_url = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    
    all_cards = []
    page = 1
    max_pages = 100  
    
    while page <= max_pages:
        url = f"{base_url}?page={page}"
        print(f"-> Solicitando página {page}...")
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"⚠️ La API respondió con código {response.status_code} en la página {page}.")
                break
                
            data = response.json()
            cards_chunk = []
            if isinstance(data, dict):
                cards_chunk = data.get("data", data.get("results", data.get("cards", [])))
            elif isinstance(data, list):
                cards_chunk = data
                
            if not cards_chunk:
                print(f"-> Página {page} vacía. Fin de la paginación.")
                break
                
            all_cards.extend(cards_chunk)
            print(f"   (+{len(cards_chunk)} cartas añadidas. Total acumulado: {len(all_cards)})")
            page += 1
        except Exception as e:
            print(f"⚠️ Error al conectar con la API en la página {page}: {e}")
            break
            
    return all_cards

def main():
    cards_list = get_all_cards_from_api()

    if not cards_list or not isinstance(cards_list, list):
        print("ALERTA DE SEGURIDAD: No se pudo obtener la lista de cartas de la API.")
        sys.exit(1)

    print(f"Total de registros descargados de la API: {len(cards_list)}")

    # Diccionario temporal para agrupar todas las entradas crudas por código de carta
    raw_grouped_by_code = {}

    for item in cards_list:
        if not isinstance(item, dict):
            continue

        card_code = (
            item.get("card_number") or 
            item.get("number") or 
            item.get("code") or 
            ""
        ).strip().upper()

        if not re.match(r'^[A-Z]{2,4}\d{2}-\d{3}', card_code):
            continue

        raw_name = item.get("name") or item.get("title") or ""
        name = html.unescape(raw_name).strip()
        
        if not card_code or not name or "Placeholder" in name:
            continue

        rarity = item.get("rarity", "Common")
        image_url = item.get("image_url") or item.get("imageUrl") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        
        # Extracción robusta de precios
        price = 0.0
        for p_key in ["market_price", "marketPrice", "price", "tcgplayer_price", "cardmarket_price"]:
            val = item.get(p_key)
            if val is not None:
                try:
                    price = float(val)
                    if price > 0:
                        break
                except (ValueError, TypeError):
                    continue
        
        if price <= 0:
            price = DEFAULT_PRICE_USD

        if card_code not in raw_grouped_by_code:
            raw_grouped_by_code[card_code] = []

        raw_grouped_by_code[card_code].append({
            "name": name,
            "imageUrl": image_url,
            "price": price,
            "rarity": rarity
        })

    # Construir el JSON final estructurado con base y variantes reales
    grouped_cards = {}

    for card_code, entries in raw_grouped_by_code.items():
        # Ordenamos las entradas para que la que tenga el nombre más corto (ej. "Trafalgar Law") sea la base
        entries.sort(key=lambda x: len(x["name"]))

        base_entry = entries[0]
        base_name = base_entry["name"].split("(")[0].strip()

        grouped_cards[card_code] = {
            "code": card_code,
            "game": "One Piece",
            "name": base_name,
            "imageUrl": base_entry["imageUrl"],
            "price": base_entry["price"],
            "currency": "USD",
            "rarity": base_entry["rarity"],
            "variants": []
        }

        # Las entradas adicionales se consideran variantes de esa misma carta
        variant_counter = 1
        seen_variant_names = set()

        for entry in entries[1:]:
            var_name = entry["name"]
            if var_name in seen_variant_names:
                continue
            seen_variant_names.add(var_name)

            suffix = f"_P{variant_counter}"
            var_unique_id = f"{card_code}{suffix}"

            grouped_cards[card_code]["variants"].append({
                "id": var_unique_id,
                "suffix": suffix,
                "name": var_name,
                "imageUrl": entry["imageUrl"],
                "price": entry["price"],
                "currency": "USD",
                "rarity": entry["rarity"]
            })
            variant_counter += 1

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base estructuradas correctamente: {total_base_cards}")

    if total_base_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_base_cards} cartas base. Abortando.")
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(grouped_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_base_cards} cartas estructuradas correctamente en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
