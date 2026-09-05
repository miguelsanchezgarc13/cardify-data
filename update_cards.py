import requests
import json
import os

# 1. Lista de códigos que queremos monitorizar (esto lo podemos automatizar luego para que sean TODOS)
TARGET_CODES = ["OP01-016", "OP10-014", "OP11-053", "EB02-016", "OP01-001", "ST01-001"]

def fetch_card_data(code):
    print(f"Buscando datos para {code}...")
    # Aquí nos conectamos a una fuente de datos (ejemplo: un buscador de precios)
    # Por ahora simulamos la subida/bajada de precios real
    # En una versión avanzada, aquí usaríamos BeautifulSoup para leer la web
    
    # Simulación de consulta:
    base_data = {
        "OP01-016": {"name": "Nami", "rarity": "SR", "img": "https://images.ygoprodeck.com/images/cards/OP01-016.jpg", "price": 15.50},
        "OP10-014": {"name": "Franky Chopper", "rarity": "C", "img": "https://images.ygoprodeck.com/images/cards/OP10-014.jpg", "price": 0.55},
        "OP11-053": {"name": "Tony Tony.Chopper", "rarity": "UC", "img": "https://images.ygoprodeck.com/images/cards/OP11-053.jpg", "price": 1.40},
        "EB02-016": {"name": "Chopperman", "rarity": "C", "img": "https://images.ygoprodeck.com/images/cards/EB02-016.jpg", "price": 0.35},
    }
    
    return base_data.get(code)

def main():
    new_database = {}
    for code in TARGET_CODES:
        data = fetch_card_data(code)
        if data:
            new_database[code] = {
                "code": code,
                "name": data["name"],
                "imageUrl": data["img"],
                "price": data["price"],
                "rarity": data["rarity"]
            }

    # Guardar el resultado en el archivo JSON
    with open("cards.json", "w", encoding="utf-8") as f:
        json.dump(new_database, f, indent=2, ensure_ascii=False)
    print("¡cards.json actualizado con éxito!")

if __name__ == "__main__":
    main()