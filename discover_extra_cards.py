#!/usr/bin/env python3
"""
One Piece TCG catalogue pipeline v3.4.

Live sources:
  1) Bandai official card list -> card/game/printing/image metadata
  2) Cardmarket public product catalogue -> product identifiers
  3) Cardmarket public daily price guide -> EUR prices

Persistent local knowledge:
  data/cardmarket_mapping.json -> Bandai printing <-> Cardmarket idProduct

The old community Cardmarket snapshot is used only once, if available, to seed
cardmarket_mapping.json. It is never used as a live price source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import html as html_lib
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    print(
        "Falta beautifulsoup4. Instala dependencias con: "
        "pip install requests beautifulsoup4"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Official/live source URLs
# ---------------------------------------------------------------------------

BANDAI_CARDLIST_URLS = [
    "https://en.onepiece-cardgame.com/cardlist/",
    "https://asia-en.onepiece-cardgame.com/cardlist/",
]
CARDMARKET_PRODUCTS_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/productList/"
    "products_singles_18.json"
)
CARDMARKET_PRICE_GUIDE_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/"
    "price_guide_18.json"
)
CARDMARKET_BASE_URL = "https://www.cardmarket.com"
CARDMARKET_PRODUCT_REDIRECT = "https://www.cardmarket.com/en/OnePiece/Products?idProduct={product_id}"

VEGAPULL_PINNED_VERSION = "1.3.0"
VEGAPULL_MIN_VERSION = (1, 2, 3)

DEFAULT_RAW_DIR = Path("raw")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUTPUT_DIR = Path("output")

RAW_FILENAMES = {
    "bandai": "bandai_cards_raw.json",
    "cardmarket_products": "cardmarket_products_raw.json",
    "cardmarket_prices": "cardmarket_price_guide_raw.json",
}

MAPPING_FILENAME = "cardmarket_mapping.json"
IMAGE_CACHE_FILENAME = "image_health_cache.json"
CATALOG_FILENAME = "cards_multisource_v3.json"
REPORT_FILENAME = "cards_multisource_v3_report.json"
REVIEW_FILENAME = "cardmarket_mapping_review.json"

LEGACY_PRICE_FILENAME = "cardmarket_prices_raw.json"
LEGACY_CARDS_FILENAME = "cardmarket_cards_raw.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; OPTCG-Catalogue/3.4; "
        "+https://github.com/)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

CARD_CODE_RE = re.compile(r"\b(?:OP\d{2}|EB\d{2}|ST\d{2}|PRB\d{2}|P)-\d{3}\b", re.I)
VARIANT_SUFFIX_RE = re.compile(r"(?:_(?:p|r)\d+)+$", re.I)
IMAGE_ID_RE = re.compile(
    r"/([^/?]+?)(?:_EN)?\.(?:png|jpe?g|webp|gif)(?:\?.*)?$", re.I
)
RARITY_CODE_RE = re.compile(r"\b(SEC|SR|UC|SP|TR|L|R|C|P)\b", re.I)
CATEGORY_RE = re.compile(r"\b(LEADER|CHARACTER|EVENT|STAGE|DON!!?)\b", re.I)
VERSION_RE = re.compile(r"\(V\.\s*(\d+)\)", re.I)

RARITY_MAP = {
    "L": "Leader",
    "C": "Common",
    "UC": "Uncommon",
    "R": "Rare",
    "SR": "Super Rare",
    "SEC": "Secret Rare",
    "P": "Promo",
    "SP": "Special",
    "TR": "Treasure Rare",
}

KNOWN_FIELD_LABELS = {
    "life": "Life",
    "cost": "Cost",
    "attribute": "Attribute",
    "power": "Power",
    "counter": "Counter",
    "color": "Color",
    "block icon": "Block Icon",
    "block": "Block Icon",
    "type": "Type",
    "effect": "Effect",
    "trigger": "Trigger",
    "card set(s)": "Card Set(s)",
    "card sets": "Card Set(s)",
    "notes": "Notes",
}


# ---------------------------------------------------------------------------
# CLI and utilities
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Genera el catálogo One Piece TCG desde Bandai oficial y "
            "Cardmarket oficial, usando un mapping persistente de idProduct."
        )
    )
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="No descarga las fuentes principales; reconstruye desde raw/.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Genera archivos pero no hace commit/push a GitHub.",
    )
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="No valida URLs de imagen por HTTP en esta ejecución.",
    )
    parser.add_argument(
        "--image-check-all",
        action="store_true",
        help="Revalida todas las imágenes, ignorando el cache previo.",
    )
    parser.add_argument(
        "--no-auto-map",
        action="store_true",
        help="No añade automáticamente mappings Cardmarket inequívocos nuevos.",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Carpeta raw/.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Carpeta data/ persistente.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Carpeta output/.",
    )
    parser.add_argument(
        "--legacy-cardmarket-prices",
        type=Path,
        default=None,
        help=(
            "Snapshot antiguo de Cardmarket usado SOLO para sembrar el mapping. "
            "Por defecto: raw/cardmarket_prices_raw.json si existe."
        ),
    )
    parser.add_argument(
        "--legacy-cardmarket-cards",
        type=Path,
        default=None,
        help=(
            "Índice histórico de printings Cardmarket usado SOLO para enriquecer "
            "la semilla con set/imagen. Por defecto: raw/cardmarket_cards_raw.json "
            "si existe."
        ),
    )
    parser.add_argument(
        "--bandai-delay",
        type=float,
        default=0.45,
        help="Pausa entre descargas de packs Bandai mediante vega (segundos).",
    )
    parser.add_argument(
        "--vega-bin",
        default=os.environ.get("VEGA_BIN", "vega"),
        help=(
            "Ruta/nombre del binario vega. Si se deja en 'vega' y no existe, "
            "el script instala automáticamente vegapull 1.3.0 con cargo."
        ),
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Ejecuta pruebas locales sin red y termina.",
    )
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_id(value) -> str:
    return str(value or "").strip().upper()


def base_code(value) -> str:
    return VARIANT_SUFFIX_RE.sub("", canonical_id(value))


def normalize_text(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def slugify(value) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-")


def nullable_text(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NULL", "-"}:
        return None
    return text


def get_number(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().upper() in {"NULL", "-", ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_rarity(value):
    text = nullable_text(value)
    if not text:
        return None
    return RARITY_MAP.get(text.upper(), text)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def hash_payload(data) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_session() -> requests.Session:
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_json(session: requests.Session, url: str):
    response = session.get(url, timeout=120)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(f"{url} no devolvió JSON válido: {error}") from error


def fetch_text(session: requests.Session, url: str, params=None) -> str:
    response = session.get(url, params=params, timeout=120)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# Bandai official scraper
# ---------------------------------------------------------------------------


def image_id_from_url(url: str | None) -> str | None:
    match = IMAGE_ID_RE.search(str(url or ""))
    return canonical_id(match.group(1)) if match else None


def normalize_label(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def clean_bandai_label(value: str) -> str:
    """Normalize Bandai option labels, including literal escaped <br> markup."""
    text = str(value or "")
    text = re.sub(r"<br\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"&lt;br\b.*?&gt;", " ", text, flags=re.I)
    return " ".join(text.split())


def discover_bandai_series(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for select in soup.find_all("select"):
        options = []
        for option in select.find_all("option"):
            value = str(option.get("value") or "").strip()
            label = clean_bandai_label(option.get_text(" ", strip=True))
            if not value or not label:
                continue
            if not re.fullmatch(r"\d{5,9}", value):
                continue
            options.append({"id": value, "label": label})

        if not options:
            continue

        marker = " ".join(
            [str(select.get("name") or ""), str(select.get("id") or "")]
        ).casefold()
        score = len(options) + (1000 if "series" in marker else 0)
        candidates.append((score, options))

    if not candidates:
        raise RuntimeError(
            "Bandai respondió, pero no se pudo descubrir el selector de series. "
            "La estructura HTML probablemente ha cambiado."
        )

    _, best = max(candidates, key=lambda item: item[0])
    seen = set()
    result = []
    for item in best:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        result.append(item)
    return result


def _container_field_labels(container: Tag) -> set[str]:
    labels = set()
    for heading in container.find_all(["h3", "dt"]):
        label = normalize_label(heading.get_text(" ", strip=True))
        if label in KNOWN_FIELD_LABELS:
            labels.add(label)
    return labels


def find_bandai_card_container(img: Tag, source_printing_id: str) -> Tag | None:
    current = img.parent
    code = base_code(source_printing_id)

    for _ in range(12):
        if not isinstance(current, Tag):
            break
        labels = _container_field_labels(current)
        text = current.get_text(" ", strip=True).upper()
        if len(labels) >= 3 and code in text:
            return current
        current = current.parent
    return None


def extract_labelled_value(container: Tag, canonical_label: str) -> str | None:
    wanted = normalize_label(canonical_label)

    for heading in container.find_all(["h3", "dt"]):
        current_label = normalize_label(heading.get_text(" ", strip=True))
        normalized_target = KNOWN_FIELD_LABELS.get(current_label)
        if normalize_label(normalized_target or current_label) != wanted:
            continue

        parent = heading.parent if isinstance(heading.parent, Tag) else None
        if parent is None:
            continue

        # Bandai sometimes represents attributes only as image alt text.
        alt_values = []
        for sub_img in parent.find_all("img"):
            alt = nullable_text(sub_img.get("alt"))
            if alt and normalize_text(alt) not in {"image", "card view", "text view"}:
                alt_values.append(alt)

        raw = " ".join(parent.get_text(" ", strip=True).split())
        heading_text = " ".join(heading.get_text(" ", strip=True).split())
        if raw.casefold().startswith(heading_text.casefold()):
            raw = raw[len(heading_text):].strip()

        parts = []
        for value in [*alt_values, raw]:
            value = nullable_text(value)
            if not value:
                continue
            if normalize_text(value) not in {normalize_text(item) for item in parts}:
                parts.append(value)

        return " / ".join(parts) if parts else None

    return None


def strip_parallel_label(name: str | None) -> str | None:
    text = nullable_text(name)
    if not text:
        return None
    return re.sub(
        r"\s*\((?:Parallel|Alternate Art|Manga Art|Reprint)\)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()


def split_slash(value: str | None) -> list[str]:
    text = nullable_text(value)
    if not text:
        return []
    values = [item.strip() for item in re.split(r"\s*/\s*", text) if item.strip()]
    deduped = []
    seen = set()
    for item in values:
        key = normalize_text(item)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def parse_bandai_card_page(
    html: str,
    source_url: str,
    series_id: str,
    series_label: str,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    seen = set()

    for img in soup.find_all("img"):
        src = str(img.get("src") or "").strip()
        if "/images/cardlist/card/" not in src:
            continue

        full_url = urljoin(source_url, src)
        source_printing_id = image_id_from_url(full_url)
        if not source_printing_id or not CARD_CODE_RE.search(base_code(source_printing_id)):
            continue

        container = find_bandai_card_container(img, source_printing_id)
        if container is None:
            continue

        unique_key = (source_printing_id, full_url, series_id)
        if unique_key in seen:
            continue
        seen.add(unique_key)

        text = " ".join(container.get_text(" ", strip=True).split())
        code_match = CARD_CODE_RE.search(text)
        card_no = canonical_id(code_match.group(0)) if code_match else base_code(source_printing_id)

        rarity_match = RARITY_CODE_RE.search(text)
        category_match = CATEGORY_RE.search(text)

        image_alt = nullable_text(img.get("alt"))
        if image_alt and normalize_text(image_alt) in {"image", "card view", "text view"}:
            image_alt = None

        life = get_number(extract_labelled_value(container, "Life"))
        cost = get_number(extract_labelled_value(container, "Cost"))
        category = category_match.group(1).title() if category_match else None
        if category and category.upper().startswith("DON"):
            category = "Don"

        name = image_alt
        if not name:
            # Fallback: use visible text immediately around the code/header.
            candidate_names = []
            for tag in container.find_all(["h1", "h2", "h4", "strong", "p"]):
                candidate = nullable_text(tag.get_text(" ", strip=True))
                if not candidate:
                    continue
                if card_no in candidate.upper():
                    continue
                if CATEGORY_RE.fullmatch(candidate.upper()) or RARITY_CODE_RE.fullmatch(candidate.upper()):
                    continue
                if len(candidate) <= 100:
                    candidate_names.append(candidate)
            name = candidate_names[0] if candidate_names else card_no

        effect = extract_labelled_value(container, "Effect")
        trigger = extract_labelled_value(container, "Trigger")
        card_sets_text = extract_labelled_value(container, "Card Set(s)")
        notes = extract_labelled_value(container, "Notes")

        record = {
            "cardNo": card_no,
            "sourcePrintingId": source_printing_id,
            "name": strip_parallel_label(name) or card_no,
            "displayName": name,
            "rarityCode": rarity_match.group(1).upper() if rarity_match else None,
            "rarity": normalize_rarity(rarity_match.group(1)) if rarity_match else None,
            "category": category,
            "life": life,
            "cost": None if category == "Leader" else cost,
            "power": get_number(extract_labelled_value(container, "Power")),
            "counter": get_number(extract_labelled_value(container, "Counter")),
            "colors": split_slash(extract_labelled_value(container, "Color")),
            "attributes": split_slash(extract_labelled_value(container, "Attribute")),
            "block": get_number(extract_labelled_value(container, "Block Icon")),
            "types": split_slash(extract_labelled_value(container, "Type")),
            "effect": effect,
            "trigger": trigger,
            "cardSetsText": card_sets_text,
            "notes": notes,
            "isParallel": bool(
                re.search(r"_P\d+", source_printing_id, re.I)
                or "parallel" in normalize_text(name)
            ),
            "isReprint": bool(re.search(r"_R\d+", source_printing_id, re.I)),
            "imageUrl": full_url,
            "seriesId": series_id,
            "seriesLabel": series_label,
            "sourceUrl": source_url,
        }
        records.append(record)

    return records


def count_bandai_card_images(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    return sum(
        1
        for img in soup.find_all("img")
        if "/images/cardlist/card/" in str(img.get("src") or "")
    )


def selected_bandai_series_id(html: str) -> str | None:
    """Return the series option marked selected, if Bandai exposes it in HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for option in soup.find_all("option"):
        value = str(option.get("value") or "").strip()
        if re.fullmatch(r"\d{5,9}", value) and option.has_attr("selected"):
            return value
    return None


