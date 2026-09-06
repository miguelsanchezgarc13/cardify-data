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
    print("Obteniendo precios de mercado actualizados...")
    prices_dict = {}
    try:
        url = "https://optcg-api.arjunbansal-ai.workers.dev/cards/all"
        response = requests.get(url, timeout=15)
        print(f"Estado de la API de precios: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"Tipo de datos recibidos de precios: {type(data)}")
            cards_list = data if isinstance(data, list) else data.get("cards", [])
            print(f"Total de precios encontrados en la API externa: {len(cards_list)}")
            
            for card in cards_list:
                card_id = card.get("id") or card.get("card_id")
                price = card.get("price") or card.get("market_price", 0.0)
                if card_id:
                    prices_dict[card_id] = float(price)
        else:
            print(f"⚠️ La API de precios devolvió el código HTTP: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Error detallado al conectar con la API de precios: {e}")
        
    return prices_dict

def main():
    try:
        base_cards = get_base_cards()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo base: {e}")
        sys.exit(1)

    market_prices = get_market_prices()
    print(f"Diccionario de precios cargado con éxito ({len(market_prices)} precios mapeados).")
    
    updated_cards = {}

    for card_id, item in base_cards.items():
        name = item.get("name") or item.get("title")
        if not card_id or not name or "Placeholder" in name:
            continue

        card_code = card_id.strip()
        image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        rarity = item.get("rarity", "C")
        
        # Asignamos el precio real de mercado si existe en el diccionario
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
