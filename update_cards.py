import os
import sys
import json
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

def get_base_cards():
    print("Descargando cartas base de One Piece TCG...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()

def get_market_prices():
    print("Obteniendo precios de mercado actualizados desde fuente alternativa...")
    prices_dict = {}
    try:
        # Fuente pública alternativa de datos y precios para One Piece TCG
        url = "https://raw.githubusercontent.com/punk-records/one-piece-prices/main/prices.json"
        
        # Como alternativa por si el repositorio usa otra estructura, probamos un endpoint público de respaldo
        response = requests.get(url, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            # Si el JSON es un diccionario de tipo {"OP01-001": {"price": 25.0}, ...} o una lista
            if isinstance(data, dict):
                for card_id, info in data.items():
                    price = info.get("price") or info.get("market_price", 0.0)
                    prices_dict[card_id] = float(price)
            elif isinstance(data, list):
                for card in data:
                    card_id = card.get("id") or card.get("card_id")
                    price = card.get("price") or card.get("market_price", 0.0)
                    if card_id:
                        prices_dict[card_id] = float(price)
            print(f"Precios cargados correctamente: {len(prices_dict)} registros.")
        else:
            print(f"⚠️ La fuente alternativa devolvió el código HTTP: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Aviso: No se pudieron obtener los precios externos ({e}). Las cartas se mantendrán sin precio de mercado por ahora.")
        
    return prices_dict

def main():
    try:
        base_cards = get_base_cards()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo base: {e}")
        sys.exit(1)

    market_prices = get_market_prices()
    
    updated_cards = {}

    for card_id, item in base_cards.items():
        name = item.get("name") or item.get("title")
        if not card_id or not name or "Placeholder" in name:
            continue

        card_code = card_id.strip()
        image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        rarity = item.get("rarity", "C")
        
        # Asignamos el precio real de mercado si lo encontró la fuente, o 0.0
        price = market_prices.get(card_code, float(item.get("price", 0.00)))

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
    except Exception as e:
        print(f"Error al escribir el archivo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
