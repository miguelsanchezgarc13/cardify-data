import requests
import json

def get_one_piece_data():
    print("🚀 Iniciando misión de rescate de datos de One Piece...")
    cards_db = {}
    
    # URL CORREGIDA: Ahora incluye la carpeta /en/ para los nombres en inglés
    url = "https://raw.githubusercontent.com/limitless-tcg/optcg-data/main/en/cards.json"
    
    try:
        response = requests.get(url)
        if response.status_code == 200:
            raw_data = response.json()
            print(f"✅ ¡Conexión establecida! Hemos encontrado {len(raw_data)} cartas.")
            
            for card in raw_data:
                # El ID en esta base de datos viene como 'id' (ej: OP01-001)
                code = card.get('id')
                if not code: continue
                
                # Extraemos el nombre real y la rareza
                cards_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Warrior'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 3.50, # Precio de prueba hasta que montemos el scraper de precios
                    "rarity": card.get('rarity', 'R')
                }
            return cards_db
        else:
            print(f"❌ Error 404: No se encontró el archivo en la nube. Código: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Error inesperado: {e}")
    
    return {}

def main():
    final_database = get_one_piece_data()
    
    if not final_database:
        print("⛔ ALERTA: No hemos recibido datos. Abortamos para proteger tu cards.json.")
        return

    print("💾 Guardando los datos reales en cards.json...")
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_database, f, indent=2, ensure_ascii=False)
    
    print(f"🎉 ¡MISIÓN CUMPLIDA! cards.json actualizado con {len(final_database)} cartas reales.")

if __name__ == "__main__":
    main()
