import json
import requests

# 1. Tu función existente que extrae/descarga las cartas base (nombres, imágenes, códigos)
def get_base_cards():
    print("Descargando cartas base de One Piece TCG...")
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/index/cards_by_id.json"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return {}

# 2. NUEVA FUNCIÓN (Opción 1): Obtener precios de mercado actualizados
def get_market_prices():
    print("Obteniendo precios de mercado actualizados...")
    prices_dict = {}
    try:
        # Puedes usar una API pública o un repositorio comunitario que mantenga precios actualizados
        # Ejemplo: Endpoint o JSON de precios de referencia para One Piece TCG
        response = requests.get("https://optcg-api.arjunbansal-ai.workers.dev/cards/all", timeout=10)
        if response.status_code == 200:
            data = response.json()
            # Asumiendo que devuelve una lista de cartas con su ID y su precio en USD/EUR
            cards_list = data if isinstance(data, list) else data.get("cards", [])
            for card in cards_list:
                card_id = card.get("id") or card.get("card_id")
                price = card.get("price") or card.get("market_price", 0.0)
                if card_id:
                    prices_dict[card_id] = float(price)
    except Exception as e:
        print(f"⚠️ No se pudieron cargar los precios en línea ({e}), usando valores por defecto o caché local.")
    
    return prices_dict

def update_cards():
    # Obtener cartas base
    base_cards = get_base_cards()
    
    # Obtener precios de mercado
    market_prices = get_market_prices()
    
    updated_cards = {}
    
    for card_id, card_data in base_cards.items():
        # Copiamos la data original de la carta
        card_entry = card_data if isinstance(card_data, dict) else {"data": card_data}
        
        # Asignamos el precio real si existe en el diccionario de precios, si no, se queda en 0.0 o un estimado
        real_price = market_prices.get(card_id, 0.0)
        card_entry["price"] = real_price
        
        updated_cards[card_id] = card_entry

    # Guardar el resultado final enriquecido para Cardify
    output_filename = "cards_with_prices.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(updated_cards, f, ensure_ascii=False, indent=4)
        
    print(f"✅ Base de datos de Cardify actualizada con éxito. Guardado en {output_filename} (Total cartas: {len(updated_cards)})")

if __name__ == "__main__":
    update_cards()