def fetch_bandai_series_records(
    session: requests.Session,
    selected_url: str,
    item: dict,
) -> tuple[list[dict], list[dict]]:
    """Fetch one Bandai series using the current query shape plus a legacy fallback.

    As of September 2026 the official site and active scrapers use ?series=<id>.
    Older versions of this script incorrectly sent search=true as well, which can
    return the card-list shell with zero card rows.
    """
    attempts = [
        ("current", {"series": item["id"]}),
        ("legacy-search-flag", {"search": "true", "series": item["id"]}),
    ]
    diagnostics = []

    for attempt_name, params in attempts:
        html = fetch_text(session, selected_url, params=params)
        marker_count = count_bandai_card_images(html)
        selected_series = selected_bandai_series_id(html)
        records = parse_bandai_card_page(
            html,
            selected_url,
            item["id"],
            item["label"],
        )

        # If Bandai explicitly says a different series is selected, the query was
        # ignored/redirected. Never tag those cards as the requested release.
        if selected_series and selected_series != item["id"]:
            records = []
            mismatch = True
        else:
            mismatch = False

        diagnostics.append(
            {
                "attempt": attempt_name,
                "params": params,
                "selectedSeries": selected_series,
                "seriesMismatch": mismatch,
                "candidateCardImages": marker_count,
                "records": len(records),
            }
        )
        if records:
            if attempt_name != "current":
                print(
                    f"  Bandai fallback usado para series={item['id']}: "
                    f"{attempt_name}"
                )
            return records, diagnostics

    return [], diagnostics


def _version_tuple(value: str | None) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        return ()
    return tuple(int(x) for x in match.groups())


