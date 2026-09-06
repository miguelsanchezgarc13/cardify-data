import os
import sys
import json
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

def main():
    print("Descargando catálogo base de One Piece TCG...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"
    
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        base_cards = response.json()
    except Exception as e:
        print(f"Error crítico al descargar el catálogo base: {e}")
        sys.exit(1)

    updated_cards = {}

    for card_id, item in base_cards.items():
        name = item.get("name") or item.get("title")
        if not card_id or not name or "Placeholder" in name:
            continue

        card_code = card_id.strip()
        image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        rarity = item.get("rarity", "C")
        
        # Campo de precio preparado para la estructura de la app
        price = float(item.get("price", 0.00))

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
