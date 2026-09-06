import os
import sys
import json
import re
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

def get_base_cards():
    print("Descargando catálogo base de One Piece TCG...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()

def get_shopify_store_prices():
    print("Obteniendo precios reales de mercado (Tienda externa - Shopify)...")
    prices_dict = {}
    page = 1
    
    # Bucle para recorrer la paginación de la tienda de forma automatizada
    while True:
        url = f"https://www.elduelista.com/collections/one-piece-single/products.json?limit=250&page={page}"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                break
                
            data = response.json()
            products = data.get("products", [])
            if not products:
                break
                
            print(f"Página {page} procesada: {len(products)} productos encontrados.")
            
            for product in products:
                title = product.get("title", "")
                variants = product.get("variants", [])
                if variants:
                    try:
                        price = float(variants[0].get("price", 0.0))
                    except ValueError:
                        continue
                    
                    # Expresión regular para detectar códigos oficiales de cartas (ej: OP01-001, EB01-001)
                    matches = re.findall(r'([A-Z]{2,3}\d{2}-\d{3})', title.upper())
                    for code in matches:
                        prices_dict[code] = price
            
            # Si devuelve menos de 250 productos, hemos llegado al final del catálogo
            if len(products) < 250:
                break
            page += 1
        except Exception as e:
            print(f"⚠️ Aviso al consultar la página {page} de precios: {e}")
            break
            
    print(f"Total de precios mapeados con éxito: {len(prices_dict)}")
    return prices_dict

def main():
    try:
        base_cards = get_base_cards()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo base: {e}")
        sys.exit(1)

    # Obtenemos los precios de mercado en directo
    market_prices = get_shopify_store_prices()
    
    updated_cards = {}

    for card_id, item in base_cards.items():
        name = item.get("name") or item.get("title")
        if not card_id or not name or "Placeholder" in name:
            continue

        card_code = card_id.strip().upper()
        image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        rarity = item.get("rarity", "C")
        
        # Inyectamos el precio real si la tienda lo tiene listado; si no, queda a 0.0 temporalmente
        price = market_prices.get(card_code, 0.0)

        updated_cards[card_code] = {
            "code": card_code,
            "game": "One Piece",
            "name": name,
            "imageUrl": image_url,
            "price": price,
            "rarity": rarity
        }

    total_cards = len(updated_cards)
    print(f"Total de cartas válidas procesadas: {total_cards}")

    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas. Abortando.")
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(updated_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
