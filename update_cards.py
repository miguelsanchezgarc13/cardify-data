import requests
import json

def main():
    print("🚀 Iniciando el buscador de tesoros de Cardify...")
    final_db = {}
    
    # ESTA URL ES LA CLAVE: Es el repositorio más activo de datos de One Piece TCG
    url = "https://raw.githubusercontent.com/mrcat-onepiece/onepiece-cardgame-data/main/data/cards.json"
    
    try:
        print(f"📡 Intentando conectar con: {url}")
        response = requests.get(url, timeout=20)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"✅ ¡Conseguido! Hemos recibido {len(raw_data)} cartas.")
            
            for card in raw_data:
                # En este archivo el código es 'id' y el nombre es 'name'
                code = card.get('id')
                if not code: continue
                
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Card'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 2.0, 
                    "rarity": card.get('rarity', 'R')
                }
            
            if len(final_db) > 500:
                print(f"🎉 ¡ÉXITO! Se han cargado {len(final_db)} nombres reales.")
                # Confirmación visual de Zoro
                if "OP01-001" in final_db:
                    print(f"🔍 CONFIRMADO: {final_db['OP01-001']['code']} es {final_db['OP01-001']['name']}")
            return final_db
        else:
            print(f"❌ Error 404: La URL ha vuelto a cambiar. Código: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Fallo en la conexión: {e}")

    # GENERADOR DE EMERGENCIA (Solo si internet falla)
    print("🛠️ Generando base de datos de respaldo...")
    expansions = ["OP01", "OP02", "OP03", "OP04", "OP05", "OP06", "OP07", "OP08", "OP09", "OP10", "OP11", "ST01", "EB01"]
    for exp in expansions:
        for i in range(1, 130):
            code = f"{exp}-{str(i).zfill(3)}"
            if code not in final_db:
                final_db[code] = {
                    "code": code, "game": "One Piece", "name": f"Character {code}", 
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 3.0, "rarity": "R"
                }
    return final_db

def main_runner():
    cards = main()
    if cards:
        print(f"💾 Guardando {len(cards)} cartas en cards.json...")
        with open("cards.json", "w", encoding="utf-8") as f:
            json.dump(cards, f, indent=2, ensure_ascii=False)
        print("🏁 Misión completada.")

if __name__ == "__main__":
    main_runner()
