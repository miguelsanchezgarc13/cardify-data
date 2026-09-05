import requests
import json

def get_one_piece_data():
    print("Iniciando extracción de datos reales de One Piece...")
    cards_db = {}
    
    # URL de una base de datos de One Piece TCG muy fiable y abierta
    url = "https://raw.githubusercontent.com/limitless-tcg/optcg-data/main/cards.json"
    
    try:
        response = requests.get(url)
        # CORRECCIÓN TÉCNICA: Usamos status_code (estándar de Python)
        if response.status_code == 200:
            raw_data = response.json()
            print(f"¡Conexión exitosa! Procesando {len(raw_data)} cartas...")
            
            for card in raw_data:
                code = card.get('id')
                if not code: continue
                
                # Mapeamos los datos reales a nuestro formato de Cardify
                cards_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Name'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 2.50, # Precio base (el scraper de precios reales es el siguiente nivel)
                    "rarity": card.get('rarity', 'R')
                }
            return cards_db
        else:
            print(f"Error: La fuente de datos respondió con código {response.status_code}")
    except Exception as e:
        print(f"Error al conectar con la base de datos: {e}")
    
    return {}

def main():
    # Obtenemos los datos de la "biblioteca" de One Piece
    final_database = get_one_piece_data()
    
    if not final_database:
        print("CUIDADO: No se han obtenido datos. Abortando para no borrar el JSON actual.")
        return

    # Guardamos el resultado en cards.json
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_database, f, indent=2, ensure_ascii=False)
    
    print(f"¡BRUTAL! cards.json actualizado con {len(final_database)} cartas con nombres REALES.")

if __name__ == "__main__":
    main()
