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

RAW_DIR = "raw"

RAW_SOURCES = {
    "cardmarket_prices_raw.json": CARDMARKET_PRICES_URL,
    "cardmarket_cards_raw.json": CARDMARKET_CARDS_URL,
}

OFFICIAL_RAW_FILE = "official_cards_raw.json"
OPTCGAPI_RAW_FILE = "optcgapi_prices_raw.json"

PUBLISH_FILES = [
    os.path.join(RAW_DIR, "cardmarket_cards_raw.json"),
    os.path.join(RAW_DIR, "cardmarket_prices_raw.json"),
    os.path.join(RAW_DIR, OFFICIAL_RAW_FILE),
    os.path.join(RAW_DIR, OPTCGAPI_RAW_FILE),
]

HEADERS = {
    "User-Agent": "OPTCG-App-Data-Discovery/1.0",
}


def run_git(args, capture_output=False):
    try:
        return subprocess.run(
            ["git", *args],
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Error ejecutando git {' '.join(args)}: {error}")
        if isinstance(error, subprocess.CalledProcessError):
            if error.stdout:
                print(error.stdout)
            if error.stderr:
                print(error.stderr)
        sys.exit(1)


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
    Descarga la API oficial página a página y conserva cada respuesta completa.
    No transforma los registros ni elimina metadatos de paginación.
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

    for output_file, source_url in RAW_SOURCES.items():
        print(f"Descargando fuente Cardmarket: {source_url}")
        downloaded_data[output_file] = fetch_json(source_url)

    print("Descargando API oficial completa...")
    downloaded_data[OFFICIAL_RAW_FILE] = fetch_official_raw_pages()

    print("Descargando API OPTCGAPI de precios...")
    downloaded_data[OPTCGAPI_RAW_FILE] = fetch_json(PRICE_API_URL)

    return downloaded_data


def save_raw_data(downloaded_data):
    os.makedirs(RAW_DIR, exist_ok=True)

    for output_file, data in downloaded_data.items():
        output_path = os.path.join(RAW_DIR, output_file)

        try:
            with open(output_path, "w", encoding="utf-8") as output_handle:
                json.dump(data, output_handle, indent=2, ensure_ascii=False)
                output_handle.write("\n")
        except (OSError, TypeError) as error:
            print(f"Error al escribir {output_path}: {error}")
            sys.exit(1)

        print(f"RAW guardado: {output_path}")


def print_structure_summary(data):
    print("\nResumen de RAW descargados:")

    for output_file, source_data in data.items():
        output_path = os.path.join(RAW_DIR, output_file)

        if output_file == OFFICIAL_RAW_FILE and isinstance(source_data, list):
            card_count = 0
            for page in source_data:
                chunk = page.get("data", []) if isinstance(page, dict) else page
                if isinstance(chunk, list):
                    card_count += len(chunk)

            print(
                f"- {output_path}: {len(source_data)} páginas, "
                f"{card_count} tarjetas."
            )

        elif isinstance(source_data, list):
            print(f"- {output_path}: {len(source_data)} registros.")

        elif isinstance(source_data, dict):
            print(
                f"- {output_path}: objeto con "
                f"{len(source_data)} claves principales."
            )

        else:
            print(
                f"- {output_path}: raíz de tipo "
                f"{type(source_data).__name__}."
            )


def get_branch_name():
    branch_name = os.environ.get("GITHUB_REF_NAME")

    if branch_name:
        return branch_name

    result = run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
    )
    branch_name = result.stdout.strip()

    if not branch_name or branch_name == "HEAD":
        print(
            "No se pudo determinar una rama Git válida. "
            "En GitHub Actions debería existir GITHUB_REF_NAME."
        )
        sys.exit(1)

    return branch_name


def verify_remote_commit(branch_name):
    local_head = run_git(
        ["rev-parse", "HEAD"],
        capture_output=True,
    ).stdout.strip()

    remote_result = run_git(
        ["ls-remote", "origin", f"refs/heads/{branch_name}"],
        capture_output=True,
    )

    remote_line = remote_result.stdout.strip()

    if not remote_line:
        print(
            f"No se encontró la rama remota origin/{branch_name} "
            "después del push."
        )
        sys.exit(1)

    remote_head = remote_line.split()[0]

    if remote_head != local_head:
        print("ERROR: el commit remoto no coincide con el commit local.")
        print(f"Local : {local_head}")
        print(f"Remoto: {remote_head}")
        sys.exit(1)

    print(f"Push verificado correctamente en origin/{branch_name}.")
    print(f"Commit publicado: {local_head}")


def print_github_location(branch_name):
    repository = os.environ.get("GITHUB_REPOSITORY")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    if repository:
        print(
            "\nLos RAW están publicados en:"
        )
        print(
            f"{server_url}/{repository}/tree/{branch_name}/{RAW_DIR}"
        )
    else:
        print(
            f"\nLos RAW están publicados en la carpeta '{RAW_DIR}/' "
            f"de la rama '{branch_name}'."
        )


def publish_raw_data():
    branch_name = get_branch_name()

    print(f"\nPublicando RAW en la rama: {branch_name}")

    run_git(["config", "user.name", "github-actions[bot]"])
    run_git(
        [
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ]
    )

    run_git(["add", "--", *PUBLISH_FILES])

    changes = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        check=False,
    )

    if changes.returncode == 0:
        print(
            "Los RAW no han cambiado respecto al commit actual; "
            "no es necesario crear un nuevo commit."
        )

        # Aunque no haya cambios, comprobamos que los cuatro archivos
        # están realmente versionados en Git.
        for path in PUBLISH_FILES:
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", path],
                text=True,
                capture_output=True,
            )
            if tracked.returncode != 0:
                print(
                    f"ERROR: {path} existe localmente pero no está "
                    "versionado en Git."
                )
                sys.exit(1)

        verify_remote_commit(branch_name)
        print_github_location(branch_name)
        return

    if changes.returncode != 1:
        print(
            "No se pudo determinar correctamente si existen "
            "cambios preparados para commit."
        )
        sys.exit(1)

    run_git(
        [
            "commit",
            "-m",
            "chore: actualizar raws de fuentes",
        ]
    )

    print("Enviando commit a GitHub...")

    try:
        subprocess.run(
            [
                "git",
                "push",
                "origin",
                f"HEAD:{branch_name}",
            ],
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print("\nERROR: GitHub rechazó el push.")
        print(
            "Si esto se ejecuta desde GitHub Actions, comprueba que "
            "el workflow tenga:"
        )
        print("permissions:")
        print("  contents: write")
        print(f"\nDetalle: {error}")
        sys.exit(1)

    verify_remote_commit(branch_name)
    print_github_location(branch_name)


def main():
    raw_data = fetch_all_raw_data()
    save_raw_data(raw_data)
    print_structure_summary(raw_data)
    publish_raw_data()


if __name__ == "__main__":
    main()
