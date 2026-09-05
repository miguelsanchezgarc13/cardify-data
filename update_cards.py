import requests
import json

# --- CONFIGURACIÓN ---
GAMES_TO_UPDATE = ["onepiece"] 

def get_one_piece_data():
    print("Iniciando extracción de One Piece...")
    cards_db = {}
    
    # Expansiones actuales
    expansions = ["OP01", "OP02", "ST01", "EB01", "OP10", "OP11"]
    
    for exp in expansions:
        # Generamos un rango de cartas (en el scraper real esto se lee de la web)
        for i in range(1, 20): 
            code = f"{exp}-{str(i).zfill(3)}"
            
            # URL más aproximada a la realidad de One Piece
            # Nota: La oficial suele ser .png y estar en su CDN
            img_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png"
            
            cards_db[code] = {
                "code": code,
                "game": "One Piece", # NUEVO CAMPO
                "name": f"Character {code}", 
                "imageUrl": img_url,
                "price": 5.0, # Precio base para la prueba
                "rarity": "R"
            }
    return cards_db

def main():
    final_database = {}
    
    if "onepiece" in GAMES_TO_UPDATE:
        op_cards = get_one_piece_data()
        final_database.update(op_cards)
        
    # Guardamos todo en el JSON
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_database, f, indent=2, ensure_ascii=False)
    
    print(f"¡Éxito! Base de datos actualizada con {len(final_database)} cartas y campo 'game'.")

if __name__ == "__main__":
    main()