def _vega_version(vega_bin: str) -> str:
    proc = subprocess.run(
        [vega_bin, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"No se pudo ejecutar {vega_bin!r} --version: {text or proc.returncode}"
        )
    return text


def ensure_vega_binary(requested: str = "vega") -> tuple[str, str]:
    """Resolve vega; bootstrap the pinned release with cargo on CI if needed.

    vegapull/vega is an extractor only. The live data source remains Bandai's
    official card-list site. Pinning the version keeps CI deterministic.
    """
    resolved = shutil.which(requested)
    if resolved:
        version = _vega_version(resolved)
        parsed = _version_tuple(version)
        if parsed and parsed >= VEGAPULL_MIN_VERSION:
            print(f"Extractor Bandai: {version} ({resolved})")
            return resolved, version
        if requested != "vega":
            raise RuntimeError(
                f"El binario vega indicado es demasiado antiguo: {version}. "
                f"Se requiere >= {'.'.join(map(str, VEGAPULL_MIN_VERSION))}."
            )
        print(
            f"vega encontrado pero demasiado antiguo ({version}); "
            f"se instalará vegapull {VEGAPULL_PINNED_VERSION}."
        )

    elif requested != "vega":
        raise RuntimeError(f"No existe el binario vega indicado: {requested}")

    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError(
            "No se encontró 'vega' ni 'cargo'. En GitHub Actions usa un runner "
            "con Rust/cargo o instala vegapull antes de ejecutar el script."
        )

    print(
        f"vega no disponible. Instalando vegapull {VEGAPULL_PINNED_VERSION} "
        "desde crates.io (una sola vez en este runner)..."
    )
    cmd = [
        cargo,
        "install",
        "vegapull",
        "--version",
        VEGAPULL_PINNED_VERSION,
        "--locked",
        "--quiet",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "No se pudo instalar vegapull automáticamente con cargo. "
            f"Comando: {' '.join(cmd)}\n{detail[-4000:]}"
        )

    resolved = shutil.which("vega")
    if not resolved:
        candidate = Path.home() / ".cargo" / "bin" / "vega"
        if candidate.exists():
            resolved = str(candidate)
    if not resolved:
        raise RuntimeError(
            "cargo terminó sin error pero el binario 'vega' no apareció en PATH."
        )

    version = _vega_version(resolved)
    print(f"Extractor Bandai instalado: {version} ({resolved})")
    return resolved, version


def run_vega_command(
    vega_bin: str,
    args: list[str],
    attempts: int = 4,
    initial_backoff: float = 3.0,
) -> subprocess.CompletedProcess:
    """Run vega with retries for transient Bandai/network failures."""
    last = None
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            [vega_bin, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc
        last = proc
        if attempt < attempts:
            wait = initial_backoff * (2 ** (attempt - 1))
            print(
                f"  vega falló ({attempt}/{attempts}); reintento en {wait:.0f}s..."
            )
            time.sleep(wait)

    detail = ""
    if last is not None:
        detail = (last.stderr or last.stdout or "").strip()
    raise RuntimeError(
        f"vega falló tras {attempts} intentos: {' '.join(args)}\n{detail[-5000:]}"
    )


def _clean_vega_text(value) -> str | None:
    text = nullable_text(value)
    if not text:
        return None
    text = html_lib.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # Preserve inner text even for malformed custom tags such as <slash>.
    text = re.sub(r"<[^>]+>", "", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return nullable_text(text)


def _clean_vega_array(value) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    out = []
    seen = set()
    for item in values:
        cleaned = _clean_vega_text(item)
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _clean_bandai_types(value) -> list[str]:
    """Remove the UI label prefix that vega can preserve in the first Type value."""
    out = []
    seen = set()
    for item in _clean_vega_array(value):
        cleaned = re.sub(r"^\s*Type\s+", "", item, flags=re.I).strip()
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _clean_bandai_trigger(value) -> str | None:
    text = _clean_vega_text(value)
    if not text:
        return None
    # vega may return the HTML label plus the actual game keyword, e.g.
    # "Trigger [Trigger] Play this card.". Keep only the game text.
    text = re.sub(r"^\s*Trigger\s+", "", text, flags=re.I)
    return nullable_text(text)


def _normalize_bandai_record_for_output(record: dict) -> dict:
    """Clean known extractor presentation artifacts without changing mechanics."""
    cleaned = dict(record)
    cleaned["types"] = _clean_bandai_types(record.get("types"))
    cleaned["trigger"] = _clean_bandai_trigger(record.get("trigger"))
    return cleaned


def _normalize_vega_rarity(value) -> str | None:
    text = _clean_vega_text(value)
    if not text:
        return None
    aliases = {
        "common": "Common",
        "uncommon": "Uncommon",
        "rare": "Rare",
        "superrare": "Super Rare",
        "secretrare": "Secret Rare",
        "leader": "Leader",
        "special": "Special",
        "treasurerare": "Treasure Rare",
        "promo": "Promo",
    }
    return aliases.get(re.sub(r"\s+", "", text).casefold(), normalize_rarity(text))


def _normalize_vega_block(value):
    text = nullable_text(value)
    if text and text.upper() == "X":
        return "X"
    return get_number(value)


def _vega_pack_label(pack: dict) -> str:
    raw = _clean_vega_text(pack.get("raw_title"))
    if raw:
        return clean_bandai_label(raw)
    parts = pack.get("title_parts") if isinstance(pack.get("title_parts"), dict) else {}
    values = [parts.get("prefix"), parts.get("title"), parts.get("label")]
    label = " ".join(x for x in (_clean_vega_text(v) for v in values) if x)
    return clean_bandai_label(label) or str(pack.get("id") or "Bandai pack")


def _normalize_vega_packs(data) -> list[dict]:
    if isinstance(data, dict):
        rows = [value for value in data.values() if isinstance(value, dict)]
    elif isinstance(data, list):
        rows = [value for value in data if isinstance(value, dict)]
    else:
        rows = []
    out = []
    seen = set()
    for row in rows:
        pack_id = str(row.get("id") or "").strip()
        if not pack_id or pack_id in seen:
            continue
        seen.add(pack_id)
        out.append({**row, "id": pack_id, "label": _vega_pack_label(row)})
    return out


def _vega_card_rows(data) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("cards", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        # Defensive fallback if a future vega version returns id -> card.
        if data and all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
    return []


def normalize_vega_card(row: dict, pack: dict) -> dict | None:
    source_printing_id = canonical_id(row.get("id"))
    if not source_printing_id or not CARD_CODE_RE.search(base_code(source_printing_id)):
        return None

    category_raw = _clean_vega_text(row.get("category"))
    category = category_raw.title() if category_raw else None
    if category and category.upper().startswith("DON"):
        category = "Don"

    raw_cost = get_number(row.get("cost"))
    explicit_life = get_number(row.get("life"))
    life = explicit_life if explicit_life is not None else (raw_cost if category == "Leader" else None)
    cost = None if category == "Leader" else raw_cost

    image_url = nullable_text(row.get("img_full_url") or row.get("image_url"))
    if not image_url:
        relative = nullable_text(row.get("img_url"))
        if relative:
            image_url = urljoin(BANDAI_CARDLIST_URLS[0], relative)

    name = _clean_vega_text(row.get("name")) or base_code(source_printing_id)
    effect = _clean_vega_text(row.get("effect"))
    trigger = _clean_bandai_trigger(row.get("trigger"))
    label = pack.get("label") or _vega_pack_label(pack)
    pack_id = str(row.get("pack_id") or pack.get("id") or "").strip()

    return {
        "cardNo": base_code(source_printing_id),
        "sourcePrintingId": source_printing_id,
        "name": strip_parallel_label(name) or name,
        "displayName": name,
        "rarityCode": None,
        "rarity": _normalize_vega_rarity(row.get("rarity")),
        "category": category,
        "life": life,
        "cost": cost,
        "power": get_number(row.get("power")),
        "counter": get_number(row.get("counter")),
        "colors": _clean_vega_array(row.get("colors")),
        "attributes": _clean_vega_array(row.get("attributes")),
        "block": _normalize_vega_block(row.get("block_number") if "block_number" in row else row.get("block")),
        "types": _clean_bandai_types(row.get("types")),
        "effect": effect,
        "trigger": trigger,
        "cardSetsText": label,
        "notes": None,
        "isParallel": bool(re.search(r"_P\d+", source_printing_id, re.I)),
        "isReprint": bool(re.search(r"_R\d+", source_printing_id, re.I)),
        "imageUrl": image_url,
        "seriesId": pack_id,
        "seriesLabel": label,
        "sourceUrl": f"{BANDAI_CARDLIST_URLS[0]}?series={pack_id}" if pack_id else BANDAI_CARDLIST_URLS[0],
    }


def fetch_bandai_raw(
    session: requests.Session,
    delay_seconds: float,
    vega_requested: str = "vega",
) -> dict:
    """Fetch Bandai data through the pinned vega extractor.

    Runtime source is still Bandai. vega is used because plain requests from
    CI can receive the card-list shell without card blocks due to Bandai's
    cookie/bot protections.
    """
    del session  # Cardmarket still uses the requests session; Bandai uses vega.
    vega_bin, vega_version = ensure_vega_binary(vega_requested)

    with tempfile.TemporaryDirectory(prefix="optcg-vega-") as temp_name:
        work_dir = Path(temp_name)
        print("Bandai: descargando lista oficial de packs mediante vega...")
        run_vega_command(
            vega_bin,
            ["pull", "--language", "english", "--output", str(work_dir), "packs"],
        )
        packs_path = work_dir / "json" / "packs.json"
        if not packs_path.exists():
            raise RuntimeError(f"vega no generó el fichero esperado: {packs_path}")
        packs_raw = load_json(packs_path)
        packs = _normalize_vega_packs(packs_raw)
        if len(packs) < 20:
            raise RuntimeError(
                f"vega devolvió una lista de packs anormalmente pequeña: {len(packs)}"
            )
        print(f"Bandai: {len(packs)} packs/series detectados.")

        all_records = []
        series_reports = []
        prefetched = {}

        smoke_pack = next((p for p in packs if p["id"] == "569116"), packs[0])
        smoke_id = smoke_pack["id"]
        print(f"Smoke test Bandai/vega: pack={smoke_id} ({smoke_pack['label']})")
        run_vega_command(
            vega_bin,
            ["pull", "--language", "english", "--output", str(work_dir), "cards", smoke_id],
        )
        smoke_path = work_dir / "json" / f"cards_{smoke_id}.json"
        smoke_rows = _vega_card_rows(load_json(smoke_path, default=[]))
        smoke_records = [normalize_vega_card(row, smoke_pack) for row in smoke_rows]
        smoke_records = [row for row in smoke_records if row]
        if len(smoke_records) < 50:
            raise RuntimeError(
                "Bandai/vega respondió con un smoke test anormalmente pequeño: "
                f"pack={smoke_id}, records={len(smoke_records)}"
            )
        prefetched[smoke_id] = smoke_records
        print(f"Smoke test Bandai/vega OK: {len(smoke_records)} printings")

        failures = []
        for position, pack in enumerate(sorted(packs, key=lambda p: p["id"]), start=1):
            pack_id = pack["id"]
            print(f"Bandai {position}/{len(packs)}: {pack['label']} (pack={pack_id})")
            if pack_id in prefetched:
                records = prefetched[pack_id]
            else:
                target = work_dir / "json" / f"cards_{pack_id}.json"
                if target.exists():
                    target.unlink()
                try:
                    run_vega_command(
                        vega_bin,
                        ["pull", "--language", "english", "--output", str(work_dir), "cards", pack_id],
                    )
                    rows = _vega_card_rows(load_json(target, default=[]))
                    records = [normalize_vega_card(row, pack) for row in rows]
                    records = [row for row in records if row]
                except Exception as error:
                    failures.append({"id": pack_id, "label": pack["label"], "error": str(error)})
                    records = []

            if not records:
                failures.append({
                    "id": pack_id,
                    "label": pack["label"],
                    "error": "0 cartas utilizables",
                })
            else:
                all_records.extend(records)
            series_reports.append({**pack, "records": len(records)})
            if delay_seconds > 0 and position < len(packs):
                time.sleep(delay_seconds)

        # We deliberately do not publish a partial Bandai catalogue. A single
        # missing official pack can make Cardmarket mappings/prices misleading.
        if failures:
            # Deduplicate duplicate failure entries for the same pack.
            unique = {}
            for failure in failures:
                unique[failure["id"]] = failure
            raise RuntimeError(
                "Bandai/vega no pudo completar todos los packs; se aborta para "
                "no publicar un catálogo parcial. Fallos: "
                + json.dumps(list(unique.values()), ensure_ascii=False)[:8000]
            )

        if len(all_records) < 1000:
            raise RuntimeError(
                "Bandai/vega devolvió un catálogo anormalmente pequeño; "
                f"records={len(all_records)}"
            )

        return {
            "source": "Bandai official One Piece Card Game cardlist",
            "sourceUrl": BANDAI_CARDLIST_URLS[0],
            "extractor": {
                "name": "vegapull/vega",
                "version": vega_version,
                "pinnedVersion": VEGAPULL_PINNED_VERSION,
            },
            "fetchedAt": utc_now_iso(),
            "packs": packs,
            "series": series_reports,
            "cards": all_records,
        }


# ---------------------------------------------------------------------------
# Cardmarket official public downloads
# ---------------------------------------------------------------------------


def extract_rows(data, preferred_keys: tuple[str, ...]) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in preferred_keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    # Defensive fallback for future wrapper-name changes: choose the largest
    # list of dict objects at root rather than silently returning bad data.
    candidates = []
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value[:10]):
            candidates.append((len(value), key, value))
    if candidates:
        return max(candidates, key=lambda item: item[0])[2]
    return []


def normalize_cardmarket_product(row: dict) -> dict | None:
    product_id = get_number(row.get("idProduct") or row.get("id_product"))
    if product_id is None:
        return None

    url = (
        nullable_text(row.get("website"))
        or nullable_text(row.get("url"))
        or nullable_text(row.get("cardmarketUrl"))
    )
    if url and url.startswith("/"):
        url = urljoin(CARDMARKET_BASE_URL, url)

    product_id = int(product_id)
    if not url:
        # Cardmarket supports stable redirects based only on game + idProduct.
        # This gives every mapped product a navigable product URL even though
        # the public product-catalog JSON does not include the pretty URL slug.
        url = CARDMARKET_PRODUCT_REDIRECT.format(product_id=product_id)

    return {
        "idProduct": product_id,
        "name": nullable_text(row.get("name") or row.get("enName")),
        "idCategory": get_number(row.get("idCategory") or row.get("id_category")),
        "categoryName": nullable_text(row.get("categoryName") or row.get("category_name")),
        "idExpansion": get_number(row.get("idExpansion") or row.get("id_expansion")),
        "idMetacard": get_number(
            row.get("idMetacard")
            or row.get("idMetaproduct")
            or row.get("id_metacard")
        ),
        "dateAdded": nullable_text(row.get("dateAdded") or row.get("date_added")),
        "website": url,
    }


def normalize_price_row(row: dict) -> dict | None:
    product_id = get_number(row.get("idProduct") or row.get("id_product"))
    if product_id is None:
        return None

    def n(*keys):
        for key in keys:
            if key in row:
                return get_number(row.get(key))
        return None

    product_id = int(product_id)
    return {
        "idProduct": product_id,
        "idCategory": n("idCategory", "id_category"),
        "avg": n("avg"),
        "low": n("low"),
        "trend": n("trend"),
        "avg1": n("avg1"),
        "avg7": n("avg7"),
        "avg30": n("avg30"),
        "avgHolo": n("avg-holo", "avg_holo"),
        "lowHolo": n("low-holo", "low_holo"),
        "trendHolo": n("trend-holo", "trend_holo"),
        "avg1Holo": n("avg1-holo", "avg1_holo"),
        "avg7Holo": n("avg7-holo", "avg7_holo"),
        "avg30Holo": n("avg30-holo", "avg30_holo"),
    }


def price_guide_created_at(data) -> str | None:
    if isinstance(data, dict):
        return nullable_text(data.get("createdAt") or data.get("updatedAt"))
    return None


# ---------------------------------------------------------------------------
# Persistent Bandai-printing -> Cardmarket-idProduct mapping
# ---------------------------------------------------------------------------


def empty_mapping() -> dict:
    return {
        "schemaVersion": 3,
        "description": (
            "Persistent mapping from physical printing IDs to Cardmarket idProduct. "
            "Mappings are validated generically by card code, release/expansion coherence, "
            "Cardmarket URL family and one-product-per-printing uniqueness. Prices are never stored here."
        ),
        "updatedAt": utc_now_iso(),
        "mappings": {},
    }


def bootstrap_mapping_from_legacy(
    legacy_prices_path: Path | None,
    legacy_cards_path: Path | None = None,
) -> dict:
    price_data = load_json(legacy_prices_path) if legacy_prices_path and legacy_prices_path.exists() else {}
    cards_data = load_json(legacy_cards_path) if legacy_cards_path and legacy_cards_path.exists() else {}

    price_rows = price_data.get("cards", {}) if isinstance(price_data, dict) else {}
    card_rows = cards_data if isinstance(cards_data, dict) else {}
    if not isinstance(price_rows, dict):
        price_rows = {}
    if not isinstance(card_rows, dict):
        card_rows = {}
    if not price_rows and not card_rows:
        raise RuntimeError("No hay datos legacy utilizables para sembrar Cardmarket mapping")

    mapping = empty_mapping()
    source_date = nullable_text(price_data.get("updatedAt")) if isinstance(price_data, dict) else None

    # Preserve every known historical printing, even when the old price snapshot
    # did not yet have a Cardmarket idProduct. This metadata is useful to
    # distinguish a reprint that reuses the same Bandai art/image.
    all_printing_ids = set(price_rows) | set(card_rows)
    for raw_printing_id in sorted(all_printing_ids):
        price_row = price_rows.get(raw_printing_id, {})
        card_row = card_rows.get(raw_printing_id, {})
        if not isinstance(price_row, dict):
            price_row = {}
        if not isinstance(card_row, dict):
            card_row = {}

        product_id = get_number(price_row.get("cmId"))
        url = nullable_text(price_row.get("cm"))
        if product_id is None and not url and not card_row:
            continue

        key = canonical_id(raw_printing_id)
        mapping["mappings"][key] = {
            "productId": int(product_id) if product_id is not None else None,
            "url": url,
            "confirmed": product_id is not None,
            "source": (
                "legacy-cardmarket-snapshot"
                if price_row
                else "legacy-cardmarket-card-index"
            ),
            "sourceSnapshotDate": source_date,
            "legacySet": nullable_text(card_row.get("set")),
            "legacyName": nullable_text(card_row.get("name")),
            "legacyRarity": nullable_text(card_row.get("rarity")),
            "legacyImageUrl": nullable_text(card_row.get("image")),
            "addedAt": utc_now_iso(),
        }

    mapping["updatedAt"] = utc_now_iso()
    return mapping


def load_or_bootstrap_mapping(
    data_dir: Path,
    legacy_prices_path: Path | None,
    legacy_cards_path: Path | None = None,
) -> tuple[dict, bool]:
    mapping_path = data_dir / MAPPING_FILENAME
    existing = load_json(mapping_path)
    if isinstance(existing, dict) and isinstance(existing.get("mappings"), dict):
        return existing, False

    has_legacy = bool(
        (legacy_prices_path and legacy_prices_path.exists())
        or (legacy_cards_path and legacy_cards_path.exists())
    )
    if has_legacy:
        print("Creando mapping persistente desde snapshots legacy de Cardmarket...")
        mapping = bootstrap_mapping_from_legacy(legacy_prices_path, legacy_cards_path)
        save_json(mapping_path, mapping)
        return mapping, True

    print(
        "AVISO: no existe cardmarket_mapping.json ni snapshots legacy. "
        "Se creará un mapping vacío; los productos quedarán en revisión."
    )
    mapping = empty_mapping()
    save_json(mapping_path, mapping)
    return mapping, True

def enrich_existing_mapping(mapping: dict, products_by_id: dict[int, dict]) -> None:
    changed = False
    for _, entry in mapping.get("mappings", {}).items():
        if not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        if product_id is None:
            continue
        product = products_by_id.get(int(product_id))
        if not product:
            continue
        for source_key, target_key in (
            ("name", "productName"),
            ("idExpansion", "idExpansion"),
            ("idMetacard", "idMetacard"),
            ("dateAdded", "dateAdded"),
        ):
            if product.get(source_key) is not None and entry.get(target_key) != product.get(source_key):
                entry[target_key] = product.get(source_key)
                changed = True
        if not entry.get("url") and product.get("website"):
            entry["url"] = product["website"]
            changed = True
    if changed:
        mapping["updatedAt"] = utc_now_iso()


def art_identity(printing_id: str | None) -> str:
    """Artwork family used only for conservative Cardmarket alias matching."""
    return re.sub(r"_R\d+", "", canonical_id(printing_id), flags=re.I)


def compact_release_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def legacy_set_matches_bandai_record(legacy_set: str | None, record: dict) -> bool:
    legacy = compact_release_text(legacy_set)
    if not legacy:
        return False
    haystack = compact_release_text(
        " ".join(
            str(x or "")
            for x in (record.get("seriesLabel"), record.get("cardSetsText"))
        )
    )
    if legacy in haystack:
        return True

    event_match = re.fullmatch(r"eventpack0*(\d+)", legacy)
    if event_match and f"eventpackvol{int(event_match.group(1))}" in haystack:
        return True
    return False


def _product_card_code(product: dict | None) -> str | None:
    if not isinstance(product, dict):
        return None
    match = CARD_CODE_RE.search(str(product.get("name") or ""))
    return canonical_id(match.group(0)) if match else None


def _product_display_name(product: dict | None) -> str:
    if not isinstance(product, dict):
        return ""
    name = str(product.get("name") or "")
    name = CARD_CODE_RE.sub("", name)
    name = re.sub(r"[()]+", " ", name)
    return normalize_text(name)


def _structured_release_code(value: str | None) -> str | None:
    """Normalize OP-03/OP03, EB-01, ST-10 and PRB-02 style release codes."""
    compact = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    match = re.fullmatch(r"(OP|EB|ST|PRB)0*(\d{1,2})", compact)
    if not match:
        return None
    return f"{match.group(1)}{int(match.group(2)):02d}"


def _cardmarket_url_expansion_slug(url: str | None) -> str | None:
    """Return the pretty Cardmarket expansion path segment after /Singles/."""
    if not url:
        return None
    try:
        parts = [part for part in urlparse(str(url)).path.split("/") if part]
    except Exception:
        return None
    lowered = [part.casefold() for part in parts]
    if "singles" not in lowered:
        return None
    idx = lowered.index("singles")
    if idx + 1 >= len(parts):
        return None
    slug = slugify(parts[idx + 1])
    return slug or None


def _dominant_counter_value(
    counter: Counter,
    *,
    exclude_value=None,
    min_support: int = 3,
    min_ratio: float = 0.90,
) -> dict | None:
    """High-confidence majority, optionally evaluated leave-one-out."""
    work = Counter(counter)
    if exclude_value is not None and work.get(exclude_value, 0) > 0:
        work[exclude_value] -= 1
        if work[exclude_value] <= 0:
            del work[exclude_value]
    total = sum(work.values())
    if total < min_support or not work:
        return None
    value, count = work.most_common(1)[0]
    ratio = count / total
    if ratio < min_ratio:
        return None
    return {"value": value, "count": count, "total": total, "ratio": ratio}


def _mapping_product_rows(mapping: dict, products_by_id: dict[int, dict]) -> dict[str, dict]:
    rows = {}
    for key, entry in mapping.setdefault("mappings", {}).items():
        if not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        if product_id is None:
            continue
        product_id = int(product_id)
        product = products_by_id.get(product_id)
        expected_code = base_code(key)
        actual_code = _product_card_code(product)
        if product is None or actual_code != expected_code:
            continue
        rows[canonical_id(key)] = {
            "key": canonical_id(key),
            "entry": entry,
            "product": product,
            "productId": product_id,
            "baseCode": expected_code,
            "idExpansion": product.get("idExpansion"),
            "urlSlug": _cardmarket_url_expansion_slug(entry.get("url")),
            "releaseCode": _structured_release_code(entry.get("legacySet")),
        }
    return rows


def _build_mapping_profiles(mapping: dict, products_by_id: dict[int, dict]) -> dict:
    """
    Build statistical profiles from the mapping itself, not from hard-coded cards.

    Two independent signals are retained:
      * structured Bandai/legacy release code -> Cardmarket expansion ID
      * Cardmarket expansion ID -> pretty URL expansion slug

    Release profiles intentionally use non-Promo card numbers as anchors. A P-xxx
    card can be distributed inside a starter product while Cardmarket still keeps
    another P-xxx printing under Promos, so P-xxx rows are consumers of the profile,
    not evidence used to create it.
    """
    rows = _mapping_product_rows(mapping, products_by_id)
    release_expansions = defaultdict(Counter)
    release_slugs = defaultdict(Counter)
    expansion_slugs = defaultdict(Counter)

    for row in rows.values():
        expansion = row.get("idExpansion")
        slug = row.get("urlSlug")
        release = row.get("releaseCode")
        if expansion is not None and slug:
            expansion_slugs[expansion][slug] += 1
        if release and not row["baseCode"].startswith("P-") and expansion is not None:
            release_expansions[release][expansion] += 1
            if slug:
                release_slugs[(release, expansion)][slug] += 1

    # Reverse map only expansions whose URL family is itself very stable.
    stable_expansion_slug = {}
    slug_to_expansions = defaultdict(set)
    for expansion, counts in expansion_slugs.items():
        dominant = _dominant_counter_value(
            counts, min_support=4, min_ratio=0.90
        )
        if dominant:
            stable_expansion_slug[expansion] = dominant
            slug_to_expansions[dominant["value"]].add(expansion)

    return {
        "rows": rows,
        "releaseExpansions": release_expansions,
        "releaseSlugs": release_slugs,
        "expansionSlugs": expansion_slugs,
        "stableExpansionSlug": stable_expansion_slug,
        "slugToExpansions": slug_to_expansions,
    }


def _candidate_products_for_expected_expansions(
    mapping_key: str,
    entry: dict,
    products_by_id: dict[int, dict],
    expected_expansions: set,
    blocked_product_ids: set[int],
) -> list[dict]:
    expected_code = base_code(mapping_key)
    legacy_name = normalize_text(entry.get("legacyName"))
    candidates = []
    for product in products_by_id.values():
        if product.get("idExpansion") not in expected_expansions:
            continue
        if _product_card_code(product) != expected_code:
            continue
        if legacy_name and _product_display_name(product) != legacy_name:
            continue
        product_id = int(product["idProduct"])
        if product_id in blocked_product_ids:
            continue
        candidates.append(product)
    return sorted(candidates, key=lambda item: int(item["idProduct"]))


def _repair_mapping_entry(
    mapping_key: str,
    entry: dict,
    candidate: dict,
    old_product_id: int,
    reason: str,
    evidence: dict,
) -> dict:
    old_url = entry.get("url")
    entry["previousProductId"] = old_product_id
    if old_url:
        entry["previousUrl"] = old_url
    entry["productId"] = int(candidate["idProduct"])
    # Preserve a specific legacy pretty URL when it still identifies the same
    # card code/release. The downloadable catalogue only gives us a generic
    # idProduct redirect, which is less informative for later QA.
    if not old_url:
        entry["url"] = candidate.get("website")
    entry["confirmed"] = False
    entry["source"] = "auto-repair-mapping-consistency"
    entry["productName"] = candidate.get("name")
    entry["idExpansion"] = candidate.get("idExpansion")
    entry["idMetacard"] = candidate.get("idMetacard")
    entry["dateAdded"] = candidate.get("dateAdded")
    entry["validationEvidence"] = evidence
    for field in ("invalidProductId", "invalidUrl", "invalidReason"):
        entry.pop(field, None)
    return {
        "mappingKey": canonical_id(mapping_key),
        "oldProductId": old_product_id,
        "newProductId": int(candidate["idProduct"]),
        "oldProductName": None,
        "newProductName": candidate.get("name"),
        "reason": reason,
        "evidence": evidence,
    }


def _quarantine_mapping_entry(
    mapping_key: str,
    entry: dict,
    old_product_id: int,
    reason: str,
    evidence: dict,
    candidate_count: int | None = None,
) -> dict:
    old_url = entry.get("url")
    entry["invalidProductId"] = old_product_id
    if old_url:
        entry["invalidUrl"] = old_url
    entry["productId"] = None
    entry["url"] = None
    entry["confirmed"] = False
    entry["source"] = f"quarantined-{reason}"
    entry["invalidReason"] = reason
    entry["validationEvidence"] = evidence
    item = {
        "mappingKey": canonical_id(mapping_key),
        "oldProductId": old_product_id,
        "oldProductName": entry.get("productName"),
        "reason": reason,
        "evidence": evidence,
    }
    if candidate_count is not None:
        item["candidateCount"] = candidate_count
    return item


def _validate_product_code_mismatches(mapping: dict, products_by_id: dict[int, dict]) -> dict:
    """V3.3 invariant: a product can never belong to a different card number."""
    entries = mapping.setdefault("mappings", {})
    by_code_expansion = defaultdict(list)
    for product in products_by_id.values():
        code = _product_card_code(product)
        if code:
            by_code_expansion[(code, product.get("idExpansion"))].append(product)

    repaired = []
    quarantined = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        if product_id is None:
            continue
        product_id = int(product_id)
        product = products_by_id.get(product_id)
        expected_code = base_code(key)
        actual_code = _product_card_code(product)
        if product is not None and actual_code == expected_code:
            continue

        expansion = entry.get("idExpansion")
        legacy_name = normalize_text(entry.get("legacyName"))
        candidates = []
        for candidate in by_code_expansion.get((expected_code, expansion), []):
            if legacy_name and _product_display_name(candidate) != legacy_name:
                continue
            candidates.append(candidate)

        evidence = {
            "expectedCode": expected_code,
            "actualCode": actual_code,
            "idExpansion": expansion,
        }
        if len(candidates) == 1:
            old_name = (product or {}).get("name")
            result = _repair_mapping_entry(
                key,
                entry,
                candidates[0],
                product_id,
                "product-code-mismatch",
                evidence,
            )
            result["oldProductName"] = old_name
            repaired.append(result)
        else:
            reason = (
                f"product-code-mismatch:{expected_code}->{actual_code or 'unknown'}"
            )
            quarantined.append(
                _quarantine_mapping_entry(
                    key, entry, product_id, reason, evidence, len(candidates)
                )
            )
    return {"repaired": repaired, "quarantined": quarantined}


def _validate_release_and_url_consistency(
    mapping: dict,
    products_by_id: dict[int, dict],
) -> dict:
    """
    Detect release/language/version-family drift without hard-coded card IDs.

    The rule is deliberately conservative:
      * release expansion inference is leave-one-out and needs >=90% agreement;
      * URL-family inference needs >=95% agreement for the current expansion;
      * automatic repair is allowed only when all available signals agree on one
        expansion and exactly one unused product candidate exists;
      * otherwise the mapping is quarantined instead of guessing a price.
    """
    entries = mapping.setdefault("mappings", {})
    profiles = _build_mapping_profiles(mapping, products_by_id)
    rows = profiles["rows"]
    suspect = {}

    for key, row in rows.items():
        entry = row["entry"]
        current_expansion = row.get("idExpansion")
        own_slug = row.get("urlSlug")
        release_code = row.get("releaseCode")
        reasons = []
        expected_expansion_sets = []
        evidence = {
            "currentExpansion": current_expansion,
            "urlSlug": own_slug,
            "legacySet": entry.get("legacySet"),
        }

        # Structured release inference. For normal OP/EB/ST/PRB card numbers,
        # evaluate leave-one-out. P-xxx cards consume the profile only when their
        # pretty Cardmarket URL agrees with the release's dominant URL family.
        if release_code:
            counts = profiles["releaseExpansions"].get(release_code, Counter())
            if row["baseCode"].startswith("P-"):
                dominant = _dominant_counter_value(
                    counts, min_support=4, min_ratio=0.90
                )
                if dominant and own_slug:
                    expected_expansion = dominant["value"]
                    slug_profile = _dominant_counter_value(
                        profiles["releaseSlugs"].get(
                            (release_code, expected_expansion), Counter()
                        ),
                        min_support=3,
                        min_ratio=0.90,
                    )
                    if slug_profile and slug_profile["value"] == own_slug:
                        evidence["releaseProfile"] = dominant
                        evidence["releaseUrlProfile"] = slug_profile
                        if current_expansion != expected_expansion:
                            reasons.append("release-expansion-mismatch")
                            expected_expansion_sets.append({expected_expansion})
            else:
                dominant = _dominant_counter_value(
                    counts,
                    exclude_value=current_expansion,
                    min_support=3,
                    min_ratio=0.90,
                )
                if dominant:
                    evidence["releaseProfile"] = dominant
                    if current_expansion != dominant["value"]:
                        reasons.append("release-expansion-mismatch")
                        expected_expansion_sets.append({dominant["value"]})

        # Cardmarket-native check: one idExpansion should overwhelmingly use one
        # pretty /Singles/<expansion>/ URL family. This catches swapped original
        # vs reprint IDs even when both products have the same card number.
        if current_expansion is not None and own_slug:
            dominant_slug = _dominant_counter_value(
                profiles["expansionSlugs"].get(current_expansion, Counter()),
                exclude_value=own_slug,
                min_support=4,
                min_ratio=0.95,
            )
            if dominant_slug and dominant_slug["value"] != own_slug:
                reasons.append("cardmarket-url-expansion-mismatch")
                evidence["currentExpansionUrlProfile"] = dominant_slug
                reverse = {
                    expansion
                    for expansion, profile in profiles["stableExpansionSlug"].items()
                    if profile["value"] == own_slug
                }
                if reverse:
                    expected_expansion_sets.append(reverse)
                    evidence["urlCompatibleExpansions"] = sorted(reverse)

        if not reasons:
            continue

        # Intersect independent expected-expansion signals. If they disagree or
        # no reliable target can be derived, quarantine rather than guess.
        expected = None
        if expected_expansion_sets:
            expected = set(expected_expansion_sets[0])
            for values in expected_expansion_sets[1:]:
                expected &= set(values)
        suspect[key] = {
            "row": row,
            "reasons": sorted(set(reasons)),
            "evidence": evidence,
            "expectedExpansions": expected or set(),
        }

    # Product IDs owned by entries that are NOT suspect cannot be stolen by an
    # auto-repair. IDs owned only by suspect rows may be reused after those rows
    # are repaired/quarantined in this same validation pass.
    owners = defaultdict(set)
    for key, row in rows.items():
        owners[row["productId"]].add(key)
    suspect_keys = set(suspect)
    blocked_product_ids = {
        product_id
        for product_id, keys in owners.items()
        if any(key not in suspect_keys for key in keys)
    }

    repaired = []
    quarantined = []
    for key in sorted(suspect):
        item = suspect[key]
        row = item["row"]
        entry = row["entry"]
        old_product_id = row["productId"]
        expected_expansions = item["expectedExpansions"]
        reason = "+".join(item["reasons"])

        candidates = []
        if len(expected_expansions) == 1:
            candidates = _candidate_products_for_expected_expansions(
                key,
                entry,
                products_by_id,
                expected_expansions,
                blocked_product_ids - {old_product_id},
            )

        if len(candidates) == 1:
            result = _repair_mapping_entry(
                key,
                entry,
                candidates[0],
                old_product_id,
                reason,
                item["evidence"],
            )
            result["oldProductName"] = row["product"].get("name")
            repaired.append(result)
        else:
            quarantined.append(
                _quarantine_mapping_entry(
                    key,
                    entry,
                    old_product_id,
                    reason,
                    item["evidence"],
                    len(candidates),
                )
            )

    return {"repaired": repaired, "quarantined": quarantined}


def _quarantine_duplicate_product_ids(mapping: dict) -> dict:
    """One physical Bandai printing <-> one Cardmarket product ID."""
    entries = mapping.setdefault("mappings", {})
    owners = defaultdict(list)
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        if product_id is not None:
            owners[int(product_id)].append(canonical_id(key))

    duplicate_groups = []
    quarantined = []
    for product_id, keys in sorted(owners.items()):
        unique_keys = sorted(set(keys))
        if len(unique_keys) <= 1:
            continue
        duplicate_groups.append({"productId": product_id, "mappingKeys": unique_keys})
        for key in unique_keys:
            entry = entries[key]
            evidence = {"productId": product_id, "mappingKeys": unique_keys}
            quarantined.append(
                _quarantine_mapping_entry(
                    key,
                    entry,
                    product_id,
                    "duplicate-product-id-across-printings",
                    evidence,
                    len(unique_keys),
                )
            )
    return {"groups": duplicate_groups, "quarantined": quarantined}


def validate_and_repair_mapping(mapping: dict, products_by_id: dict[int, dict]) -> dict:
    """
    Generic mapping integrity firewall.

    Nothing in this validator is keyed to a known card ID. Production decisions
    are derived from generic invariants and high-confidence catalogue evidence;
    regression examples live only in self-tests.
    """
    mapping["schemaVersion"] = max(int(mapping.get("schemaVersion") or 0), 3)
    repaired = []
    quarantined = []

    code_result = _validate_product_code_mismatches(mapping, products_by_id)
    repaired.extend(code_result["repaired"])
    quarantined.extend(code_result["quarantined"])

    # Two rounds let the first repairs remove poisoned outliers from the
    # statistical profiles before evaluating smaller release families.
    consistency_rounds = []
    for _ in range(2):
        result = _validate_release_and_url_consistency(mapping, products_by_id)
        consistency_rounds.append(
            {"repaired": len(result["repaired"]), "quarantined": len(result["quarantined"])}
        )
        repaired.extend(result["repaired"])
        quarantined.extend(result["quarantined"])
        if not result["repaired"] and not result["quarantined"]:
            break

    duplicate_result = _quarantine_duplicate_product_ids(mapping)
    quarantined.extend(duplicate_result["quarantined"])

    if repaired or quarantined:
        mapping["updatedAt"] = utc_now_iso()

    reason_counts = Counter()
    for item in repaired + quarantined:
        for reason in str(item.get("reason") or "unknown").split("+"):
            reason_counts[reason] += 1

    return {
        "repaired": repaired,
        "quarantined": quarantined,
        "duplicateProductGroups": duplicate_result["groups"],
        "summary": {
            "repaired": len(repaired),
            "quarantined": len(quarantined),
            "duplicateProductGroups": len(duplicate_result["groups"]),
            "consistencyRounds": consistency_rounds,
            "reasonCounts": dict(sorted(reason_counts.items())),
        },
    }

def resolve_cardmarket_mapping_key_for_bandai_record(record: dict, mapping_entries: dict) -> tuple[str | None, str | None]:
    """
    Resolve only the Cardmarket mapping key. Never change Bandai printing identity.

    Rules:
    1. Exact Bandai sourcePrintingId with a productId wins.
    2. If an exact mapping entry exists but is unresolved/null, respect that
       physical distinction and do not borrow another printing's price.
    3. With no exact entry, allow a different historical key only when exactly
       one same-art mapping has a productId AND its legacy release matches Bandai.
    4. Never fall back to the only artwork candidate when release does not match.
    """
    source_pid = canonical_id(record.get("sourcePrintingId"))
    exact_entry = mapping_entries.get(source_pid)
    if isinstance(exact_entry, dict):
        if get_number(exact_entry.get("productId")) is not None:
            return source_pid, "exact"
        return None, None

    art = art_identity(source_pid)
    release_matches = []
    for key, entry in mapping_entries.items():
        if not isinstance(entry, dict):
            continue
        if get_number(entry.get("productId")) is None:
            continue
        if art_identity(key) != art:
            continue
        if legacy_set_matches_bandai_record(entry.get("legacySet"), record):
            release_matches.append(canonical_id(key))

    if len(release_matches) == 1:
        return release_matches[0], "release-alias"
    return None, None



def product_contains_code(product: dict, code: str) -> bool:
    name = str(product.get("name") or "").upper()
    return code.upper() in name


def auto_map_and_build_review(
    bandai_cards: list[dict],
    mapping: dict,
    products: list[dict],
    allow_auto_map: bool,
) -> dict:
    mapping_entries = mapping.setdefault("mappings", {})
    products_by_base = defaultdict(list)
    for product in products:
        product_name = str(product.get("name") or "")
        for match in CARD_CODE_RE.finditer(product_name):
            products_by_base[canonical_id(match.group(0))].append(product)

    bandai_by_base = defaultdict(list)
    for card in bandai_cards:
        source_id = canonical_id(card.get("sourcePrintingId"))
        bandai_by_base[base_code(source_id)].append((source_id, card))

    auto_added = []
    review_items = []

    mapped_product_ids = {
        int(e.get("productId"))
        for e in mapping_entries.values()
        if isinstance(e, dict) and get_number(e.get("productId")) is not None
    }

    for base in sorted(bandai_by_base):
        source_ids = sorted({source_id for source_id, _ in bandai_by_base[base]})
        missing_ids = []
        for source_id in source_ids:
            examples = [c for sid, c in bandai_by_base[base] if sid == source_id]
            representative = examples[0]
            cm_key, _ = resolve_cardmarket_mapping_key_for_bandai_record(
                representative, mapping_entries
            )
            if cm_key is None:
                missing_ids.append(source_id)

        if not missing_ids:
            continue

        candidates = products_by_base.get(base, [])
        unmapped_candidate_products = [
            p for p in candidates if int(p["idProduct"]) not in mapped_product_ids
        ]

        # Truly trivial only: one Bandai physical printing in the whole card
        # family and one unused Cardmarket product with the exact card number.
        if (
            allow_auto_map
            and len(missing_ids) == 1
            and len(unmapped_candidate_products) == 1
            and len(source_ids) == 1
        ):
            printing_id = missing_ids[0]
            product = unmapped_candidate_products[0]
            mapping_entries[printing_id] = {
                "productId": int(product["idProduct"]),
                "url": product.get("website"),
                "confirmed": False,
                "source": "auto-exact-card-code-unique",
                "addedAt": utc_now_iso(),
                "productName": product.get("name"),
                "idExpansion": product.get("idExpansion"),
                "idMetacard": product.get("idMetacard"),
                "dateAdded": product.get("dateAdded"),
            }
            mapped_product_ids.add(int(product["idProduct"]))
            auto_added.append({"printingId": printing_id, "productId": product["idProduct"]})
            continue

        for printing_id in missing_ids:
            bandai_examples = [
                c for source_id, c in bandai_by_base[base]
                if source_id == printing_id
            ]
            candidate_rows = []
            for product in candidates[:25]:
                candidate_rows.append(
                    {
                        "idProduct": product.get("idProduct"),
                        "name": product.get("name"),
                        "idExpansion": product.get("idExpansion"),
                        "idMetacard": product.get("idMetacard"),
                        "dateAdded": product.get("dateAdded"),
                        "website": product.get("website"),
                        "version": (
                            int(VERSION_RE.search(str(product.get("name") or "")).group(1))
                            if VERSION_RE.search(str(product.get("name") or ""))
                            else None
                        ),
                    }
                )

            review_items.append(
                {
                    "printingId": printing_id,
                    "baseCode": base,
                    "name": bandai_examples[0].get("name") if bandai_examples else None,
                    "imageUrl": bandai_examples[0].get("imageUrl") if bandai_examples else None,
                    "bandaiReleases": sorted(
                        {
                            x.get("seriesLabel")
                            for x in bandai_examples
                            if x.get("seriesLabel")
                        }
                    ),
                    "legacyMapping": mapping_entries.get(printing_id),
                    "reason": (
                        "No existe un mapping persistente inequívoco. "
                        "No se asigna precio hasta confirmar idProduct."
                    ),
                    "candidateProducts": candidate_rows,
                }
            )

    if auto_added:
        mapping["updatedAt"] = utc_now_iso()

    return {
        "generatedAt": utc_now_iso(),
        "autoMappingsAdded": auto_added,
        "needsReview": review_items,
    }


# ---------------------------------------------------------------------------
# Image health cache
# ---------------------------------------------------------------------------


def sniff_image_signature(chunk: bytes) -> str | None:
    if chunk.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if chunk.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(chunk) >= 12 and chunk[:4] == b"RIFF" and chunk[8:12] == b"WEBP":
        return "webp"
    if chunk.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def validate_image_url(session: requests.Session, url: str) -> dict:
    checked_at = utc_now_iso()
    try:
        response = session.get(
            url,
            headers={"Range": "bytes=0-4095", "Accept": "image/*,*/*;q=0.5"},
            timeout=45,
            stream=True,
            allow_redirects=True,
        )
        status = response.status_code
        content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        content_disposition = nullable_text(response.headers.get("Content-Disposition"))
        chunk = next(response.iter_content(chunk_size=4096), b"")
        signature = sniff_image_signature(chunk)
        response.close()

        is_attachment = bool(
            content_disposition
            and "attachment" in content_disposition.casefold()
        )
        ok = (
            status in {200, 206}
            and content_type.startswith("image/")
            and signature is not None
            and not is_attachment
        )
        if ok:
            error = None
        elif is_attachment:
            error = "Content-Disposition=attachment; no se publica como imageUrl render-safe"
        else:
            error = "Respuesta no validada como imagen renderizable"

        return {
            "ok": ok,
            "checkedAt": checked_at,
            "httpStatus": status,
            "contentType": content_type or None,
            "contentDisposition": content_disposition,
            "signature": signature,
            "finalUrl": str(response.url),
            "error": error,
        }
    except requests.RequestException as error:
        return {
            "ok": False,
            "checkedAt": checked_at,
            "httpStatus": None,
            "contentType": None,
            "contentDisposition": None,
            "signature": None,
            "finalUrl": None,
            "error": str(error),
        }


def update_image_health_cache(
    session: requests.Session,
    bandai_cards: list[dict],
    cache: dict,
    skip: bool,
    force_all: bool,
) -> tuple[dict, dict]:
    cache.setdefault("schemaVersion", 1)
    cache.setdefault("images", {})
    images = cache["images"]

    urls = sorted({str(card.get("imageUrl")) for card in bandai_cards if card.get("imageUrl")})
    checked = 0
    failed = []

    if skip:
        return cache, {"totalUrls": len(urls), "checkedThisRun": 0, "failed": []}

    for index, url in enumerate(urls, start=1):
        if not force_all and isinstance(images.get(url), dict) and images[url].get("ok") is True:
            continue
        print(f"Imagen {index}/{len(urls)}: {url}")
        result = validate_image_url(session, url)
        images[url] = result
        checked += 1
        if not result.get("ok"):
            failed.append({"url": url, **result})
        time.sleep(0.05)

    cache["updatedAt"] = utc_now_iso()
    return cache, {"totalUrls": len(urls), "checkedThisRun": checked, "failed": failed}


# ---------------------------------------------------------------------------
# Catalogue builder
# ---------------------------------------------------------------------------


def variant_type(source_printing_id: str, name: str | None, rarity: str | None) -> str:
    pid = canonical_id(source_printing_id)
    text = normalize_text(f"{name or ''} {rarity or ''}")
    if "manga" in text:
        return "manga"
    if "treasure rare" in text:
        return "treasure_rare"
    if "special" in text:
        return "special"
    if re.search(r"_R\d+", pid):
        return "reprint"
    if re.search(r"_P\d+", pid):
        return "parallel"
    return "base"


def richness(record: dict) -> int:
    keys = [
        "name", "rarity", "category", "life", "cost", "power", "counter",
        "colors", "attributes", "block", "types", "effect", "trigger",
    ]
    return sum(1 for key in keys if record.get(key) not in (None, "", [], {}))


def choose_canonical_record(records: list[dict], base: str) -> dict:
    base_records = [r for r in records if canonical_id(r.get("sourcePrintingId")) == base]
    pool = base_records or records
    return max(pool, key=richness)


def natural_printing_sort_key(printing: dict):
    pid = canonical_id(printing.get("printingId") or printing.get("sourcePrintingId"))
    base = base_code(pid)
    suffix = pid[len(base):]
    parts = re.findall(r"_([PR])(\d+)", suffix)
    rank = 0 if not parts else 1
    return (base, rank, [(kind, int(number)) for kind, number in parts], printing.get("id"))


def _printing_mechanics(record: dict) -> dict:
    r = _normalize_bandai_record_for_output(record)
    return {
        "life": r.get("life"),
        "cost": r.get("cost"),
        "power": r.get("power"),
        "counter": r.get("counter"),
        "colors": r.get("colors") or [],
        "attributes": r.get("attributes") or [],
        "block": r.get("block"),
        "types": r.get("types") or [],
        "effect": r.get("effect"),
        "trigger": r.get("trigger"),
    }


def build_catalog(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    image_cache: dict,
) -> tuple[dict, dict]:
    by_base = defaultdict(list)
    for raw_record in bandai_cards:
        record = _normalize_bandai_record_for_output(raw_record)
        pid = canonical_id(record.get("sourcePrintingId"))
        if pid:
            by_base[base_code(pid)].append(record)

    mapping_entries = mapping.get("mappings", {})
    health = image_cache.get("images", {}) if isinstance(image_cache, dict) else {}
    output = {}
    collisions = []
    mechanic_conflicts = []
    printings_with_price = 0
    printings_with_mapping = 0
    printings_without_valid_image = 0
    mapping_relation_counts = defaultdict(int)

    for base in sorted(by_base):
        records = by_base[base]
        canonical = _normalize_bandai_record_for_output(choose_canonical_record(records, base))

        mechanics = {}
        for field in ("name", "category", "life", "cost", "power", "counter", "colors", "attributes", "types", "effect", "trigger"):
            values = {
                json.dumps(_normalize_bandai_record_for_output(r).get(field), ensure_ascii=False, sort_keys=True)
                for r in records
                if _normalize_bandai_record_for_output(r).get(field) not in (None, "", [], {})
            }
            if len(values) > 1:
                mechanics[field] = [json.loads(v) for v in sorted(values)]
        if mechanics:
            mechanic_conflicts.append({"baseCode": base, "fields": mechanics})

        # Bandai identity is authoritative. The only merge allowed is when the
        # exact same sourcePrintingId appears in more than one Bandai release.
        grouped_printings = defaultdict(list)
        for record in records:
            source_printing_id = canonical_id(record.get("sourcePrintingId"))
            grouped_printings[source_printing_id].append(record)

        printings = []
        for printing_id in sorted(grouped_printings):
            same_id_records = grouped_printings[printing_id]
            by_image = defaultdict(list)
            for record in same_id_records:
                by_image[str(record.get("imageUrl") or "")].append(record)

            for image_url, image_records in sorted(by_image.items()):
                internal_id = printing_id
                if len(by_image) > 1:
                    digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:8].upper()
                    internal_id = f"{printing_id}~BANDAI-IMAGE~{digest}"
                    collisions.append(
                        {
                            "printingId": printing_id,
                            "bandaiSourcePrintingIds": sorted(
                                {canonical_id(r.get("sourcePrintingId")) for r in image_records}
                            ),
                            "internalId": internal_id,
                            "imageUrl": image_url,
                        }
                    )

                best = _normalize_bandai_record_for_output(max(image_records, key=richness))
                releases = []
                seen_release = set()
                for r in image_records:
                    release_key = (r.get("seriesId"), r.get("seriesLabel"), r.get("cardSetsText"))
                    if release_key in seen_release:
                        continue
                    seen_release.add(release_key)
                    releases.append(
                        {
                            "seriesId": r.get("seriesId"),
                            "seriesLabel": r.get("seriesLabel"),
                            "cardSetsText": r.get("cardSetsText"),
                        }
                    )

                image_status = health.get(image_url) if image_url else None
                if image_status is None:
                    safe_image_url = image_url
                elif image_status.get("ok"):
                    safe_image_url = image_status.get("finalUrl") or image_url
                else:
                    safe_image_url = None
                    printings_without_valid_image += 1

                cm_key, cm_relation = resolve_cardmarket_mapping_key_for_bandai_record(
                    best, mapping_entries
                )
                map_entry = mapping_entries.get(cm_key) if cm_key else None
                cm = None
                if isinstance(map_entry, dict) and get_number(map_entry.get("productId")) is not None:
                    printings_with_mapping += 1
                    mapping_relation_counts[cm_relation or "unknown"] += 1
                    product_id = int(get_number(map_entry.get("productId")))
                    product = products_by_id.get(product_id)
                    price = prices_by_id.get(product_id)
                    if price:
                        printings_with_price += 1
                    cm = {
                        "mappingKey": cm_key,
                        "mappingRelation": cm_relation,
                        "productId": product_id,
                        "url": map_entry.get("url") or (product or {}).get("website"),
                        "mappingConfirmed": bool(map_entry.get("confirmed")),
                        "mappingSource": map_entry.get("source"),
                        "product": product,
                        "price": (
                            {
                                "currency": "EUR",
                                "createdAt": price_created_at,
                                **price,
                                "valuationEur": (
                                    price.get("trend")
                                    if price.get("trend") is not None
                                    else price.get("avg7")
                                    if price.get("avg7") is not None
                                    else price.get("avg30")
                                    if price.get("avg30") is not None
                                    else price.get("avg")
                                ),
                            }
                            if price
                            else None
                        ),
                    }

                printing_mechanics = _printing_mechanics(best)
                canonical_mechanics = _printing_mechanics(canonical)
                mechanics_differ = [
                    field for field in printing_mechanics
                    if printing_mechanics[field] != canonical_mechanics[field]
                ]

                printings.append(
                    {
                        "id": internal_id,
                        "printingId": printing_id,
                        "sourcePrintingId": printing_id,
                        "bandaiSourcePrintingIds": [printing_id],
                        "baseCode": base,
                        "variantType": variant_type(printing_id, best.get("displayName"), best.get("rarity")),
                        "isParallel": bool(best.get("isParallel")) or bool(re.search(r"_P\d+", printing_id)),
                        "isReprint": bool(best.get("isReprint")) or bool(re.search(r"_R\d+", printing_id)),
                        "rarity": best.get("rarity"),
                        "imageUrl": safe_image_url,
                        "imageSourceUrl": image_url or None,
                        "imageHealth": image_status,
                        "releases": releases,
                        "mechanics": printing_mechanics,
                        "mechanicsDifferFromBase": mechanics_differ,
                        "cardmarket": cm,
                        "source": "bandai",
                    }
                )

        printings.sort(key=natural_printing_sort_key)
        canonical_mechanics = _printing_mechanics(canonical)
        output[base] = {
            "code": base,
            "game": "One Piece",
            "name": canonical.get("name") or base,
            "rarity": canonical.get("rarity"),
            "type": canonical.get("category"),
            **canonical_mechanics,
            "sources": ["bandai", *( ["cardmarket"] if any(p.get("cardmarket") for p in printings) else [] )],
            "printings": printings,
        }

    stats = {
        "cards": len(output),
        "printings": sum(len(card["printings"]) for card in output.values()),
        "printingsWithCardmarketMapping": printings_with_mapping,
        "printingsWithCardmarketPrice": printings_with_price,
        "printingsWithoutValidatedImage": printings_without_valid_image,
        "cardmarketMappingRelations": dict(sorted(mapping_relation_counts.items())),
        "bandaiPrintingIdImageCollisions": collisions,
        "mechanicConflicts": mechanic_conflicts,
    }
    return output, stats


# ---------------------------------------------------------------------------
# Raw source orchestration
# ---------------------------------------------------------------------------


def fetch_live_raw(session: requests.Session, bandai_delay: float, vega_bin: str = "vega") -> dict:
    print("Descargando Bandai oficial...")
    bandai = fetch_bandai_raw(session, bandai_delay, vega_bin)

    print("Descargando catálogo público oficial de Cardmarket...")
    cm_products = fetch_json(session, CARDMARKET_PRODUCTS_URL)

    print("Descargando Price Guide público oficial de Cardmarket...")
    cm_prices = fetch_json(session, CARDMARKET_PRICE_GUIDE_URL)

    return {
        "bandai": bandai,
        "cardmarket_products": cm_products,
        "cardmarket_prices": cm_prices,
    }


def save_raw(raw_dir: Path, raw_data: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for key, filename in RAW_FILENAMES.items():
        path = raw_dir / filename
        save_json(path, raw_data[key])
        print(f"RAW guardado: {path}")


def load_raw(raw_dir: Path) -> dict:
    result = {}
    for key, filename in RAW_FILENAMES.items():
        path = raw_dir / filename
        data = load_json(path)
        if data is None:
            raise RuntimeError(f"Falta RAW requerido: {path}")
        result[key] = data
        print(f"RAW cargado: {path}")
    return result


# ---------------------------------------------------------------------------
# Git publication
# ---------------------------------------------------------------------------


def git_run(args: list[str], capture=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        capture_output=capture,
    )


def publish_to_git(paths: list[Path]) -> None:
    try:
        branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
        if not branch:
            branch = git_run(["rev-parse", "--abbrev-ref", "HEAD"], capture=True).stdout.strip()
        if not branch or branch == "HEAD" or "/merge" in branch:
            raise RuntimeError(f"Rama Git no válida para push: {branch!r}")

        git_run(["config", "user.name", "github-actions[bot]"])
        git_run([
            "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ])

        path_strings = [str(path) for path in paths if path.exists()]
        if not path_strings:
            print("No hay archivos para publicar.")
            return

        git_run(["add", "-f", "--", *path_strings])
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if changed.returncode == 0:
            print("No hay cambios para commit.")
            return
        if changed.returncode != 1:
            raise RuntimeError("git diff --cached devolvió un estado inesperado")

        git_run(["commit", "-m", "chore: update Bandai and Cardmarket catalogue"])
        git_run(["push", "origin", f"HEAD:{branch}"])

        local_sha = git_run(["rev-parse", "HEAD"], capture=True).stdout.strip()
        remote = git_run(["ls-remote", "origin", f"refs/heads/{branch}"], capture=True).stdout.strip()
        remote_sha = remote.split()[0] if remote else None
        if local_sha != remote_sha:
            raise RuntimeError(
                f"Push no verificable: local={local_sha}, remoto={remote_sha}"
            )
        print(f"Push verificado en origin/{branch}: {local_sha}")
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"ERROR publicando en GitHub: {error}")
        print("En GitHub Actions comprueba: permissions: contents: write")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Self tests
# ---------------------------------------------------------------------------


def run_self_test() -> None:
    fixture = """
    <html><body>
      <select name="series">
        <option value="">All</option>
        <option value="569117">The World's Strongest Warriors</option>
      </select>
      <div class="card-wrapper">
        <div><img src="../images/cardlist/card/OP17-001.png?260828" alt="Edward.Newgate"></div>
        <div class="header">OP17-001 | L | LEADER</div>
        <div class="text"><h3>Life</h3>5</div>
        <div class="text"><h3>Attribute</h3><img alt="Special" src="special.png">Special</div>
        <div class="text"><h3>Power</h3>5000</div>
        <div class="text"><h3>Counter</h3>-</div>
        <div class="text"><h3>Color</h3>Red</div>
        <div class="text"><h3>Block Icon</h3>5</div>
        <div class="text"><h3>Type</h3>The Four Emperors/Whitebeard Pirates</div>
        <div class="text"><h3>Effect</h3>[Once Per Turn] Test.</div>
        <div class="text"><h3>Card Set(s)</h3>OP17</div>
      </div>
      <div class="card-wrapper">
        <div><img src="../images/cardlist/card/OP17-001_p1.png?260828" alt="Edward.Newgate (Parallel)"></div>
        <div class="header">OP17-001 | L | LEADER</div>
        <div class="text"><h3>Life</h3>5</div>
        <div class="text"><h3>Attribute</h3>Special</div>
        <div class="text"><h3>Power</h3>5000</div>
        <div class="text"><h3>Counter</h3>-</div>
        <div class="text"><h3>Color</h3>Red</div>
        <div class="text"><h3>Block Icon</h3>5</div>
        <div class="text"><h3>Type</h3>The Four Emperors/Whitebeard Pirates</div>
        <div class="text"><h3>Effect</h3>[Once Per Turn] Test.</div>
        <div class="text"><h3>Card Set(s)</h3>OP17</div>
      </div>
    </body></html>
    """

    series = discover_bandai_series(fixture)
    assert series == [{"id": "569117", "label": "The World's Strongest Warriors"}]

    cards = parse_bandai_card_page(
        fixture,
        "https://en.onepiece-cardgame.com/cardlist/",
        "569117",
        "The World's Strongest Warriors",
    )
    assert len(cards) == 2, cards
    assert cards[0]["sourcePrintingId"] == "OP17-001"
    assert cards[0]["life"] == 5 and cards[0]["cost"] is None
    assert cards[1]["sourcePrintingId"] == "OP17-001_P1"
    assert cards[1]["isParallel"] is True

    vega_pack = {
        "id": "569117",
        "raw_title": "BOOSTER PACK -THE WORLD'S STRONGEST WARRIORS- [OP-17]",
    }
    vega_row = {
        "id": "OP17-001_p1",
        "pack_id": "569117",
        "name": "Edward.Newgate",
        "rarity": "Leader",
        "category": "Leader",
        "img_full_url": "https://en.onepiece-cardgame.com/images/cardlist/card/OP17-001_p1.png?260828",
        "colors": ["Red"],
        "cost": 5,
        "attributes": ["Special"],
        "power": 5000,
        "counter": None,
        "block_number": 5,
        "types": ["The Four Emperors", "Whitebeard Pirates"],
        "effect": "[Once Per Turn] Test.<br>Second line.",
        "trigger": None,
    }
    vega_card = normalize_vega_card(vega_row, {**vega_pack, "label": _vega_pack_label(vega_pack)})
    assert vega_card is not None
    assert vega_card["sourcePrintingId"] == "OP17-001_P1"
    assert vega_card["life"] == 5 and vega_card["cost"] is None
    assert vega_card["isParallel"] is True
    assert "Second line." in vega_card["effect"]
    assert _clean_bandai_types(["Type Land of Wano", "Kouzuki Clan"]) == ["Land of Wano", "Kouzuki Clan"]
    assert _clean_bandai_trigger("Trigger [Trigger] Play this card.") == "[Trigger] Play this card."

    legacy = {
        "updatedAt": "2026-08-04",
        "cards": {
            "OP17-001": {
                "cmId": 123,
                "cm": "https://www.cardmarket.com/en/OnePiece/Products/Singles/X/Y",
            }
        },
    }
    temp = Path("/tmp/optcg_v3_self_test_legacy.json")
    save_json(temp, legacy)
    mapping = bootstrap_mapping_from_legacy(temp)
    assert mapping["mappings"]["OP17-001"]["productId"] == 123

    products = [
        normalize_cardmarket_product(
            {
                "idProduct": 123,
                "name": "Edward.Newgate (OP17-001) (V.1)",
                "idCategory": 16,
                "categoryName": "One Piece Single",
                "idExpansion": 999,
                "idMetacard": 888,
                "dateAdded": "2026-08-20 10:00:00",
            }
        )
    ]
    products = [x for x in products if x]
    price = normalize_price_row(
        {
            "idProduct": 123,
            "avg": 1.2,
            "low": 0.8,
            "trend": 1.1,
            "avg1": 1.0,
            "avg7": 1.05,
            "avg30": 0.95,
        }
    )
    image_cache = {
        "images": {
            cards[0]["imageUrl"]: {
                "ok": True,
                "finalUrl": cards[0]["imageUrl"],
            },
            cards[1]["imageUrl"]: {
                "ok": True,
                "finalUrl": cards[1]["imageUrl"],
            },
        }
    }
    catalog, stats = build_catalog(
        cards,
        mapping,
        {123: products[0]},
        {123: price},
        "2026-09-09T02:00:00+0200",
        image_cache,
    )
    assert catalog["OP17-001"]["life"] == 5
    assert catalog["OP17-001"]["printings"][0]["cardmarket"]["price"]["trend"] == 1.1
    assert stats["cards"] == 1

    # Regression: Bandai printing identity is never rewritten by Cardmarket.
    # An explicit unresolved reprint mapping must not borrow the original price.
    reprint_mapping = {
        "OP01-120_P2": {"productId": 690959, "legacySet": "OP01"},
        "OP01-120_P2_R1": {"productId": None, "legacySet": "PRB01"},
    }
    original = {
        "sourcePrintingId": "OP01-120_P2",
        "seriesLabel": "ROMANCE DAWN",
        "cardSetsText": "-ROMANCE DAWN- [OP-01]",
    }
    reprint = {
        "sourcePrintingId": "OP01-120_P2_R1",
        "seriesLabel": "ONE PIECE CARD THE BEST",
        "cardSetsText": "-ONE PIECE CARD THE BEST- [PRB-01]",
    }
    assert resolve_cardmarket_mapping_key_for_bandai_record(original, reprint_mapping) == ("OP01-120_P2", "exact")
    assert resolve_cardmarket_mapping_key_for_bandai_record(reprint, reprint_mapping) == (None, None)

    # Exact Bandai IDs with a product mapping win even if legacy release metadata
    # is imperfect; this avoids suppressing valid promo mappings.
    promo_mapping = {"P-041": {"productId": 750655, "legacySet": "ST18"}}
    promo = {"sourcePrintingId": "P-041", "seriesLabel": "Promotion card", "cardSetsText": "Promotion card"}
    assert resolve_cardmarket_mapping_key_for_bandai_record(promo, promo_mapping) == ("P-041", "exact")

    # V3.4 regression: release/expansion drift is repaired generically, without
    # naming any real card. Three trustworthy OP99 rows establish expansion 100;
    # the fourth row points to the same card code in expansion 200 and must move
    # to the unique product in expansion 100.
    qa_products = {}
    for pid, code, expansion in (
        (1001, "OP99-001", 100),
        (1002, "OP99-002", 100),
        (1003, "OP99-003", 100),
        (1004, "OP99-004", 200),
        (1005, "OP99-004", 100),
        (2001, "P-099", 300),
    ):
        product = normalize_cardmarket_product(
            {
                "idProduct": pid,
                "name": f"Test Card ({code})",
                "idCategory": 1621,
                "categoryName": "One Piece Single",
                "idExpansion": expansion,
                "idMetacard": pid + 5000,
                "dateAdded": "2026-09-01 00:00:00",
            }
        )
        qa_products[pid] = product

    qa_mapping = {
        "schemaVersion": 2,
        "mappings": {
            "OP99-001": {
                "productId": 1001, "legacySet": "OP99", "legacyName": "Test Card",
                "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Test-Set/Test-Card-OP99-001",
            },
            "OP99-002": {
                "productId": 1002, "legacySet": "OP99", "legacyName": "Test Card",
                "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Test-Set/Test-Card-OP99-002",
            },
            "OP99-003": {
                "productId": 1003, "legacySet": "OP99", "legacyName": "Test Card",
                "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Test-Set/Test-Card-OP99-003",
            },
            "OP99-004": {
                "productId": 1004, "legacySet": "OP99", "legacyName": "Test Card",
                "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Test-Set/Test-Card-OP99-004",
                "idExpansion": 200,
            },
            # P-xxx may be distributed in a starter product while Cardmarket
            # keeps another printing under Promos. It must not inherit ST18's
            # expansion merely from legacySet.
            "P-099": {
                "productId": 2001, "legacySet": "ST18", "legacyName": "Test Card",
                "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Promos/Test-Card-P-099",
                "idExpansion": 300,
            },
        },
    }
    qa_result = validate_and_repair_mapping(qa_mapping, qa_products)
    assert qa_mapping["mappings"]["OP99-004"]["productId"] == 1005
    assert qa_mapping["mappings"]["P-099"]["productId"] == 2001
    assert any(x["mappingKey"] == "OP99-004" for x in qa_result["repaired"])

    # V3.4 regression: the same Cardmarket idProduct cannot price two distinct
    # physical Bandai printings. Ambiguity is quarantined, never guessed.
    duplicate_mapping = {
        "schemaVersion": 3,
        "mappings": {
            "P-001": {"productId": 2001, "legacyName": "Test Card"},
            "P-001_P1": {"productId": 2001, "legacyName": "Test Card"},
        },
    }
    duplicate_products = {
        2001: normalize_cardmarket_product(
            {
                "idProduct": 2001,
                "name": "Test Card (P-001)",
                "idCategory": 1621,
                "idExpansion": 300,
                "idMetacard": 9999,
            }
        )
    }
    duplicate_result = validate_and_repair_mapping(duplicate_mapping, duplicate_products)
    assert duplicate_mapping["mappings"]["P-001"]["productId"] is None
    assert duplicate_mapping["mappings"]["P-001_P1"]["productId"] is None
    assert duplicate_result["summary"]["duplicateProductGroups"] == 1

    # Regression: current Bandai query contract is series=<id> first.
    class _FakeResponse:
        def __init__(self, text):
            self.text = text
            self.status_code = 200
        def raise_for_status(self):
            return None

    class _FakeSession:
        def __init__(self, text):
            self.text = text
            self.calls = []
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return _FakeResponse(self.text)

    fake = _FakeSession(fixture)
    fetched, diag = fetch_bandai_series_records(
        fake,
        "https://en.onepiece-cardgame.com/cardlist/",
        {"id": "569117", "label": "The World's Strongest Warriors"},
    )
    assert len(fetched) == 2
    assert fake.calls[0][1] == {"series": "569117"}
    assert diag[0]["attempt"] == "current"

    print("SELF-TEST OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return

    session = make_session()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.from_raw:
        raw_data = load_raw(args.raw_dir)
    else:
        raw_data = fetch_live_raw(session, args.bandai_delay, args.vega_bin)
        save_raw(args.raw_dir, raw_data)

    bandai_root = raw_data["bandai"]
    bandai_cards = bandai_root.get("cards", []) if isinstance(bandai_root, dict) else []
    if not bandai_cards:
        raise RuntimeError("Bandai RAW no contiene cartas; se aborta para no publicar un catálogo vacío.")

    product_rows_raw = extract_rows(
        raw_data["cardmarket_products"],
        ("products", "product", "data"),
    )
    products = [normalize_cardmarket_product(row) for row in product_rows_raw]
    products = [item for item in products if item]
    if not products:
        raise RuntimeError("El catálogo oficial de Cardmarket no contiene productos utilizables.")
    products_by_id = {item["idProduct"]: item for item in products}

    price_rows_raw = extract_rows(
        raw_data["cardmarket_prices"],
        ("priceGuides", "priceGuide", "prices", "data"),
    )
    prices = [normalize_price_row(row) for row in price_rows_raw]
    prices = [item for item in prices if item]
    if not prices:
        raise RuntimeError("El Price Guide oficial de Cardmarket no contiene precios utilizables.")
    prices_by_id = {item["idProduct"]: item for item in prices}
    price_created_at = price_guide_created_at(raw_data["cardmarket_prices"])

    legacy_prices_path = args.legacy_cardmarket_prices
    if legacy_prices_path is None:
        default_legacy_prices = args.raw_dir / LEGACY_PRICE_FILENAME
        legacy_prices_path = default_legacy_prices if default_legacy_prices.exists() else None

    legacy_cards_path = args.legacy_cardmarket_cards
    if legacy_cards_path is None:
        default_legacy_cards = args.raw_dir / LEGACY_CARDS_FILENAME
        legacy_cards_path = default_legacy_cards if default_legacy_cards.exists() else None

    mapping, bootstrapped = load_or_bootstrap_mapping(
        args.data_dir, legacy_prices_path, legacy_cards_path
    )
    enrich_existing_mapping(mapping, products_by_id)
    mapping_validation = validate_and_repair_mapping(mapping, products_by_id)

    review = auto_map_and_build_review(
        bandai_cards,
        mapping,
        products,
        allow_auto_map=not args.no_auto_map,
    )
    save_json(args.data_dir / MAPPING_FILENAME, mapping)

    image_cache_path = args.data_dir / IMAGE_CACHE_FILENAME
    image_cache = load_json(image_cache_path, default={}) or {}
    image_cache, image_report = update_image_health_cache(
        session,
        bandai_cards,
        image_cache,
        skip=args.skip_image_check,
        force_all=args.image_check_all,
    )
    save_json(image_cache_path, image_cache)

    catalog, catalogue_stats = build_catalog(
        bandai_cards,
        mapping,
        products_by_id,
        prices_by_id,
        price_created_at,
        image_cache,
    )

    report = {
        "generatedAt": utc_now_iso(),
        "schemaVersion": 3,
        "sources": {
            "bandai": {
                "url": bandai_root.get("sourceUrl") if isinstance(bandai_root, dict) else None,
                "fetchedAt": bandai_root.get("fetchedAt") if isinstance(bandai_root, dict) else None,
                "records": len(bandai_cards),
                "series": len(bandai_root.get("series", [])) if isinstance(bandai_root, dict) else None,
            },
            "cardmarketProducts": {
                "url": CARDMARKET_PRODUCTS_URL,
                "records": len(products),
                "createdAt": (
                    raw_data["cardmarket_products"].get("createdAt")
                    if isinstance(raw_data["cardmarket_products"], dict)
                    else None
                ),
            },
            "cardmarketPriceGuide": {
                "url": CARDMARKET_PRICE_GUIDE_URL,
                "records": len(prices),
                "createdAt": price_created_at,
            },
        },
        "mapping": {
            "bootstrappedThisRun": bootstrapped,
            "entries": len(mapping.get("mappings", {})),
            "autoMappingsAdded": len(review.get("autoMappingsAdded", [])),
            "validationRepairs": mapping_validation.get("repaired", []),
            "validationQuarantined": mapping_validation.get("quarantined", []),
            "validationSummary": mapping_validation.get("summary", {}),
            "duplicateProductGroups": mapping_validation.get("duplicateProductGroups", []),
            "needsReview": len(review.get("needsReview", [])),
        },
        "images": {
            "totalUrls": image_report.get("totalUrls"),
            "checkedThisRun": image_report.get("checkedThisRun"),
            "failedThisRun": len(image_report.get("failed", [])),
            "failed": image_report.get("failed", [])[:100],
        },
        "output": catalogue_stats,
        "fingerprints": {
            "catalog": hash_payload(catalog),
            "mapping": hash_payload(mapping),
        },
    }

    catalog_path = args.output_dir / CATALOG_FILENAME
    report_path = args.output_dir / REPORT_FILENAME
    review_path = args.output_dir / REVIEW_FILENAME
    save_json(catalog_path, catalog)
    save_json(report_path, report)
    save_json(review_path, review)

    print("\nGeneración completada:")
    print(f"- Cartas base: {catalogue_stats['cards']}")
    print(f"- Impresiones: {catalogue_stats['printings']}")
    print(f"- Con mapping Cardmarket: {catalogue_stats['printingsWithCardmarketMapping']}")
    print(f"- Con precio Cardmarket actual: {catalogue_stats['printingsWithCardmarketPrice']}")
    print(f"- Mappings pendientes de revisión: {len(review.get('needsReview', []))}")
    print(
        "- Mapping QA: "
        f"{len(mapping_validation.get('repaired', []))} reparados / "
        f"{len(mapping_validation.get('quarantined', []))} en cuarentena"
    )
    print(f"- Price Guide createdAt: {price_created_at}")

    if not args.no_push:
        publish_paths = [
            args.raw_dir / RAW_FILENAMES["bandai"],
            args.raw_dir / RAW_FILENAMES["cardmarket_products"],
            args.raw_dir / RAW_FILENAMES["cardmarket_prices"],
            args.data_dir / MAPPING_FILENAME,
            args.data_dir / IMAGE_CACHE_FILENAME,
            catalog_path,
            report_path,
            review_path,
        ]
        publish_to_git(publish_paths)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrumpido por el usuario.")
        sys.exit(130)
    except Exception as error:
        print(f"ERROR: {error}")
        sys.exit(1)
