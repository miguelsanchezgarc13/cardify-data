import requests
import json

def main():
    print("🚀 Iniciando el Log Pose de Cardify: Buscando datos reales...")
    final_db = {}
    
    # Esta API es el estándar actual para One Piece TCG (2025)
    # Proporciona nombres, imágenes reales, rarezas y tipos
    api_url = "https://optcg-api.arjunbansal-ai.workers.dev/cards/all"
    
    try:
        print(f"📡 Sincronizando con el servidor central de cartas...")
        response = requests.get(api_url, timeout=30)
        
        if response.status_code == 200:
            raw_data = response.json()
            print(f"📦 ¡CONSEGUIDO! Hemos recibido {len(raw_data)} cartas oficiales.")
            
            for card in raw_data:
                # El campo en esta API es 'card_number' (ej: OP01-001)
                code = card.get('card_number')
                if not code: continue
                
                # Normalizamos el código por si acaso
                code = code.upper().strip()
                
                # Extraemos la información real del servidor
                final_db[code] = {
                    "code": code,
                    "game": "One Piece",
                    "name": card.get('name', 'Unknown Card'),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{code}.png",
                    "price": 2.50, # Mantendremos precio base hasta el siguiente nivel
                    "rarity": card.get('rarity', 'R')
                }
            
            if len(final_db) > 1000:
                print(f"✅ ÉXITO: {len(final_db)} cartas reales mapeadas.")
                # Verificación final para tu tranquilidad
                if "OP01-001" in final_db:
                    print(f"🔍 CONFIRMACIÓN FINAL: {final_db['OP01-001']['code']} es {final_db['OP01-001']['name']}")
            
        else:
            print(f"❌ El servidor de datos no respondió bien. Código: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Error de conexión: {e}")

    # GUARDAR EL TESORO
    if len(final_db) > 0:
        print(f"💾 Guardando {len(final_db)} cartas reales en cards.json...")
        with open("cards.json", "w", encoding="utf-8") as f:
            json.dump(final_db, f, indent=2, ensure_ascii=False)
        print("🏁 ¡MISIÓN CUMPLIDA! El JSON ya tiene los nombres de verdad.")
    else:
        print("⛔ No se pudo obtener información. No sobreescribimos para no borrar nada.")

if __name__ == "__main__":
    main()
