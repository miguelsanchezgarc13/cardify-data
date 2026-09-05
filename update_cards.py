import requests
import json
import time

def get_one_piece_data():
    print("Iniciando extracción de datos reales de One Piece...")
    cards_db = {}
    
    # Usamos la base de datos de 'optcg.gg' que es muy fiable para nombres
    # Intentamos descargar su diccionario completo
    try:
        # Nota: Esta es una URL de una base de datos comunitaria muy usada en scrapers
        url = "https://raw.githubusercontent.com/limitless-tcg/optcg-data/main/cards.json"
        response = requests.get(url)
        
        if response.statusCode == 200:
            raw_data = response.json()
            for card in raw_data:
                code = card.get('id') # Ejemplo: OP01-001
                if not code: continue
                
                # Construimos nuestra estructura Cardify
                cards_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', f"Unknown {code}"),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 0.5, # El precio real requiere un scraper más lento, lo dejamos base
                    "rarity": card.get('rarity', 'R')
                }
            print(f"¡Éxito! Se han importado {len(cards_db)} cartas reales.")
            return cards_db
    except Exception as e:
        print(f"Error al conectar con la base de datos real: {e}")
        # Si falla la base de datos externa, volvemos al modo simulado
        return {}

def main():
    final_database = get_one_piece_data()
    
    # Si por algún motivo falló la descarga, no machacamos el archivo con un vacío
    if not final_database:
        print("Error: No se han podido obtener datos. Abortando actualización.")
        return

    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_database, f, indent=2, ensure_ascii=False)
    
    print("¡Proceso completado!")

if __name__ == "__main__":
    main()
