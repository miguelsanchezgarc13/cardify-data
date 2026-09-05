import requests
import json

def main():
    print("🚀 Iniciando misión masiva de Cardify...")
    final_db = {}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Lista de fuentes (Si una falla, probamos la siguiente)
    sources = [
        "https://raw.githubusercontent.com/limitless-tcg/optcg-data/main/en/cards.json",
        "https://raw.githubusercontent.com/optcg/optcg-data/main/cards.json"
    ]
    
    for url in sources:
        try:
            print(f"📡 Intentando conectar con: {url}")
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code == 200:
                data = response.json()
                print(f"📦 Recibidos {len(data)} elementos. Mapeando nombres...")
                
                count = 0
                for card in data:
                    # Probamos diferentes formas en que los códigos y nombres vienen guardados
                    code = card.get('id') or card.get('card_number')
                    name = card.get('name')
                    
                    if code and name:
                        final_db[code] = {
                            "code": code,
                            "game": "One Piece",
                            "name": name,
                            "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                            "price": 2.50,
                            "rarity": card.get('rarity', 'R')
                        }
                        count += 1
                
                if count > 100:
                    print(f"✅ ¡ÉXITO! Se han cargado {count} nombres reales de {url}")
                    # Mostramos los primeros 3 para confirmar en el log
                    sample = list(final_db.values())[:3]
                    for s in sample: print(f"   Ejemplo: {s['code']} -> {s['name']}")
                    break # Si ya tenemos datos, no hace falta seguir probando fuentes
            else:
                print(f"❌ Fallo de servidor (Código {response.status_code})")
        except Exception as e:
            print(f"⚠️ Error con esta fuente: {e}")

    # 2. Generador Maestro (Solo para cartas que no tengan nombre real todavía)
    print("🛠️ Completando huecos con el generador maestro...")
    expansions = ["OP01", "OP02", "OP03", "OP04", "OP05", "OP06", "OP07", "OP08", "OP09", "OP10", "OP11", "ST01", "ST02", "ST10", "EB01", "EB02"]
    for exp in expansions:
        for i in range(1, 130):
            code = f"{exp}-{str(i).zfill(3)}"
            if code not in final_db:
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": f"Character {code}", 
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 1.0,
                    "rarity": "R"
                }

    # 3. Guardar el archivo masivo
    print(f"💾 Guardando {len(final_db)} cartas en el cofre (cards.json)...")
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_db, f, indent=2, ensure_ascii=False)
    
    print("🎉 ¡TODO LISTO! El JSON masivo está actualizado.")

if __name__ == "__main__":
    main()
