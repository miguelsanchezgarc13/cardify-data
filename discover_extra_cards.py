import json
import os
import subprocess
import sys

import requests


CARDMARKET_PRICES_URL = (
    "https://raw.githubusercontent.com/michalkiral/optcg-data-cardmarket/main/"
    "data/prices/summary.json"
)
CARDMARKET_CARDS_URL = (
    "https://raw.githubusercontent.com/michalkiral/optcg-data-cardmarket/main/"
    "data/index/cards_by_id.json"
)
RAW_OUTPUT_FILES = {
    "cardmarket_prices_raw.json": CARDMARKET_PRICES_URL,
    "cardmarket_cards_raw.json": CARDMARKET_CARDS_URL,
}


def publish_raw_data():
    branch_name = os.environ.get("GITHUB_REF_NAME")
    if not branch_name:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()

    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )
        subprocess.run(["git", "add", "--", *RAW_OUTPUT_FILES], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en los RAW de Cardmarket; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar datos de cardmarket"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", f"HEAD:{branch_name}"], check=True)
        print(f"RAW de Cardmarket publicados: {', '.join(RAW_OUTPUT_FILES)}")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Error al publicar los RAW de Cardmarket: {error}")
        sys.exit(1)


def fetch_community_data():
    headers = {
        "User-Agent": "OPTCG-App-Data-Discovery/1.0",
    }

    downloaded_data = {}
    for output_file, source_url in RAW_OUTPUT_FILES.items():
        print(f"Descargando fuente Cardmarket: {source_url}")
        try:
            response = requests.get(source_url, headers=headers, timeout=60)
            response.raise_for_status()
            downloaded_data[output_file] = response.json()
        except requests.RequestException as error:
            print(f"Error de red descargando {output_file}: {error}")
            sys.exit(1)
        except ValueError as error:
            print(f"{output_file} no contiene un JSON válido: {error}")
            sys.exit(1)

    return downloaded_data


def save_raw_data(downloaded_data):
    for output_file, data in downloaded_data.items():
        try:
            with open(output_file, "w", encoding="utf-8") as output_handle:
                json.dump(data, output_handle, indent=2, ensure_ascii=False)
        except (OSError, TypeError) as error:
            print(f"Error al escribir {output_file}: {error}")
            sys.exit(1)

        print(f"Datos crudos guardados en {output_file}.")


def print_structure_summary(data):
    for output_file, source_data in data.items():
        if isinstance(source_data, list):
            print(f"{output_file}: {len(source_data)} registros.")
        elif isinstance(source_data, dict):
            print(f"{output_file}: objeto con {len(source_data)} claves principales.")
        else:
            print(f"{output_file}: tipo de raíz {type(source_data).__name__}.")


def main():
    community_data = fetch_community_data()
    save_raw_data(community_data)
    publish_raw_data()
    print_structure_summary(community_data)


if __name__ == "__main__":
    main()
