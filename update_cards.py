import html
import json
import re
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
DEFAULT_PRICE_USD = 0.05  # Precio por defecto en dólares para cartas comunes sin valor listado

def get_base_cards():
    print("Descargando catálogo maestro de One Piece TCG (punk-records)...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()

def fetch_api_prices():
    """
    Se conecta a la API comunitaria para obtener los precios de mercado en USD.
    NOTA: Deberás ajustar la URL exacta ('https://optcgapi.com/api/cards') y los nombres 
    de los campos ('market_price', 'card_id') según la documentación oficial de la API que utilices.
    """
    print("Obteniendo precios de mercado globales (USD)...")
    prices_dict = {}
    
    # URL de ejemplo basada en tu investigación. Ajustar si el endpoint es distinto (ej. /v1/cards)
    api_url = "https://optcgapi.com/api/cards" 
    
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        # Si la API requiere paginación, puedes añadir un bucle while aquí como hacíamos con Shopify
        response = requests.get(api_url, headers=headers, timeout=20)
        
        if response.status_code == 200:
            data = response.json()
            # Asumimos que la API devuelve un array de cartas o un objeto con una clave 'data'
            cards_array = data.get("data", data) if isinstance(data, dict) else data
            
            for card in cards_array:
                # Ajustar las claves según el JSON exacto que devuelva la API
                card_id = card.get("id", "").upper()
                
                # Buscar el precio. Algunas APIs lo anidan dentro de "prices" o "tcgplayer"
                market_price = card.get("marketPrice") or card.get("market_price") or 0.0
                
                if card_id and float(market_price) > 0:
                    prices_dict[card_id] = float(market_price)
                    
        print(f"-> Precios únicos extraídos de la API: {len(prices_dict)}")
    except Exception as e:
        print(f"⚠️ Error al consultar la API de precios: {e}. Se usarán precios por defecto.")
        
    return prices_dict

def extract_image_url(item, fallback_code):
    for field in ["img_full_url", "image", "img_url", "imageUrl"]:
        url = item.get(field)
        if url and isinstance(url, str) and url.strip():
            return url.strip()
    return f"https://en.onepiece-cardgame.com/images/cardlist/card/{fallback_code}.png"

def main():
    try:
        base_cards = get_base_cards()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo maestro: {e}")
        sys.exit(1)

    # Obtenemos los precios en USD desde la API
    market_prices = fetch_api_prices()
    
    raw_base_cards = {}
    raw_variants = []

    # Clasificamos entre cartas base y variantes según la presencia de sufijos
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
        name = html.unescape(item.get("name") or item.get("title") or "")
        rarity = item.get("rarity", "C")
        image_url = extract_image_url(item, card_code)
        price = market_prices.get(card_code, DEFAULT_PRICE_USD)

        updated_cards[card_code] = {
            "code": card_code,
            "game": "One Piece",
            "name": name,
            "imageUrl": image_url,
            "price": price,
            "currency": "USD",  # Indicador de divisa añadido
            "rarity": rarity,
            "variants": []
        }

    # 2. Procesar variantes y anidarlas en su carta base
    for var_id, item in raw_variants:
        base_match = re.match(r'^([A-Z]{2,3}\d{2}-\d{3})', var_id)
        if not base_match:
            continue
        base_code = base_match.group(1)

        if base_code in updated_cards:
            suffix = var_id.replace(base_code, "")
            var_name = html.unescape(item.get("name") or item.get("title") or updated_cards[base_code]["name"])
            var_rarity = item.get("rarity", updated_cards[base_code]["rarity"])
            
            var_image_url = extract_image_url(item, base_code)
            var_price = market_prices.get(var_id, DEFAULT_PRICE_USD)

            updated_cards[base_code]["variants"].append({
                "id": var_id,
                "suffix": suffix,
                "name": var_name,
                "imageUrl": var_image_url,
                "price": var_price,
                "currency": "USD",
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
        print(f"Éxito: {total_cards} cartas con estructura anidada guardadas en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
