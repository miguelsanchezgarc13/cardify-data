import json
import requests
import sys
import os

# Configuración de Seguridad
MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

class TCGUpdater:
    def __init__(self, game_name):
        self.game_name = game_name
        self.headers = {
            # Cabecera fundamental para evitar bloqueos básicos 403
            "User-Agent": "CardifyApp-GitHubActions/1.0 (Contact: admin@cardify.app)",
            "Accept": "application/json"
        }

    def fetch_data(self):
        raise NotImplementedError("Debe implementarse en la clase hija")

class OnePieceUpdater(TCGUpdater):
    def __init__(self):
        super().__init__("One Piece")
        # URL DE DATOS: Aquí debes poner la API de precios (ej. TCGPlayer API) 
        # o la URL Raw de GitHub de algún repositorio comunitario actualizado.
        self.data_url = os.getenv("OP_API_URL", "https://api.ejemplo-comunidad-tcg.com/v1/onepiece/cards")

    def fetch_data(self):
        print(f"Descargando datos de {self.game_name}...")
        try:
            response = requests.get(self.data_url, headers=self.headers, timeout=15)
            response.raise_for_status()
            raw_data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error crítico de red al obtener {self.game_name}: {e}")
            sys.exit(1) # Salida 1 aborta el GitHub Action (falla el paso)

        cards_dict = {}

        for item in raw_data:
            card_id = item.get("id")
            name = item.get("name")
            
            # RESTRICCIÓN: Si no hay nombre oficial o es un placeholder, lo saltamos.
            if not name or "Character" in name or "Placeholder" in name or not card_id:
                continue

            # REQUISITO: URL de imagen oficial construida dinámicamente
            image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_id}.png"
            
            cards_dict[card_id] = {
                "code": card_id,
                "game": self.game_name,
                "name": name,
                "imageUrl": image_url,
                "price": float(item.get("market_price", 0.00)),
                "rarity": item.get("rarity", "C")
            }
            
        return cards_dict

def main():
    updaters = [
        OnePieceUpdater(),
        # En el futuro simplemente añades: PokemonUpdater(), NarutoUpdater()
    ]
    
    final_database = {}

    for updater in updaters:
        game_data = updater.fetch_data()
        final_database.update(game_data)

    # REQUISITO DE SEGURIDAD: Comprobación de volumen antes de guardar
    total_cards = len(final_database)
    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas. "
              f"El umbral es {MIN_CARDS_THRESHOLD}. Abortando para proteger el JSON actual.")
        sys.exit(1)

    # Guardado seguro
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_database, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error al escribir el archivo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
