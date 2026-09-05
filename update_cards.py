import requests
import json

def main():
    print("🚀 Iniciando conexión con Punk Records Database (2025)...")
    final_db = {}
    
    # Esta es la URL del índice maestro de cartas de One Piece
    url = "https://raw.githubusercontent.com/buhbbl/punk-records/main/index/cards_by_id.json"
    
    try:
        print(f"📡 Descargando índice oficial de nombres...")
        response = requests.get(url, timeout=30)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"📦 ¡Base de datos recibida! Procesando {len(raw_data)} entradas...")
            
            # La estructura es un objeto donde la llave es el ID de la carta
            for card_id, card_data in raw_data.items():
                # El ID viene como OP01-001, lo normalizamos
                code = card_id.upper()
                
                # Extraemos solo la info oficial
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card_data.get('name', f"Card {code}"),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 3.0, # Precio base
                    "rarity": card_data.get('rarity', 'R')
                }
            
            if len(final_db) > 1000:
                print(f"✅ ¡ÉXITO! Se han recuperado {len(final_db)} nombres oficiales.")
                # Verificación para el log
                if "OP01-001" in final_db:
                    print(f"🔍 CONFIRMADO POR EL ROBOT: OP01-001 es {final_db['OP01-001']['name']}")
        else:
            print(f"❌ Error de servidor: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Fallo de conexión: {e}")

    # Si la descarga funcionó, guardamos. Si no, no tocamos nada para no borrar.
    if len(final_db) > 0:
        print(f"💾 Sincronizando {len(final_db)} cartas reales en cards.json...")
        with open("cards.json", "w", encoding="utf-8") as f:
            json.dump(final_db, f, indent=2, ensure_ascii=False)
        print("🏁 Sincronización finalizada con éxito.")
    else:
        print("⛔ No se pudo obtener información oficial. Abortando actualización.")

if __name__ == "__main__":
    main()
