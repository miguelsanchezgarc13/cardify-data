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
                print(f"⚠️ La API respondió con código {response.status_code} en la página {page}. Fin de la descarga.")
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

    grouped_cards = {}
    variant_counters = {}

    for item in cards_list:
        if not isinstance(item, dict):
            continue

        # Código oficial base (ej. OP01-047)
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
        
        # Extraer precio de la API de forma segura
        price = 0.0
        for p_key in ["price", "marketPrice", "market_price"]:
            if p_key in item and item[p_key] is not None:
                try:
                    price = float(item[p_key])
                    break
                except (ValueError, TypeError):
                    continue
        if price <= 0:
            price = DEFAULT_PRICE_USD

        # Detectar si es variante por los paréntesis (ej. "(Parallel)", "(SP)")
        is_variant = "(" in name and ")" in name

        if not is_variant:
            # --- ES LA CARTA BASE ---
            if card_code not in grouped_cards:
                grouped_cards[card_code] = {
                    "code": card_code,
                    "game": "One Piece",
                    "name": name,
                    "imageUrl": image_url,
                    "price": price,
                    "currency": "USD",
                    "rarity": rarity,
                    "variants": []
                }
            else:
                # Si ya existía la base pero se registró sin precio o queremos asegurar datos
                grouped_cards[card_code]["name"] = name
                grouped_cards[card_code]["price"] = price
                grouped_cards[card_code]["imageUrl"] = image_url
                grouped_cards[card_code]["rarity"] = rarity
        else:
            # --- ES UNA VARIANTE ANIDADA ---
            base_code = card_code
            
            # Asegurar que la estructura base exista aunque la API devuelva la variante primero
            if base_code not in grouped_cards:
                clean_base_name = name.split("(")[0].strip()
                grouped_cards[base_code] = {
                    "code": base_code,
                    "game": "One Piece",
                    "name": clean_base_name,
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{base_code}.png",
                    "price": DEFAULT_PRICE_USD,
                    "currency": "USD",
                    "rarity": rarity,
                    "variants": []
                }

            if base_code not in variant_counters:
                variant_counters[base_code] = 1
            else:
                variant_counters[base_code] += 1
            
            var_index = variant_counters[base_code]
            suffix = f"_P{var_index}"
            var_unique_id = f"{base_code}{suffix}"

            # Evitar duplicar la misma variante si la API la repite
            existing_variant_names = [v["name"] for v in grouped_cards[base_code]["variants"]]
            if name not in existing_variant_names:
                grouped_cards[base_code]["variants"].append({
                    "id": var_unique_id,
                    "suffix": suffix,
                    "name": name,
                    "imageUrl": image_url,
                    "price": price,
                    "currency": "USD",
                    "rarity": rarity
                })

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base agrupadas correctamente: {total_base_cards}")

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
