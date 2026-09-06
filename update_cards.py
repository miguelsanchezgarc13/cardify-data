import requests
import json

def main():
    print("🚀 Iniciando el motor de datos real de Cardify (Versión 2025)...")
    final_db = {}
    
    # ESTA ES LA FUENTE CLAVE: Servidor de datos de la comunidad One Piece TCG
    # Proporciona el JSON masivo con nombres oficiales
    url = "https://optcg.gg/api/cards"
    
    try:
        print(f"📡 Sincronizando con el servidor central...")
        # Añadimos un User-Agent para que no nos bloqueen como robot
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"📦 ¡CONSEGUIDO! Hemos recibido {len(raw_data)} cartas oficiales.")
            
            for card in raw_data:
                # En esta fuente, el código suele ser 'card_number'
                code = card.get('card_number')
                if not code: continue
                
                # Extraemos la información REAL sin inventar nada
                final_db[code.upper()] = {
                    "code": code.upper(),
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Name'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code.upper()}.png",
                    "price": 2.50, # Precio base (siguiente nivel: scraper de Cardmarket)
                    "rarity": card.get('rarity', 'R')
                }
            
            if len(final_db) > 1000:
                print(f"✅ ÉXITO TOTAL: {len(final_db)} cartas mapeadas con nombres reales.")
                # Confirmación visual en el log para nuestra tranquilidad
                if "OP01-001" in final_db:
                    print(f"🔍 VERIFICACIÓN: {final_db['OP01-001']['code']} -> {final_db['OP01-001']['name']}")
        else:
            print(f"❌ Error de conexión: {response.status_code}. El servidor ha denegado el acceso.")
            
    except Exception as e:
        print(f"⚠️ Error técnico: {e}")

    # GUARDAR SOLO SI TENEMOS DATOS REALES
    if len(final_db) > 0:
        print(f"💾 Guardando {len(final_db)} cartas reales en cards.json...")
        with open("cards.json", "w", encoding="utf-8") as f:
            json.dump(final_db, f, indent=2, ensure_ascii=False)
        print("🏁 Base de datos actualizada con éxito.")
    else:
        print("⛔ No se pudo obtener información oficial. Abortando para no borrar nada.")

if __name__ == "__main__":
    main()
