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
OFFICIAL_API_URL = "https://optcg-api.ryanmichaelhirst.us/api/v1/cards"
PRICE_API_URL = "https://www.optcgapi.com/api/allSetCards/"

CARDMARKET_RAW_SOURCES = {
    "cardmarket_prices_raw.json": CARDMARKET_PRICES_URL,
    "cardmarket_cards_raw.json": CARDMARKET_CARDS_URL,
}

OFFICIAL_RAW_FILE = "official_cards_raw.json"
OPTCGAPI_RAW_FILE = "optcgapi_prices_raw.json"

PUBLISH_FILES = [
    *CARDMARKET_RAW_SOURCES,
    OFFICIAL_RAW_FILE,
    OPTCGAPI_RAW_FILE,
]

HEADERS = {
    "User-Agent": "OPTCG-App-Data-Discovery/1.0",
}


def fetch_json(url, params=None):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        print(f"Error de red descargando {url}: {error}")
        sys.exit(1)
    except ValueError as error:
        print(f"La respuesta de {url} no contiene un JSON válido: {error}")
        sys.exit(1)


def fetch_official_raw_pages():
    """
    Descarga la API oficial página a página y conserva cada respuesta tal cual.

    El archivo official_cards_raw.json será una lista de respuestas de página:
    [
      {respuesta JSON de la página 1},
      {respuesta JSON de la página 2},
      ...
    ]

    Así no perdemos metadatos de paginación ni transformamos los registros.
    """
    pages = []
    page = 1
    per_page = 100

    while True:
        print(f"Descargando API oficial: página {page}...")
        data = fetch_json(
            OFFICIAL_API_URL,
            params={"page": page, "per_page": per_page},
        )
        pages.append(data)

        chunk = data.get("data", []) if isinstance(data, dict) else data
        if not isinstance(chunk, list):
            print(
                "La API oficial devolvió una estructura inesperada: "
                "no se encontró una lista de tarjetas."
            )
            sys.exit(1)

        if not chunk:
            break

        total_pages = data.get("total_pages") if isinstance(data, dict) else None
        if total_pages is not None:
            try:
                if page >= int(total_pages):
                    break
            except (TypeError, ValueError):
                print(
                    "La API oficial devolvió un total_pages no válido; "
                    "se continuará usando el tamaño de página."
                )
                total_pages = None

        if total_pages is None and len(chunk) < per_page:
            break

        page += 1

    return pages


def fetch_all_raw_data():
    downloaded_data = {}

    for output_file, source_url in CARDMARKET_RAW_SOURCES.items():
        print(f"Descargando fuente Cardmarket: {source_url}")
        downloaded_data[output_file] = fetch_json(source_url)

    print("Descargando API oficial completa...")
    downloaded_data[OFFICIAL_RAW_FILE] = fetch_official_raw_pages()

    print("Descargando API OPTCGAPI de precios...")
    downloaded_data[OPTCGAPI_RAW_FILE] = fetch_json(PRICE_API_URL)

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
        if output_file == OFFICIAL_RAW_FILE and isinstance(source_data, list):
            card_count = 0
            for page in source_data:
                chunk = page.get("data", []) if isinstance(page, dict) else page
                if isinstance(chunk, list):
                    card_count += len(chunk)

            print(
                f"{output_file}: {len(source_data)} respuestas de página, "
                f"{card_count} tarjetas."
            )
        elif isinstance(source_data, list):
            print(f"{output_file}: {len(source_data)} registros.")
        elif isinstance(source_data, dict):
            print(
                f"{output_file}: objeto con "
                f"{len(source_data)} claves principales."
            )
        else:
            print(
                f"{output_file}: tipo de raíz "
                f"{type(source_data).__name__}."
            )


def publish_raw_data():
    branch_name = os.environ.get("GITHUB_REF_NAME")
    if not branch_name:
        try:
            branch_name = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"No se pudo determinar la rama de Git: {error}")
            sys.exit(1)

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ],
            check=True,
        )

        subprocess.run(["git", "add", "--", *PUBLISH_FILES], check=True)

        changes = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changes.returncode == 0:
            print("No hay cambios en los RAW; no se crea ningún commit.")
            return

        subprocess.run(
            ["git", "commit", "-m", "chore: actualizar raws de fuentes"],
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch_name}"],
            check=True,
        )

        print(
            "Archivos RAW publicados: "
            + ", ".join(PUBLISH_FILES)
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Error al publicar los RAW: {error}")
        sys.exit(1)


def main():
    raw_data = fetch_all_raw_data()
    save_raw_data(raw_data)
    print_structure_summary(raw_data)
    publish_raw_data()


if __name__ == "__main__":
    main()
