import html
import json
import os
import re
import subprocess
import sys
import requests

MIN_CARDS_THRESHOLD = 500
OUTPUT_FILE = "cards.json"
RAW_OUTPUT_FILE = "cards_api_raw.json"
DEFAULT_PRICE_USD = 0.05


def publish_generated_files(include_cards_file=True):
    files_to_add = [RAW_OUTPUT_FILE]
    if include_cards_file:
        files_to_add.append(OUTPUT_FILE)

    branch_name = os.environ.get("GITHUB_REF_NAME")
    if not branch_name:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()

    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", "--", *files_to_add], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en los archivos generados; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar catálogo de cartas"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", f"HEAD:{branch_name}"], check=True)
        print(f"Archivos publicados en la rama {branch_name}: {', '.join(files_to_add)}")
    except (OSError, subprocess.CalledProcessError) as publish_err:
        print(f"Error al publicar los archivos generados en GitHub: {publish_err}")
        sys.exit(1)

def get_all_cards_from_api():
    print("Descargando catálogo completo y precios de optcgapi.com...")
    base_url = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, /"
    }
    
    all_cards = []
    page = 1
    
    while True:
        url = f"{base_url}?page={page}"
        print(f"-> Solicitando página {page}...")
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                break
                
            data = response.json()
            cards_chunk = data.get("data", data.get("results", [])) if isinstance(data, dict) else data
                
            if not cards_chunk:
                break
                
            all_cards.extend(cards_chunk)
            if len(cards_chunk) < 10:  
                break
                
            page += 1
        except Exception as e:
            print(f"⚠️ Error al conectar con la API en la página {page}: {e}")
            break
            
    return all_cards

def main():
    cards_list = get_all_cards_from_api()

    if not cards_list or not isinstance(cards_list, list):
        print("ALERTA DE SEGURIDAD: No se pudo obtener la lista de cartas de la API.")
        sys.exit(1)

    print(f"Total de registros descargados de la API: {len(cards_list)}")

    try:
        with open(RAW_OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(cards_list, f, indent=2, ensure_ascii=False)
        print(f"Respuesta cruda de la API guardada en {RAW_OUTPUT_FILE}.")
    except (OSError, TypeError) as write_err:
        print(f"Error al escribir la respuesta cruda de la API: {write_err}")
        sys.exit(1)

    grouped_cards = {}
    variant_counters = {}

    for item in cards_list:
        # BUSCAMOS EL CÓDIGO OFICIAL REAL (ej. OP01-001, EB01-001) en lugar del ID interno aleatorio
        card_code = (
            item.get("card_number") or 
            item.get("number") or 
            item.get("code") or 
            ""
        ).strip().upper()

        # Si el formato del código no parece un código de TCG válido (ej. formato antiguo con guion), lo filtramos
        if not re.match(r'^[A-Z]{2,4}\d{2}-\d{3}', card_code):
            # Intentamos buscar si viene en otro campo o descartamos si no es un código válido
            continue

        raw_name = item.get("name") or item.get("title") or ""
        name = html.unescape(raw_name).strip()
        
        if not card_code or not name or "Placeholder" in name:
            continue

        rarity = item.get("rarity", "Common")
        
        # URL de imagen oficial limpia basada en el código real de la carta
        image_url = item.get("image_url") or item.get("imageUrl") or f"https://en.onepiece-cardgame.com/images/cardlist/card/{card_code}.png"
        
        # Obtener precio en USD
        price = 0.0
        for p_key in ["price", "marketPrice", "market_price"]:
            if p_key in item and item[p_key] is not None:
                try:
                    price = float(item[p_key])
                    break
                except (ValueError, TypeError):
                    continue
        if price <= 0:
            price = DEFAULT_PRICE_USD

        # Detectar variantes mediante los paréntesis en el nombre
        is_variant = "(" in name and ")" in name

        if not is_variant:
            # CARTA BASE
            grouped_cards[card_code] = {
                "code": card_code,
                "game": "One Piece",
                "name": name,
                "imageUrl": image_url,
                "price": price,
                "currency": "USD",
                "rarity": rarity,
                "variants": []
            }
        else:
            # VARIANTE ANIDADA
            base_code = card_code
            if base_code not in grouped_cards:
                grouped_cards[base_code] = {
                    "code": base_code,
                    "game": "One Piece",
                    "name": name.split("(")[0].strip(),
                    "imageUrl": f"https://en.onepiece-cardgame.com/images/cardlist/card/{base_code}.png",
                    "price": DEFAULT_PRICE_USD,
                    "currency": "USD",
                    "rarity": rarity,
                    "variants": []
                }

            if base_code not in variant_counters:
                variant_counters[base_code] = 1
            else:
                variant_counters[base_code] += 1
            
            var_index = variant_counters[base_code]
            suffix = f"_P{var_index}"
            var_unique_id = f"{base_code}{suffix}"

            grouped_cards[base_code]["variants"].append({
                "id": var_unique_id,
                "suffix": suffix,
                "name": name,
                "imageUrl": image_url,
                "price": price,
                "currency": "USD",
                "rarity": rarity
            })

    total_base_cards = len(grouped_cards)
    print(f"Total de cartas base agrupadas correctamente: {total_base_cards}")

    if total_base_cards < MIN_CARDS_THRESHOLD:
        print(f"ALERTA DE SEGURIDAD: Solo se procesaron {total_base_cards} cartas base. Abortando.")
        publish_generated_files(include_cards_file=False)
        sys.exit(1)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(grouped_cards, f, indent=2, ensure_ascii=False)
        print(f"Éxito: {total_base_cards} cartas estructuradas correctamente en {OUTPUT_FILE}.")
    except Exception as write_err:
        print(f"Error al escribir el archivo: {write_err}")
        sys.exit(1)

    publish_generated_files()

if __name__ == "__main__":
    main()
