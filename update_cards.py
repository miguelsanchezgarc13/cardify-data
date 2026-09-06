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

    def fetch_data(self):
        raise NotImplementedError("Debe implementarse en la clase hija")

class OnePieceUpdater(TCGUpdater):
    def __init__(self):
        super().__init__("One Piece")
        # Credenciales de TCGplayer inyectadas desde GitHub Secrets
        self.client_id = os.getenv("TCGPLAYER_CLIENT_ID")
        self.client_secret = os.getenv("TCGPLAYER_CLIENT_SECRET")
        self.base_url = "https://api.tcgplayer.com"
        self.category_id = 71 # ID oficial de la categoría One Piece en TCGplayer
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "CardifyApp-GitHubActions/1.0",
            "Accept": "application/json"
        })

    def _authenticate(self):
        print(f"[{self.game_name}] Autenticando con TCGplayer...")
        auth_url = f"{self.base_url}/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        response = self.session.post(auth_url, data=payload, timeout=10)
        
        if response.status_code != 200:
            print("Error crítico: Falló la autenticación con TCGplayer. Verifica tus credenciales.")
            sys.exit(1)
            
        token = response.json().get("access_token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def fetch_data(self):
        if not self.client_id or not self.client_secret:
            print("Error: Credenciales de TCGplayer no encontradas en las variables de entorno.")
            sys.exit(1)

        self._authenticate()
        
        print(f"[{self.game_name}] Descargando catálogo de productos...")
        products_url = f"{self.base_url}/catalog/products"
        offset = 0
        limit = 100
        total_items = 1
        
        cards_dict = {}
        product_ids = []

        # 1. Obtener el catálogo completo (paginado)
        while offset < total_items:
            params = {
                "categoryId": self.category_id,
                "offset": offset,
                "limit": limit,
                "getExtendedFields": "true" # Necesario para obtener el código (ej. OP01-001)
            }
            
            response = self.session.get(products_url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            total_items = data.get("totalItems", 0)
            
            for item in data.get("results", []):
                name = item.get("name", "")
                prod_id = item.get("productId")
                
                # Extraer el código oficial (OP01-001) y rareza del extendedData
                card_code = None
                rarity = "C"
                for ext in item.get("extendedData", []):
                    if ext.get("name") == "Number":
                        card_code = ext.get("value")
                    elif ext.get("name") == "Rarity":
                        rarity = ext.get("value")
                
                # RESTRICCIÓN: Solo guardamos si tiene código oficial y un nombre válido
                if not card_code or not name or "Placeholder" in name:
                    continue
                
                # Limpiar códigos (a veces TCGplayer añade sufijos raros)
                card_code = card_code.strip()
                
                # URL de imagen oficial construida dinámicamente
                image_url = f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
                
                cards_dict[prod_id] = {
                    "code": card_code,
                    "game": self.game_name,
                    "name": name,
                    "imageUrl": image_url,
                    "price": 0.00, # Lo rellenaremos en el paso 2
                    "rarity": rarity
                }
                product_ids.append(str(prod_id))
                
            offset += limit

        # 2. Obtener los precios en lotes (TCGplayer permite máximo 250 IDs por petición)
        print(f"[{self.game_name}] Catálogo descargado. Obteniendo precios de {len(product_ids)} cartas...")
        batch_size = 250
        final_database = {}

        for i in range(0, len(product_ids), batch_size):
            batch_ids = ",".join(product_ids[i:i+batch_size])
            pricing_url = f"{self.base_url}/pricing/product/{batch_ids}"
            
            price_response = self.session.get(pricing_url, timeout=15)
            price_response.raise_for_status()
            price_data = price_response.json().get("results", [])
            
            for p_item in price_data:
                prod_id = p_item.get("productId")
                # Usamos el Market Price. Si es nulo, intentamos usar un precio de referencia bajo
                market_price = p_item.get("marketPrice") or p_item.get("lowPrice") or 0.00
                
                # TCGplayer devuelve precios para distintas calidades (Normal, Foil, Holofoil).
                # Solo actualizamos si el precio es mayor a 0 para quedarnos con el valor real.
                if prod_id in cards_dict and market_price > 0:
                    card_code = cards_dict[prod_id]["code"]
                    # Evitamos sobreescribir con versiones baratas si ya procesamos una versión cara (ej. paralela)
                    # o simplemente asignamos el primer precio válido que encontremos
                    if card_code not in final_database:
                        cards_dict[prod_id]["price"] = float(market_price)
                        final_database[card_code] = cards_dict[prod_id]

        return final_database

def main():
    updaters = [
        OnePieceUpdater(),
        # PokemonUpdater(), # Listo para expandirse
    ]
    
    final_database = {}

    for updater in updaters:
        try:
            game_data = updater.fetch_data()
            final_database.update(game_data)
        except Exception as e:
            print(f"Error procesando {updater.game_name}: {e}")
            sys.exit(1)

    # REQUISITO: Comprobación de volumen antes de guardar (Seguridad)
    total_cards = len(final_database)
    if total_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_cards} cartas. "
              f"El umbral es {MIN_CARDS_THRESHOLD}. Abortando guardado.")
        sys.exit(1)

    # Guardado seguro del JSON
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_database, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_cards} cartas guardadas correctamente en {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error al escribir el archivo: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
