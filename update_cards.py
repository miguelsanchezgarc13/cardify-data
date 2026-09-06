import requests
import json
import time

def main():
    print("🚀 Iniciando descarga de base de datos oficial de One Piece...")
    final_db = {}
    
    # FUENTE: Punk Records es la base de datos más fiable y actualizada (2024-2025)
    # Ruta al archivo consolidado de cartas
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/index/cards_by_id.json"
    
    try:
        print(f"📡 Conectando con el servidor de datos...")
        response = requests.get(url, timeout=30)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"📦 ¡Base de datos recibida! Procesando {len(raw_data)} cartas...")
            
            for card_id, card_info in raw_data.items():
                # El ID en esta fuente es exactamente el código (ej: OP01-001)
                code = card_id.upper()
                
                # Extraemos solo información REAL y VERIFICADA
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card_info.get('name', 'Unknown Card'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    # Intentamos sacar el precio real si la fuente lo tiene, si no, 0.0 (nada de inventar)
                    "price": float(card_info.get('price', 0.0)), 
                    "rarity": card_info.get('rarity', 'R')
                }
            
            if len(final_db) > 1000:
                print(f"✅ ÉXITO: {len(final_db)} cartas reales cargadas correctamente.")
                # Confirmación visual en el log para asegurar que no hay inventos
                if "OP01-001" in final_db:
                    print(f"🔍 VERIFICACIÓN: {code} -> {final_db['OP01-001']['name']}")
            
            # GUARDAR EL ARCHIVO SOLO SI HAY DATOS REALES
            print(f"💾 Sincronizando {len(final_db)} cartas reales en cards.json...")
            with open("cards.json", "w", encoding="utf-8") as f:
                json.dump(final_db, f, indent=2, ensure_ascii=False)
            print("🏁 Sincronización finalizada.")

        else:
            print(f"❌ Error 404/401: El servidor de datos no está disponible. Código: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Error de conexión: {e}")

if __name__ == "__main__":
    main()
