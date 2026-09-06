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
    # Forzamos un límite alto por página si la API lo permite, o iteramos masivamente
    per_page = 100 
    
    while True:
        url = f"{base_url}?page={page}&limit={per_page}"
        print(f"-> Descargando bloque de páginas (Página {page})...")
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"⚠️ Fin de la paginación o código de estado {response.status_code}")
                break
                
            data = response.json()
            
            # Extraer los datos según la estructura de la API
            cards_chunk = []
            if isinstance(data, dict):
                cards_chunk = data.get("data", data.get("results", data.get("cards", [])))
            elif isinstance(data, list):
                cards_chunk = data
                
            if not cards_chunk:
                break
                
            all_cards.extend(cards_chunk)
            
            # Si el bloque recibido es menor que el límite, hemos llegado al final
            if len(cards_chunk) < per_page:
                break
                
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

    print(f"Total de registros brutos descargados: {len(cards_list)}")

    grouped_cards = {}
    variant_counters = {}

    for item in cards_list:
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

        # Detectar si esta entrada concreta es una variante (contiene paréntesis en el nombre)
        is_variant = "(" in name and ")" in name

        if not is_variant:
            # Es la CARTA BASE oficial
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
                # Si ya existía la base, actualizamos sus datos principales por si acaso
                grouped_cards[card_code]["name"] = name
                grouped_cards[card_code]["price"] = price
                grouped_cards[card_code]["imageUrl"] = image_url
                grouped_cards[card_code]["rarity"] = rarity
        else:
            # Es una VARIANTE (ej. Alternate Art, SPR...)
            if card_code not in grouped_cards:
                # Creamos una base temporal por si la API listó antes la variante que la carta normal
                base_clean_name = name.split("(")[0].strip()
                grouped_cards[card_code] = {
                    "code": card_code,
                    "game": "One Piece",
                    "name": base_clean_name,
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png",
                    "price": DEFAULT_PRICE_USD,
                    "currency": "USD",
                    "rarity": rarity,
                    "variants": []
                }

            # Asignar sufijo único incremental para las variantes de este código
            if card_code not in variant_counters:
                variant_counters[card_code] = 1
            else:
                variant_counters[card_code] += 1
            
            var_index = variant_counters[card_code]
            suffix = f"_P{var_index}"
            var_unique_id = f"{card_code}{suffix}"

            # Evitar duplicar exactamente la misma variante si la API repite registros
            existing_variants = [v["name"] for v in grouped_cards[card_code]["variants"]]
            if name not in existing_variants:
                grouped_cards[card_code]["variants"].append({
                    "id": var_unique_id,
                    "suffix": suffix,
                    "name": name,  # Nombre descriptivo completo (ej: "Kouzuki Oden (Alternate Art)")
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
