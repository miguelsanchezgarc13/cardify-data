import html
import json
import re
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
DEFAULT_PRICE_USD = 0.05  # Precio por defecto en USD para cartas comunes sin valor listado

def get_all_cards_from_api():
    print("Descargando catálogo completo y precios de optcgapi.com...")
    base_url = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    
    all_cards = []
    page = 1
    
    while True:
        url = f"{base_url}?page={page}"
        print(f"-> Solicitando página {page}...")
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"⚠️ La API respondió con código {response.status_code} en la página {page}.")
                break
                
            data = response.json()
            
            # La API suele estructurar la respuesta paginada dentro de una clave o como lista directa
            cards_chunk = []
            if isinstance(data, dict):
                cards_chunk = data.get("data", data.get("results", []))
            elif isinstance(data, list):
                cards_chunk = data
                
            if not cards_chunk:
                break
                
            all_cards.extend(cards_chunk)
            
            # Si el bloque devuelto es pequeño, asumimos que hemos llegado al final
            if len(cards_chunk) < 10:  
                break
                
            page += 1
        except Exception as e:
            print(f"⚠️ Error al conectar con la API en la página {page}: {e}")
            break
            
    return all_cards

def main():
    cards_list = get_all_cards_from_api()

    if not cards_list or not isinstance(cards_list, list):
        print(f"ALERTA DE SEGURIDAD: No se pudo obtener la lista de cartas de la API.")
        sys.exit(1)

    print(f"Total de registros descargados de la API: {len(cards_list)}")

    grouped_cards = {}
    variant_counters = {}

    for item in cards_list:
        card_code = (item.get("id") or item.get("card_id") or "").strip().upper()
        raw_name = item.get("name") or item.get("title") or ""
        name = html.unescape(raw_name).strip()
        
        if not card_code or not name or "Placeholder" in name:
            continue

        rarity = item.get("rarity", "Common")
        image_url = item.get("image_url") or item.get("imageUrl") or item.get("img_full_url") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        
        # Obtener precio en USD
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

        # Detectar variantes mediante los paréntesis en el nombre (ej. "Kouzuki Oden (Alternate Art)")
        is_variant = "(" in name and ")" in name

        if not is_variant:
            # CARTA BASE
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
            # VARIANTE ANIDADA
            base_code = card_code
            if base_code not in grouped_cards:
                grouped_cards[base_code] = {
                    "code": base_code,
                    "game": "One Piece",
                    "name": name.split("(")[0].strip(),
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

            grouped_cards[base_code]["variants"].append({
                "id": var_unique_id,
                "suffix": suffix,
                "name": name,  # Nombre descriptivo completo, ej: "Kouzuki Oden (Alternate Art)"
                "imageUrl": image_url,
                "price": price,
                "currency": "USD",
                "rarity": rarity
            })

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base agrupadas: {total_base_cards}")

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
