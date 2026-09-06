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
    # Endpoint público que devuelve todas las cartas de una sola vez
    url = "https://optcg-api.ryanmichaelhirst.us/cards"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def main():
    try:
        raw_data = get_all_cards_from_api()
    except Exception as e:
        print(f"Error crítico al descargar datos de la API: {e}")
        sys.exit(1)

    # La API puede devolver una lista directa o un objeto con las cartas dentro de una clave
    cards_list = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data

    if not cards_list:
        print("ALERTA DE SEGURIDAD: La API no devolvió ninguna carta.")
        sys.exit(1)

    # Diccionario temporal para agrupar cartas base y sus variantes por código (ej. "EB01-001")
    grouped_cards = {}
    
    # Contador para autogenerar sufijos únicos en las variantes si comparten código base
    variant_counters = {}

    for item in cards_list:
        # Extraer campos principales de la API
        card_code = (item.get("id") or item.get("card_id") or "").strip().upper()
        raw_name = item.get("name") or item.get("title") or ""
        name = html.unescape(raw_name).strip()
        
        if not card_code or not name or "Placeholder" in name:
            continue

        rarity = item.get("rarity", "Common")
        image_url = item.get("image_url") or item.get("imageUrl") or item.get("img_full_url") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        
        # Obtener precio en USD (buscando en varias claves comunes de la API)
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

        # Detectar si es una variante analizando si el nombre contiene paréntesis (ej. "Kouzuki Oden (Alternate Art)")
        is_variant = "(" in name and ")" in name

        if not is_variant:
            # Es la CARTA BASE
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
            # Es una VARIANTE (ej. Alternate Art, SPR, etc.)
            # Buscamos si ya existe su carta base en el diccionario
            base_code = card_code
            if base_code not in grouped_cards:
                # Si la carta base no se procesó antes, creamos una entrada preliminar para evitar perderla
                grouped_cards[base_code] = {
                    "code": base_code,
                    "game": "One Piece",
                    "name": name.split("(")[0].strip(), # Limpiamos el nombre base
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{base_code}.png",
                    "price": DEFAULT_PRICE_USD,
                    "currency": "USD",
                    "rarity": rarity,
                    "variants": []
                }

            # Gestionar sufijo único para la variante (ej. _P1, _P2...) basándose en cuántas lleva
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
                "name": name,  # Aquí se guardará el nombre completo molón, ej: "Kouzuki Oden (Alternate Art)"
                "imageUrl": image_url,
                "price": price,
                "currency": "USD",
                "rarity": rarity
            })

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base procesadas: {total_base_cards}")

    if total_base_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_base_cards} cartas base. Abortando.")
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(grouped_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_base_cards} cartas con estructura anidada y nombres descriptivos guardadas en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
