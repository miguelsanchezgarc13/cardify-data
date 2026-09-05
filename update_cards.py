import requests
import json
import time

def get_one_piece_data():
    # Fuente de datos de alta disponibilidad (Limitless TCG)
    url = "https://raw.githubusercontent.com/limitless-tcg/optcg-data/main/en/cards.json"
    
    for attempt in range(3): # Lo intenta 3 veces si hay fallos de red
        try:
            print(f"🛰️ Intento {attempt + 1}: Conectando con la base de datos...")
            response = requests.get(url, timeout=20)
            if response.status_code == 200:
                raw_data = response.json()
                cards_db = {}
                
                for card in raw_data:
                    # El código en esta fuente es 'id' (OP01-001)
                    code = card.get('id')
                    if not code: continue
                    
                    cards_db[code] = {
                        "code": code,
                        "game": "One Piece",
                        "name": card.get('name', 'Unknown Card'),
                        "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                        "price": 2.0, # Base
                        "rarity": card.get('rarity', 'R')
                    }
                
                # SEGURIDAD: Solo aceptamos si hemos bajado más de 500 cartas
                if len(cards_db) > 500:
                    print(f"✅ ¡ÉXITO TOTAL! Descargadas {len(cards_db)} cartas reales.")
                    return cards_db
                else:
                    print("⚠️ Datos insuficientes descargados. Reintentando...")
            
        except Exception as e:
            print(f"❌ Error en intento {attempt + 1}: {e}")
            time.sleep(2) # Espera antes de reintentar
            
    return {}

def main():
    print("🚀 Iniciando actualización masiva de Cardify...")
    final_database = get_one_piece_data()
    
    if not final_database:
        print("⛔ ERROR CRÍTICO: No se pudo descargar la base de datos masiva.")
        print("Para proteger tu colección, NO guardaremos un archivo vacío.")
        return

    # Guardamos TODA la base de datos (Zoro y todos los demás)
    print(f"💾 Guardando {len(final_database)} cartas en cards.json...")
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_database, f, indent=2, ensure_ascii=False)
    
    print("🎉 ¡SISTEMA ACTUALIZADO AL 100%! Revisa tu JSON masivo.")

if __name__ == "__main__":
    main()
