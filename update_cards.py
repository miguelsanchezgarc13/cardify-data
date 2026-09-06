import os
import sys
import json
import requests

# Configuración de Seguridad
MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"

class TCGUpdater:
    def __init__(self, game_name):
        self.game_name = game_name
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "CardifyApp-GitHubActions/1.0",
            "Accept": "application/json"
        })

    def fetch_data(self):
        raise NotImplementedError("Debe implementarse en la clase hija")

class OnePieceUpdater(TCGUpdater):
    def __init__(self):
        super().__init__("One Piece")
        # Fuente pública y actualizada del índice general de cartas de One Piece TCG
        self.index_url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english/index/cards_by_id.json"

    def fetch_data(self):
        print(f"[{self.game_name}] Descargando base de datos abierta desde el repositorio comunitario...")
        try:
            response = self.session.get(self.index_url, timeout=20)
            response.raise_for_status()
            raw_data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error crítico de red al obtener datos de {self.game_name}: {e}")
            sys.exit(1)

        cards_dict = {}

        # El índice es un diccionario donde la clave es el ID de la carta (ej. OP01-001)
        for card_id, item in raw_data.items():
            name = item.get("name") or item.get("title")
            
            # RESTRICCIÓN: Si no hay nombre oficial o es un placeholder, lo saltamos
            if not card_id or not name or "Placeholder" in name:
                continue

            card_code = card_id.strip()
            
            # URL de imagen oficial construida dinámicamente según Bandai
            image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
            
            # Extraemos la rareza si viene incluida, o por defecto Common
            rarity = item.get("rarity", "C")
            
            # Precio estimativo/base de mercado provisto por el dataset o 0.00 para empezar
            price = float(item.get("price", 0.00))

            cards_dict[card_code] = {
                "code": card_code,
                "game": self.game_name,
                "name": name,
                "imageUrl": image_url,
                "price": price,
                "rarity": rarity
            }
            
        return cards_dict

def main():
    updaters = [
        OnePieceUpdater(),
        # Aquí podrás añadir en el futuro: PokemonUpdater(), etc.
    ]
    
    final_database = {}

    for updater in updaters:
        game_data = updater.fetch_data()
        final_database.update(game_data)

    # REQUISITO DE SEGURIDAD: Comprobación de volumen antes de guardar
    total_cards = len(final_database)
    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas. "
              f"El umbral mínimo es {MIN_CARDS_THRESHOLD}. Abortando para proteger el JSON actual.")
        sys.exit(1)

    # Guardado seguro del archivo cards.json
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_database, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas procesadas y guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error al escribir el archivo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
