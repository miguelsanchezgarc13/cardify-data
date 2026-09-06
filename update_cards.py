import os
import sys
import json
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

def get_base_cards():
    print("Descargando cartas base de One Piece TCG...")
    # URL corregida incluyendo la ruta 'english/'
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()

def get_market_prices():
    print("Obteniendo precios de mercado actualizados...")
    prices_dict = {}
    try:
        # API pública de referencia para precios de One Piece TCG
        response = requests.get("https://optcg-api.arjunbansal-ai.workers.dev/cards/all", timeout=10)
        if response.status_code == 200:
            data = response.json()
            cards_list = data if isinstance(data, list) else data.get("cards", [])
            for card in cards_list:
                card_id = card.get("id") or card.get("card_id")
                price = card.get("price") or card.get("market_price", 0.0)
                if card_id:
                    prices_dict[card_id] = float(price)
    except Exception as e:
        print(f"⚠️ Aviso al obtener precios en línea ({e}), continuando con valores por defecto.")
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
        
        # Asignamos el precio real de mercado o el valor por defecto
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

    # Seguridad: Si no llega al umbral, aborta para no corromper el JSON
    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas (mínimo {MIN_CARDS_THRESHOLD}). Abortando.")
        sys.exit(1)

    # Guardado seguro directo sobre cards.json
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(updated_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error al escribir el archivo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
