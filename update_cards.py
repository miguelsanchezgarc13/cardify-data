import json
import re
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
DEFAULT_PRICE = 0.02  # Precio por defecto para cartas comunes sin stock listado

def get_base_cards():
    print("Descargando catálogo base de One Piece TCG...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()

def fetch_shopify_store(base_url, store_name):
    print(f"Obteniendo precios de mercado desde: {store_name}...")
    prices_dict = {}
    page = 1
    
    while True:
        url = f"{base_url}?limit=250&page={page}"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                break
                
            data = response.json()
            products = data.get("products", [])
            if not products:
                break
                
            for product in products:
                title = product.get("title", "")
                variants = product.get("variants", [])
                if variants:
                    try:
                        price = float(variants[0].get("price", 0.0))
                    except ValueError:
                        continue
                    
                    # Regex mejorado para capturar tanto el código base como variantes con sufijo (ej. OP01-001_P1)
                    matches = re.findall(r'([A-Z]{2,3}\d{2}-\d{3}(?:_[A-Za-z0-9]+)?)', title.upper())
                    for code in matches:
                        if price > 0:
                            prices_dict[code] = price
            
            if len(products) < 250:
                break
            page += 1
        except Exception as e:
            print(f"⚠️ Aviso al consultar {store_name} (página {page}): {e}")
            break
            
    print(f"-> Precios extraídos de {store_name}: {len(prices_dict)}")
    return prices_dict

def main():
    try:
        base_cards = get_base_cards()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo base: {e}")
        sys.exit(1)

    # Pasada 1: El Duelista
    prices_duelista = fetch_shopify_store(
        "https://www.elduelista.com/collections/one-piece-single/products.json", 
        "El Duelista"
    )
    
    # Pasada 2: Pokemillon
    prices_pokemillon = fetch_shopify_store(
        "https://www.pokemillon.com/collections/cartas-sueltas-one-piece-1/products.json", 
        "Pokemillon"
    )

    # Fusionamos ambas fuentes de precios
    market_prices = prices_pokemillon.copy()
    market_prices.update(prices_duelista)
    print(f"Total combinado de precios únicos de mercado: {len(market_prices)}")
    
    raw_base_cards = {}
    raw_variants = []

    # Clasificamos la BBDD externa entre cartas base y variantes según la presencia de sufijos
    for card_id, item in base_cards.items():
        name = item.get("name") or item.get("title")
        if not card_id or not name or "Placeholder" in name:
            continue

        card_id_clean = card_id.strip().upper()
        if "_" in card_id_clean:
            raw_variants.append((card_id_clean, item))
        else:
            raw_base_cards[card_id_clean] = item

    updated_cards = {}

    # 1. Procesar cartas base
    for card_code, item in raw_base_cards.items():
        name = item.get("name") or item.get("title")
        rarity = item.get("rarity", "C")
        image_url = item.get("image") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        price = market_prices.get(card_code, DEFAULT_PRICE)

        updated_cards[card_code] = {
            "code": card_code,
            "game": "One Piece",
            "name": name,
            "imageUrl": image_url,
            "price": price,
            "rarity": rarity,
            "variants": []
        }

    # 2. Procesar variantes y anidarlas en su carta base correspondiente
    for var_id, item in raw_variants:
        base_match = re.match(r'^([A-Z]{2,3}\d{2}-\d{3})', var_id)
        if not base_match:
            continue
        base_code = base_match.group(1)

        if base_code in updated_cards:
            suffix = var_id.replace(base_code, "")
            var_name = item.get("name") or item.get("title") or updated_cards[base_code]["name"]
            var_rarity = item.get("rarity", updated_cards[base_code]["rarity"])
            var_image_url = item.get("image") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{var_id.lower()}.png"
            var_price = market_prices.get(var_id, DEFAULT_PRICE)

            updated_cards[base_code]["variants"].append({
                "id": var_id,
                "suffix": suffix,
                "name": var_name,
                "imageUrl": var_image_url,
                "price": var_price,
                "rarity": var_rarity
            })

    total_cards = len(updated_cards)
    print(f"Total de cartas base procesadas: {total_cards}")

    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas base. Abortando.")
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(updated_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas base con sus variantes anidadas guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
