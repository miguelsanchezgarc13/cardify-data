import requests
import json

def main():
    print("🚀 Iniciando búsqueda en la base de datos Nakama...")
    final_db = {}
    
    # URL de la API de NakamaDecks (la más completa para One Piece)
    url = "https://nakamadecks.com/api/cards"
    
    try:
        print(f"📡 Conectando con Nakama...")
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            data = response.json()
            print(f"📦 ¡Increíble! Hemos recibido {len(data)} cartas.")
            
            for card in data:
                # Nakama usa 'id' para el código (ej: OP01-001)
                code = card.get('id')
                if not code: continue
                
                # Extraemos los datos REALES
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', f"Character {code}"),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": float(card.get('price_avg', 5.0)), # ¡PRECIO REAL PROMEDIO!
                    "rarity": card.get('rarity', 'R')
                }
            
            print(f"✅ ¡ÉXITO! {len(final_db)} nombres y precios reales cargados.")
            # Verificamos a Zoro para darte una alegría en el log
            if "OP01-001" in final_db:
                print(f"   Confirmado: {final_db['OP01-001']['name']} está en la base de datos.")
                
        else:
            print(f"❌ Error de conexión (Código {response.status_code})")
    except Exception as e:
        print(f"⚠️ Fallo crítico: {e}")

    # 2. Generador de Emergencia (Solo si la API falló totalmente)
    if len(final_db) < 100:
        print("🛠️ Usando generador maestro por fallo de red...")
        expansions = ["OP01", "OP02", "OP03", "OP04", "OP05", "OP06", "OP07", "OP08", "OP09", "OP10", "OP11", "ST01", "ST10", "EB01"]
        for exp in expansions:
            for i in range(1, 130):
                code = f"{exp}-{str(i).zfill(3)}"
                if code not in final_db:
                    final_db[code] = {
                        "code": code, "game": "One Piece", "name": f"Character {code}", 
                        "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                        "price": 2.50, "rarity": "R"
                    }

    # 3. Guardar el cofre del tesoro
    print(f"💾 Guardando {len(final_db)} cartas en cards.json...")
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_db, f, indent=2, ensure_ascii=False)
    
    print("🎉 ¡MISIÓN FINALIZADA! Revisa tu JSON ahora.")

if __name__ == "__main__":
    main()
