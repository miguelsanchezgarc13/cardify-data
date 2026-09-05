import requests
import json

def main():
    print("🚀 Iniciando motor de datos Cardify (Versión Blindada)...")
    final_db = {}
    
    # DICCIONARIO MAESTRO (Nombres garantizados aunque falle internet)
    master_names = {
        "OP01-001": "Roronoa Zoro",
        "OP01-016": "Nami",
        "ST01-001": "Monkey.D.Luffy",
        "OP10-014": "Franky Chopper",
        "OP11-053": "Tony Tony.Chopper",
        "EB02-016": "Chopperman",
        "OP01-002": "Trafalgar Law",
        "OP01-003": "Monkey.D.Luffy (Leader)",
        "OP01-013": "Sanji",
        "OP01-025": "Roronoa Zoro (Character)"
    }

    # 1. Intentar descargar nombres reales de una fuente alternativa estable
    try:
        print("📡 Intentando descargar base de datos comunitaria...")
        # Esta URL es un espejo de datos muy estable
        url = "https://raw.githubusercontent.com/mrcat-onepiece/onepiece-cardgame-data/main/data/cards.json"
        response = requests.get(url, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            print(f"📦 ¡Conexión exitosa! Mapeando {len(data)} cartas...")
            for card in data:
                code = card.get('id')
                if code:
                    final_db[code] = {
                        "code": code,
                        "game": "One Piece",
                        "name": card.get('name', f"Character {code}"),
                        "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                        "price": 3.0,
                        "rarity": card.get('rarity', 'R')
                    }
    except Exception as e:
        print(f"⚠️ Nota: No se pudo conectar a la base externa ({e}). Usaremos el generador.")

    # 2. Generador Maestro + Inyección de Nombres Maestros
    print("🛠️ Reforzando base de datos con expansiones y nombres conocidos...")
    expansions = ["OP01", "OP02", "OP03", "OP04", "OP05", "OP06", "OP07", "OP08", "OP09", "OP10", "OP11", "ST01", "EB01", "EB02"]
    
    for exp in expansions:
        for i in range(1, 130):
            code = f"{exp}-{str(i).zfill(3)}"
            
            # Si la carta ya existe (por la descarga), solo aseguramos que el nombre sea el del maestro
            if code in master_names:
                name = master_names[code]
            elif code in final_db:
                name = final_db[code]['name']
            else:
                name = f"Card {code}"

            final_db[code] = {
                "code": code,
                "game": "One Piece",
                "name": name,
                "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                "price": 2.50,
                "rarity": "R"
            }

    # 3. Guardar el archivo definitivo
    print(f"💾 Guardando {len(final_db)} cartas. ¡Zoro y Chopper están a salvo!")
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(final_db, f, indent=2, ensure_ascii=False)
    
    print("🎉 ¡TODO LISTO! Abre tu JSON y busca a Roronoa Zoro.")

if __name__ == "__main__":
    main()
