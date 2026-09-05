import requests
import json

def main():
    print("🚀 Iniciando el Cosechador Real de Cardify...")
    final_db = {}
    
    # Esta es la fuente REAL que usa la comunidad (Limitless TCG data)
    # Contiene nombres, rarezas y códigos exactos
    url = "https://raw.githubusercontent.com/LimitlessTCG/optcg-data/main/cards.json"
    
    try:
        print(f"📡 Conectando con la base de datos de Limitless...")
        response = requests.get(url, timeout=20)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"✅ ¡Conexión exitosa! Procesando {len(raw_data)} cartas...")
            
            for card in raw_data:
                # ¡AQUÍ ESTABA EL SECRETO! Limitless usa 'id' pero con formato 'OP01-001'
                code = card.get('id')
                if not code: continue
                
                # Extraemos la información real
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Name'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 2.50, # Mantener base hasta el scraper de precios
                    "rarity": card.get('rarity', 'R')
                }
            
            if len(final_db) > 100:
                print(f"🎉 ¡ÉXITO TOTAL! Hemos recuperado {len(final_db)} nombres reales.")
                # Confirmación visual en el log para nosotros
                if "OP01-001" in final_db:
                    print(f"🔍 Verificación: {final_db['OP01-001']['code']} es {final_db['OP01-001']['name']}")
        else:
            print(f"❌ Error de servidor: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Error inesperado: {e}")

    # Solo si falló la descarga masiva, rellenamos con genéricos
    if not final_db:
        print("🛠️ Usando generador de emergencia (algo salió mal con internet)...")
        # ... (bucle de emergencia que ya teníamos)

    # GUARDAR EL ARCHIVO
    if len(final_db) > 0:
        print(f"💾 Guardando {len(final_db)} cartas en el JSON de GitHub...")
        with open("cards.json", "w", encoding="utf-8") as f:
            json.dump(final_db, f, indent=2, ensure_ascii=False)
        print("🏁 Proceso finalizado.")

if __name__ == "__main__":
    main()
