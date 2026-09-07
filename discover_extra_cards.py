import json
import os
import subprocess
import sys

import requests


COMMUNITY_JSON_URL = (
    "https://raw.githubusercontent.com/sstockdev/optcgdb/main/out/global/cards.json"
)
RAW_OUTPUT_FILE = "optcgdb_extra_cards_raw.json"


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
        subprocess.run(["git", "add", "--", RAW_OUTPUT_FILE], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en el RAW comunitario; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar datos de optcgdb"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", f"HEAD:{branch_name}"], check=True)
        print(f"RAW comunitario publicado: {RAW_OUTPUT_FILE}")
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Error al publicar el RAW comunitario: {error}")
        sys.exit(1)


def fetch_community_data():
    print(f"Descargando fuente comunitaria: {COMMUNITY_JSON_URL}")
    headers = {"User-Agent": "OPTCG-App-Data-Discovery/1.0"}

    try:
        response = requests.get(
            COMMUNITY_JSON_URL,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Error de red durante la descarga: {error}")
        sys.exit(1)

    try:
        return response.json()
    except ValueError as error:
        print(f"La respuesta no contiene un JSON válido: {error}")
        sys.exit(1)


def save_raw_data(data):
    try:
        with open(RAW_OUTPUT_FILE, "w", encoding="utf-8") as output_file:
            json.dump(data, output_file, indent=2, ensure_ascii=False)
    except (OSError, TypeError) as error:
        print(f"Error al escribir {RAW_OUTPUT_FILE}: {error}")
        sys.exit(1)

    print(f"Datos crudos guardados en {RAW_OUTPUT_FILE}.")


def print_structure_summary(data):
    if isinstance(data, list):
        print(f"Registros encontrados: {len(data)}")
        if data:
            print("Campos del primer registro:")
            print(json.dumps(data[0], indent=2, ensure_ascii=False)[:2000])
        return

    if isinstance(data, dict):
        print(f"La raíz es un objeto. Claves encontradas: {list(data)[:20]}")
        return

    print(f"Tipo de raíz no esperado: {type(data).__name__}")


def main():
    community_data = fetch_community_data()
    save_raw_data(community_data)
    publish_raw_data()
    print_structure_summary(community_data)


if __name__ == "__main__":
    main()
