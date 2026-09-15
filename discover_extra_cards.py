#!/usr/bin/env python3
"""
One Piece TCG catalogue pipeline v3.13.0 (OPlay multilingual printing layer over the validated v3.11.x baseline).

Live sources:
  1) Bandai official card list -> official EN card/game/printing metadata
  2) OPlayTCG public library/sitemaps -> multilingual physical printings, sets and exact artwork URLs
  3) Cardmarket public product catalogue -> idProduct / idMetacard market identifiers
  4) Cardmarket public daily price guide -> EUR prices
  5) Cardmarket public non-singles catalogue -> expansion/language evidence

OPTCGAPI is retired in V3.13.0. Cardmarket HTML image discovery is legacy/opt-in only.

Persistent local knowledge:
  data/cardmarket_mapping.json -> Bandai printing <-> Cardmarket idProduct
  data/cardmarket_printing_metadata_v1.json -> explicit Cardmarket version/language overrides
  data/oplay_cardmarket_mapping_v1.json -> exact OPlay printing <-> Cardmarket idProduct links
  output/cardmarket_price_history_v3.json -> compact daily EUR valuation history
  output/storage_report_v1.json -> per-folder/file storage audit

Default image policy is REMOTE: exact Bandai/OPlay URLs are written directly into the catalog.
No new images are downloaded unless --image-storage-mode cache is explicitly selected.

V3.13.0 preserves the validated V3.11.x Bandai/Cardmarket identity and pricing contract,
then overlays OPlay as the canonical multilingual physical-printing/image source whenever
Bandai does not already provide the same English printing. OPlay artwork is printing-scoped;
ambiguous Cardmarket market products are never assigned an OPlay image by guesswork.
Unknown market products remain auditable but can be hidden from collection/version UI when
canonical OPlay printings exist for the same card.

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
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

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
CARDMARKET_NONSINGLES_URL = (
    "https://downloads.s3.cardmarket.com/productCatalog/productList/"
    "products_nonsingles_18.json"
)
CARDMARKET_BASE_URL = "https://www.cardmarket.com"
CARDMARKET_PRODUCT_REDIRECT = "https://www.cardmarket.com/en/OnePiece/Products?idProduct={product_id}"

OPLAY_BASE_URL = "https://oplaytcg.com"
OPLAY_LIBRARY_URL = f"{OPLAY_BASE_URL}/en/library"
OPLAY_SITEMAP_URL = f"{OPLAY_BASE_URL}/sitemap.xml"
OPLAY_IMAGE_HOST = "cards.oplaytcg.com"
OPLAY_LANGUAGES = {
    "en": {"language": "en", "label": "English"},
    "jp": {"language": "ja", "label": "Japanese"},
    "fr": {"language": "fr", "label": "French"},
    "th": {"language": "th", "label": "Thai"},
    "tc": {"language": "zh-Hant", "label": "Traditional Chinese"},
    "cn": {"language": "zh-Hans", "label": "Simplified Chinese"},
    "kr": {"language": "ko", "label": "Korean"},
}
OPLAY_RAW_FILENAME = "oplaytcg_catalog_raw.json"
OPLAY_CARDMARKET_MAPPING_FILENAME = "oplay_cardmarket_mapping_v1.json"
CARDMARKET_PRINTING_METADATA_FILENAME = "cardmarket_printing_metadata_v1.json"
OPLAY_MAPPING_REVIEW_FILENAME = "oplay_mapping_review.json"
STORAGE_REPORT_FILENAME = "storage_report_v1.json"

# Optional community fallback used ONLY for missing preview images. OPTCGAPI.com
# documents these GET endpoints as open/no-auth and explicitly lists app/database
# consumption as a supported use. A failure here never aborts the official pipeline.
OPTCGAPI_BASE_URL = "https://optcgapi.com"
OPTCGAPI_DON_URL = f"{OPTCGAPI_BASE_URL}/api/allDonCards/"
OPTCGAPI_PROMO_URLS = [
    f"{OPTCGAPI_BASE_URL}/api/allPromos/",      # observed live route
    f"{OPTCGAPI_BASE_URL}/api/allPromoCards/", # documented fallback
]

VEGAPULL_PINNED_VERSION = "1.3.0"
VEGAPULL_MIN_VERSION = (1, 2, 3)

DEFAULT_RAW_DIR = Path("raw")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_OUTPUT_DIR = Path("output")

RAW_FILENAMES = {
    "bandai": "bandai_cards_raw.json",
    "cardmarket_products": "cardmarket_products_raw.json",
    "cardmarket_prices": "cardmarket_price_guide_raw.json",
    "cardmarket_nonsingles": "cardmarket_products_nonsingles_raw.json",
    "oplay": OPLAY_RAW_FILENAME,
}
OPTIONAL_RAW_KEYS = {"oplay", "cardmarket_nonsingles"}

MAPPING_FILENAME = "cardmarket_mapping.json"
IMAGE_CACHE_FILENAME = "image_health_cache.json"
CARDMARKET_IMAGE_MAPPING_FILENAME = "cardmarket_image_mapping.json"
CARDMARKET_PRODUCT_IMAGE_HOST = "product-images.s3.cardmarket.com"
CARDMARKET_IMAGE_FAILURE_RETRY_DAYS = 7
IMAGE_ASSET_MANIFEST_FILENAME = "image_assets_v1.json"
CARDMARKET_IMAGE_OVERRIDES_FILENAME = "cardmarket_image_overrides.json"
CARDMARKET_IMAGE_PENDING_FILENAME = "cardmarket_image_pending.json"
DEFAULT_IMAGE_DIR = Path("images")
DEFAULT_IMAGE_PUBLIC_BASE_URL = os.environ.get(
    "CARDIFY_IMAGE_PUBLIC_BASE_URL",
    "https://raw.githubusercontent.com/miguelsanchezgarc13/cardify-data/refs/heads/main/images",
).rstrip("/")
CATALOG_FILENAME = "cards_multisource_v3.json"
REPORT_FILENAME = "cards_multisource_v3_report.json"
REVIEW_FILENAME = "cardmarket_mapping_review.json"
SETS_FILENAME = "sets_multisource_v3.json"
PRICE_HISTORY_FILENAME = "cardmarket_price_history_v3.json"
MANIFEST_FILENAME = "catalog_manifest_v3.json"

LEGACY_PRICE_FILENAME = "cardmarket_prices_raw.json"
LEGACY_CARDS_FILENAME = "cardmarket_cards_raw.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; OPTCG-Catalogue/3.12.0; "
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
        "--image-dir",
        type=Path,
        default=DEFAULT_IMAGE_DIR,
        help="Carpeta persistente de imágenes exactas descargadas (por defecto: images/).",
    )
    parser.add_argument(
        "--image-public-base-url",
        default=DEFAULT_IMAGE_PUBLIC_BASE_URL,
        help=(
            "URL pública base desde la que Cardify leerá images/. Por defecto apunta "
            "al branch main de cardify-data en raw.githubusercontent.com."
        ),
    )
    parser.add_argument(
        "--no-persist-images",
        action="store_true",
        help="No descarga nuevas imágenes a images/; reutiliza únicamente las ya guardadas.",
    )
    parser.add_argument(
        "--refresh-image-assets",
        action="store_true",
        help="Fuerza volver a descargar assets ya existentes. Normalmente NO debe usarse.",
    )
    parser.add_argument(
        "--image-asset-max-new",
        type=int,
        default=0,
        help="Máximo de nuevos ficheros de imagen a guardar por run. 0 = sin límite.",
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
        "--no-oplay",
        action="store_true",
        help="Desactiva OPlay y conserva únicamente la baseline Bandai/Cardmarket.",
    )
    parser.add_argument(
        "--oplay-refresh-metadata",
        action="store_true",
        help="Vuelve a descargar metadata de páginas OPlay ya cacheadas. Normalmente no hace falta.",
    )
    parser.add_argument(
        "--oplay-card-metadata-max-new",
        type=int,
        default=0,
        help="Máximo de páginas de cartas OPlay nuevas a consultar por run; 0 = sin límite.",
    )
    parser.add_argument(
        "--oplay-deep-crawl",
        action="store_true",
        help=(
            "Fallback caro: si el sitemap no expone imágenes, recorre páginas por idioma. "
            "No se activa por defecto."
        ),
    )
    parser.add_argument(
        "--image-storage-mode",
        choices=("remote", "cache"),
        default="remote",
        help=(
            "remote (por defecto): usa URLs exactas Bandai/OPlay sin descargar imágenes. "
            "cache: conserva/descarga assets en images/."
        ),
    )
    image_discovery_group = parser.add_mutually_exclusive_group()
    image_discovery_group.add_argument(
        "--cardmarket-image-discovery",
        dest="cardmarket_image_discovery",
        action="store_true",
        help=(
            "Consulta automáticamente Cardmarket/CDN para intentar "
            "descubrir imágenes exactas de idProduct. Los assets ya guardados no se consultan de nuevo."
        ),
    )
    image_discovery_group.add_argument(
        "--no-cardmarket-image-discovery",
        dest="cardmarket_image_discovery",
        action="store_false",
        help="Desactiva el discovery de imágenes nuevas de Cardmarket (los assets guardados se conservan).",
    )
    parser.set_defaults(cardmarket_image_discovery=False)
    parser.add_argument(
        "--cardmarket-image-delay",
        type=float,
        default=0.75,
        help=(
            "Pausa entre páginas de producto Cardmarket al descubrir imágenes "
            "nuevas (segundos; por defecto: 0.75)."
        ),
    )
    parser.add_argument(
        "--cardmarket-image-max-new",
        type=int,
        default=0,
        help=(
            "Máximo de páginas Cardmarket nuevas a consultar por run. 0 = sin "
            "límite. Las imágenes ya cacheadas no consumen el límite."
        ),
    )
    parser.add_argument(
        "--price-history-days",
        type=int,
        default=90,
        help=(
            "Días de histórico diario de valoración Cardmarket a conservar "
            "en output/cardmarket_price_history_v3.json (por defecto: 90)."
        ),
    )
    parser.add_argument(
        "--no-price-history",
        action="store_true",
        help="No actualiza el histórico diario de valoraciones Cardmarket.",
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


def save_json_compact(path: Path, data) -> None:
    """Write canonical JSON without whitespace; semantics are identical to save_json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
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
        # The current public bulk catalogue normally exposes only idExpansion,
        # but keep these optional fields if Cardmarket adds them in the future or
        # a compatible cached source already contains them.
        "expansionName": nullable_text(
            row.get("expansionName") or row.get("expansion_name") or row.get("setName")
        ),
        "expansionCode": nullable_text(
            row.get("expansionCode") or row.get("expansion_code") or row.get("setCode")
        ),
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
        "schemaVersion": 5,
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

        # V3.6: the public Cardmarket catalogue occasionally contains a wrong
        # card number while the product name and expansion are unambiguous. A
        # name-based mapping is allowed to survive future QA runs only when it
        # was created by our strict release+exact-name rule and the evidence
        # still matches the live product. This is not a generic code-mismatch
        # bypass.
        name_evidence = entry.get("validationEvidence") or {}
        if (
            product is not None
            and entry.get("source") == "auto-bandai-release-name-unique"
            and name_evidence.get("rule")
            == "unique-unused-product-name-in-bandai-release-expansion"
            and slugify(_product_display_name(product))
            == name_evidence.get("normalizedName")
            and product.get("idExpansion")
            == (name_evidence.get("releaseProfile") or {}).get("value")
        ):
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


def _bandai_structured_release_codes(records: list[dict]) -> set[str]:
    """Extract a unique OP/EB/ST/PRB release identity from Bandai records."""
    codes = set()
    for record in records:
        text = " ".join(
            str(record.get(field) or "")
            for field in ("seriesLabel", "cardSetsText")
        )
        for match in re.finditer(r"\b(OP|EB|ST|PRB)[-\s]?0*(\d{1,2})\b", text.upper()):
            codes.add(f"{match.group(1)}{int(match.group(2)):02d}")
    return codes


def _stable_release_expansion_profiles(
    mapping: dict,
    products_by_id: dict[int, dict],
) -> dict[str, dict]:
    """High-confidence legacy/current mapping evidence: Bandai release -> Cardmarket expansion."""
    profiles = _build_mapping_profiles(mapping, products_by_id)
    stable = {}
    for release_code, counts in profiles["releaseExpansions"].items():
        dominant = _dominant_counter_value(
            counts,
            min_support=3,
            min_ratio=0.90,
        )
        if dominant:
            stable[release_code] = dominant
    return stable



def analyze_bandai_series_expansions_from_catalog(
    bandai_cards: list[dict],
    products: list[dict],
) -> dict[str, dict]:
    """Independent Bandai-series -> Cardmarket-expansion diagnostics.

    Validation uses *coverage* (recall) rather than product-count precision:
    old Cardmarket expansions can contain extra variants while still containing
    every card in the Bandai release. This keeps original ST01/ST02-style
    expansions plausible while still rejecting unrelated Promo/Other families.
    A unique high-F1 candidate may be used as a fallback profile, but near-tied
    regional twins remain deliberately ambiguous.
    """
    by_series = defaultdict(list)
    for card in bandai_cards:
        series_id = str(card.get("seriesId") or "")
        if series_id:
            by_series[series_id].append(card)

    expansion_counts = defaultdict(Counter)
    for product in products:
        code = _card_code_from_product(product)
        if code:
            expansion_counts[product.get("idExpansion")][code] += 1

    diagnostics = {}
    for series_id, records in sorted(by_series.items()):
        release_codes = _bandai_structured_release_codes(records)
        if not release_codes:
            continue

        bandai_counts = Counter(
            canonical_id(card.get("cardNo"))
            for card in records
            if canonical_id(card.get("cardNo"))
        )
        total = sum(bandai_counts.values())
        if total < 10:
            continue

        ranked = []
        for expansion_id, product_counts in expansion_counts.items():
            score = _multiset_similarity(bandai_counts, product_counts)
            if score["intersection"] < 10:
                continue
            ranked.append({"idExpansion": expansion_id, **score})
        ranked.sort(
            key=lambda row: (
                row["f1"],
                row["recall"],
                -row["distance"],
                row["intersection"],
            ),
            reverse=True,
        )
        if not ranked:
            continue

        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        # An expansion is plausible for identity validation when it covers at
        # least 95% of the Bandai release. Precision is intentionally omitted:
        # an old expansion may contain extra alternate products.
        plausible = [
            row["idExpansion"]
            for row in ranked
            if row["recall"] >= 0.95
            and row["intersection"] >= max(10, int(total * 0.95))
        ]

        trusted = None
        margin = top["f1"] - (second["f1"] if second else 0.0)
        if (
            len(plausible) == 1
            and top["idExpansion"] == plausible[0]
            and top["f1"] >= 0.90
            and top["precision"] >= 0.90
            and top["recall"] >= 0.95
            and margin >= 0.05
        ):
            trusted = {
                "value": top["idExpansion"],
                "count": top["intersection"],
                "total": top["bandaiTotal"],
                "ratio": top["recall"],
                "precision": top["precision"],
                "f1": top["f1"],
                "margin": margin,
                "source": "bandai-cardmarket-series-multiset",
                "seriesId": series_id,
            }

        diagnostics[series_id] = {
            "seriesId": series_id,
            "seriesLabels": sorted(
                {str(r.get("seriesLabel") or "") for r in records if r.get("seriesLabel")}
            ),
            "releaseCodes": sorted(release_codes),
            "bandaiTotal": total,
            "top": top,
            "second": second,
            "plausibleExpansions": plausible,
            "trustedProfile": trusted,
        }
    return diagnostics


def _stable_bandai_series_expansion_profiles(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
    series_diagnostics: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """High-confidence current Bandai series -> Cardmarket expansion profiles.

    Current exact mappings are the primary evidence. A minimum of five mapped
    printings and >=90% agreement prevents two stale mappings from defining a
    new release (notably OP17). When mapping evidence is insufficient, a unique
    high-confidence catalogue multiset profile may be used.
    """
    entries = mapping.setdefault("mappings", {})
    by_series = defaultdict(list)
    for card in bandai_cards:
        series_id = str(card.get("seriesId") or "")
        if series_id:
            by_series[series_id].append(card)

    stable = {}
    diagnostics = series_diagnostics or {}
    for series_id, records in sorted(by_series.items()):
        if not _bandai_structured_release_codes(records):
            continue

        counts = Counter()
        seen_printings = set()
        for record in records:
            printing_id = canonical_id(record.get("sourcePrintingId"))
            if not printing_id or printing_id in seen_printings:
                continue
            seen_printings.add(printing_id)
            entry = entries.get(printing_id)
            if not isinstance(entry, dict):
                continue
            product_number = get_number(entry.get("productId"))
            if product_number is None:
                continue
            product = products_by_id.get(int(product_number))
            expansion = (
                product.get("idExpansion")
                if isinstance(product, dict)
                else entry.get("idExpansion")
            )
            if expansion is not None:
                counts[expansion] += 1

        dominant = _dominant_counter_value(counts, min_support=5, min_ratio=0.90)
        diag = diagnostics.get(series_id) or {}
        plausible = set(diag.get("plausibleExpansions") or [])
        if dominant and (not plausible or dominant["value"] in plausible):
            stable[series_id] = {
                **dominant,
                "source": "bandai-current-series-mapping-majority",
                "seriesId": series_id,
                "releaseCodes": sorted(_bandai_structured_release_codes(records)),
            }
            continue

        trusted = diag.get("trustedProfile")
        if isinstance(trusted, dict):
            stable[series_id] = dict(trusted)

    return stable


def _bandai_expected_expansion_profile(
    records: list[dict],
    release_profiles: dict[str, dict],
    series_profiles: dict[str, dict],
) -> dict | None:
    """Return one trusted expected expansion for current Bandai records."""
    series_matches = []
    for record in records:
        profile = series_profiles.get(str(record.get("seriesId") or ""))
        if isinstance(profile, dict) and profile.get("value") is not None:
            series_matches.append(profile)
    series_values = {profile["value"] for profile in series_matches}
    if len(series_values) == 1:
        value = next(iter(series_values))
        representative = next(p for p in series_matches if p["value"] == value)
        return {
            **representative,
            "value": value,
            "evidenceKind": "bandai-series",
            "seriesIds": sorted(
                {str(r.get("seriesId")) for r in records if r.get("seriesId")}
            ),
            "releaseCodes": sorted(_bandai_structured_release_codes(records)),
        }

    release_codes = _bandai_structured_release_codes(records)
    release_matches = [
        release_profiles[code]
        for code in sorted(release_codes)
        if code in release_profiles
    ]
    release_values = {profile["value"] for profile in release_matches}
    if len(release_values) == 1:
        value = next(iter(release_values))
        representative = next(p for p in release_matches if p["value"] == value)
        return {
            **representative,
            "value": value,
            "evidenceKind": "release-code",
            "releaseCodes": sorted(release_codes),
        }
    return None


def _bandai_plausible_expansions_for_records(
    records: list[dict],
    series_diagnostics: dict[str, dict],
) -> set[int]:
    sets = []
    for record in records:
        diag = series_diagnostics.get(str(record.get("seriesId") or ""))
        if not isinstance(diag, dict):
            continue
        values = {int(v) for v in (diag.get("plausibleExpansions") or []) if v is not None}
        if values:
            sets.append(values)
    if not sets:
        return set()
    result = set(sets[0])
    for values in sets[1:]:
        result &= values
    return result

def validate_mapping_against_bandai(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
    release_profiles: dict[str, dict] | None = None,
    series_profiles: dict[str, dict] | None = None,
    series_diagnostics: dict[str, dict] | None = None,
) -> dict:
    """Bandai-authoritative identity firewall (V3.7).

    Every *active current Bandai printing* is checked before publication.
    Series-level evidence is preferred because combined releases such as
    OP15-EB04 contain more than one structured code. If no unique trusted
    expansion exists (e.g. OP17 regional twins), catalogue coverage still
    supplies a set of plausible expansions; products outside that set are
    quarantined without guessing which twin is correct.
    """
    entries = mapping.setdefault("mappings", {})
    mapping["schemaVersion"] = max(int(mapping.get("schemaVersion") or 0), 5)
    stable_release = (
        dict(release_profiles)
        if release_profiles is not None
        else _stable_release_expansion_profiles(mapping, products_by_id)
    )
    stable_series = dict(series_profiles or {})
    diagnostics = dict(series_diagnostics or {})

    by_source = defaultdict(list)
    for card in bandai_cards:
        source_id = canonical_id(card.get("sourcePrintingId"))
        if source_id:
            by_source[source_id].append(card)

    quarantined = []
    for printing_id, records in sorted(by_source.items()):
        entry = entries.get(printing_id)
        if not isinstance(entry, dict):
            continue
        product_number = get_number(entry.get("productId"))
        if product_number is None:
            continue

        product_id = int(product_number)
        product = products_by_id.get(product_id)
        current_expansion = (
            product.get("idExpansion")
            if isinstance(product, dict)
            else entry.get("idExpansion")
        )

        expected = _bandai_expected_expansion_profile(
            records, stable_release, stable_series
        )
        plausible = _bandai_plausible_expansions_for_records(records, diagnostics)

        reason = None
        expected_expansion = None
        if expected is not None:
            expected_expansion = expected.get("value")
            if current_expansion != expected_expansion:
                reason = "bandai-series-expansion-mismatch" if (
                    expected.get("evidenceKind") == "bandai-series"
                ) else "bandai-release-expansion-mismatch"
        elif plausible and current_expansion not in plausible:
            reason = "bandai-series-expansion-not-plausible"

        if reason is None:
            continue

        old_url = entry.get("url")
        old_source = entry.get("source")
        previous_evidence = entry.get("validationEvidence")
        entry["invalidProductId"] = product_id
        entry["invalidUrl"] = old_url
        entry["invalidReason"] = reason
        entry["productId"] = None
        entry["url"] = None
        entry["confirmed"] = False
        entry["source"] = "auto-quarantine-bandai-identity-v37"
        if previous_evidence is not None:
            entry["previousValidationEvidence"] = previous_evidence
        entry["validationEvidence"] = {
            "releaseCodes": sorted(_bandai_structured_release_codes(records)),
            "seriesIds": sorted(
                {str(r.get("seriesId")) for r in records if r.get("seriesId")}
            ),
            "seriesProfile": expected if expected and expected.get("evidenceKind") == "bandai-series" else None,
            "releaseProfile": expected if expected and expected.get("evidenceKind") == "release-code" else None,
            "plausibleExpansions": sorted(plausible),
            "currentExpansion": current_expansion,
            "expectedExpansion": expected_expansion,
            "rule": "current-bandai-printing-must-match-release-expansion",
        }

        quarantined.append(
            {
                "printingId": printing_id,
                "oldProductId": product_id,
                "oldSource": old_source,
                "releaseCodes": sorted(_bandai_structured_release_codes(records)),
                "seriesIds": sorted(
                    {str(r.get("seriesId")) for r in records if r.get("seriesId")}
                ),
                "currentExpansion": current_expansion,
                "expectedExpansion": expected_expansion,
                "plausibleExpansions": sorted(plausible),
                "reason": reason,
            }
        )

    if quarantined:
        mapping["updatedAt"] = utc_now_iso()

    reason_counts = Counter(item["reason"] for item in quarantined)
    return {
        "quarantined": quarantined,
        "summary": {
            "quarantined": len(quarantined),
            "reasonCounts": dict(sorted(reason_counts.items())),
        },
        "releaseProfiles": stable_release,
        "seriesProfiles": stable_series,
    }

def _card_code_from_product(product: dict) -> str | None:
    matches = list(CARD_CODE_RE.finditer(str(product.get("name") or "")))
    if not matches:
        return None
    return canonical_id(matches[-1].group(0))


def _multiset_similarity(left: Counter, right: Counter) -> dict:
    keys = set(left) | set(right)
    intersection = sum(min(left[key], right[key]) for key in keys)
    missing = sum(max(left[key] - right[key], 0) for key in keys)
    extra = sum(max(right[key] - left[key], 0) for key in keys)
    left_total = sum(left.values())
    right_total = sum(right.values())
    precision = intersection / right_total if right_total else 0.0
    recall = intersection / left_total if left_total else 0.0
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "intersection": intersection,
        "missing": missing,
        "extra": extra,
        "bandaiTotal": left_total,
        "cardmarketTotal": right_total,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "distance": missing + extra,
    }


def infer_release_expansions_from_catalog(
    bandai_cards: list[dict],
    products: list[dict],
    existing_profiles: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """
    Infer *new* Cardmarket expansion IDs from the public catalogue only when the
    Bandai release multiset has one overwhelmingly better Cardmarket match.

    This intentionally rejects near ties such as English vs Asia-region/Japanese
    twins. It never overrides an already established release profile.
    """
    by_series = defaultdict(list)
    for card in bandai_cards:
        series_id = str(card.get("seriesId") or "")
        if series_id:
            by_series[series_id].append(card)

    expansion_counts = defaultdict(Counter)
    for product in products:
        code = _card_code_from_product(product)
        if code:
            expansion_counts[product.get("idExpansion")][code] += 1

    candidates_by_release = defaultdict(list)
    diagnostics = {}

    for series_id, records in by_series.items():
        release_codes = _bandai_structured_release_codes(records)
        if len(release_codes) != 1:
            continue
        release_code = next(iter(release_codes))
        if release_code in existing_profiles:
            continue

        bandai_counts = Counter(
            canonical_id(card.get("cardNo"))
            for card in records
            if canonical_id(card.get("cardNo"))
        )
        if sum(bandai_counts.values()) < 10:
            continue

        ranked = []
        for expansion_id, product_counts in expansion_counts.items():
            score = _multiset_similarity(bandai_counts, product_counts)
            if score["intersection"] < 10:
                continue
            ranked.append(
                {
                    "idExpansion": expansion_id,
                    **score,
                }
            )
        ranked.sort(
            key=lambda row: (
                row["f1"],
                -row["distance"],
                row["intersection"],
            ),
            reverse=True,
        )
        if not ranked:
            continue

        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        margin = top["f1"] - (second["f1"] if second else 0.0)
        diagnostics[release_code] = {
            "seriesId": series_id,
            "top": top,
            "second": second,
            "margin": margin,
        }

        if (
            top["f1"] >= 0.90
            and top["precision"] >= 0.90
            and top["recall"] >= 0.90
            and margin >= 0.05
        ):
            candidates_by_release[release_code].append(
                {
                    "value": top["idExpansion"],
                    "count": top["intersection"],
                    "total": top["bandaiTotal"],
                    "ratio": top["recall"],
                    "precision": top["precision"],
                    "f1": top["f1"],
                    "margin": margin,
                    "source": "bandai-cardmarket-multiset",
                    "seriesId": series_id,
                }
            )

    inferred = {}
    for release_code, proposals in candidates_by_release.items():
        expansion_ids = {proposal["value"] for proposal in proposals}
        if len(expansion_ids) == 1:
            inferred[release_code] = max(
                proposals,
                key=lambda proposal: (
                    proposal["f1"],
                    proposal["count"],
                ),
            )

    return inferred, diagnostics


def _semantic_class_from_printing_id(printing_id: str) -> str:
    pid = canonical_id(printing_id)
    if re.search(r"_R\d+(?:_|$)", pid):
        return "reprint"
    if re.search(r"_P\d+(?:_|$)", pid):
        return "parallel"
    return "base"


def _semantic_class_from_bandai_records(
    printing_id: str,
    records: list[dict],
) -> str:
    if any(bool(record.get("isReprint")) for record in records):
        return "reprint"
    if any(bool(record.get("isParallel")) for record in records):
        return "parallel"
    return _semantic_class_from_printing_id(printing_id)


def _product_expansion_for_mapping_entry(
    entry: dict,
    products_by_id: dict[int, dict],
) -> int | None:
    product_number = get_number(entry.get("productId"))
    if product_number is None:
        return None
    product = products_by_id.get(int(product_number))
    if isinstance(product, dict):
        return product.get("idExpansion")
    return entry.get("idExpansion")



def _quarantine_semantic_drift_entry(
    mapping_key: str,
    entry: dict,
    reason: str,
    evidence: dict,
) -> dict:
    product_number = get_number(entry.get("productId"))
    product_id = int(product_number) if product_number is not None else None
    old_url = entry.get("url")
    old_source = entry.get("source")
    previous_evidence = entry.get("validationEvidence")
    entry["invalidProductId"] = product_id
    entry["invalidUrl"] = old_url
    entry["invalidReason"] = reason
    entry["productId"] = None
    entry["url"] = None
    entry["confirmed"] = False
    entry["source"] = "auto-quarantine-bandai-semantic-drift-v37"
    if previous_evidence is not None:
        entry["previousValidationEvidence"] = previous_evidence
    entry["validationEvidence"] = {**evidence, "rule": reason}
    return {
        "mappingKey": mapping_key,
        "oldProductId": product_id,
        "oldSource": old_source,
        "reason": reason,
        **evidence,
    }


def _reconcile_release_specific_mapping_drift(
    bandai_by_source: dict[str, list[dict]],
    mapping: dict,
    products_by_id: dict[int, dict],
    release_profiles: dict[str, dict],
    series_profiles: dict[str, dict],
    allow_migrate: bool,
) -> dict:
    """V3.7 repair for Bandai suffix drift across generic/release buckets.

    A Cardmarket product tied to a trusted release must not remain on an orphan
    or generic Bandai printing when current Bandai exposes another printing of
    the same card in that release. One-to-one cases are migrated without using
    V.1/V.2/order heuristics. Ambiguous cases are quarantined, never priced.
    """
    entries = mapping.setdefault("mappings", {})

    targets_by_base_expansion = defaultdict(list)
    for printing_id, records in sorted(bandai_by_source.items()):
        expected = _bandai_expected_expansion_profile(
            records, release_profiles, series_profiles
        )
        if not expected or expected.get("value") is None:
            continue
        targets_by_base_expansion[(base_code(printing_id), expected["value"])].append(
            {
                "printingId": printing_id,
                "records": records,
                "expected": expected,
                "semanticClass": _semantic_class_from_bandai_records(
                    printing_id, records
                ),
            }
        )

    direct_quarantine = []
    proposals = []
    for donor_key, donor in sorted(entries.items()):
        if not isinstance(donor, dict):
            continue
        product_number = get_number(donor.get("productId"))
        if product_number is None:
            continue
        product_id = int(product_number)
        product = products_by_id.get(product_id)
        product_expansion = (
            product.get("idExpansion")
            if isinstance(product, dict)
            else donor.get("idExpansion")
        )
        if product_expansion is None:
            continue

        canonical_donor = canonical_id(donor_key)
        donor_records = bandai_by_source.get(canonical_donor, [])
        donor_expected = (
            _bandai_expected_expansion_profile(
                donor_records, release_profiles, series_profiles
            )
            if donor_records
            else None
        )
        # Explicit current release mappings are handled by the identity firewall.
        if donor_expected is not None:
            continue

        targets = [
            target
            for target in targets_by_base_expansion.get(
                (base_code(donor_key), product_expansion), []
            )
            if target["printingId"] != canonical_donor
        ]
        if not targets:
            continue

        donor_semantic = _semantic_class_from_printing_id(donor_key)
        same_semantic = [
            target for target in targets
            if target["semanticClass"] == donor_semantic
        ]
        if len(same_semantic) == 1:
            eligible = same_semantic
        elif len(targets) == 1:
            # Release identity is stronger than a shifted P/base suffix. This is
            # required for P-057 -> P-057_P1 style drift.
            eligible = targets
        else:
            eligible = []

        evidence = {
            "productId": product_id,
            "idExpansion": product_expansion,
            "baseCode": base_code(donor_key),
            "donorSemanticClass": donor_semantic,
            "candidateBandaiPrintingIds": [t["printingId"] for t in targets],
        }

        if len(eligible) != 1:
            direct_quarantine.append(
                (donor_key, donor, "ambiguous-release-specific-generic-mapping", evidence)
            )
            continue

        target = eligible[0]
        target_key = target["printingId"]
        target_entry = entries.get(target_key)
        target_active = (
            isinstance(target_entry, dict)
            and get_number(target_entry.get("productId")) is not None
        )
        if target_active:
            direct_quarantine.append(
                (
                    donor_key,
                    donor,
                    "release-specific-product-on-generic-id-target-already-mapped",
                    {
                        **evidence,
                        "targetPrintingId": target_key,
                        "targetProductId": int(get_number(target_entry.get("productId"))),
                    },
                )
            )
            continue

        proposals.append(
            {
                "donorKey": donor_key,
                "donor": donor,
                "productId": product_id,
                "productExpansion": product_expansion,
                "target": target,
                "evidence": evidence,
            }
        )

    by_target = defaultdict(list)
    for proposal in proposals:
        by_target[proposal["target"]["printingId"]].append(proposal)

    migrated = []
    quarantined = []
    for donor_key, donor, reason, evidence in direct_quarantine:
        quarantined.append(
            _quarantine_semantic_drift_entry(donor_key, donor, reason, evidence)
        )

    for target_key, competing in sorted(by_target.items()):
        if len(competing) != 1 or not allow_migrate:
            reason = (
                "multiple-release-specific-donors-for-bandai-printing"
                if len(competing) != 1
                else "release-specific-alias-requires-auto-map"
            )
            for proposal in competing:
                quarantined.append(
                    _quarantine_semantic_drift_entry(
                        proposal["donorKey"],
                        proposal["donor"],
                        reason,
                        {
                            **proposal["evidence"],
                            "targetPrintingId": target_key,
                            "competingDonorKeys": [p["donorKey"] for p in competing],
                        },
                    )
                )
            continue

        proposal = competing[0]
        donor = proposal["donor"]
        target_info = proposal["target"]
        target = entries.get(target_key)
        if not isinstance(target, dict):
            target = {}
            entries[target_key] = target

        if target.get("invalidProductId") is not None:
            target.setdefault("supersededProductId", target.get("invalidProductId"))
            target.setdefault("supersededUrl", target.get("invalidUrl"))
        previous_legacy_set = target.get("legacySet")
        release_codes = target_info["expected"].get("releaseCodes") or []
        donor_structured_set = _structured_release_code(donor.get("legacySet"))
        new_legacy_set = (
            release_codes[0]
            if len(release_codes) == 1
            else donor_structured_set or donor.get("legacySet")
        )
        if (
            previous_legacy_set
            and new_legacy_set
            and compact_release_text(previous_legacy_set) != compact_release_text(new_legacy_set)
        ):
            target.setdefault("previousLegacySet", previous_legacy_set)

        product_id = proposal["productId"]
        product = products_by_id.get(product_id) or {}
        if new_legacy_set:
            target["legacySet"] = new_legacy_set
        target["productId"] = product_id
        target["url"] = donor.get("url") or product.get("website") or CARDMARKET_PRODUCT_REDIRECT.format(product_id=product_id)
        target["confirmed"] = False
        target["source"] = "auto-bandai-release-semantic-alias-v37"
        target.setdefault("addedAt", utc_now_iso())
        target["productName"] = product.get("name") or donor.get("productName")
        target["idExpansion"] = product.get("idExpansion", donor.get("idExpansion"))
        target["idMetacard"] = product.get("idMetacard", donor.get("idMetacard"))
        target["dateAdded"] = product.get("dateAdded", donor.get("dateAdded"))
        target["validationEvidence"] = {
            "targetBandaiProfile": target_info["expected"],
            "donorMappingKey": proposal["donorKey"],
            "donorSemanticClass": proposal["evidence"]["donorSemanticClass"],
            "targetSemanticClass": target_info["semanticClass"],
            "idExpansion": proposal["productExpansion"],
            "rule": "unique-release-specific-product-moved-to-current-bandai-printing",
        }
        for field in ("invalidProductId", "invalidUrl", "invalidReason"):
            target.pop(field, None)

        donor["previousProductId"] = product_id
        donor["previousUrl"] = donor.get("url")
        donor["productId"] = None
        donor["url"] = None
        donor["confirmed"] = False
        donor["source"] = "alias-moved-to-current-bandai-id-v37"
        donor["aliasMovedTo"] = target_key

        migrated.append(
            {
                "printingId": target_key,
                "productId": product_id,
                "donorMappingKey": proposal["donorKey"],
                "idExpansion": proposal["productExpansion"],
                "donorSemanticClass": proposal["evidence"]["donorSemanticClass"],
                "targetSemanticClass": target_info["semanticClass"],
                "reason": "release-specific-semantic-drift",
            }
        )

    if migrated or quarantined:
        mapping["updatedAt"] = utc_now_iso()
    return {"migrated": migrated, "quarantined": quarantined}

def _migrate_semantic_release_aliases(
    bandai_by_source: dict[str, list[dict]],
    mapping: dict,
    products_by_id: dict[int, dict],
    release_profiles: dict[str, dict],
) -> list[dict]:
    """
    Repair suffix drift without guessing suffix numbers.

    A historical mapped P/R key may no longer exist in current Bandai, or may
    now live in Bandai's generic Promotion/Other bucket. We move its Cardmarket
    product only when:
      * current Bandai gives one explicit structured release,
      * the product belongs to that release's trusted Cardmarket expansion,
      * historical and current semantic classes agree (parallel/reprint/base),
      * exactly one donor exists, and
      * the donor is orphaned or generic in current Bandai.

    Explicit current Bandai releases are never stolen from.
    """
    entries = mapping.setdefault("mappings", {})
    unresolved = []

    for printing_id, records in sorted(bandai_by_source.items()):
        if not records:
            continue
        cm_key, _ = resolve_cardmarket_mapping_key_for_bandai_record(
            records[0], entries
        )
        if cm_key is not None:
            continue

        release_codes = _bandai_structured_release_codes(records)
        if len(release_codes) != 1:
            continue
        release_code = next(iter(release_codes))
        profile = release_profiles.get(release_code)
        if not profile:
            continue

        unresolved.append(
            {
                "printingId": printing_id,
                "baseCode": base_code(printing_id),
                "releaseCode": release_code,
                "idExpansion": profile["value"],
                "releaseProfile": profile,
                "semanticClass": _semantic_class_from_bandai_records(
                    printing_id, records
                ),
            }
        )

    proposals = []
    for target in unresolved:
        donors = []
        for donor_key, donor_entry in entries.items():
            if donor_key == target["printingId"]:
                continue
            if not isinstance(donor_entry, dict):
                continue
            product_number = get_number(donor_entry.get("productId"))
            if product_number is None:
                continue
            if base_code(donor_key) != target["baseCode"]:
                continue
            if (
                _product_expansion_for_mapping_entry(
                    donor_entry, products_by_id
                )
                != target["idExpansion"]
            ):
                continue
            if (
                _semantic_class_from_printing_id(donor_key)
                != target["semanticClass"]
            ):
                continue

            donor_records = bandai_by_source.get(canonical_id(donor_key), [])
            donor_release_codes = (
                _bandai_structured_release_codes(donor_records)
                if donor_records
                else set()
            )
            # Only orphan/generic donors. Never steal from another explicit
            # current Bandai release.
            if donor_release_codes:
                continue

            donors.append(
                {
                    "mappingKey": donor_key,
                    "productId": int(product_number),
                }
            )

        if len(donors) == 1:
            proposals.append({**target, "donor": donors[0]})

    product_owners = defaultdict(list)
    for proposal in proposals:
        product_owners[proposal["donor"]["productId"]].append(proposal)

    migrated = []
    for proposal in proposals:
        product_id = proposal["donor"]["productId"]
        if len(product_owners[product_id]) != 1:
            continue

        printing_id = proposal["printingId"]
        donor_key = proposal["donor"]["mappingKey"]
        donor = entries[donor_key]
        target = entries.get(printing_id)
        if not isinstance(target, dict):
            target = {}
            entries[printing_id] = target

        previous_legacy_set = target.get("legacySet")
        if (
            previous_legacy_set
            and _structured_release_code(previous_legacy_set)
            != proposal["releaseCode"]
        ):
            target.setdefault("previousLegacySet", previous_legacy_set)

        if target.get("invalidProductId") is not None:
            target.setdefault("supersededProductId", target.get("invalidProductId"))
            target.setdefault("supersededUrl", target.get("invalidUrl"))

        product = products_by_id[product_id]
        target["legacySet"] = proposal["releaseCode"]
        target["productId"] = product_id
        target["url"] = (
            donor.get("url")
            or CARDMARKET_PRODUCT_REDIRECT.format(product_id=product_id)
        )
        target["confirmed"] = False
        target["source"] = "auto-bandai-release-semantic-alias"
        target.setdefault("addedAt", utc_now_iso())
        target["productName"] = product.get("name")
        target["idExpansion"] = product.get("idExpansion")
        target["idMetacard"] = product.get("idMetacard")
        target["dateAdded"] = product.get("dateAdded")
        target["validationEvidence"] = {
            "bandaiReleaseCode": proposal["releaseCode"],
            "releaseProfile": proposal["releaseProfile"],
            "donorMappingKey": donor_key,
            "semanticClass": proposal["semanticClass"],
            "rule": "unique-semantic-alias-in-bandai-release-expansion",
        }
        for field in ("invalidProductId", "invalidUrl", "invalidReason"):
            target.pop(field, None)

        donor["previousProductId"] = product_id
        donor["previousUrl"] = donor.get("url")
        donor["productId"] = None
        donor["url"] = None
        donor["confirmed"] = False
        donor["source"] = "alias-moved-to-current-bandai-id"
        donor["aliasMovedTo"] = printing_id

        migrated.append(
            {
                "printingId": printing_id,
                "productId": product_id,
                "donorMappingKey": donor_key,
                "bandaiReleaseCode": proposal["releaseCode"],
                "semanticClass": proposal["semanticClass"],
            }
        )

    if migrated:
        mapping["updatedAt"] = utc_now_iso()
    return migrated


def _auto_map_by_bandai_release(
    bandai_by_base: dict,
    mapping: dict,
    products_by_base: dict,
    products_by_id: dict[int, dict],
    mapped_product_ids: set[int],
    release_profiles: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Resolve unmapped printings only when Bandai supplies one structured release,
    a trusted Cardmarket expansion is known for that release, and exactly one
    unused Cardmarket product with the exact card code is available.

    Proposals are accepted only one-to-one. If two Bandai printings compete for
    the same Cardmarket idProduct, neither is mapped. This deliberately avoids
    inferring V.1/V.2 from order, dates or ascending product IDs.
    """
    entries = mapping.setdefault("mappings", {})
    stable_release_expansion = (
        dict(release_profiles)
        if release_profiles is not None
        else _stable_release_expansion_profiles(mapping, products_by_id)
    )

    proposals = []
    for base in sorted(bandai_by_base):
        by_source = defaultdict(list)
        for source_id, record in bandai_by_base[base]:
            by_source[source_id].append(record)

        for printing_id, records in sorted(by_source.items()):
            representative = records[0]
            cm_key, _ = resolve_cardmarket_mapping_key_for_bandai_record(
                representative, entries
            )
            if cm_key is not None:
                continue

            release_codes = _bandai_structured_release_codes(records)
            if len(release_codes) != 1:
                continue
            release_code = next(iter(release_codes))
            profile = stable_release_expansion.get(release_code)
            if not profile:
                continue
            expected_expansion = profile["value"]

            candidates = [
                product
                for product in products_by_base.get(base, [])
                if product.get("idExpansion") == expected_expansion
                and int(product["idProduct"]) not in mapped_product_ids
            ]
            if len(candidates) != 1:
                continue

            proposals.append(
                {
                    "printingId": printing_id,
                    "releaseCode": release_code,
                    "releaseProfile": profile,
                    "candidate": candidates[0],
                }
            )

    proposal_owners = defaultdict(list)
    for proposal in proposals:
        proposal_owners[int(proposal["candidate"]["idProduct"])].append(proposal)

    accepted = []
    for proposal in proposals:
        candidate = proposal["candidate"]
        product_id = int(candidate["idProduct"])
        if len(proposal_owners[product_id]) != 1:
            continue

        printing_id = proposal["printingId"]
        release_code = proposal["releaseCode"]
        entry = entries.get(printing_id)
        if not isinstance(entry, dict):
            entry = {}
            entries[printing_id] = entry

        previous_legacy_set = entry.get("legacySet")
        if (
            previous_legacy_set
            and _structured_release_code(previous_legacy_set) != release_code
        ):
            entry.setdefault("previousLegacySet", previous_legacy_set)
        if entry.get("invalidProductId") is not None:
            entry.setdefault("supersededProductId", entry.get("invalidProductId"))
            entry.setdefault("supersededUrl", entry.get("invalidUrl"))

        entry["legacySet"] = release_code
        entry["productId"] = product_id
        entry["url"] = candidate.get("website")
        entry["confirmed"] = False
        entry["source"] = "auto-bandai-release-unique"
        entry.setdefault("addedAt", utc_now_iso())
        entry["productName"] = candidate.get("name")
        entry["idExpansion"] = candidate.get("idExpansion")
        entry["idMetacard"] = candidate.get("idMetacard")
        entry["dateAdded"] = candidate.get("dateAdded")
        entry["validationEvidence"] = {
            "bandaiReleaseCode": release_code,
            "releaseProfile": proposal["releaseProfile"],
            "rule": "unique-unused-product-in-bandai-release-expansion",
        }
        for field in ("invalidProductId", "invalidUrl", "invalidReason"):
            entry.pop(field, None)

        mapped_product_ids.add(product_id)
        accepted.append(
            {
                "printingId": printing_id,
                "productId": product_id,
                "reason": "bandai-release-expansion-unique",
                "bandaiReleaseCode": release_code,
                "idExpansion": candidate.get("idExpansion"),
            }
        )

    if accepted:
        mapping["updatedAt"] = utc_now_iso()
    return accepted


def _auto_map_by_bandai_release_name(
    bandai_by_source: dict[str, list[dict]],
    mapping: dict,
    products: list[dict],
    products_by_id: dict[int, dict],
    mapped_product_ids: set[int],
    release_profiles: dict[str, dict],
) -> list[dict]:
    """
    Secondary typo-safe fallback inside an already trusted expansion.

    It is used only when one unused Cardmarket product has exactly the same
    normalized card name as Bandai. One-to-one ownership is still mandatory.
    This handles catalogue code typos without fuzzy matching.
    """
    entries = mapping.setdefault("mappings", {})
    products_by_expansion_name = defaultdict(list)
    for product in products:
        key = (
            product.get("idExpansion"),
            slugify(_product_display_name(product)),
        )
        products_by_expansion_name[key].append(product)

    proposals = []
    for printing_id, records in sorted(bandai_by_source.items()):
        if not records:
            continue
        cm_key, _ = resolve_cardmarket_mapping_key_for_bandai_record(
            records[0], entries
        )
        if cm_key is not None:
            continue

        release_codes = _bandai_structured_release_codes(records)
        if len(release_codes) != 1:
            continue
        release_code = next(iter(release_codes))
        profile = release_profiles.get(release_code)
        if not profile:
            continue

        normalized_name = slugify(records[0].get("name"))
        if not normalized_name:
            continue

        candidates = [
            product
            for product in products_by_expansion_name.get(
                (profile["value"], normalized_name),
                [],
            )
            if int(product["idProduct"]) not in mapped_product_ids
        ]
        if len(candidates) != 1:
            continue

        proposals.append(
            {
                "printingId": printing_id,
                "releaseCode": release_code,
                "releaseProfile": profile,
                "normalizedName": normalized_name,
                "candidate": candidates[0],
            }
        )

    proposal_owners = defaultdict(list)
    for proposal in proposals:
        proposal_owners[int(proposal["candidate"]["idProduct"])].append(proposal)

    accepted = []
    for proposal in proposals:
        product = proposal["candidate"]
        product_id = int(product["idProduct"])
        if len(proposal_owners[product_id]) != 1:
            continue

        printing_id = proposal["printingId"]
        entry = entries.get(printing_id)
        if not isinstance(entry, dict):
            entry = {}
            entries[printing_id] = entry

        previous_legacy_set = entry.get("legacySet")
        if (
            previous_legacy_set
            and _structured_release_code(previous_legacy_set)
            != proposal["releaseCode"]
        ):
            entry.setdefault("previousLegacySet", previous_legacy_set)
        if entry.get("invalidProductId") is not None:
            entry.setdefault("supersededProductId", entry.get("invalidProductId"))
            entry.setdefault("supersededUrl", entry.get("invalidUrl"))

        entry["legacySet"] = proposal["releaseCode"]
        entry["productId"] = product_id
        entry["url"] = product.get("website")
        entry["confirmed"] = False
        entry["source"] = "auto-bandai-release-name-unique"
        entry.setdefault("addedAt", utc_now_iso())
        entry["productName"] = product.get("name")
        entry["idExpansion"] = product.get("idExpansion")
        entry["idMetacard"] = product.get("idMetacard")
        entry["dateAdded"] = product.get("dateAdded")
        entry["validationEvidence"] = {
            "bandaiReleaseCode": proposal["releaseCode"],
            "releaseProfile": proposal["releaseProfile"],
            "normalizedName": proposal["normalizedName"],
            "rule": "unique-unused-product-name-in-bandai-release-expansion",
        }
        for field in ("invalidProductId", "invalidUrl", "invalidReason"):
            entry.pop(field, None)

        mapped_product_ids.add(product_id)
        accepted.append(
            {
                "printingId": printing_id,
                "productId": product_id,
                "reason": "bandai-release-name-unique",
                "bandaiReleaseCode": proposal["releaseCode"],
                "idExpansion": product.get("idExpansion"),
            }
        )

    if accepted:
        mapping["updatedAt"] = utc_now_iso()
    return accepted


def auto_map_and_build_review(
    bandai_cards: list[dict],
    mapping: dict,
    products: list[dict],
    allow_auto_map: bool,
    series_expansion_diagnostics: dict[str, dict] | None = None,
    series_expansion_profiles: dict[str, dict] | None = None,
) -> dict:
    mapping_entries = mapping.setdefault("mappings", {})
    products_by_id = {int(product["idProduct"]): product for product in products}

    products_by_base = defaultdict(list)
    for product in products:
        product_name = str(product.get("name") or "")
        for match in CARD_CODE_RE.finditer(product_name):
            products_by_base[canonical_id(match.group(0))].append(product)

    bandai_by_base = defaultdict(list)
    bandai_by_source = defaultdict(list)
    for card in bandai_cards:
        source_id = canonical_id(card.get("sourcePrintingId"))
        if not source_id:
            continue
        bandai_by_base[base_code(source_id)].append((source_id, card))
        bandai_by_source[source_id].append(card)

    existing_profiles = _stable_release_expansion_profiles(
        mapping, products_by_id
    )
    inferred_profiles, inference_diagnostics = (
        infer_release_expansions_from_catalog(
            bandai_cards,
            products,
            existing_profiles,
        )
    )
    release_profiles = dict(existing_profiles)
    for release_code, profile in inferred_profiles.items():
        release_profiles.setdefault(release_code, profile)

    if series_expansion_diagnostics is None:
        series_expansion_diagnostics = analyze_bandai_series_expansions_from_catalog(
            bandai_cards, products
        )
    if series_expansion_profiles is None:
        series_expansion_profiles = _stable_bandai_series_expansion_profiles(
            bandai_cards, mapping, products_by_id, series_expansion_diagnostics
        )

    auto_added = []
    semantic_aliases = []
    semantic_drift_quarantined = []

    if allow_auto_map:
        # One pass is enough in normal data, but a second fixed-point pass makes
        # the behavior deterministic if an alias move frees a unique candidate.
        for _ in range(3):
            changed = 0

            drift = _reconcile_release_specific_mapping_drift(
                bandai_by_source,
                mapping,
                products_by_id,
                release_profiles,
                series_expansion_profiles,
                allow_migrate=True,
            )
            semantic_aliases.extend(drift["migrated"])
            semantic_drift_quarantined.extend(drift["quarantined"])
            changed += len(drift["migrated"]) + len(drift["quarantined"])

            moved = _migrate_semantic_release_aliases(
                bandai_by_source,
                mapping,
                products_by_id,
                release_profiles,
            )
            semantic_aliases.extend(moved)
            changed += len(moved)

            mapped_product_ids = {
                int(entry.get("productId"))
                for entry in mapping_entries.values()
                if (
                    isinstance(entry, dict)
                    and get_number(entry.get("productId")) is not None
                )
            }

            exact_added = _auto_map_by_bandai_release(
                bandai_by_base,
                mapping,
                products_by_base,
                products_by_id,
                mapped_product_ids,
                release_profiles=release_profiles,
            )
            auto_added.extend(exact_added)
            changed += len(exact_added)

            name_added = _auto_map_by_bandai_release_name(
                bandai_by_source,
                mapping,
                products,
                products_by_id,
                mapped_product_ids,
                release_profiles,
            )
            auto_added.extend(name_added)
            changed += len(name_added)

            if not changed:
                break
    else:
        drift = _reconcile_release_specific_mapping_drift(
            bandai_by_source,
            mapping,
            products_by_id,
            release_profiles,
            series_expansion_profiles,
            allow_migrate=False,
        )
        semantic_drift_quarantined.extend(drift["quarantined"])

    review_items = []

    mapped_product_ids = {
        int(entry.get("productId"))
        for entry in mapping_entries.values()
        if (
            isinstance(entry, dict)
            and get_number(entry.get("productId")) is not None
        )
    }

    for base in sorted(bandai_by_base):
        source_ids = sorted({source_id for source_id, _ in bandai_by_base[base]})
        missing_ids = []
        for source_id in source_ids:
            examples = [
                card
                for sid, card in bandai_by_base[base]
                if sid == source_id
            ]
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
            product
            for product in candidates
            if int(product["idProduct"]) not in mapped_product_ids
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
            auto_added.append(
                {
                    "printingId": printing_id,
                    "productId": product["idProduct"],
                    "reason": "exact-card-code-family-unique",
                }
            )
            continue

        for printing_id in missing_ids:
            bandai_examples = [
                card
                for source_id, card in bandai_by_base[base]
                if source_id == printing_id
            ]

            candidate_union = {
                int(product["idProduct"]): product
                for product in candidates
            }
            normalized_name = (
                slugify(bandai_examples[0].get("name"))
                if bandai_examples
                else ""
            )
            if normalized_name:
                release_codes = _bandai_structured_release_codes(
                    bandai_examples
                )
                if len(release_codes) == 1:
                    profile = release_profiles.get(next(iter(release_codes)))
                    if profile:
                        for product in products:
                            if (
                                product.get("idExpansion") == profile["value"]
                                and slugify(_product_display_name(product))
                                == normalized_name
                            ):
                                candidate_union[int(product["idProduct"])] = product

            candidate_rows = []
            for product in list(candidate_union.values())[:25]:
                candidate_rows.append(
                    {
                        "idProduct": product.get("idProduct"),
                        "name": product.get("name"),
                        "idExpansion": product.get("idExpansion"),
                        "idMetacard": product.get("idMetacard"),
                        "dateAdded": product.get("dateAdded"),
                        "website": product.get("website"),
                        "version": (
                            int(
                                VERSION_RE.search(
                                    str(product.get("name") or "")
                                ).group(1)
                            )
                            if VERSION_RE.search(
                                str(product.get("name") or "")
                            )
                            else None
                        ),
                    }
                )

            review_items.append(
                {
                    "printingId": printing_id,
                    "baseCode": base,
                    "name": (
                        bandai_examples[0].get("name")
                        if bandai_examples
                        else None
                    ),
                    "imageUrl": (
                        bandai_examples[0].get("imageUrl")
                        if bandai_examples
                        else None
                    ),
                    "bandaiReleases": sorted(
                        {
                            card.get("seriesLabel")
                            for card in bandai_examples
                            if card.get("seriesLabel")
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

    if auto_added or semantic_aliases:
        mapping["updatedAt"] = utc_now_iso()

    return {
        "generatedAt": utc_now_iso(),
        "autoMappingsAdded": auto_added,
        "semanticAliasesMoved": semantic_aliases,
        "semanticDriftQuarantined": semantic_drift_quarantined,
        "inferredReleaseProfiles": inferred_profiles,
        "inferenceDiagnostics": inference_diagnostics,
        "seriesExpansionProfiles": series_expansion_profiles,
        "seriesExpansionDiagnostics": series_expansion_diagnostics,
        "needsReview": review_items,
    }



# ---------------------------------------------------------------------------
# Optional community image enrichment (V3.10.2)
# ---------------------------------------------------------------------------

COMMUNITY_NAME_KEYS = (
    "don_card_name", "promo_card_name", "card_name", "name", "display_name",
    "full_name", "product_name",
)
COMMUNITY_FULL_NAME_KEYS = (
    "optcg_don_name", "don_card_name", "promo_card_name", "full_name",
    "display_name", "product_name", "card_name", "name",
)
COMMUNITY_IMAGE_URL_KEYS = (
    "card_image", "image_url", "imageUrl", "image", "image_path", "imagePath",
)
COMMUNITY_IMAGE_ID_KEYS = (
    "card_image_id", "image_id", "imageId",
)
COMMUNITY_CODE_KEYS = (
    "card_set_id", "card_id", "cardId", "card_no", "card_number", "code",
)


def _dict_get_ci(row: dict, keys: tuple[str, ...]):
    """Case-insensitive scalar lookup with a shallow nested fallback."""
    if not isinstance(row, dict):
        return None
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.casefold())
        if value not in (None, "", [], {}):
            return value
    for nested_key in ("card", "fields", "data", "result"):
        nested = lowered.get(nested_key)
        if isinstance(nested, dict):
            value = _dict_get_ci(nested, keys)
            if value not in (None, "", [], {}):
                return value
    return None


def _iter_community_rows(value, depth: int = 0):
    """Yield likely card rows from APIs whose wrapper shape may change."""
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
                yield from _iter_community_rows(item, depth + 1)
        return
    if not isinstance(value, dict):
        return

    lower_keys = {str(key).casefold() for key in value}
    has_name = any(key.casefold() in lower_keys for key in COMMUNITY_NAME_KEYS)
    has_image = any(key.casefold() in lower_keys for key in COMMUNITY_IMAGE_URL_KEYS)
    if has_name and has_image:
        yield value

    for nested in value.values():
        if isinstance(nested, (dict, list)):
            yield from _iter_community_rows(nested, depth + 1)


def _fetch_json_first(session: requests.Session, urls: list[str]) -> tuple[object | None, str | None, list[dict]]:
    """Fetch an *optional* JSON source without inheriting official-source retries.

    Bandai/Cardmarket are required and intentionally use the hardened session with
    retries. Community image enrichment must never turn a temporary third-party
    outage into a multi-minute catalogue failure, so it uses a short best-effort
    request and falls back cleanly. ``session`` is kept in the signature to make
    the orchestration explicit and for future per-source adapters.
    """
    del session
    attempts = []
    for url in urls:
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = response.json()
            attempts.append({"url": url, "ok": True, "error": None})
            return data, url, attempts
        except Exception as error:
            attempts.append({"url": url, "ok": False, "error": str(error)[:1000]})
    return None, None, attempts


def fetch_community_image_raw(session: requests.Session) -> dict:
    """Fetch optional image-only metadata from OPTCGAPI.com.

    This source is deliberately non-authoritative. Its prices/game metadata are
    ignored; only a candidate name/code/image URL is retained for conservative
    matching against entities already created from Bandai/Cardmarket.
    """
    result = {
        "source": "OPTCGAPI.com community image fallback",
        "sourceUrl": "https://optcgapi.com/documentation",
        "fetchedAt": utc_now_iso(),
        "don": None,
        "promos": None,
        "requests": [],
    }

    don, don_url, attempts = _fetch_json_first(session, [OPTCGAPI_DON_URL])
    result["requests"].extend(attempts)
    if don is not None:
        result["don"] = {"endpoint": don_url, "payload": don}
    else:
        print("AVISO: OPTCGAPI DON no disponible; se continúa sin imágenes DON externas.")

    promos, promo_url, attempts = _fetch_json_first(session, OPTCGAPI_PROMO_URLS)
    result["requests"].extend(attempts)
    if promos is not None:
        result["promos"] = {"endpoint": promo_url, "payload": promos}
    else:
        print("AVISO: OPTCGAPI promos no disponible; se continúa sin imágenes promo externas.")

    return result


def normalize_community_image_records(raw: dict | None) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    normalized = []
    seen = set()

    for bucket_name, kind in (("don", "don"), ("promos", "promo")):
        bucket = raw.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        endpoint = nullable_text(bucket.get("endpoint"))
        payload = bucket.get("payload")
        for row in _iter_community_rows(payload):
            raw_name = nullable_text(_dict_get_ci(row, COMMUNITY_NAME_KEYS))
            full_name = nullable_text(_dict_get_ci(row, COMMUNITY_FULL_NAME_KEYS)) or raw_name
            image_url = nullable_text(_dict_get_ci(row, COMMUNITY_IMAGE_URL_KEYS))
            if not raw_name or not image_url:
                continue
            image_url = urljoin(OPTCGAPI_BASE_URL + "/", image_url)

            code_value = nullable_text(_dict_get_ci(row, COMMUNITY_CODE_KEYS))
            code_match = CARD_CODE_RE.search(str(code_value or raw_name))
            code = canonical_id(code_match.group(0)) if code_match else None
            image_id = nullable_text(_dict_get_ci(row, COMMUNITY_IMAGE_ID_KEYS))
            record_id = nullable_text(
                _dict_get_ci(row, ("id", "pk", "don_id", "promo_id", "card_pk"))
            )
            key = (kind, full_name, image_url, code)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({
                "kind": kind,
                "name": raw_name,
                "fullName": full_name,
                "code": code,
                "imageUrl": image_url,
                "imageId": image_id,
                "recordId": record_id,
                "endpoint": endpoint,
                "source": "optcgapi.com",
            })
    return normalized


def _normalize_design_name(value: str | None, *, kind: str | None = None) -> str:
    text = nullable_text(value) or ""
    # For standard promos, strip a trailing release title while keeping the
    # actual card/design name. DON!! uses the dedicated release-aware normalizer
    # below because its set/event qualifiers are essential for safe matching.
    if kind != "don":
        text = re.split(
            r"\s+-\s+(?=(?:One Piece|Premium Booster|Booster|Extra Booster|Starter|Promotion|Promo))",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
    text = CARD_CODE_RE.sub(" ", text)
    text = VERSION_RE.sub(" ", text)
    text = re.sub(r"\b(?:version|ver\.?)[\s_-]*\d+\b", " ", text, flags=re.I)
    if kind == "don":
        text = re.sub(r"\bDON\s*!*\s*(?:CARD)?\b", " ", text, flags=re.I)
    text = normalize_text(text)
    tokens = []
    for token in re.findall(r"[a-z0-9]+", text):
        if len(token) > 4 and token.endswith("s") and token not in {"series"}:
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens)


def _normalize_don_match_text(value: str | None) -> str:
    """Normalize Cardmarket/OPTCGAPI DON labels to comparable semantics.

    V3.10 matched only the short character/design name. That produced false
    positives such as ``PRB02 - Nico Robin`` -> an EB03 Robin image. V3.10.2
    keeps release/event qualifiers and canonicalises common abbreviations so a
    design is only matched inside the same semantic release family.
    """
    text = normalize_text(value)
    text = text.replace("&", " and ")

    # Expand source-side product phrases to the abbreviations Cardmarket uses.
    text = re.sub(
        r"premium\s+booster\s*[- ]*the\s+best\s*[- ]*vol\.?\s*2",
        " prb02 ", text, flags=re.I,
    )
    text = re.sub(
        r"premium\s+booster\s*[- ]*the\s+best\s*[- ]*(?!vol)",
        " prb01 ", text, flags=re.I,
    )
    text = re.sub(
        r"double\s+pack\s+set\s+vol(?:ume)?\.?\s*(\d+)",
        lambda m: f" doublepack dp{int(m.group(1)):02d} ", text, flags=re.I,
    )
    text = re.sub(
        r"tin\s+pack\s+set\s+vol(?:ume)?\.?\s*(\d+)",
        lambda m: f" tinpack ts{int(m.group(1)):02d} ", text, flags=re.I,
    )
    text = re.sub(
        r"devil\s+fruits?\s+collection\s+vol\.?\s*(\d+)",
        lambda m: f" devilfruits df{int(m.group(1)):02d} ", text, flags=re.I,
    )
    text = re.sub(
        r"special\s+don\s*!*\s+set\s+vol\.?\s*(\d+)",
        lambda m: f" specialdon specialdon{int(m.group(1)):02d} ", text, flags=re.I,
    )
    text = re.sub(r"special\s+don\s*!*\s+set", " specialdon ", text, flags=re.I)

    # Canonical compact release codes: PRB-02 / PRB02 -> prb02, etc.
    text = re.sub(
        r"\b(prb|op|eb|st)\s*[- ]?\s*(\d{1,2})\b",
        lambda m: f"{m.group(1).lower()}{int(m.group(2)):02d}", text, flags=re.I,
    )
    text = re.sub(
        r"\b(dp|ts|df)\s*[- ]?\s*(\d{1,2})\b",
        lambda m: f"{m.group(1).lower()}{int(m.group(2)):02d}", text, flags=re.I,
    )
    # Cardmarket historically labels PRB-01 DON!! simply as "PRB".
    text = re.sub(r"\bprb\b", " prb01 ", text, flags=re.I)
    text = re.sub(r"\bdfc\b", " devilfruits ", text, flags=re.I)

    # Remove wrappers that carry no design identity.
    text = re.sub(r"\bdon\s*!*\s*(?:card)?\b", " ", text, flags=re.I)
    text = re.sub(r"\bone\s+piece\s+promotion\s+cards?\b", " ", text, flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = " ".join(text.split())

    # Conservative, well-established character aliases used by the two sources.
    aliases = (
        ("edward newgate", "whitebeard"),
        ("marshall d teach", "teach"),
        ("blackbeard", "teach"),
        ("jewelry bonney", "bonney"),
        ("trafalgar law", "law"),
        ("eustass captain kid", "kid"),
        ("donquixote rosinante", "rosinante"),
        ("donquixote doflamingo", "doflamingo"),
        ("rob lucci", "lucci"),
        ("gecko moria", "moria"),
        ("monkey d luffy", "luffy"),
        ("gol d roger", "roger"),
        ("portgas d ace", "ace"),
        ("tony tony chopper", "chopper"),
        ("dracule mihawk", "mihawk"),
        ("charlotte katakuri", "katakuri"),
        ("kouzuki oden", "oden"),
        ("nico robin", "robin"),
        ("promo", "promotion"),
        ("finals", "final"),
        ("worlds", "world"),
    )
    for source, target in aliases:
        text = re.sub(
            r"(?<![a-z0-9])" + re.escape(source) + r"(?![a-z0-9])",
            target,
            text,
        )
    return " ".join(text.split())


def _don_release_markers(value: str | None) -> set[str]:
    return {
        token
        for token in _normalize_don_match_text(value).split()
        if re.fullmatch(r"(?:prb|op|eb|st|dp|ts|df)\d{2}|specialdon\d{2}", token)
    }


def _don_has_gold_marker(value: str | None) -> bool:
    return "gold" in _normalize_don_match_text(value).split()


def _name_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    common = left_tokens & right_tokens
    union = left_tokens | right_tokens
    jaccard = len(common) / len(union) if union else 0.0
    containment = (
        len(common) / min(len(left_tokens), len(right_tokens))
        if left_tokens and right_tokens
        else 0.0
    )
    sequence = SequenceMatcher(None, left, right).ratio()
    score = 0.68 * sequence + 0.32 * jaccard
    if min(len(left_tokens), len(right_tokens)) >= 2 and containment >= 0.90:
        score = max(score, 0.92 + 0.05 * min(jaccard, 1.0))
    if min(len(left), len(right)) >= 10 and (left in right or right in left):
        score = max(score, 0.94)
    return min(score, 1.0)


def match_community_reference_image(
    *,
    kind: str,
    display_name: str | None,
    printed_codes: list[str] | None,
    community_images: list[dict],
) -> tuple[dict | None, dict]:
    """Return one conservative reference image or an explicit non-match.

    Standard promos still require printed code + name. DON!! additionally uses
    OPTCGAPI's full design/release label, release-family compatibility and a
    Gold/non-Gold guard. Bare one-token DON character names are intentionally
    left unmatched: a placeholder is better than a convincing wrong artwork.
    """
    wanted_codes = {canonical_id(code) for code in (printed_codes or []) if code}
    if kind == "don":
        wanted_name = _normalize_don_match_text(display_name)
        wanted_markers = _don_release_markers(display_name)
        wanted_gold = _don_has_gold_marker(display_name)
        if not wanted_markers and len(wanted_name.split()) < 2:
            return None, {
                "wantedName": wanted_name,
                "wantedMarkers": [],
                "candidateCount": 0,
                "topScore": 0.0,
                "secondScore": 0.0,
                "requiredScore": 0.90,
                "requiredMargin": 0.05,
                "status": "insufficient-design-qualifier",
            }
    else:
        wanted_name = _normalize_design_name(display_name, kind=kind)
        wanted_markers = set()
        wanted_gold = False

    ranked = []
    rejected_release = 0
    rejected_gold = 0
    for item in community_images:
        if item.get("kind") != kind:
            continue
        if kind != "don":
            item_code = canonical_id(item.get("code")) if item.get("code") else None
            if not item_code or item_code not in wanted_codes:
                continue
            candidate_name = _normalize_design_name(item.get("name"), kind=kind)
        else:
            full_name = item.get("fullName") or item.get("name")
            item_markers = _don_release_markers(full_name)
            if wanted_markers and not wanted_markers.issubset(item_markers):
                rejected_release += 1
                continue
            if wanted_gold != _don_has_gold_marker(full_name):
                rejected_gold += 1
                continue
            candidate_name = _normalize_don_match_text(full_name)
        score = _name_similarity(wanted_name, candidate_name)
        ranked.append((score, candidate_name, item))

    ranked.sort(key=lambda row: (row[0], row[1], str(row[2].get("imageUrl"))), reverse=True)
    unique = []
    seen_urls = set()
    for score, candidate_name, item in ranked:
        url = item.get("imageUrl")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique.append((score, candidate_name, item))

    threshold = 0.90 if kind == "don" else 0.80
    margin_required = 0.05 if kind == "don" else 0.04
    top_score = unique[0][0] if unique else 0.0
    second_score = unique[1][0] if len(unique) > 1 else 0.0
    diagnostic = {
        "wantedName": wanted_name,
        "wantedMarkers": sorted(wanted_markers),
        "candidateCount": len(unique),
        "rejectedReleaseMismatch": rejected_release if kind == "don" else 0,
        "rejectedGoldMismatch": rejected_gold if kind == "don" else 0,
        "topScore": round(top_score, 4),
        "secondScore": round(second_score, 4),
        "requiredScore": threshold,
        "requiredMargin": margin_required,
    }
    if unique:
        diagnostic["topSourceName"] = unique[0][2].get("name")
        diagnostic["topSourceFullName"] = unique[0][2].get("fullName")
    if not unique or top_score < threshold or (len(unique) > 1 and top_score - second_score < margin_required):
        diagnostic["status"] = "unmatched-or-ambiguous"
        return None, diagnostic

    item = unique[0][2]
    match = {
        "url": item.get("imageUrl"),
        "sourceUrl": item.get("endpoint"),
        "source": item.get("source") or "optcgapi.com",
        "sourceRecordId": item.get("recordId"),
        "sourceImageId": item.get("imageId"),
        "sourceName": item.get("name"),
        "sourceFullName": item.get("fullName"),
        "sourceCode": item.get("code"),
        "matchMethod": "code+name" if kind != "don" else "release-aware-design",
        "matchScore": round(top_score, 4),
        "authoritative": False,
        "scope": "entity-reference",
    }
    diagnostic["status"] = "matched"
    return match, diagnostic

def safe_image_from_cache(image_url: str | None, image_cache: dict) -> tuple[str | None, dict | None]:
    if not image_url:
        return None, None
    health = (image_cache.get("images", {}) if isinstance(image_cache, dict) else {}).get(image_url)
    if health is None:
        return image_url, None
    if health.get("ok"):
        return health.get("finalUrl") or image_url, health
    return None, health


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
    extra_urls: list[str] | None = None,
    cardmarket_urls: list[str] | None = None,
) -> tuple[dict, dict]:
    cache.setdefault("schemaVersion", 2)
    cache.setdefault("images", {})
    images = cache["images"]

    official_urls = {str(card.get("imageUrl")) for card in bandai_cards if card.get("imageUrl")}
    community_urls = {str(url) for url in (extra_urls or []) if url}
    cardmarket_exact_urls = {str(url) for url in (cardmarket_urls or []) if url}
    urls = sorted(official_urls | community_urls | cardmarket_exact_urls)
    checked = 0
    failed = []

    if skip:
        return cache, {
            "totalUrls": len(urls),
            "officialUrls": len(official_urls),
            "communityUrls": len(community_urls),
            "cardmarketExactUrls": len(cardmarket_exact_urls),
            "checkedThisRun": 0,
            "failed": [],
        }

    for index, url in enumerate(urls, start=1):
        if not force_all and isinstance(images.get(url), dict) and images[url].get("ok") is True:
            continue
        if url in cardmarket_exact_urls and url not in official_urls:
            source_label = "cardmarket-exact"
        elif url in community_urls and url not in official_urls:
            source_label = "community"
        else:
            source_label = "official"
        print(f"Imagen {index}/{len(urls)} [{source_label}]: {url}")
        result = validate_image_url(session, url)
        images[url] = result
        checked += 1
        if not result.get("ok"):
            failed.append({"url": url, "sourceKind": source_label, **result})
        time.sleep(0.05)

    cache["updatedAt"] = utc_now_iso()
    return cache, {
        "totalUrls": len(urls),
        "officialUrls": len(official_urls),
        "communityUrls": len(community_urls),
        "cardmarketExactUrls": len(cardmarket_exact_urls),
        "checkedThisRun": checked,
        "failed": failed,
    }


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


def _price_valuation_eur(price: dict | None):
    if not isinstance(price, dict):
        return None
    return (
        price.get("trend")
        if price.get("trend") is not None
        else price.get("avg7")
        if price.get("avg7") is not None
        else price.get("avg30")
        if price.get("avg30") is not None
        else price.get("avg")
    )



def _release_kind(label: str | None) -> str:
    text = normalize_text(label)
    if "starter deck" in text or "ultra deck" in text:
        return "starter-deck"
    if "premium booster" in text:
        return "premium-booster"
    if "extra booster" in text:
        return "extra-booster"
    if "booster pack" in text:
        return "booster"
    if "promotion" in text or "promo" in text:
        return "promotion"
    if "other product" in text:
        return "other-product"
    return "other"


def _release_code_from_label(label: str | None) -> str | None:
    text = str(label or "")
    bracket = re.findall(r"\[([^\]]+)\]", text)
    candidate = bracket[-1].strip() if bracket else ""
    if candidate:
        # Keep combined releases such as OP14-EB04 intact while normalizing
        # simple OP-16 / ST-01 labels to their official display form.
        candidate = candidate.upper().replace(" ", "")
        if re.fullmatch(r"(?:OP|ST|EB|PRB)-?\d{2}", candidate):
            prefix = re.match(r"[A-Z]+", candidate).group(0)
            number = re.search(r"\d{2}", candidate).group(0)
            return f"{prefix}-{number}"
        return candidate
    if "promotion card" in text.casefold():
        return "PROMO"
    if "other product card" in text.casefold():
        return "OTHER"
    return None


def _bandai_release_payload(record: dict) -> dict:
    series_id = nullable_text(record.get("seriesId"))
    label = nullable_text(record.get("seriesLabel") or record.get("cardSetsText"))
    return {
        "releaseId": f"BANDAI-{series_id}" if series_id else None,
        "source": "bandai",
        "seriesId": series_id,
        "code": _release_code_from_label(label),
        "kind": _release_kind(label),
        "displayName": label,
        "seriesLabel": record.get("seriesLabel"),
        "cardSetsText": record.get("cardSetsText"),
    }


def _cardmarket_release_payload(product: dict) -> dict:
    expansion_number = get_number((product or {}).get("idExpansion"))
    expansion_id = int(expansion_number) if expansion_number is not None else None
    expansion_name = nullable_text((product or {}).get("expansionName"))
    expansion_code = nullable_text((product or {}).get("expansionCode"))
    return {
        "releaseId": f"CM-EXP-{expansion_id}" if expansion_id is not None else None,
        "source": "cardmarket",
        "seriesId": None,
        "code": expansion_code,
        "kind": "cardmarket-expansion",
        "displayName": expansion_name or (
            f"Cardmarket expansion #{expansion_id}" if expansion_id is not None else "Cardmarket"
        ),
        "seriesLabel": None,
        "cardSetsText": None,
        "cardmarketExpansionId": expansion_id,
    }


def build_catalog(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    image_cache: dict,
    series_expansion_profiles: dict[str, dict] | None = None,
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
    code_discrepancies = []
    printings_with_price_guide = 0
    printings_with_valuation = 0
    printings_with_mapping = 0
    printings_without_valid_image = 0
    mapping_relation_counts = defaultdict(int)
    series_expansion_profiles = series_expansion_profiles or {}

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
                    releases.append(_bandai_release_payload(r))

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
                    valuation = _price_valuation_eur(price)
                    if price:
                        printings_with_price_guide += 1
                    if isinstance(valuation, (int, float)):
                        printings_with_valuation += 1

                    product_code = _product_card_code(product)
                    code_matches = (product_code == base) if product_code else None
                    bandai_name = normalize_text(best.get("name") or canonical.get("name") or "")
                    product_name = _product_display_name(product)
                    name_matches = (bandai_name == product_name) if bandai_name and product_name else None

                    expected_expansions = sorted({
                        int(profile["value"])
                        for release in releases
                        for profile in [series_expansion_profiles.get(str(release.get("seriesId") or ""))]
                        if isinstance(profile, dict) and get_number(profile.get("value")) is not None
                    })
                    product_expansion = get_number((product or {}).get("idExpansion"))
                    release_matches = (
                        int(product_expansion) in expected_expansions
                        if expected_expansions and product_expansion is not None
                        else None
                    )
                    mapping_evidence = {
                        "bandaiBaseCode": base,
                        "cardmarketProductCode": product_code,
                        "codeMatches": code_matches,
                        "nameMatches": name_matches,
                        "releaseMatches": release_matches,
                        "expectedExpansionIds": expected_expansions,
                        "cardmarketExpansionId": (
                            int(product_expansion) if product_expansion is not None else None
                        ),
                        "sourceCodeDiscrepancy": code_matches is False,
                    }
                    if code_matches is False:
                        code_discrepancies.append({
                            "printingId": printing_id,
                            "bandaiBaseCode": base,
                            "bandaiName": best.get("name") or canonical.get("name"),
                            "productId": product_id,
                            "cardmarketProductCode": product_code,
                            "cardmarketProductName": (product or {}).get("name"),
                            "mappingConfirmed": bool(map_entry.get("confirmed")),
                            "mappingSource": map_entry.get("source"),
                            **mapping_evidence,
                        })

                    cm = {
                        "mappingKey": cm_key,
                        "mappingRelation": cm_relation,
                        "productId": product_id,
                        "url": map_entry.get("url") or (product or {}).get("website"),
                        "mappingConfirmed": bool(map_entry.get("confirmed")),
                        "mappingSource": map_entry.get("source"),
                        "mappingEvidence": mapping_evidence,
                        "product": product,
                        "price": (
                            {
                                "currency": "EUR",
                                "createdAt": price_created_at,
                                **price,
                                "valuationEur": valuation,
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
                        "language": "en",
                        "languageLabel": "English",
                        "languageSource": "bandai-english-catalog",
                        "editionCode": None,
                        "editionName": None,
                        "editionSlug": None,
                        "languageGroup": "en",
                        "imageUrl": safe_image_url,
                        "imageSourceUrl": image_url or None,
                        "imageHealth": image_status,
                        "image": {
                            "url": safe_image_url,
                            "sourceUrl": image_url or None,
                            "source": "bandai",
                            "authoritative": True,
                            "scope": "printing",
                            "validated": (
                                bool(image_status.get("ok"))
                                if isinstance(image_status, dict)
                                else None
                            ),
                        },
                        "releases": releases,
                        "mechanics": printing_mechanics,
                        "mechanicsDifferFromBase": mechanics_differ,
                        "cardmarket": cm,
                        "source": "bandai",
                    }
                )

        printings.sort(key=natural_printing_sort_key)
        canonical_mechanics = _printing_mechanics(canonical)
        preview_printing = next((p for p in printings if p.get("imageUrl")), None)
        release_ids = sorted({
            release.get("releaseId")
            for printing in printings
            for release in printing.get("releases", [])
            if release.get("releaseId")
        })
        output[base] = {
            "catalogId": base,
            "code": base,
            "printedCodes": [base],
            "game": "One Piece",
            "name": canonical.get("name") or base,
            "rarity": canonical.get("rarity"),
            "type": canonical.get("category"),
            **canonical_mechanics,
            "sources": ["bandai", *( ["cardmarket"] if any(p.get("cardmarket") for p in printings) else [] )],
            "releaseIds": release_ids,
            "previewImageUrl": preview_printing.get("imageUrl") if preview_printing else None,
            "previewImage": (
                {
                    "url": preview_printing.get("imageUrl"),
                    "source": "bandai",
                    "authoritative": True,
                    "scope": "printing",
                    "printingId": preview_printing.get("printingId"),
                }
                if preview_printing
                else None
            ),
            "printings": printings,
        }

    stats = {
        "cards": len(output),
        "printings": sum(len(card["printings"]) for card in output.values()),
        "printingsWithCardmarketMapping": printings_with_mapping,
        "printingsWithCardmarketPriceGuide": printings_with_price_guide,
        "printingsWithCardmarketValuation": printings_with_valuation,
        "bandaiPrintingsWithoutValidatedImage": printings_without_valid_image,
        "cardmarketMappingRelations": dict(sorted(mapping_relation_counts.items())),
        "bandaiPrintingIdImageCollisions": collisions,
        "cardmarketCodeDiscrepancies": code_discrepancies,
        "mechanicConflicts": mechanic_conflicts,
    }
    return output, stats


# ---------------------------------------------------------------------------
# Cardmarket-only supplemental catalogue (standard codes missing in Bandai + DON!!)
# ---------------------------------------------------------------------------

DON_PRODUCT_RE = re.compile(r"^\s*DON!!(?:\s|\(|$)", re.I)


def _is_cardmarket_don_product(product: dict) -> bool:
    return bool(DON_PRODUCT_RE.search(str((product or {}).get("name") or "")))


def _parse_utc_iso(value: str | None) -> datetime | None:
    text = nullable_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cardmarket_image_candidate(raw_url: str | None, page_url: str) -> str | None:
    text = html_lib.unescape(str(raw_url or "")).strip().strip("'\"")
    if not text:
        return None
    text = text.replace("\\/", "/")
    if text.startswith("//"):
        text = "https:" + text
    url = urljoin(page_url, text)
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.casefold() != CARDMARKET_PRODUCT_IMAGE_HOST:
        return None
    if not parsed.hostname:
        return None
    # Normalize to HTTPS while preserving path/query exactly as published.
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.scheme == "http":
        url = "https://" + parsed.netloc + parsed.path + (
            ("?" + parsed.query) if parsed.query else ""
        )
    return url


def _cardmarket_image_score(url: str, product_id: int) -> int | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() != CARDMARKET_PRODUCT_IMAGE_HOST:
        return None
    pid = str(int(product_id))
    path = parsed.path or ""
    basename = path.rsplit("/", 1)[-1]
    if not re.search(r"\.(?:png|jpe?g|webp|gif)$", basename, flags=re.I):
        return None

    exact_file = bool(re.fullmatch(re.escape(pid) + r"\.(?:png|jpe?g|webp|gif)", basename, flags=re.I))
    path_segment = f"/{pid}/" in path
    if not exact_file and not path_segment:
        # The core safety invariant: only accept a Cardmarket image URL that
        # carries the exact idProduct in its own path/filename.
        return None

    score = 100
    if exact_file:
        score += 40
    if path_segment:
        score += 30
    if parsed.scheme == "https":
        score += 5
    if basename.casefold().endswith(".png"):
        score += 2
    return score



IMAGE_MAX_BYTES = 25 * 1024 * 1024
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif")


def _image_extension_from_bytes(data: bytes, content_type: str | None = None, url: str | None = None) -> str | None:
    head = data[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    if ctype in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if ctype == "image/png":
        return "png"
    if ctype == "image/webp":
        return "webp"
    if ctype == "image/gif":
        return "gif"
    suffix = Path(urlparse(str(url or "")).path).suffix.lower().lstrip(".")
    if suffix in IMAGE_EXTENSIONS:
        return "jpg" if suffix == "jpeg" else suffix
    return None


def _existing_image_extension(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size < 32:
            return None
        with path.open("rb") as handle:
            head = handle.read(32)
        return _image_extension_from_bytes(head, url=str(path))
    except OSError:
        return None


def _asset_public_url(public_base_url: str, relative_path: str) -> str:
    return f"{str(public_base_url).rstrip('/')}/{relative_path.replace(os.sep, '/')}"


def _asset_health(record: dict) -> dict:
    return {
        "ok": True,
        "checkedAt": record.get("storedAt") or record.get("adoptedAt") or utc_now_iso(),
        "httpStatus": record.get("httpStatus"),
        "contentType": record.get("contentType"),
        "contentDisposition": None,
        "signature": record.get("extension"),
        "finalUrl": record.get("publicUrl"),
        "error": None,
        "assetLocalPath": record.get("localPath"),
        "sha256": record.get("sha256"),
    }


def _safe_asset_component(value: str | None, fallback: str = "misc") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return text or fallback


def _bandai_asset_stem(printing_id: str) -> Path:
    clean = _safe_asset_component(printing_id)
    family_match = re.match(r"^(OP\d{2}|EB\d{2}|ST\d{2}|PRB\d{2}|P)-?", clean, re.I)
    family = (family_match.group(1).upper() if family_match else "misc")
    return Path("bandai") / family / clean


def _cardmarket_asset_stem(product_id: int) -> Path:
    pid = str(int(product_id))
    return Path("cardmarket") / pid[:3] / pid


def _find_existing_asset(image_dir: Path, stem: Path, manifest_record: dict | None = None) -> tuple[Path, str] | None:
    candidates: list[Path] = []
    if isinstance(manifest_record, dict) and manifest_record.get("localPath"):
        candidates.append(image_dir / str(manifest_record["localPath"]))
    stem_path = image_dir / stem
    for ext in IMAGE_EXTENSIONS:
        candidates.append(stem_path.with_suffix("." + ext))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ext = _existing_image_extension(candidate)
        if ext:
            return candidate, ext
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_persistent_image_asset(
    session: requests.Session,
    manifest: dict,
    *,
    asset_key: str,
    image_dir: Path,
    relative_stem: Path,
    public_base_url: str,
    source_url: str | None,
    source: str,
    referer: str | None = None,
    refresh: bool = False,
    allow_download: bool = True,
    metadata: dict | None = None,
) -> tuple[dict | None, str]:
    """Return a persistent local image record and never re-download a valid asset by default."""
    manifest["schemaVersion"] = 1
    manifest["publicBaseUrl"] = str(public_base_url).rstrip("/")
    assets = manifest.setdefault("assets", {})
    if not isinstance(assets, dict):
        assets = {}
        manifest["assets"] = assets
    existing_record = assets.get(asset_key) if isinstance(assets.get(asset_key), dict) else None
    existing = _find_existing_asset(image_dir, relative_stem, existing_record)
    if existing and not refresh:
        path, ext = existing
        relative = path.relative_to(image_dir).as_posix()
        record = {
            **(existing_record or {}),
            "key": asset_key,
            "source": source,
            "sourceUrl": source_url or (existing_record or {}).get("sourceUrl"),
            "localPath": relative,
            "publicUrl": _asset_public_url(public_base_url, relative),
            "extension": ext,
            "bytes": path.stat().st_size,
            "sha256": (existing_record or {}).get("sha256") or _sha256_file(path),
            "adoptedAt": (existing_record or {}).get("adoptedAt") or utc_now_iso(),
            **(metadata or {}),
        }
        assets[asset_key] = record
        return record, "reused" if existing_record else "adopted"

    if not source_url or not allow_download:
        return None, "pending"

    headers = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        response = session.get(source_url, headers=headers, timeout=60, stream=True, allow_redirects=True)
    except requests.RequestException as error:
        return {"key": asset_key, "sourceUrl": source_url, "error": str(error)[:1000]}, "error"
    try:
        status = int(response.status_code)
        if status not in {200, 206}:
            return {
                "key": asset_key,
                "sourceUrl": source_url,
                "httpStatus": status,
                "error": f"HTTP {status}",
            }, "error"
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=128 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > IMAGE_MAX_BYTES:
                return {"key": asset_key, "sourceUrl": source_url, "error": "image-too-large"}, "error"
            chunks.append(chunk)
        payload = b"".join(chunks)
        ext = _image_extension_from_bytes(payload, content_type=content_type, url=source_url)
        if not ext:
            return {
                "key": asset_key,
                "sourceUrl": source_url,
                "httpStatus": status,
                "contentType": content_type or None,
                "error": "unsupported-or-non-image-payload",
            }, "error"
        destination = (image_dir / relative_stem).with_suffix("." + ext)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.write_bytes(payload)
        if _existing_image_extension(temp) is None:
            temp.unlink(missing_ok=True)
            return {"key": asset_key, "sourceUrl": source_url, "error": "invalid-image-signature"}, "error"
        for other_ext in IMAGE_EXTENSIONS:
            other = (image_dir / relative_stem).with_suffix("." + other_ext)
            if other != destination:
                other.unlink(missing_ok=True)
        temp.replace(destination)
        relative = destination.relative_to(image_dir).as_posix()
        record = {
            "key": asset_key,
            "source": source,
            "sourceUrl": source_url,
            "sourceFinalUrl": str(response.url),
            "localPath": relative,
            "publicUrl": _asset_public_url(public_base_url, relative),
            "extension": ext,
            "contentType": content_type or None,
            "httpStatus": status,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "storedAt": utc_now_iso(),
            **(metadata or {}),
        }
        assets[asset_key] = record
        return record, "downloaded"
    finally:
        response.close()


def _cardmarket_market_version(html: str | None, page_url: str | None) -> tuple[str | None, str | None]:
    text_url = str(page_url or "")
    match = re.search(r"-V(\d+)(?:$|[/?#])", text_url, re.I)
    if not match and html:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        heading = soup.find("h1")
        heading_text = heading.get_text(" ", strip=True) if heading else ""
        match = VERSION_RE.search(f"{title} {heading_text}")
    if not match:
        return None, None
    version = str(int(match.group(1)))
    return version, f"Version {version}"


def seed_cardmarket_image_metadata_from_mapping(image_mapping: dict, mapping: dict) -> dict:
    """Seed only explicit Cardmarket version evidence already present in stored product URLs."""
    image_mapping = image_mapping if isinstance(image_mapping, dict) else {}
    products = image_mapping.setdefault("products", {})
    seeded = 0
    conflicts = 0
    for _, entry in (mapping or {}).get("mappings", {}).items():
        if not isinstance(entry, dict):
            continue
        pid = get_number(entry.get("productId"))
        if pid is None:
            continue
        url = nullable_text(entry.get("url"))
        version, label = _cardmarket_market_version(None, url)
        if not version:
            continue
        key = str(int(pid))
        record = products.get(key) if isinstance(products.get(key), dict) else {"productId": int(pid)}
        previous = nullable_text(record.get("marketVersion"))
        if previous and previous != version:
            record["versionConflict"] = sorted({previous, version})
            record.pop("marketVersion", None)
            record.pop("marketVersionLabel", None)
            conflicts += 1
        else:
            record["marketVersion"] = version
            record["marketVersionLabel"] = label
            record["versionSource"] = "stored-cardmarket-product-url"
            if url and not record.get("productPage"):
                record["productPage"] = url
            seeded += 1
        products[key] = record
    return {"seeded": seeded, "conflicts": conflicts}


def apply_cardmarket_image_overrides(image_mapping: dict, overrides: dict, products_by_id: dict[int, dict]) -> dict:
    """Apply user-maintained, idProduct-keyed metadata without allowing identity changes."""
    products = image_mapping.setdefault("products", {})
    rows = (overrides or {}).get("products", overrides if isinstance(overrides, dict) else {})
    if not isinstance(rows, dict):
        return {"applied": 0, "ignored": 0}
    applied = 0
    ignored = 0
    allowed = {
        "marketVersion", "marketVersionLabel", "editionCode", "editionName", "editionSlug",
        "language", "languageLabel", "languageGroup", "languageSource", "productPage", "imageUrl",
    }
    for raw_pid, override in rows.items():
        pid = get_number(raw_pid)
        if pid is None or int(pid) not in products_by_id or not isinstance(override, dict):
            ignored += 1
            continue
        pid = int(pid)
        record = products.get(str(pid)) if isinstance(products.get(str(pid)), dict) else {"productId": pid}
        for field in allowed:
            if field in override and override[field] is not None:
                if field == "imageUrl" and _cardmarket_image_score(str(override[field]), pid) is None:
                    continue
                record[field] = override[field]
        if record.get("marketVersion") and not record.get("marketVersionLabel"):
            record["marketVersionLabel"] = f"Version {record['marketVersion']}"
        record["overrideSource"] = "data/cardmarket_image_overrides.json"
        products[str(pid)] = record
        applied += 1
    return {"applied": applied, "ignored": ignored}


def _cardmarket_image_directory_from_url(url: str | None, product_id: int) -> str | None:
    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").casefold() != CARDMARKET_PRODUCT_IMAGE_HOST:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    pid = str(int(product_id))
    for index, part in enumerate(parts):
        if part == pid and index >= 1:
            previous = parts[index - 1]
            if previous.isdigit() and index >= 2:
                previous = parts[index - 2]
            return previous if previous and previous != pid else None
    return None


def _cardmarket_image_dir_candidates(product: dict, context: dict | None = None, known_dir: str | None = None) -> list[str]:
    context = context or {}
    raw: list[str] = []
    if known_dir:
        raw.append(str(known_dir))
    edition = nullable_text(context.get("editionCode"))
    if edition:
        raw.append(edition)
    for value in (context.get("baseCode"), _product_card_code(product), product.get("name")):
        text = str(value or "").upper()
        for token in re.findall(r"(?:OP|EB|ST|PRB)\s*-?\s*0*(\d{1,2})", text):
            prefix_match = re.search(r"(OP|EB|ST|PRB)\s*-?\s*0*" + re.escape(token), text)
            if prefix_match:
                raw.append(f"{prefix_match.group(1)}{int(token):02d}")
    code = nullable_text(context.get("baseCode")) or _product_card_code(product)
    if code and str(code).upper().startswith("P-"):
        raw.extend(["P", "P-JP", "STP", "UP", "OPPR"])
    name_upper = str(product.get("name") or "").upper()
    for token in re.findall(r"\b(?:OP|EB|ST|PRB)\d{2}\b", name_upper):
        raw.append(token)
    if _is_cardmarket_don_product(product):
        raw.extend(["PRB01", "PRB01-JP", "PRB02", "PRB02-JP", "STP", "UP", "P", "P-JP", "OPPR"])

    expanded: list[str] = []
    language_group = nullable_text(context.get("languageGroup"))
    for item in raw:
        item = re.sub(r"\s+", "", str(item).upper())
        if not item:
            continue
        expanded.append(item)
        if re.fullmatch(r"(?:OP|EB|ST|PRB)\d{2}", item):
            expanded.append(item + "P")
            if language_group in {"ja", "non-en"} or "-JP" in str(edition or "").upper():
                expanded.extend([item + "-JP", item + "JP"])
            else:
                expanded.extend([item + "-JP", item + "JP"])
    seen = set()
    result = []
    for item in expanded:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= 18:
            break
    return result


def fetch_cardmarket_cdn_image_record(
    session: requests.Session,
    product: dict,
    *,
    context: dict | None = None,
    known_dir: str | None = None,
) -> dict | None:
    """Probe only URLs whose path contains the exact idProduct; a 200 image proves scope."""
    product_id = int(product["idProduct"])
    id_category = int(get_number(product.get("idCategory")) or 1621)
    page_url = nullable_text(product.get("website")) or CARDMARKET_PRODUCT_REDIRECT.format(product_id=product_id)
    for directory in _cardmarket_image_dir_candidates(product, context=context, known_dir=known_dir):
        for ext in ("jpg", "png", "webp"):
            image_url = f"https://{CARDMARKET_PRODUCT_IMAGE_HOST}/{id_category}/{directory}/{product_id}/{product_id}.{ext}"
            if _cardmarket_image_score(image_url, product_id) is None:
                continue
            try:
                response = session.get(
                    image_url,
                    headers={"Referer": page_url, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
                    timeout=20,
                    stream=True,
                    allow_redirects=True,
                )
            except requests.RequestException:
                continue
            try:
                if response.status_code not in {200, 206}:
                    continue
                head = next((chunk for chunk in response.iter_content(chunk_size=64) if chunk), b"")
                image_ext = _image_extension_from_bytes(head, response.headers.get("Content-Type"), image_url)
                if not image_ext:
                    continue
                return {
                    "productId": product_id,
                    "productPage": page_url,
                    "attemptedAt": utc_now_iso(),
                    "source": "cardmarket-product-cdn",
                    "matchMethod": "idProduct-cdn-probe",
                    "exactProductMatch": True,
                    "status": "found",
                    "httpStatus": int(response.status_code),
                    "imageUrl": image_url,
                    "cdnDirectory": directory,
                    "discoveredAt": utc_now_iso(),
                    "error": None,
                }
            finally:
                response.close()
    return None


def build_cardmarket_image_target_contexts(
    target_product_ids: list[int],
    products_by_id: dict[int, dict],
    expansion_metadata: dict[int, dict],
    bandai_variant_candidates: dict[int, str],
) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for product_id in target_product_ids:
        product = products_by_id.get(int(product_id)) or {}
        metadata = _product_expansion_metadata(product, expansion_metadata)
        result[int(product_id)] = {
            "baseCode": bandai_variant_candidates.get(int(product_id)) or _product_card_code(product),
            **metadata,
        }
    return result


def persist_bandai_image_assets(
    session: requests.Session,
    bandai_cards: list[dict],
    image_dir: Path,
    manifest: dict,
    image_cache: dict,
    public_base_url: str,
    *,
    allow_downloads: bool,
    refresh: bool,
    max_new: int = 0,
) -> dict:
    stats = {"targets": 0, "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0}
    images_cache = image_cache.setdefault("images", {})
    seen = set()
    for card in bandai_cards:
        printing_id = canonical_id(card.get("sourcePrintingId"))
        source_url = nullable_text(card.get("imageUrl"))
        if not printing_id or not source_url or printing_id in seen:
            continue
        seen.add(printing_id)
        stats["targets"] += 1
        can_download = allow_downloads and (not max_new or stats["downloaded"] < max_new)
        record, action = ensure_persistent_image_asset(
            session,
            manifest,
            asset_key=f"bandai:{printing_id}",
            image_dir=image_dir,
            relative_stem=_bandai_asset_stem(printing_id),
            public_base_url=public_base_url,
            source_url=source_url,
            source="bandai",
            referer=str(card.get("sourceUrl") or BANDAI_CARDLIST_URLS[0]),
            refresh=refresh,
            allow_download=can_download,
            metadata={"printingId": printing_id},
        )
        if action in stats:
            stats[action] += 1
        elif action == "error":
            stats["errors"] += 1
        if record and record.get("publicUrl"):
            images_cache[source_url] = _asset_health(record)
    manifest["updatedAt"] = utc_now_iso()
    return stats


def persist_cardmarket_image_assets(
    session: requests.Session,
    image_mapping: dict,
    target_product_ids: list[int],
    image_dir: Path,
    manifest: dict,
    image_cache: dict,
    public_base_url: str,
    *,
    allow_downloads: bool,
    refresh: bool,
    max_new: int = 0,
) -> dict:
    products = image_mapping.setdefault("products", {})
    images_cache = image_cache.setdefault("images", {})
    stats = {"targets": len(target_product_ids), "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0, "manual": 0}
    for product_id in target_product_ids:
        product_id = int(product_id)
        key = str(product_id)
        discovery = products.get(key) if isinstance(products.get(key), dict) else {"productId": product_id}
        source_url = nullable_text(discovery.get("imageUrl"))
        referer = nullable_text(discovery.get("productPageFinal")) or nullable_text(discovery.get("productPage")) or CARDMARKET_PRODUCT_REDIRECT.format(product_id=product_id)
        can_download = allow_downloads and (not max_new or stats["downloaded"] < max_new)
        asset, action = ensure_persistent_image_asset(
            session,
            manifest,
            asset_key=f"cardmarket:{product_id}",
            image_dir=image_dir,
            relative_stem=_cardmarket_asset_stem(product_id),
            public_base_url=public_base_url,
            source_url=source_url,
            source="cardmarket",
            referer=referer,
            refresh=refresh,
            allow_download=can_download,
            metadata={"productId": product_id},
        )
        if action in stats:
            stats[action] += 1
        elif action == "error":
            stats["errors"] += 1
        if not asset or not asset.get("publicUrl"):
            products[key] = discovery
            continue
        if action == "adopted" and not source_url:
            stats["manual"] += 1
        discovery.update({
            "productId": product_id,
            "status": "stored" if source_url else "stored-manual",
            "localPath": asset.get("localPath"),
            "publicUrl": asset.get("publicUrl"),
            "assetSha256": asset.get("sha256"),
            "assetBytes": asset.get("bytes"),
            "assetStoredAt": asset.get("storedAt") or asset.get("adoptedAt"),
            "exactProductMatch": True,
            "assetSource": "downloaded-cardmarket" if source_url else "manual-idProduct-file",
        })
        products[key] = discovery
        if source_url:
            images_cache[source_url] = _asset_health(asset)
    manifest["updatedAt"] = utc_now_iso()
    return stats


def build_cardmarket_image_pending(
    target_product_ids: list[int],
    products_by_id: dict[int, dict],
    contexts: dict[int, dict],
    image_mapping: dict,
) -> dict:
    entries = (image_mapping or {}).get("products", {})
    pending = []
    for product_id in target_product_ids:
        product_id = int(product_id)
        record = entries.get(str(product_id)) if isinstance(entries.get(str(product_id)), dict) else {}
        if record.get("publicUrl") and record.get("localPath"):
            continue
        product = products_by_id.get(product_id) or {}
        context = contexts.get(product_id) or {}
        pending.append({
            "idProduct": product_id,
            "name": product.get("name"),
            "idExpansion": product.get("idExpansion"),
            "idMetacard": product.get("idMetacard"),
            "baseCode": context.get("baseCode"),
            "editionCode": record.get("editionCode") or context.get("editionCode"),
            "languageGroup": record.get("languageGroup") or context.get("languageGroup"),
            "marketVersion": record.get("marketVersion"),
            "lastStatus": record.get("status"),
            "lastError": record.get("error"),
            "manualFileStem": _cardmarket_asset_stem(product_id).as_posix(),
        })
    return {
        "schemaVersion": 1,
        "catalogVersion": "3.13.0",
        "generatedAt": utc_now_iso(),
        "pendingCount": len(pending),
        "instructions": "Añade jpg/png/webp con el idProduct indicado bajo images/<manualFileStem> y el siguiente run lo adoptará sin scraping.",
        "products": pending,
    }


def extract_cardmarket_product_image_url(
    html: str,
    page_url: str,
    product_id: int,
) -> str | None:
    """Extract the exact Cardmarket product image from one public product page.

    No URL path/category (OPPR, ST-10, ST-10-JP, ...) is guessed. Candidates are
    read from the HTML and accepted only when hosted on Cardmarket's product-image
    S3 host and when the URL itself contains the exact idProduct.
    """
    candidates: list[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    scalar_attrs = ("src", "data-src", "data-original", "data-lazy-src", "content", "href")
    srcset_attrs = ("srcset", "data-srcset")

    for tag in soup.find_all(True):
        for attr in scalar_attrs:
            value = tag.get(attr)
            if isinstance(value, str):
                candidates.append(value)
        for attr in srcset_attrs:
            value = tag.get(attr)
            if not isinstance(value, str):
                continue
            for item in value.split(","):
                candidate = item.strip().split(" ", 1)[0]
                if candidate:
                    candidates.append(candidate)

    # Fallback for JSON/JS snippets where the image is not represented as a
    # normal DOM attribute. Keep it deliberately host-specific.
    raw_html = html_lib.unescape(html or "").replace("\\/", "/")
    pattern = re.compile(
        r"(?:https?:)?//product-images\.s3\.cardmarket\.com/[^\s\"'<>]+",
        flags=re.I,
    )
    candidates.extend(pattern.findall(raw_html))

    ranked: list[tuple[int, int, str]] = []
    seen = set()
    for index, candidate in enumerate(candidates):
        url = _cardmarket_image_candidate(candidate, page_url)
        if not url or url in seen:
            continue
        seen.add(url)
        score = _cardmarket_image_score(url, product_id)
        if score is None:
            continue
        ranked.append((score, -index, url))

    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def make_cardmarket_product_page_session() -> requests.Session:
    # Do not aggressively retry 403/429. If Cardmarket asks us to slow/stop,
    # discovery halts gracefully and the normal catalogue generation continues.
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=1.0,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.7",
        "Referer": "https://www.cardmarket.com/en/OnePiece",
    })
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_cardmarket_product_image_record(
    session: requests.Session,
    product: dict,
) -> dict:
    product_id = int(product["idProduct"])
    page_url = nullable_text(product.get("website")) or CARDMARKET_PRODUCT_REDIRECT.format(
        product_id=product_id
    )
    attempted_at = utc_now_iso()
    base = {
        "productId": product_id,
        "productPage": page_url,
        "attemptedAt": attempted_at,
        "source": "cardmarket-product-page",
        "matchMethod": "idProduct-in-image-url",
        "exactProductMatch": True,
    }
    try:
        response = session.get(page_url, timeout=45, allow_redirects=True)
    except requests.RequestException as error:
        return {**base, "status": "error", "httpStatus": None, "error": str(error)[:1000]}

    status = int(response.status_code)
    final_page = str(response.url)
    if status == 429:
        retry_after = nullable_text(response.headers.get("Retry-After"))
        response.close()
        return {
            **base,
            "productPageFinal": final_page,
            "status": "rate-limited",
            "httpStatus": status,
            "retryAfter": retry_after,
            "error": "Cardmarket devolvió HTTP 429; discovery detenido para respetar el límite.",
        }
    if status in {401, 403}:
        response.close()
        return {
            **base,
            "productPageFinal": final_page,
            "status": "blocked",
            "httpStatus": status,
            "error": f"Cardmarket devolvió HTTP {status}; discovery detenido de forma conservadora.",
        }
    if status >= 400:
        response.close()
        return {
            **base,
            "productPageFinal": final_page,
            "status": "http-error",
            "httpStatus": status,
            "error": f"HTTP {status}",
        }

    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    html = response.text
    response.close()
    market_version, market_version_label = _cardmarket_market_version(html, final_page)
    image_url = extract_cardmarket_product_image_url(html, final_page, product_id)
    if not image_url:
        return {
            **base,
            "productPageFinal": final_page,
            "status": "not-found",
            "httpStatus": status,
            "contentType": content_type or None,
            "error": "No se encontró una URL de product-images con el mismo idProduct.",
        }
    return {
        **base,
        "productPageFinal": final_page,
        "status": "found",
        "httpStatus": status,
        "contentType": content_type or None,
        "imageUrl": image_url,
        "marketVersion": market_version,
        "marketVersionLabel": market_version_label,
        "versionSource": "cardmarket-product-page" if market_version else None,
        "discoveredAt": attempted_at,
        "error": None,
    }


def cardmarket_supplement_product_ids(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
) -> list[int]:
    """Return exactly the idProducts that can become Cardmarket-only printings."""
    bandai_printing_ids = {
        canonical_id(card.get("sourcePrintingId"))
        for card in bandai_cards
        if canonical_id(card.get("sourcePrintingId"))
    }
    bandai_codes = {base_code(printing_id) for printing_id in bandai_printing_ids}
    # Exclude only products actually attached to a printing present in the
    # current Bandai snapshot. Stale/historical mapping keys must not hide a
    # Cardmarket-only product from image discovery.
    mapped_product_ids = {
        int(product_id)
        for printing_id, entry in (mapping or {}).get("mappings", {}).items()
        if canonical_id(printing_id) in bandai_printing_ids and isinstance(entry, dict)
        for product_id in [get_number(entry.get("productId"))]
        if product_id is not None
    }
    result = []
    for product_id, product in products_by_id.items():
        product_id = int(product_id)
        if product_id in mapped_product_ids:
            continue
        if _is_cardmarket_don_product(product):
            if get_number(product.get("idMetacard")) is not None:
                result.append(product_id)
            continue
        code = _product_card_code(product)
        if code and code not in bandai_codes:
            result.append(product_id)
    return sorted(set(result))



# ---------------------------------------------------------------------------
# Cardmarket expansion/language evidence + Bandai-linked physical variants
# ---------------------------------------------------------------------------

# One deliberately small verified override. Cardmarket's public One Piece
# expansion catalogue identifies P-JP as "Promos (Japanese)"; the current bulk
# singles catalogue uses idExpansion=5511 for the verified P-028 Japanese
# products 707718/764433. Keeping this explicit is safer than guessing every
# non-English expansion from dates or product order.
CARDMARKET_VERIFIED_EXPANSIONS = {
    5511: {
        "editionCode": "P-JP",
        "editionName": "Promos (Japanese)",
        "editionSlug": "Promos-Japanese",
        "language": "ja",
        "languageLabel": "Japanese",
        "languageGroup": "ja",
        "languageSource": "verified-cardmarket-expansion",
    },
}

CARDMARKET_EDITION_CODE_BY_SLUG = {
    "promos": "P",
    "special-tournaments-promos": "STP",
    "special-tournament-promos": "STP",
    "winner-cards": "WC",
    "judge-promos": "JDG",
    "reprints": "R",
    "unnumbered-promos": "UP",
    "store-tournament-promos": "STR",
}


def _explicit_nonsingle_language_marker(name: str | None) -> str | None:
    """Return a supported *explicit qualifier* from one Cardmarket product name.

    Bare words such as ``Japanese Championship`` or ``English Collection`` are
    intentionally ignored. ``Non-English`` is checked before ``English`` even
    though the strict parenthesised patterns already prevent substring matches.
    """
    text = str(name or "")
    if re.search(r"\(\s*Non[- ]English\s*\)", text, re.I):
        return "non-en"
    if re.search(r"\(\s*Japanese(?:\s+Version)?\s*\)", text, re.I):
        return "ja"
    if re.search(r"\(\s*English(?:\s+Version)?\s*\)", text, re.I):
        return "en"
    return None


def _explicit_nonsingle_language(names: list[str]) -> dict:
    """Return expansion language only when explicit non-single evidence is homogeneous.

    Every non-single name in the expansion must carry the same supported explicit
    qualifier. A mixture of qualifiers, or qualified + unqualified product names,
    is deliberately left unknown rather than extrapolating one region/language to
    the whole expansion.
    """
    rows = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not rows:
        return {}

    markers = [_explicit_nonsingle_language_marker(name) for name in rows]
    explicit = [marker for marker in markers if marker is not None]
    if not explicit:
        return {}

    counts = Counter(explicit)
    unqualified = len(rows) - len(explicit)
    if len(counts) != 1 or unqualified:
        return {
            "language": None,
            "languageLabel": None,
            "languageGroup": None,
            "languageSource": "cardmarket-nonsingles-mixed-or-incomplete-language-evidence",
        }

    marker = explicit[0]
    if marker == "non-en":
        # Cardmarket uses Non-English for several regional products. It proves
        # only that English is wrong; it does not prove Japanese specifically.
        return {
            "language": None,
            "languageLabel": "Non-English",
            "languageGroup": "non-en",
            "languageSource": "cardmarket-nonsingles-explicit-non-english",
        }
    if marker == "ja":
        return {
            "language": "ja",
            "languageLabel": "Japanese",
            "languageGroup": "ja",
            "languageSource": "cardmarket-nonsingles-explicit-japanese",
        }
    if marker == "en":
        return {
            "language": "en",
            "languageLabel": "English",
            "languageGroup": "en",
            "languageSource": "cardmarket-nonsingles-explicit-english",
        }
    return {}


def build_cardmarket_expansion_metadata(
    mapping: dict,
    products_by_id: dict[int, dict],
    nonsingle_products: list[dict] | None = None,
) -> tuple[dict[int, dict], dict]:
    """Build conservative idExpansion metadata without per-product web requests.

    Stable pretty-URL families are retained only as edition metadata. Language
    comes from homogeneous explicit qualifiers in the official Cardmarket
    non-singles S3 catalogue. Verified overrides win last.
    """
    metadata: dict[int, dict] = {}
    profile = _build_mapping_profiles(mapping, products_by_id)
    stable_slugs = profile.get("stableExpansionSlug", {})

    for expansion, slug_profile in stable_slugs.items():
        slug = nullable_text((slug_profile or {}).get("value"))
        if not slug:
            continue
        slug_key = slugify(slug)
        # These slugs are learned from current validated Bandai mappings. Generic
        # buckets such as one-piece-products can contain mixed regional items, so
        # only specific stable families are retained as edition metadata.
        specific = bool(
            slug_key in CARDMARKET_EDITION_CODE_BY_SLUG
            or re.search(r"(?:^|-)(?:op|st|eb|prb)-?\d{1,2}(?:-|$)", slug_key, re.I)
        )
        if not specific:
            continue
        metadata[int(expansion)] = {
            "idExpansion": int(expansion),
            "editionSlug": slug,
            "editionCode": CARDMARKET_EDITION_CODE_BY_SLUG.get(slug_key),
            "editionName": " ".join(part.capitalize() for part in slug.split("-") if part),
            # V3.11.4: a Bandai-English mapping proves edition coherence, not that
            # every Cardmarket product in the expansion is English. Language is
            # filled only from homogeneous explicit non-single qualifiers below.
            "evidence": {
                "count": slug_profile.get("count"),
                "total": slug_profile.get("total"),
                "ratio": slug_profile.get("ratio"),
            },
        }

    names_by_expansion = defaultdict(list)
    for product in nonsingle_products or []:
        if not isinstance(product, dict):
            continue
        expansion = get_number(product.get("idExpansion"))
        name = nullable_text(product.get("name"))
        if expansion is None or not name:
            continue
        names_by_expansion[int(expansion)].append(name)

    for expansion, names in names_by_expansion.items():
        evidence = _explicit_nonsingle_language(names)
        if not evidence:
            continue
        current = dict(metadata.get(expansion) or {"idExpansion": expansion})
        # Explicit non-single evidence is authoritative for language classification.
        # This intentionally clears any language fields when the expansion is mixed
        # or incomplete. Verified overrides are applied afterwards and still win.
        current.update(evidence)
        current["nonsingleEvidenceSample"] = names[:8]
        metadata[expansion] = current

    for expansion, verified in CARDMARKET_VERIFIED_EXPANSIONS.items():
        metadata[int(expansion)] = {
            **(metadata.get(int(expansion)) or {"idExpansion": int(expansion)}),
            **verified,
            "idExpansion": int(expansion),
        }

    counts = Counter()
    for row in metadata.values():
        counts[row.get("language") or row.get("languageGroup") or "unknown"] += 1
    stats = {
        "expansionsWithMetadata": len(metadata),
        "languageEvidenceCounts": dict(sorted(counts.items())),
        "verifiedExpansionIds": sorted(CARDMARKET_VERIFIED_EXPANSIONS),
        "nonsingleExpansionsWithExplicitLanguage": sum(
            1 for expansion in names_by_expansion
            if _explicit_nonsingle_language(names_by_expansion[expansion])
        ),
    }
    return metadata, stats


def cardmarket_bandai_variant_candidates(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
) -> tuple[dict[int, str], dict]:
    """Find extra Cardmarket physical products that safely belong to Bandai cards.

    Safety contract: idMetacard must already be anchored by at least one current
    validated Bandai printing mapping and resolve to exactly one Bandai catalogId.
    The supplemental product must also carry the same printed code and normalized
    name. No code-only, date-order or V.1/V.2 guessing is allowed.
    """
    current_printing_ids = {
        canonical_id(card.get("sourcePrintingId"))
        for card in bandai_cards
        if canonical_id(card.get("sourcePrintingId"))
    }
    bandai_names = defaultdict(Counter)
    for card in bandai_cards:
        printing_id = canonical_id(card.get("sourcePrintingId"))
        if not printing_id:
            continue
        name = slugify(card.get("name"))
        if name:
            bandai_names[base_code(printing_id)][name] += 1

    anchors = defaultdict(set)
    current_mapped_products = set()
    for printing_id, entry in (mapping or {}).get("mappings", {}).items():
        normalized_printing = canonical_id(printing_id)
        if normalized_printing not in current_printing_ids or not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        if product_id is None:
            continue
        product_id = int(product_id)
        product = products_by_id.get(product_id)
        if not isinstance(product, dict):
            continue
        metacard = get_number(product.get("idMetacard"))
        if metacard is None:
            continue
        anchors[int(metacard)].add(base_code(normalized_printing))
        current_mapped_products.add(product_id)

    unique_anchors = {
        metacard: next(iter(catalog_ids))
        for metacard, catalog_ids in anchors.items()
        if len(catalog_ids) == 1
    }
    ambiguous_anchors = {
        metacard: sorted(catalog_ids)
        for metacard, catalog_ids in anchors.items()
        if len(catalog_ids) > 1
    }

    candidates: dict[int, str] = {}
    rejected = []
    for product_id, product in products_by_id.items():
        product_id = int(product_id)
        if product_id in current_mapped_products or _is_cardmarket_don_product(product):
            continue
        metacard = get_number(product.get("idMetacard"))
        if metacard is None or int(metacard) not in unique_anchors:
            continue
        catalog_id = unique_anchors[int(metacard)]
        product_code = _product_card_code(product)
        expected_name_rows = bandai_names.get(catalog_id, Counter()).most_common(1)
        expected_name = expected_name_rows[0][0] if expected_name_rows else ""
        product_name = slugify(_product_display_name(product))
        if product_code != catalog_id:
            rejected.append({
                "productId": product_id,
                "idMetacard": int(metacard),
                "catalogId": catalog_id,
                "reason": "printed-code-mismatch",
                "productCode": product_code,
            })
            continue
        if expected_name and product_name and expected_name != product_name:
            rejected.append({
                "productId": product_id,
                "idMetacard": int(metacard),
                "catalogId": catalog_id,
                "reason": "name-mismatch",
                "expectedName": expected_name,
                "productName": product_name,
            })
            continue
        candidates[product_id] = catalog_id

    return candidates, {
        "uniqueMetacardAnchors": len(unique_anchors),
        "ambiguousMetacardAnchors": len(ambiguous_anchors),
        "ambiguousAnchorSample": [
            {"idMetacard": metacard, "catalogIds": catalog_ids}
            for metacard, catalog_ids in sorted(ambiguous_anchors.items())[:50]
        ],
        "candidateProducts": len(candidates),
        "rejectedProducts": len(rejected),
        "rejectedSample": rejected[:100],
    }


def legacy_mapping_reference_images(mapping: dict | None) -> tuple[dict[int, dict], dict]:
    """Return product-scoped reference images preserved by legacy mappings.

    A reference is eligible only when the same Cardmarket ``idProduct`` has one
    unique ``legacyImageUrl`` across mapping entries. These images remain
    non-authoritative references and are still validated through image health.
    """
    by_product = defaultdict(list)
    for mapping_key, entry in (mapping or {}).get("mappings", {}).items():
        if not isinstance(entry, dict):
            continue
        product_id = get_number(entry.get("productId"))
        image_url = nullable_text(entry.get("legacyImageUrl"))
        if product_id is None or not image_url:
            continue
        by_product[int(product_id)].append((str(mapping_key), entry, image_url))

    result: dict[int, dict] = {}
    ambiguous = []
    for product_id, rows in sorted(by_product.items()):
        urls = sorted({image_url for _, _, image_url in rows})
        if len(urls) != 1:
            ambiguous.append({
                "productId": product_id,
                "mappingKeys": sorted({key for key, _, _ in rows}),
                "urls": urls,
            })
            continue
        url = urls[0]
        keys = sorted({key for key, _, _ in rows})
        legacy_sets = sorted({
            str(entry.get("legacySet"))
            for _, entry, _ in rows
            if nullable_text(entry.get("legacySet"))
        })
        result[product_id] = {
            "url": url,
            "sourceUrl": url,
            "source": "legacy-cardmarket-snapshot",
            "sourceRecordId": ",".join(keys),
            "sourceName": keys[0] if len(keys) == 1 else f"{len(keys)} legacy mappings",
            "sourceFullName": legacy_sets[0] if len(legacy_sets) == 1 else None,
            "matchMethod": "legacy-mapping-idProduct",
            "authoritative": False,
            "scope": "single-cardmarket-product",
            "productId": product_id,
            "validated": None,
        }

    return result, {
        "candidateProducts": len(by_product),
        "uniqueProductReferences": len(result),
        "ambiguousProducts": len(ambiguous),
        "ambiguousSample": ambiguous[:50],
    }


def _product_expansion_metadata(product: dict, expansion_metadata: dict[int, dict]) -> dict:
    expansion = get_number((product or {}).get("idExpansion"))
    if expansion is None:
        return {}
    row = expansion_metadata.get(int(expansion)) or {}
    return {
        key: row.get(key)
        for key in (
            "language", "languageLabel", "languageGroup", "languageSource",
            "editionCode", "editionName", "editionSlug",
        )
    }


def _cardmarket_image_failure_is_recent(record: dict, now: datetime) -> bool:
    if not isinstance(record, dict) or record.get("status") == "found":
        return False
    attempted = _parse_utc_iso(record.get("attemptedAt"))
    if attempted is None:
        return False
    return now - attempted < timedelta(days=CARDMARKET_IMAGE_FAILURE_RETRY_DAYS)


def discover_cardmarket_product_images(
    products_by_id: dict[int, dict],
    target_product_ids: list[int],
    cache: dict | None,
    *,
    target_contexts: dict[int, dict] | None = None,
    image_health_cache: dict | None = None,
    network_enabled: bool = True,
    delay_seconds: float = 0.75,
    max_new: int = 0,
) -> tuple[dict, dict]:
    """Populate a persistent idProduct -> exact Cardmarket image URL cache.

    Existing successful entries are reused without touching Cardmarket. Failed
    entries are retried only after a cooling period. HTTP 403/429 stops discovery
    immediately but never aborts the official catalogue/price pipeline.
    """
    cache = cache if isinstance(cache, dict) else {}
    cache["schemaVersion"] = 2
    target_contexts = target_contexts or {}
    entries = cache.setdefault("products", {})
    if not isinstance(entries, dict):
        entries = {}
        cache["products"] = entries
    expansion_dirs = cache.setdefault("expansionImageDirs", {})
    if not isinstance(expansion_dirs, dict):
        expansion_dirs = {}
        cache["expansionImageDirs"] = expansion_dirs
    health = (image_health_cache or {}).get("images", {}) if isinstance(image_health_cache, dict) else {}
    now = datetime.now(timezone.utc)
    delay_seconds = max(0.0, float(delay_seconds))
    max_new = max(0, int(max_new))

    stats = {
        "enabled": bool(network_enabled),
        "targets": len(target_product_ids),
        "cachedFound": 0,
        "attempted": 0,
        "discovered": 0,
        "cdnDiscovered": 0,
        "pageDiscovered": 0,
        "storedAssets": 0,
        "refreshedAfterImageFailure": 0,
        "notFound": 0,
        "errors": 0,
        "skippedRecentFailure": 0,
        "stoppedReason": None,
    }

    if not network_enabled:
        stats["cachedFound"] = sum(
            1
            for product_id in target_product_ids
            if isinstance(entries.get(str(product_id)), dict)
            and (entries[str(product_id)].get("publicUrl") or (entries[str(product_id)].get("status") == "found" and entries[str(product_id)].get("imageUrl")))
        )
        cache["updatedAt"] = utc_now_iso()
        return cache, stats

    session = make_cardmarket_product_page_session()
    blocked_streak = 0
    try:
        for index, product_id in enumerate(target_product_ids, start=1):
            key = str(int(product_id))
            existing = entries.get(key) if isinstance(entries.get(key), dict) else None
            cached_url = nullable_text((existing or {}).get("imageUrl"))
            cached_health = health.get(cached_url) if cached_url else None
            cached_image_failed = isinstance(cached_health, dict) and cached_health.get("ok") is False

            if existing and existing.get("publicUrl") and existing.get("localPath"):
                stats["cachedFound"] += 1
                stats["storedAssets"] += 1
                continue
            if existing and existing.get("status") == "found" and cached_url and not cached_image_failed:
                stats["cachedFound"] += 1
                continue
            if existing and not cached_image_failed and _cardmarket_image_failure_is_recent(existing, now):
                stats["skippedRecentFailure"] += 1
                continue
            if max_new and stats["attempted"] >= max_new:
                stats["stoppedReason"] = f"max-new={max_new}"
                break

            product = products_by_id.get(int(product_id))
            if not isinstance(product, dict):
                continue
            print(
                f"Cardmarket imagen exacta {index}/{len(target_product_ids)}: "
                f"idProduct={product_id}"
            )
            context = target_contexts.get(int(product_id)) or {}
            expansion_id = get_number(product.get("idExpansion"))
            known_dir = expansion_dirs.get(str(int(expansion_id))) if expansion_id is not None else None
            record = fetch_cardmarket_cdn_image_record(session, product, context=context, known_dir=known_dir)
            discovery_method = "cdn" if record else "page"
            if record is None:
                record = fetch_cardmarket_product_image_record(session, product)
            if existing:
                for field in ("marketVersion", "marketVersionLabel", "versionSource", "editionCode", "editionName", "editionSlug", "language", "languageLabel", "languageGroup", "languageSource", "productPage"):
                    if not record.get(field) and existing.get(field) is not None:
                        record[field] = existing.get(field)
            directory = _cardmarket_image_directory_from_url(record.get("imageUrl"), product_id)
            if directory and expansion_id is not None:
                expansion_dirs[str(int(expansion_id))] = directory
            entries[key] = record
            stats["attempted"] += 1
            status = record.get("status")
            if status == "found":
                stats["discovered"] += 1
                stats["cdnDiscovered" if discovery_method == "cdn" else "pageDiscovered"] += 1
                blocked_streak = 0
                if cached_image_failed:
                    stats["refreshedAfterImageFailure"] += 1
            elif status == "not-found":
                stats["notFound"] += 1
                blocked_streak = 0
            elif status == "rate-limited":
                stats["errors"] += 1
                stats["stoppedReason"] = "http-429"
                break
            elif status == "blocked":
                stats["errors"] += 1
                blocked_streak += 1
                # One 403 can be an individual page issue; two consecutive blocks
                # are treated as a site-level signal and we stop politely.
                if blocked_streak >= 2:
                    stats["stoppedReason"] = f"http-{record.get('httpStatus')}"
                    break
            else:
                stats["errors"] += 1
                blocked_streak = 0
            if delay_seconds:
                time.sleep(delay_seconds)
    finally:
        session.close()

    cache["updatedAt"] = utc_now_iso()
    return cache, stats


def cardmarket_exact_image_records(cache: dict | None) -> dict[int, dict]:
    """Return idProduct records carrying exact persisted artwork and/or explicit version metadata."""
    result: dict[int, dict] = {}
    if not isinstance(cache, dict):
        return result
    for key, record in (cache.get("products") or {}).items():
        if not isinstance(record, dict):
            continue
        product_id = get_number(record.get("productId"))
        if product_id is None:
            product_id = get_number(key)
        if product_id is None:
            continue
        product_id = int(product_id)
        source_url = nullable_text(record.get("imageUrl"))
        public_url = nullable_text(record.get("publicUrl"))
        valid_source = bool(source_url and _cardmarket_image_score(source_url, product_id) is not None)
        has_asset = bool(public_url and record.get("localPath") and record.get("exactProductMatch") is True)
        has_metadata = any(record.get(field) is not None for field in ("marketVersion", "editionCode", "language"))
        if valid_source or has_asset or has_metadata:
            result[product_id] = record
    return result


def _cardmarket_price_payload(
    product_id: int,
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
) -> dict | None:
    price = prices_by_id.get(int(product_id))
    if not price:
        return None
    valuation = _price_valuation_eur(price)
    return {
        "currency": "EUR",
        "createdAt": price_created_at,
        **price,
        "valuationEur": valuation,
    }


def _direct_cardmarket_printing(
    product: dict,
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    base: str,
    *,
    collectible_type: str,
    reference_image: dict | None = None,
    assign_reference_to_printing: bool = False,
    exact_image_record: dict | None = None,
    product_metadata: dict | None = None,
    image_cache: dict | None = None,
) -> dict:
    product_id = int(product["idProduct"])
    direct_id = f"CM-{product_id}"
    product_metadata = dict(product_metadata or {})
    for field in ("language", "languageLabel", "languageGroup", "languageSource", "editionCode", "editionName", "editionSlug"):
        if (exact_image_record or {}).get(field) is not None:
            product_metadata[field] = (exact_image_record or {}).get(field)
    market_version = nullable_text((exact_image_record or {}).get("marketVersion"))
    market_version_label = nullable_text((exact_image_record or {}).get("marketVersionLabel"))
    if market_version and not market_version_label:
        market_version_label = f"Version {market_version}"
    safe_reference_url = None
    reference_health = None
    if reference_image and reference_image.get("url"):
        safe_reference_url, reference_health = safe_image_from_cache(
            reference_image.get("url"), image_cache or {}
        )

    exact_url = nullable_text((exact_image_record or {}).get("imageUrl"))
    exact_public_url = nullable_text((exact_image_record or {}).get("publicUrl"))
    exact_health = (
        ((image_cache or {}).get("images", {}) or {}).get(exact_url)
        if exact_url
        else None
    )
    safe_exact_url = exact_public_url
    if not safe_exact_url and exact_url and isinstance(exact_health, dict) and exact_health.get("ok") is True:
        candidate_final = nullable_text(exact_health.get("finalUrl"))
        if candidate_final and (urlparse(candidate_final).hostname or "").casefold() != CARDMARKET_PRODUCT_IMAGE_HOST:
            safe_exact_url = candidate_final

    # V3.13.0 strict rule: a physical Cardmarket printing only displays its own
    # persisted idProduct image. Community/legacy references remain entity-only.
    printing_image_url = safe_exact_url
    printing_image = None
    image_health = None
    image_source_url = None
    if safe_exact_url:
        image_health = exact_health
        image_source_url = exact_url
        printing_image = {
            "url": safe_exact_url,
            "sourceUrl": (exact_image_record or {}).get("productPageFinal")
                or (exact_image_record or {}).get("productPage"),
            "sourceImageUrl": exact_url,
            "localPath": (exact_image_record or {}).get("localPath"),
            "source": "cardmarket-cache",
            # Cardmarket is authoritative for the image shown on its own exact
            # idProduct page. This flag is image-scope authority only; it does
            # not alter Bandai game identity or the mapping contract.
            "authoritative": True,
            "scope": "printing",
            "printingId": direct_id,
            "productId": product_id,
            "matchMethod": (exact_image_record or {}).get("matchMethod")
                or "idProduct-in-image-url",
            "exactProductMatch": True,
            "validated": True,
        }
    # No reference artwork is ever attached as the image of a physical printing.

    return {
        "id": direct_id,
        "printingId": direct_id,
        "sourcePrintingId": direct_id,
        "bandaiSourcePrintingIds": [],
        "baseCode": base,
        "variantType": "cardmarket-product",
        "isParallel": None,
        "isReprint": None,
        "physicalVariantUnknown": True,
        "rarity": None,
        "language": product_metadata.get("language"),
        "languageLabel": product_metadata.get("languageLabel"),
        "languageGroup": product_metadata.get("languageGroup"),
        "languageSource": product_metadata.get("languageSource"),
        "editionCode": product_metadata.get("editionCode"),
        "editionName": product_metadata.get("editionName"),
        "editionSlug": product_metadata.get("editionSlug"),
        "marketVersion": market_version,
        "marketVersionLabel": market_version_label,
        # Exact printing artwork is served only from the persistent idProduct asset.
        # Community/legacy artwork remains entity-level reference material.
        "imageUrl": printing_image_url,
        "imageSourceUrl": image_source_url if printing_image else None,
        "imageHealth": image_health if printing_image else None,
        "image": printing_image,
        "referenceImage": None,
        "releases": [_cardmarket_release_payload(product)],
        "mechanics": {
            "life": None,
            "cost": None,
            "power": None,
            "counter": None,
            "colors": [],
            "attributes": [],
            "block": None,
            "types": [],
            "effect": None,
            "trigger": None,
        },
        "mechanicsDifferFromBase": [],
        "cardmarket": {
            "mappingKey": None,
            "mappingRelation": "direct-cardmarket-product",
            "productId": product_id,
            "url": product.get("website"),
            "mappingConfirmed": True,
            "mappingSource": "cardmarket-direct-supplement",
            "product": product,
            "productExpansionMetadata": product_metadata,
            "price": _cardmarket_price_payload(product_id, prices_by_id, price_created_at),
        },
        "source": "cardmarket",
        "catalogOrigin": "cardmarket-only",
        "collectibleType": collectible_type,
    }



def add_cardmarket_bandai_variants(
    catalog: dict,
    candidates: dict[int, str],
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    *,
    expansion_metadata: dict[int, dict] | None = None,
    cardmarket_exact_images: dict[int, dict] | None = None,
    linked_reference_images: dict[int, dict] | None = None,
    image_cache: dict | None = None,
) -> dict:
    """Attach extra Cardmarket products under existing Bandai entity identity."""
    expansion_metadata = expansion_metadata or {}
    cardmarket_exact_images = cardmarket_exact_images or {}
    linked_reference_images = linked_reference_images or {}
    image_cache = image_cache or {}
    existing_product_ids = {
        int(cm["productId"])
        for card in catalog.values()
        for printing in card.get("printings", [])
        for cm in [printing.get("cardmarket")]
        if isinstance(cm, dict) and get_number(cm.get("productId")) is not None
    }
    stats = {
        "products": 0,
        "productsWithPriceGuide": 0,
        "productsWithValuation": 0,
        "productsWithExactImage": 0,
        "productsWithReferenceImage": 0,
        "languageCounts": Counter(),
        "examples": [],
    }

    for product_id, catalog_id in sorted(candidates.items()):
        product_id = int(product_id)
        if product_id in existing_product_ids:
            continue
        card = catalog.get(catalog_id)
        product = products_by_id.get(product_id)
        if not isinstance(card, dict) or not isinstance(product, dict):
            continue
        metadata = _product_expansion_metadata(product, expansion_metadata)
        printing = _direct_cardmarket_printing(
            product,
            prices_by_id,
            price_created_at,
            catalog_id,
            collectible_type="standard-card",
            reference_image=linked_reference_images.get(product_id),
            assign_reference_to_printing=False,
            exact_image_record=cardmarket_exact_images.get(product_id),
            product_metadata=metadata,
            image_cache=image_cache,
        )
        printing["variantType"] = "cardmarket-linked-variant"
        printing["physicalVariantUnknown"] = not bool(
            printing.get("editionCode") or printing.get("editionName") or printing.get("imageUrl")
        )
        printing["rarity"] = card.get("rarity")
        printing["mechanics"] = {
            key: card.get(key)
            for key in (
                "life", "cost", "power", "counter", "colors", "attributes",
                "block", "types", "effect", "trigger",
            )
        }
        printing["mechanicsDifferFromBase"] = []
        printing["catalogOrigin"] = "bandai-linked-cardmarket-variant"
        printing["identityLink"] = {
            "method": "shared-idMetacard-with-validated-bandai-mapping+code+name",
            "catalogId": catalog_id,
            "idMetacard": int(product["idMetacard"]) if get_number(product.get("idMetacard")) is not None else None,
            "printedCode": _product_card_code(product),
        }
        cm = printing.get("cardmarket") or {}
        cm["mappingRelation"] = "shared-metacard-bandai-variant"
        cm["mappingSource"] = "cardmarket-idMetacard-linked-to-bandai"
        cm["mappingConfirmed"] = True
        printing["cardmarket"] = cm
        card.setdefault("printings", []).append(printing)
        card.setdefault("sources", [])
        if "cardmarket" not in card["sources"]:
            card["sources"].append("cardmarket")
        for release in printing.get("releases", []):
            release_id = release.get("releaseId")
            if release_id and release_id not in card.setdefault("releaseIds", []):
                card["releaseIds"].append(release_id)
        card["releaseIds"] = sorted(card.get("releaseIds", []))
        existing_product_ids.add(product_id)

        stats["products"] += 1
        price = cm.get("price")
        if price is not None:
            stats["productsWithPriceGuide"] += 1
        if isinstance((price or {}).get("valuationEur"), (int, float)):
            stats["productsWithValuation"] += 1
        if isinstance(printing.get("image"), dict) and printing["image"].get("exactProductMatch") is True:
            stats["productsWithExactImage"] += 1
        if isinstance(printing.get("referenceImage"), dict) and printing["referenceImage"].get("url"):
            stats["productsWithReferenceImage"] += 1
        language_key = printing.get("language") or printing.get("languageGroup") or "unknown"
        stats["languageCounts"][language_key] += 1
        if len(stats["examples"]) < 50 and language_key != "unknown":
            stats["examples"].append({
                "catalogId": catalog_id,
                "printingId": printing.get("printingId"),
                "productId": product_id,
                "idExpansion": product.get("idExpansion"),
                "language": printing.get("language"),
                "languageLabel": printing.get("languageLabel"),
                "editionCode": printing.get("editionCode"),
            })

    for card in catalog.values():
        if isinstance(card, dict):
            card["printings"] = sorted(card.get("printings", []), key=natural_printing_sort_key)

    stats["languageCounts"] = dict(sorted(stats["languageCounts"].items()))
    stats["japaneseProducts"] = int(stats["languageCounts"].get("ja", 0))
    stats["englishProducts"] = int(stats["languageCounts"].get("en", 0))
    stats["nonEnglishUnspecifiedProducts"] = int(stats["languageCounts"].get("non-en", 0))
    stats["unknownLanguageProducts"] = int(stats["languageCounts"].get("unknown", 0))
    return stats


def add_cardmarket_supplements(
    catalog: dict,
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    *,
    community_images: list[dict] | None = None,
    cardmarket_exact_images: dict[int, dict] | None = None,
    expansion_metadata: dict[int, dict] | None = None,
    image_cache: dict | None = None,
) -> dict:
    """Add Cardmarket-only standard cards and DON!! designs conservatively.

    V3.11.4 identity contract (unchanged from V3.9):
      * Bandai cards keep the official printed code as catalogId.
      * Standard Cardmarket-only entities use idMetacard as identity:
        CMCARD-<idMetacard>. Printed codes are search aliases, not identity.
      * DON!! uses DON-CM-<idMetacard>.
      * Every idProduct remains a distinct direct Cardmarket product/printing.

    Retained from V3.10.2: Cardmarket's own public product page may provide an exact
    printing image when the discovered product-images URL itself contains the same
    idProduct and the image passes health validation. Community data remains only
    a conservative reference fallback: it can never create/merge an entity or set
    a price, and DON!! community images never claim exact idProduct scope.
    """
    community_images = community_images or []
    cardmarket_exact_images = cardmarket_exact_images or {}
    expansion_metadata = expansion_metadata or {}
    image_cache = image_cache or {}
    bandai_codes = {
        canonical_id(card.get("code"))
        for card in catalog.values()
        if isinstance(card, dict) and "bandai" in (card.get("sources") or [])
        and card.get("code")
    }
    existing_product_ids = {
        int(cm["productId"])
        for card in catalog.values()
        for printing in card.get("printings", [])
        for cm in [printing.get("cardmarket")]
        if isinstance(cm, dict) and get_number(cm.get("productId")) is not None
    }

    standard_groups = defaultdict(list)
    don_groups = defaultdict(list)
    standard_without_metacard = []

    for product in products_by_id.values():
        product_id = int(product["idProduct"])
        if product_id in existing_product_ids:
            continue
        if _is_cardmarket_don_product(product):
            metacard = get_number(product.get("idMetacard"))
            if metacard is not None:
                don_groups[int(metacard)].append(product)
            continue

        code = _product_card_code(product)
        if not code or code in bandai_codes:
            continue
        metacard = get_number(product.get("idMetacard"))
        if metacard is None:
            identity = f"PRODUCT-{product_id}"
            standard_without_metacard.append(product_id)
        else:
            identity = str(int(metacard))
        standard_groups[identity].append(product)

    # A printed promo code can occur under more than one Cardmarket metacard.
    # Code+name alone cannot prove which physical variant an external image
    # belongs to, so V3.10.2 suppresses community images for those identities.
    standard_code_identities = defaultdict(set)
    for identity, grouped_rows in standard_groups.items():
        for grouped_row in grouped_rows:
            grouped_code = _product_card_code(grouped_row)
            if grouped_code:
                standard_code_identities[grouped_code].add(str(identity))
    ambiguous_standard_codes = {
        code for code, identities in standard_code_identities.items() if len(identities) > 1
    }

    image_diag = []
    standard_reference_images = 0
    don_reference_images = 0
    exact_printing_images = 0
    cardmarket_exact_standard_products = 0
    cardmarket_exact_don_products = 0

    standard_products = 0
    standard_price_guides = 0
    standard_valuations = 0
    standard_codes = set()
    for identity in sorted(standard_groups, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x)):
        rows = sorted(standard_groups[identity], key=lambda x: int(x["idProduct"]))
        metacard = get_number(rows[0].get("idMetacard"))
        catalog_id = (
            f"CMCARD-{int(metacard)}"
            if metacard is not None
            else f"CMCARD-{identity}"
        )
        printed_codes = sorted({
            code for row in rows for code in [_product_card_code(row)] if code
        })
        standard_codes.update(printed_codes)
        code = printed_codes[0] if printed_codes else None

        names = [nullable_text(row.get("name")) for row in rows if nullable_text(row.get("name"))]
        display_name = names[0] if names else (code or catalog_id)
        if code:
            display_name = re.sub(
                r"\s*\(" + re.escape(code) + r"\)\s*(?:\(V\.\s*\d+\))?\s*$",
                "",
                display_name,
                flags=re.I,
            ).strip() or code

        if any(item_code in ambiguous_standard_codes for item_code in printed_codes):
            reference_image = None
            diagnostic = {
                "wantedName": _normalize_design_name(display_name, kind="promo"),
                "candidateCount": 0,
                "topScore": 0.0,
                "secondScore": 0.0,
                "requiredScore": 0.80,
                "requiredMargin": 0.04,
                "status": "ambiguous-cardmarket-code-across-metacards",
                "ambiguousPrintedCodes": sorted(
                    item_code for item_code in printed_codes if item_code in ambiguous_standard_codes
                ),
            }
        else:
            reference_image, diagnostic = match_community_reference_image(
                kind="promo",
                display_name=display_name,
                printed_codes=printed_codes,
                community_images=community_images,
            )
        safe_reference_url = None
        reference_health = None
        if reference_image:
            safe_reference_url, reference_health = safe_image_from_cache(
                reference_image.get("url"), image_cache
            )
            if safe_reference_url:
                reference_image = {
                    **reference_image,
                    "url": safe_reference_url,
                    "validated": (
                        bool(reference_health.get("ok"))
                        if isinstance(reference_health, dict)
                        else None
                    ),
                }
                standard_reference_images += 1
            else:
                diagnostic["status"] = "matched-image-invalid"
                reference_image = None
        image_diag.append({"catalogId": catalog_id, "kind": "standard-card", **diagnostic})

        assign_exact = False
        printings = []
        for row in rows:
            product_id = int(row["idProduct"])
            exact_record = cardmarket_exact_images.get(product_id)
            printing = _direct_cardmarket_printing(
                row,
                prices_by_id,
                price_created_at,
                code or catalog_id,
                collectible_type="standard-card",
                reference_image=reference_image,
                assign_reference_to_printing=assign_exact,
                exact_image_record=exact_record,
                product_metadata=_product_expansion_metadata(row, expansion_metadata),
                image_cache=image_cache,
            )
            printings.append(printing)
            if isinstance(printing.get("image"), dict) and printing["image"].get("exactProductMatch") is True:
                cardmarket_exact_standard_products += 1
        if assign_exact and any(
            isinstance(printing.get("image"), dict)
            and printing["image"].get("exactProductMatch") is not True
            and printing.get("imageUrl")
            for printing in printings
        ):
            exact_printing_images += 1
        standard_products += len(printings)
        for printing in printings:
            price = (printing.get("cardmarket") or {}).get("price")
            if price is not None:
                standard_price_guides += 1
            if isinstance((price or {}).get("valuationEur"), (int, float)):
                standard_valuations += 1

        release_ids = sorted({
            release.get("releaseId")
            for printing in printings
            for release in printing.get("releases", [])
            if release.get("releaseId")
        })
        exact_preview_printing = next(
            (p for p in printings if isinstance(p.get("image"), dict) and p["image"].get("exactProductMatch") is True),
            None,
        )
        entity_preview = (
            {**exact_preview_printing["image"], "printingId": exact_preview_printing.get("printingId")}
            if exact_preview_printing
            else reference_image
        )
        sources = ["cardmarket"] + (["optcgapi"] if reference_image else [])
        catalog[catalog_id] = {
            "catalogId": catalog_id,
            "code": code,
            "printedCodes": printed_codes,
            "game": "One Piece",
            "name": display_name,
            "rarity": None,
            "type": None,
            "life": None,
            "cost": None,
            "power": None,
            "counter": None,
            "colors": [],
            "attributes": [],
            "block": None,
            "types": [],
            "effect": None,
            "trigger": None,
            "sources": sources,
            "catalogOrigin": "cardmarket-only",
            "bandaiCanonical": False,
            "dataCompleteness": "market-only",
            "collectibleType": "standard-card",
            "identitySource": "cardmarket-idMetacard" if metacard is not None else "cardmarket-idProduct-fallback",
            "cardmarketMetacardId": int(metacard) if metacard is not None else None,
            "releaseIds": release_ids,
            "previewImageUrl": entity_preview.get("url") if entity_preview else None,
            "previewImage": entity_preview,
            "printings": printings,
        }

    don_products = 0
    don_price_guides = 0
    don_valuations = 0
    for metacard in sorted(don_groups):
        rows = sorted(don_groups[metacard], key=lambda x: int(x["idProduct"]))
        key = f"DON-CM-{metacard}"
        name = nullable_text(rows[0].get("name")) or "DON!!"

        reference_image, diagnostic = match_community_reference_image(
            kind="don",
            display_name=name,
            printed_codes=[],
            community_images=community_images,
        )
        safe_reference_url = None
        reference_health = None
        if reference_image:
            safe_reference_url, reference_health = safe_image_from_cache(
                reference_image.get("url"), image_cache
            )
            if safe_reference_url:
                reference_image = {
                    **reference_image,
                    "url": safe_reference_url,
                    "validated": (
                        bool(reference_health.get("ok"))
                        if isinstance(reference_health, dict)
                        else None
                    ),
                }
                don_reference_images += 1
            else:
                diagnostic["status"] = "matched-image-invalid"
                reference_image = None
        image_diag.append({"catalogId": key, "kind": "don", **diagnostic})

        # DON!! has no stable printed code linking OPTCGAPI artwork to a
        # Cardmarket idProduct. Keep community images at entity-reference level
        # even when the metacard currently has a single product.
        assign_exact = False
        printings = []
        for row in rows:
            product_id = int(row["idProduct"])
            exact_record = cardmarket_exact_images.get(product_id)
            printing = _direct_cardmarket_printing(
                row,
                prices_by_id,
                price_created_at,
                key,
                collectible_type="don",
                reference_image=reference_image,
                assign_reference_to_printing=assign_exact,
                exact_image_record=exact_record,
                product_metadata=_product_expansion_metadata(row, expansion_metadata),
                image_cache=image_cache,
            )
            printings.append(printing)
            if isinstance(printing.get("image"), dict) and printing["image"].get("exactProductMatch") is True:
                cardmarket_exact_don_products += 1
        don_products += len(printings)
        for printing in printings:
            price = (printing.get("cardmarket") or {}).get("price")
            if price is not None:
                don_price_guides += 1
            if isinstance((price or {}).get("valuationEur"), (int, float)):
                don_valuations += 1
        release_ids = sorted({
            release.get("releaseId")
            for printing in printings
            for release in printing.get("releases", [])
            if release.get("releaseId")
        })
        exact_preview_printing = next(
            (p for p in printings if isinstance(p.get("image"), dict) and p["image"].get("exactProductMatch") is True),
            None,
        )
        entity_preview = (
            {**exact_preview_printing["image"], "printingId": exact_preview_printing.get("printingId")}
            if exact_preview_printing
            else reference_image
        )
        sources = ["cardmarket"] + (["optcgapi"] if reference_image else [])
        catalog[key] = {
            "catalogId": key,
            "code": key,
            "printedCodes": [],
            "game": "One Piece",
            "name": name,
            "rarity": None,
            "type": "Don",
            "life": None,
            "cost": None,
            "power": None,
            "counter": None,
            "colors": [],
            "attributes": [],
            "block": None,
            "types": [],
            "effect": None,
            "trigger": None,
            "sources": sources,
            "catalogOrigin": "cardmarket-only",
            "bandaiCanonical": False,
            "dataCompleteness": "market-only",
            "collectibleType": "don",
            "identitySource": "cardmarket-idMetacard",
            "cardmarketMetacardId": metacard,
            "releaseIds": release_ids,
            "previewImageUrl": entity_preview.get("url") if entity_preview else None,
            "previewImage": entity_preview,
            "printings": printings,
        }

    matched_diagnostics = [item for item in image_diag if item.get("status") == "matched"]
    ambiguous_diagnostics = [item for item in image_diag if item.get("status") != "matched"]
    return {
        "standardCards": len(standard_groups),
        "standardProducts": standard_products,
        "standardProductsWithPriceGuide": standard_price_guides,
        "standardProductsWithValuation": standard_valuations,
        "standardCodes": sorted(standard_codes),
        "standardProductsWithoutMetacard": sorted(standard_without_metacard),
        "donCards": len(don_groups),
        "donProducts": don_products,
        "donProductsWithPriceGuide": don_price_guides,
        "donProductsWithValuation": don_valuations,
        "cardmarketExactImages": {
            "standardProductsWithExactImage": cardmarket_exact_standard_products,
            "donProductsWithExactImage": cardmarket_exact_don_products,
            "totalProductsWithExactImage": cardmarket_exact_standard_products + cardmarket_exact_don_products,
        },
        "communityImages": {
            "sourceRecords": len(community_images),
            "matchedEntities": len(matched_diagnostics),
            "standardEntitiesWithReferenceImage": standard_reference_images,
            "donEntitiesWithReferenceImage": don_reference_images,
            "exactSingleProductImages": exact_printing_images,
            "ambiguousStandardCodesSuppressed": sorted(ambiguous_standard_codes),
            "donExactPrintingImagePolicy": "community-disabled-no-stable-printed-code",
            "cardmarketProductPageExactPolicy": "enabled-idProduct-in-image-url+health-validation",
            "unmatchedOrAmbiguous": len(ambiguous_diagnostics),
            "diagnosticSample": ambiguous_diagnostics[:100],
        },
    }


# ---------------------------------------------------------------------------
# V3.13.0 release/set index, compact price history and manifest
# ---------------------------------------------------------------------------


def _pack_release_date(pack: dict | None) -> str | None:
    if not isinstance(pack, dict):
        return None
    for key in ("releaseDate", "release_date", "date", "release"):
        value = nullable_text(pack.get(key))
        if value:
            match = re.search(r"\d{4}-\d{2}-\d{2}", value)
            return match.group(0) if match else value
    return None


# ---------------------------------------------------------------------------
# OPlay <-> Cardmarket reconciliation and storage audit (V3.13.0)
# ---------------------------------------------------------------------------


def load_cardmarket_printing_metadata(data_dir: Path, mapping: dict) -> tuple[dict, dict]:
    """Migrate explicit version/language metadata out of the legacy image mapping.

    V3.12 no longer needs Cardmarket image discovery for normal operation, but the
    explicit V1/V2/V3 evidence collected in earlier versions remains valuable.
    """
    path = data_dir / CARDMARKET_PRINTING_METADATA_FILENAME
    data = load_json(path, default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schemaVersion", 1)
    products = data.setdefault("products", {})
    if not isinstance(products, dict):
        products = {}
        data["products"] = products
    migrated = 0
    seeded_versions = 0
    legacy_path = data_dir / CARDMARKET_IMAGE_MAPPING_FILENAME
    legacy = load_json(legacy_path, default={}) or {}
    for key, record in ((legacy.get("products") or {}) if isinstance(legacy, dict) else {}).items():
        if not isinstance(record, dict):
            continue
        pid = get_number(record.get("productId")) or get_number(key)
        if pid is None:
            continue
        pid_key = str(int(pid))
        target = products.get(pid_key) if isinstance(products.get(pid_key), dict) else {"productId": int(pid)}
        changed = False
        for field in (
            "marketVersion", "marketVersionLabel", "language", "languageLabel",
            "languageGroup", "languageSource", "editionCode", "editionName", "editionSlug",
        ):
            value = record.get(field)
            if value is not None and target.get(field) is None:
                target[field] = value
                changed = True
        if changed:
            migrated += 1
        products[pid_key] = target

    for entry in (mapping or {}).get("mappings", {}).values():
        if not isinstance(entry, dict):
            continue
        pid = get_number(entry.get("productId"))
        if pid is None:
            continue
        version, label = _cardmarket_market_version(None, nullable_text(entry.get("url")))
        if not version:
            continue
        pid_key = str(int(pid))
        target = products.get(pid_key) if isinstance(products.get(pid_key), dict) else {"productId": int(pid)}
        if target.get("marketVersion") is None:
            target["marketVersion"] = version
            target["marketVersionLabel"] = label
            target["versionSource"] = "persisted-cardmarket-product-url"
            seeded_versions += 1
        products[pid_key] = target

    data["updatedAt"] = utc_now_iso()
    return data, {"legacyRecordsMigrated": migrated, "versionsSeededFromMappingUrl": seeded_versions, "products": len(products)}


def _normalized_release_code(value: str | None) -> str | None:
    text = canonical_id(value).replace("_", "-").replace(" ", "")
    if not text:
        return None
    text = re.sub(r"-JP$", "", text)
    match = re.fullmatch(r"(OP|EB|ST|PRB)-?0*(\d{1,2})(?:P)?", text)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    if text in {"P", "PROMO", "PROMOS", "STP", "UP", "OPPR", "DON", "LIMITED"}:
        return None
    return text if re.fullmatch(r"[A-Z][A-Z0-9-]{1,24}", text) else None



def _oplay_release_match_code(value: str | None) -> str | None:
    code = canonical_id(value).replace("_", "-").replace(" ", "")
    if not code:
        return None
    if code in {"P", "PROMO", "PROMOS", "PROMOTION", "PROMOTIONCARD"}:
        return "PROMO"
    if code in {"OTHER", "OTHERPRODUCT", "OTHER-PRODUCT"}:
        return "OTHER_PRODUCT"
    normalized = _normalized_release_code(code)
    return normalized or code

def _oplay_variant_type(variant: str | None) -> str:
    value = str(variant or "base").lower()
    if value == "base":
        return "base"
    if re.fullmatch(r"p\d+", value):
        return "parallel"
    if re.fullmatch(r"r\d+", value):
        return "reprint"
    if "manga" in value:
        return "manga"
    return "special"


def _oplay_release_payload(record: dict, entity: dict | None = None, release_lookup: dict[str, dict] | None = None) -> dict:
    code = canonical_id(record.get("releaseCode")) or "OPLAY"
    # Reuse an existing Bandai release when OPlay is merely adding another
    # language/printing of the same official set. This prevents duplicate OP01,
    # PRB01, STxx... set cards in Cardify. OPlay creates its own release only
    # when Bandai/Cardmarket genuinely do not expose that release yet.
    if isinstance(entity, dict):
        target_norm = _oplay_release_match_code(code)
        for printing in entity.get("printings", []) or []:
            for release in printing.get("releases", []) or []:
                existing_code = canonical_id(release.get("code"))
                existing_norm = _oplay_release_match_code(existing_code)
                same = bool(existing_code and existing_code == code) or bool(
                    target_norm and existing_norm and target_norm == existing_norm
                )
                if same:
                    return dict(release)
    if release_lookup:
        target_norm = _oplay_release_match_code(code)
        for key in (f"exact:{code}", f"norm:{target_norm}" if target_norm else None):
            if key and key in release_lookup:
                return dict(release_lookup[key])
    kind = _release_kind(code)
    if kind == "other" and code.startswith("ST"):
        kind = "starter-deck"
    return {
        "releaseId": f"OPLAY-{code}",
        "source": "oplay",
        "seriesId": None,
        "code": code,
        "kind": kind,
        "displayName": code,
        "seriesLabel": code,
        "cardSetsText": code,
        "sourceUrl": record.get("pageUrl"),
    }


def _catalog_release_lookup(catalog: dict) -> dict[str, dict]:
    """Return deterministic existing release payloads keyed by exact/normalized code.

    Official Bandai releases win over Cardmarket when both happen to expose the same
    code. The lookup is frozen before OPlay additions so it cannot create cycles.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for card in catalog.values():
        if not isinstance(card, dict):
            continue
        for printing in card.get("printings", []) or []:
            for release in printing.get("releases", []) or []:
                if not isinstance(release, dict):
                    continue
                release_id = nullable_text(release.get("releaseId"))
                code = canonical_id(release.get("code"))
                if not release_id or not code:
                    continue
                identity = (release_id, code)
                if identity in seen:
                    continue
                seen.add(identity)
                payload = dict(release)
                buckets[f"exact:{code}"].append(payload)
                norm = _oplay_release_match_code(code)
                if norm:
                    buckets[f"norm:{norm}"].append(payload)

    def rank(release: dict) -> tuple[int, str]:
        source = str(release.get("source") or "")
        return ({"bandai": 0, "cardmarket": 1, "oplay": 2}.get(source, 9), str(release.get("releaseId") or ""))

    result = {}
    for key, rows in buckets.items():
        unique = {str(row.get("releaseId")): row for row in rows}
        if not unique:
            continue
        result[key] = sorted(unique.values(), key=rank)[0]
    return result


def _oplay_printing_id(record: dict) -> str:
    lang = _safe_asset_component(str(record.get("language") or record.get("oplayLanguage") or "xx"))
    release = _safe_asset_component(str(record.get("releaseCode") or "OPLAY"))
    source_id = _safe_asset_component(str(record.get("sourcePrintingId") or record.get("code") or "UNKNOWN"))
    return f"OPLAY-{source_id}-{lang}-{release}"


def _oplay_printing_payload(record: dict, entity: dict, *, public_url: str | None = None, release_lookup: dict[str, dict] | None = None) -> dict:
    printing_id = _oplay_printing_id(record)
    image_url = nullable_text(public_url) or nullable_text(record.get("imageUrl"))
    variant = str(record.get("variant") or "base").lower()
    image = {
        "url": image_url,
        "sourceUrl": record.get("pageUrl") or OPLAY_LIBRARY_URL,
        "sourceImageUrl": record.get("imageUrl"),
        "source": "oplay",
        "authoritative": False,
        "scope": "printing",
        "printingId": printing_id,
        "oplayKey": record.get("oplayKey"),
        "matchMethod": "oplay-printing-sitemap",
        "exactPrintingMatch": True,
        "validated": None,
    } if image_url else None
    mechanics = {
        "life": entity.get("life"),
        "cost": entity.get("cost"),
        "power": entity.get("power"),
        "counter": entity.get("counter"),
        "colors": list(entity.get("colors") or []),
        "attributes": list(entity.get("attributes") or []),
        "block": entity.get("block"),
        "types": list(entity.get("types") or []),
        "effect": entity.get("effect"),
        "trigger": entity.get("trigger"),
    }
    return {
        "id": printing_id,
        "printingId": printing_id,
        "sourcePrintingId": record.get("sourcePrintingId") or printing_id,
        "bandaiSourcePrintingIds": [],
        "baseCode": record.get("code"),
        "variantType": _oplay_variant_type(variant),
        "isParallel": variant.startswith("p") if variant != "base" else False,
        "isReprint": variant.startswith("r"),
        "physicalVariantUnknown": False,
        "displayInCollection": True,
        "rarity": entity.get("rarity"),
        "language": record.get("language"),
        "languageLabel": record.get("languageLabel"),
        "languageGroup": record.get("language"),
        "languageSource": "oplay-printing",
        "editionCode": record.get("releaseCode"),
        "editionName": record.get("releaseCode"),
        "editionSlug": slugify(record.get("releaseCode")),
        "marketVersion": None,
        "marketVersionLabel": None,
        "imageUrl": image_url,
        "imageSourceUrl": record.get("imageUrl"),
        "imageHealth": None,
        "image": image,
        "referenceImage": None,
        "releases": [_oplay_release_payload(record, entity, release_lookup)],
        "mechanics": mechanics,
        "mechanicsDifferFromBase": [],
        "cardmarket": None,
        "source": "oplay",
        "sources": ["oplay"],
        "catalogOrigin": "oplay-variant" if entity.get("bandaiCanonical") else "oplay-only",
        "collectibleType": "don" if str(record.get("code") or "").startswith("DON-") else (entity.get("collectibleType") or "standard-card"),
        "oplay": {
            "oplayKey": record.get("oplayKey"),
            "pageUrl": record.get("pageUrl"),
            "imageUrl": record.get("imageUrl"),
            "variant": variant,
            "releaseCode": record.get("releaseCode"),
            "language": record.get("language"),
            "oplayLanguage": record.get("oplayLanguage"),
        },
    }


def _strip_market_design_name(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"\bdon!!?\b", " ", text)
    text = re.sub(r"\b(?:prb|op|eb|st)\s*\d{1,2}\b", " ", text)
    text = re.sub(r"\b(?:version|ver|v)\s*\d+\b", " ", text)
    text = re.sub(r"\b(?:jp|japanese|english|non english)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _catalog_cardmarket_printings(catalog: dict) -> list[tuple[dict, dict]]:
    rows = []
    for card in catalog.values():
        if not isinstance(card, dict):
            continue
        for printing in card.get("printings", []) or []:
            cm = printing.get("cardmarket")
            if isinstance(cm, dict) and get_number(cm.get("productId")) is not None:
                rows.append((card, printing))
    return rows


def reconcile_oplay_with_catalog(
    catalog: dict,
    oplay_raw: dict | None,
    oplay_mapping: dict | None,
    *,
    oplay_public_urls: dict[str, str] | None = None,
) -> tuple[dict, dict, dict]:
    """Integrate OPlay physical printings while preventing duplicate collection entries."""
    rows = normalize_oplay_printings(oplay_raw)
    oplay_public_urls = oplay_public_urls or {}
    cards_meta = (oplay_raw or {}).get("cards", {}) if isinstance(oplay_raw, dict) else {}
    oplay_mapping = oplay_mapping if isinstance(oplay_mapping, dict) else {}
    oplay_mapping.setdefault("schemaVersion", 1)
    map_products = oplay_mapping.setdefault("products", {})
    if not isinstance(map_products, dict):
        map_products = {}
        oplay_mapping["products"] = map_products

    by_code = defaultdict(list)
    by_key = {}
    for row in rows:
        by_code[row.get("code")].append(row)
        by_key[row.get("oplayKey")] = row
    release_lookup = _catalog_release_lookup(catalog)

    stats = {
        "sourcePrintings": len(rows),
        "sourceCards": len(by_code),
        "bandaiPrintingsMatched": 0,
        "bandaiExactImagesPreserved": 0,
        "cardmarketProductsMapped": 0,
        "cardmarketProductsMappedManual": 0,
        "cardmarketProductsMappedAuto": 0,
        "cardmarketProductsHiddenAsAmbiguousMarketRows": 0,
        "oplayPrintingsAdded": 0,
        "oplayOnlyEntitiesAdded": 0,
        "legacyMarketEntitiesMergedIntoOPlay": 0,
        "legacyMarketEntitiesLeftForReview": 0,
        "englishOPlaySuppressedAgainstBandai": 0,
        "cardmarketUnknownLanguageNotAutoMapped": 0,
        "entitiesWithOPlay": 0,
        "languages": dict(sorted(Counter(row.get("language") or "unknown" for row in rows).items())),
    }
    consumed = set()
    consumed_owner: dict[str, str] = {}
    review = []

    # 1) English OPlay printings that are already present as exact Bandai printing IDs.
    for code, candidates in by_code.items():
        card = catalog.get(code)
        if not isinstance(card, dict):
            continue
        bandai_by_source = defaultdict(list)
        for printing in card.get("printings", []) or []:
            if printing.get("source") == "bandai" or printing.get("catalogOrigin") == "bandai":
                bandai_by_source[canonical_id(printing.get("sourcePrintingId"))].append(printing)
        for row in candidates:
            if row.get("language") != "en":
                continue
            matches = bandai_by_source.get(canonical_id(row.get("sourcePrintingId")), [])
            if len(matches) != 1:
                continue
            # If release evidence exists on both sides and contradicts, do not collapse.
            release_code = _oplay_release_match_code(row.get("releaseCode"))
            bandai_release_codes = {
                _oplay_release_match_code(rel.get("code"))
                for rel in matches[0].get("releases", []) or []
                if _oplay_release_match_code(rel.get("code"))
            }
            if bandai_release_codes and release_code and release_code not in bandai_release_codes and len([r for r in candidates if canonical_id(r.get("sourcePrintingId")) == canonical_id(row.get("sourcePrintingId"))]) > 1:
                continue
            printing = matches[0]
            if isinstance(printing.get("oplay"), dict):
                # One canonical OPlay corroboration per Bandai printing. Do not
                # overwrite an already proven exact physical match.
                continue
            printing["oplay"] = {
                "oplayKey": row.get("oplayKey"),
                "pageUrl": row.get("pageUrl"),
                "imageUrl": row.get("imageUrl"),
                "variant": row.get("variant"),
                "releaseCode": row.get("releaseCode"),
                "language": row.get("language"),
            }
            printing.setdefault("sources", [printing.get("source") or "bandai"])
            if "oplay" not in printing["sources"]:
                printing["sources"].append("oplay")
            consumed.add(row.get("oplayKey"))
            consumed_owner[row.get("oplayKey")] = str(printing.get("printingId") or printing.get("id") or "")
            stats["bandaiPrintingsMatched"] += 1

    # 2) Cardmarket products: only attach OPlay when the candidate is unique.
    for card, printing in _catalog_cardmarket_printings(catalog):
        cm = printing.get("cardmarket") or {}
        product_id = int(get_number(cm.get("productId")))
        product = cm.get("product") or {}
        manual = map_products.get(str(product_id)) if isinstance(map_products.get(str(product_id)), dict) else None
        chosen = None
        method = None
        candidate_rows = []
        if manual and manual.get("oplayKey") in by_key:
            manual_candidate = by_key[manual["oplayKey"]]
            owner = consumed_owner.get(manual_candidate.get("oplayKey"))
            current_printing_id = str(printing.get("printingId") or printing.get("id") or "")
            if owner in {None, current_printing_id}:
                chosen = manual_candidate
                method = "persisted-oplay-cardmarket-mapping"
                stats["cardmarketProductsMappedManual"] += 1
            else:
                candidate_rows = [manual_candidate]
        else:
            code = canonical_id(printing.get("baseCode") or _product_card_code(product))
            if code in by_code:
                candidate_rows = list(by_code[code])
            elif card.get("collectibleType") == "don" or str(card.get("catalogId") or "").startswith("DON-CM-"):
                wanted_name = _strip_market_design_name(product.get("name") or card.get("name"))
                if wanted_name:
                    for row in rows:
                        if not str(row.get("code") or "").startswith("DON-"):
                            continue
                        meta = cards_meta.get(row.get("code")) if isinstance(cards_meta, dict) else None
                        candidate_name = _strip_market_design_name((meta or {}).get("name") if isinstance(meta, dict) else row.get("name"))
                        if candidate_name and candidate_name == wanted_name:
                            candidate_rows.append(row)
            known_language = nullable_text(printing.get("language"))
            concrete_language = known_language if known_language and known_language not in {"unknown", "non-en"} else None
            if concrete_language:
                candidate_rows = [row for row in candidate_rows if row.get("language") == concrete_language]
            else:
                # V3.12 never converts Cardmarket's unknown/non-en bucket into a
                # concrete OPlay language merely because one artwork happens to exist.
                stats["cardmarketUnknownLanguageNotAutoMapped"] += 1
            release_code = _normalized_release_code(printing.get("editionCode"))
            if release_code:
                release_matches = [row for row in candidate_rows if _normalized_release_code(row.get("releaseCode")) == release_code]
                if release_matches:
                    candidate_rows = release_matches
            current_printing_id = str(printing.get("printingId") or printing.get("id") or "")
            available_rows = [
                row for row in candidate_rows
                if row.get("oplayKey") not in consumed
                or consumed_owner.get(row.get("oplayKey")) == current_printing_id
            ]
            # Do not use product/date/price/order to break ties. Exact uniqueness only,
            # and automatic mapping requires a concrete language.
            unique = {row.get("oplayKey"): row for row in available_rows if row.get("oplayKey")}
            if concrete_language and len(unique) == 1:
                chosen = next(iter(unique.values()))
                method = "unique-code-language-release"
                map_products[str(product_id)] = {
                    "productId": product_id,
                    "oplayKey": chosen.get("oplayKey"),
                    "method": method,
                    "verified": True,
                    "createdAt": utc_now_iso(),
                }
                stats["cardmarketProductsMappedAuto"] += 1

        if chosen:
            image_url = nullable_text(oplay_public_urls.get(chosen.get("oplayKey"))) or nullable_text(chosen.get("imageUrl"))
            printing_id = printing.get("printingId") or printing.get("id")
            existing_exact = nullable_text(printing.get("imageUrl"))
            existing_descriptor = printing.get("image") if isinstance(printing.get("image"), dict) else None
            is_bandai_printing = printing.get("source") == "bandai" or printing.get("catalogOrigin") == "bandai"
            if is_bandai_printing and existing_exact:
                # OPlay may corroborate the same physical printing, but official
                # Bandai artwork remains the exact image authority when present.
                stats["bandaiExactImagesPreserved"] += 1
            else:
                printing["imageUrl"] = image_url
                printing["imageSourceUrl"] = chosen.get("imageUrl")
                printing["imageHealth"] = None
                printing["image"] = {
                    "url": image_url,
                    "sourceUrl": chosen.get("pageUrl") or OPLAY_LIBRARY_URL,
                    "sourceImageUrl": chosen.get("imageUrl"),
                    "source": "oplay",
                    "authoritative": False,
                    "scope": "printing",
                    "printingId": printing_id,
                    "productId": product_id,
                    "oplayKey": chosen.get("oplayKey"),
                    "matchMethod": method,
                    "exactProductMatch": True,
                    "exactPrintingMatch": True,
                    "validated": None,
                } if image_url else None
            printing["referenceImage"] = None
            printing["physicalVariantUnknown"] = False
            printing["displayInCollection"] = True
            if printing.get("language") in {None, "unknown"}:
                printing["language"] = chosen.get("language")
                printing["languageLabel"] = chosen.get("languageLabel")
                printing["languageGroup"] = chosen.get("language")
                printing["languageSource"] = "oplay-exact-product-mapping"
            printing["oplay"] = {
                "oplayKey": chosen.get("oplayKey"),
                "pageUrl": chosen.get("pageUrl"),
                "imageUrl": chosen.get("imageUrl"),
                "variant": chosen.get("variant"),
                "releaseCode": chosen.get("releaseCode"),
                "language": chosen.get("language"),
                "mappingMethod": method,
            }
            printing.setdefault("sources", [printing.get("source") or "cardmarket"])
            if "oplay" not in printing["sources"]:
                printing["sources"].append("oplay")
            consumed.add(chosen.get("oplayKey"))
            consumed_owner[chosen.get("oplayKey")] = str(printing.get("printingId") or printing.get("id") or "")
            stats["cardmarketProductsMapped"] += 1
        else:
            relevant_oplay_exists = bool(candidate_rows)
            if relevant_oplay_exists and printing.get("source") == "cardmarket":
                printing["displayInCollection"] = False
                stats["cardmarketProductsHiddenAsAmbiguousMarketRows"] += 1
            if relevant_oplay_exists:
                review.append({
                    "productId": product_id,
                    "catalogId": card.get("catalogId"),
                    "name": product.get("name") or card.get("name"),
                    "language": printing.get("language"),
                    "editionCode": printing.get("editionCode"),
                    "marketVersion": printing.get("marketVersion"),
                    "reason": "multiple-or-incomplete-oplay-candidates; no guessing",
                    "candidateOPlayKeys": sorted({row.get("oplayKey") for row in candidate_rows if row.get("oplayKey")})[:100],
                })

    # 3) Add all unconsumed OPlay physical printings under the existing code entity,
    # or create an OPlay-only entity if Bandai/Cardmarket does not know the card yet.
    for code, candidates in sorted(by_code.items()):
        card = catalog.get(code)
        meta = cards_meta.get(code) if isinstance(cards_meta, dict) else None
        meta = meta if isinstance(meta, dict) else {}

        # V3.13.0 migration: once OPlay knows a standard promo/card code, the old
        # CMCARD-* entity is no longer a separate user-facing card identity. Move
        # its market printings under the canonical printed code before adding the
        # OPlay printings. This removes the obsolete "Solo Cardmarket" duplicate
        # layer while preserving idProduct, prices and legacy inventory aliases.
        if not isinstance(card, dict) and not str(code).startswith("DON-"):
            legacy = []
            for legacy_id, legacy_card in list(catalog.items()):
                if not isinstance(legacy_card, dict):
                    continue
                if legacy_card.get("catalogOrigin") != "cardmarket-only":
                    continue
                if legacy_card.get("collectibleType") == "don" or str(legacy_id).startswith("DON-CM-"):
                    continue
                if canonical_id(legacy_card.get("code")) != canonical_id(code):
                    continue
                legacy.append((legacy_id, legacy_card))

            if legacy:
                selected = legacy
                oplay_name = normalize_text(meta.get("name")) if meta.get("name") else ""
                if len(legacy) > 1 and oplay_name:
                    selected = [
                        item for item in legacy
                        if _name_similarity(normalize_text(item[1].get("name")), oplay_name) >= 0.55
                    ]
                # If several legacy entities share a code and none resembles the
                # OPlay identity, keep every legacy row for review instead of
                # merging a wrong Cardmarket source discrepancy.

                if selected:
                    primary_id, card = selected[0]
                    legacy_ids = []
                    merged_printings = list(card.get("printings", []) or [])
                    metacard_ids = set(card.get("cardmarketMetacardIds", []) or [])
                    if card.get("cardmarketMetacardId") is not None:
                        metacard_ids.add(card.get("cardmarketMetacardId"))
                    for legacy_id, legacy_card in selected:
                        legacy_ids.append(legacy_id)
                        if legacy_id != primary_id:
                            merged_printings.extend(legacy_card.get("printings", []) or [])
                        metacard_ids.update(legacy_card.get("cardmarketMetacardIds", []) or [])
                        if legacy_card.get("cardmarketMetacardId") is not None:
                            metacard_ids.add(legacy_card.get("cardmarketMetacardId"))
                    card["catalogId"] = code
                    card["code"] = code
                    card["printedCodes"] = sorted({code, *(card.get("printedCodes", []) or [])})
                    card["name"] = nullable_text(meta.get("name")) or card.get("name") or code
                    card["catalogOrigin"] = "oplay-canonical-with-cardmarket"
                    card["sources"] = sorted({*(card.get("sources", []) or []), "cardmarket", "oplay"})
                    card["legacyCatalogIds"] = sorted({*(card.get("legacyCatalogIds", []) or []), *legacy_ids})
                    card["cardmarketMetacardIds"] = sorted(int(x) for x in metacard_ids if get_number(x) is not None)
                    card["printings"] = merged_printings
                    catalog[code] = card
                    for legacy_id, _ in selected:
                        if legacy_id != code:
                            catalog.pop(legacy_id, None)
                    stats["legacyMarketEntitiesMergedIntoOPlay"] += len(selected)
                    stats["legacyMarketEntitiesLeftForReview"] += max(0, len(legacy) - len(selected))

        if not isinstance(card, dict):
            first = candidates[0]
            name = nullable_text(meta.get("name")) or nullable_text(first.get("name")) or code
            card = {
                "catalogId": code,
                "code": code,
                "printedCodes": [code],
                "game": "One Piece",
                "name": name,
                "rarity": meta.get("rarity"),
                "type": "Don" if code.startswith("DON-") else meta.get("type"),
                "life": meta.get("life"),
                "cost": meta.get("cost"),
                "power": meta.get("power"),
                "counter": meta.get("counter"),
                "colors": list(meta.get("colors") or []),
                "attributes": list(meta.get("attributes") or []),
                "block": meta.get("block"),
                "types": list(meta.get("types") or []),
                "effect": meta.get("effect"),
                "trigger": meta.get("trigger"),
                "sources": ["oplay"],
                "catalogOrigin": "oplay-only",
                "bandaiCanonical": False,
                "dataCompleteness": "oplay-page-and-printing-metadata",
                "releaseStatus": "unverified-by-bandai",
                "collectibleType": "don" if code.startswith("DON-") else "standard-card",
                "releaseIds": [],
                "previewImageUrl": None,
                "previewImage": None,
                "printings": [],
            }
            catalog[code] = card
            stats["oplayOnlyEntitiesAdded"] += 1
        card.setdefault("sources", [])
        if "oplay" not in card["sources"]:
            card["sources"].append("oplay")
        existing_ids = {p.get("printingId") or p.get("id") for p in card.get("printings", []) or []}
        for row in candidates:
            if row.get("oplayKey") in consumed:
                continue
            # Bandai is the canonical English physical catalogue. OPlay English
            # variants that cannot be deterministically paired with a Bandai ID are
            # suppressed rather than duplicated. OPlay remains canonical for the
            # six non-English languages and for entities Bandai does not know yet.
            if card.get("bandaiCanonical") is True and row.get("language") == "en":
                stats["englishOPlaySuppressedAgainstBandai"] += 1
                continue
            payload = _oplay_printing_payload(
                row, card, public_url=oplay_public_urls.get(row.get("oplayKey")), release_lookup=release_lookup
            )
            if payload["printingId"] in existing_ids:
                continue
            card.setdefault("printings", []).append(payload)
            existing_ids.add(payload["printingId"])
            consumed.add(row.get("oplayKey"))
            stats["oplayPrintingsAdded"] += 1
            for release in payload.get("releases", []):
                release_id = release.get("releaseId")
                if release_id and release_id not in card.setdefault("releaseIds", []):
                    card["releaseIds"].append(release_id)
        if not card.get("previewImageUrl"):
            visible = [p for p in card.get("printings", []) if p.get("displayInCollection") is not False and p.get("imageUrl")]
            preferred = next((p for p in visible if p.get("language") == "en"), visible[0] if visible else None)
            if preferred:
                card["previewImageUrl"] = preferred.get("imageUrl")
                card["previewImage"] = {
                    "url": preferred.get("imageUrl"),
                    "source": "oplay",
                    "authoritative": False,
                    "scope": "printing",
                    "printingId": preferred.get("printingId"),
                    "exactPrintingMatch": True,
                }
        card["releaseIds"] = sorted(set(card.get("releaseIds", [])))
        card["printings"] = sorted(card.get("printings", []), key=natural_printing_sort_key)

    stats["entitiesWithOPlay"] = sum(1 for card in catalog.values() if isinstance(card, dict) and "oplay" in (card.get("sources") or []))
    stats["visiblePrintings"] = sum(1 for card in catalog.values() if isinstance(card, dict) for p in card.get("printings", []) if p.get("displayInCollection") is not False)
    stats["hiddenMarketPrintings"] = sum(1 for card in catalog.values() if isinstance(card, dict) for p in card.get("printings", []) if p.get("displayInCollection") is False)
    stats["printingsWithOPlayImage"] = sum(1 for card in catalog.values() if isinstance(card, dict) for p in card.get("printings", []) if isinstance(p.get("image"), dict) and p["image"].get("source") == "oplay" and p.get("imageUrl"))
    oplay_mapping["updatedAt"] = utc_now_iso()
    return oplay_mapping, {"generatedAt": utc_now_iso(), "pendingCount": len(review), "pending": review}, stats


def persist_oplay_image_assets(
    session: requests.Session,
    oplay_printings: list[dict],
    image_dir: Path,
    manifest: dict,
    image_cache: dict,
    public_base_url: str,
    *,
    allow_downloads: bool,
    refresh: bool,
    max_new: int = 0,
) -> tuple[dict[str, str], dict]:
    """Optional cache mode. Remote mode never calls this function."""
    stats = {"targets": len(oplay_printings), "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0}
    public_urls = {}
    for row in oplay_printings:
        key = nullable_text(row.get("oplayKey"))
        source_url = nullable_text(row.get("imageUrl"))
        if not key or not source_url:
            continue
        language = _safe_asset_component(str(row.get("language") or row.get("oplayLanguage") or "xx"))
        release = _safe_asset_component(str(row.get("releaseCode") or "misc"))
        stem = _safe_asset_component(str(row.get("sourcePrintingId") or row.get("code") or "unknown"))
        relative = Path("oplay") / language / release / stem
        can_download = allow_downloads and (not max_new or stats["downloaded"] < max_new)
        asset, action = ensure_persistent_image_asset(
            session,
            manifest,
            asset_key=f"oplay:{key}",
            image_dir=image_dir,
            relative_stem=relative,
            public_base_url=public_base_url,
            source_url=source_url,
            source="oplay",
            referer=row.get("pageUrl") or OPLAY_LIBRARY_URL,
            refresh=refresh,
            allow_download=can_download,
            metadata={"oplayKey": key, "language": row.get("language"), "releaseCode": row.get("releaseCode")},
        )
        if action in stats:
            stats[action] += 1
        elif action == "error":
            stats["errors"] += 1
        if asset and asset.get("publicUrl"):
            public_urls[key] = asset["publicUrl"]
            image_cache.setdefault("images", {})[source_url] = _asset_health(asset)
    return public_urls, stats


def _directory_stats(path: Path) -> dict:
    files = 0
    total = 0
    largest = []
    if not path.exists():
        return {"files": 0, "bytes": 0, "mib": 0.0, "largestFiles": []}
    for item in path.rglob("*"):
        try:
            if not item.is_file():
                continue
            size = item.stat().st_size
        except OSError:
            continue
        files += 1
        total += size
        largest.append((size, item.as_posix()))
    largest.sort(reverse=True)
    return {
        "files": files,
        "bytes": total,
        "mib": round(total / (1024 * 1024), 3),
        "largestFiles": [{"path": name, "bytes": size} for size, name in largest[:20]],
    }


def build_storage_report(
    raw_dir: Path,
    data_dir: Path,
    output_dir: Path,
    image_dir: Path,
    *,
    oplay_raw: dict | None = None,
    image_storage_mode: str = "remote",
) -> dict:
    folders = {
        "raw": _directory_stats(raw_dir),
        "data": _directory_stats(data_dir),
        "output": _directory_stats(output_dir),
        "images": _directory_stats(image_dir),
    }
    working = sum(item["bytes"] for item in folders.values())
    git_stats = _directory_stats(Path(".git")) if Path(".git").exists() else {"files": 0, "bytes": 0, "mib": 0.0, "largestFiles": []}
    oplay_printings = normalize_oplay_printings(oplay_raw)
    image_urls = {row.get("imageUrl") for row in oplay_printings if row.get("imageUrl")}
    cached_oplay = _directory_stats(image_dir / "oplay")
    return {
        "generatedAt": utc_now_iso(),
        "schemaVersion": 1,
        "catalogVersion": "3.13.0",
        "imageStorageMode": image_storage_mode,
        "folders": folders,
        "workingDataBytes": working,
        "workingDataMiB": round(working / (1024 * 1024), 3),
        "gitRepository": git_stats,
        "oplay": {
            "printings": len(oplay_printings),
            "uniqueRemoteImageUrls": len(image_urls),
            "cachedImageFiles": cached_oplay.get("files", 0),
            "cachedImageBytes": cached_oplay.get("bytes", 0),
            "downloadPolicy": "remote-urls-only" if image_storage_mode == "remote" else "explicit-cache-mode",
        },
        "obsoleteAfterV312Pass": {
            "raw": ["optcgapi_images_raw.json"],
            "data": ["cardmarket_image_mapping.json", "image_health_cache.json", "image_assets_v1.json"],
            "output": ["cardmarket_image_pending.json"],
            "images": ["images/cardmarket/", "images/bandai/ (optional after Cardify 1.3.4 remote-image PASS)"],
            "reviewBeforeDelete": ["data/cardmarket_image_overrides.json (solo si existe y no contiene trabajo manual útil)"],
        },
    }



def _cardmarket_expansion_labels_from_mapping(mapping: dict | None) -> dict[int, str]:
    counts = defaultdict(Counter)
    for entry in (mapping or {}).get("mappings", {}).values():
        if not isinstance(entry, dict):
            continue
        expansion = get_number(entry.get("idExpansion"))
        slug = _cardmarket_url_expansion_slug(entry.get("url"))
        if expansion is None or not slug:
            continue
        counts[int(expansion)][slug] += 1
    labels = {}
    for expansion, counter in counts.items():
        slug, _ = counter.most_common(1)[0]
        labels[expansion] = " ".join(part.capitalize() for part in slug.split("-") if part)
    return labels


def _set_sort_key(item: dict):
    kind_order = {
        "booster": 0,
        "extra-booster": 1,
        "premium-booster": 2,
        "starter-deck": 3,
        "promotion": 4,
        "other-product": 5,
        "cardmarket-expansion": 6,
        "other": 7,
    }
    code = str(item.get("code") or "ZZZ")
    numbers = tuple(int(x) for x in re.findall(r"\d+", code))
    return (kind_order.get(item.get("kind"), 99), re.sub(r"\d+", "", code), numbers, code, item.get("id"))


def build_sets_index(
    catalog: dict,
    bandai_root: dict | None,
    mapping: dict | None = None,
) -> dict:
    """Build an app-friendly release index without changing catalogue identity.

    `base` progress means one owned entity in the release. `master` progress means
    every distinct printing exposed for that release. This gives Flutter enough
    information for Set / Master Set screens without re-grouping 5k printings.
    """
    packs = {}
    if isinstance(bandai_root, dict):
        for pack in bandai_root.get("packs", []) or []:
            if isinstance(pack, dict) and pack.get("id") is not None:
                packs[str(pack.get("id"))] = pack
    cm_labels = _cardmarket_expansion_labels_from_mapping(mapping)

    releases = {}
    for catalog_id, card in catalog.items():
        if not isinstance(card, dict):
            continue
        for printing in card.get("printings", []) or []:
            if printing.get("displayInCollection") is False:
                continue
            cm = printing.get("cardmarket") or {}
            cm_product = cm.get("product") or {}
            cm_expansion = get_number(cm_product.get("idExpansion"))
            for release in printing.get("releases", []) or []:
                release_id = nullable_text(release.get("releaseId"))
                if not release_id:
                    continue
                source = release.get("source") or ("bandai" if release_id.startswith("BANDAI-") else "cardmarket")
                series_id = nullable_text(release.get("seriesId"))
                pack = packs.get(series_id or "") if source == "bandai" else None
                parts = pack.get("title_parts") if isinstance(pack, dict) and isinstance(pack.get("title_parts"), dict) else {}
                raw_title = nullable_text((pack or {}).get("raw_title")) if isinstance(pack, dict) else None
                code = nullable_text(parts.get("label")) or nullable_text(release.get("code"))
                name = nullable_text(parts.get("title")) or nullable_text(release.get("displayName"))
                prefix = nullable_text(parts.get("prefix"))

                if source == "cardmarket":
                    release_expansion = get_number(release.get("cardmarketExpansionId"))
                    if release_expansion is not None:
                        release_expansion = int(release_expansion)
                        generic = f"Cardmarket expansion #{release_expansion}"
                        if not name or name == generic:
                            name = cm_labels.get(release_expansion) or generic

                item = releases.setdefault(release_id, {
                    "id": release_id,
                    "source": source,
                    "seriesId": series_id,
                    "code": code,
                    "name": name,
                    "prefix": prefix,
                    "displayName": raw_title or nullable_text(release.get("displayName")) or name,
                    "kind": release.get("kind") or _release_kind(raw_title or name),
                    "releaseDate": _pack_release_date(pack),
                    "sourceUrl": (
                        f"{BANDAI_CARDLIST_URLS[0]}?series={series_id}"
                        if source == "bandai" and series_id
                        else (release.get("sourceUrl") if source == "oplay" else None)
                    ),
                    "cardmarketExpansionIds": set(),
                    "cards": {},
                })
                if cm_expansion is not None:
                    item["cardmarketExpansionIds"].add(int(cm_expansion))
                rel_cm_exp = get_number(release.get("cardmarketExpansionId"))
                if rel_cm_exp is not None:
                    item["cardmarketExpansionIds"].add(int(rel_cm_exp))

                card_ref = item["cards"].setdefault(catalog_id, {
                    "catalogId": catalog_id,
                    "code": card.get("code"),
                    "name": card.get("name"),
                    "printingIds": [],
                    "basePrintingIds": [],
                    "valuedPrintingIds": [],
                    "imagePrintingIds": [],
                })
                printing_id = printing.get("printingId") or printing.get("id")
                if printing_id and printing_id not in card_ref["printingIds"]:
                    card_ref["printingIds"].append(printing_id)
                if printing.get("variantType") == "base" and printing_id not in card_ref["basePrintingIds"]:
                    card_ref["basePrintingIds"].append(printing_id)
                valuation = ((printing.get("cardmarket") or {}).get("price") or {}).get("valuationEur")
                if isinstance(valuation, (int, float)) and printing_id not in card_ref["valuedPrintingIds"]:
                    card_ref["valuedPrintingIds"].append(printing_id)
                if printing.get("imageUrl") and printing_id not in card_ref["imagePrintingIds"]:
                    card_ref["imagePrintingIds"].append(printing_id)

    result_sets = []
    for item in releases.values():
        cards = []
        for card_ref in item.pop("cards").values():
            for field in ("printingIds", "basePrintingIds", "valuedPrintingIds", "imagePrintingIds"):
                card_ref[field] = sorted(card_ref[field])
            cards.append(card_ref)
        cards.sort(key=lambda row: (str(row.get("code") or "ZZZ"), str(row.get("name") or ""), row["catalogId"]))
        printing_count = sum(len(row["printingIds"]) for row in cards)
        valued_count = sum(len(row["valuedPrintingIds"]) for row in cards)
        image_count = sum(len(row["imagePrintingIds"]) for row in cards)
        base_printing_count = sum(len(row["basePrintingIds"]) for row in cards)
        item["cardmarketExpansionIds"] = sorted(item["cardmarketExpansionIds"])
        item["entityCount"] = len(cards)
        item["printingCount"] = printing_count
        item["basePrintingCount"] = base_printing_count
        item["valuedPrintingCount"] = valued_count
        item["printingImageCount"] = image_count
        item["collectionTargets"] = {
            "base": len(cards),
            "master": printing_count,
        }
        item["cards"] = cards
        result_sets.append(item)

    result_sets.sort(key=_set_sort_key)
    return {
        "schemaVersion": 1,
        "catalogVersion": "3.13.0",
        "generatedAt": utc_now_iso(),
        "definitions": {
            "baseTarget": "one owned catalog entity that appears in the release",
            "masterTarget": "every distinct printing that appears in the release",
        },
        "officialBandaiSetCount": sum(1 for item in result_sets if item.get("source") == "bandai"),
        "oplaySetCount": sum(1 for item in result_sets if item.get("source") == "oplay"),
        "cardmarketExpansionCount": sum(1 for item in result_sets if item.get("source") == "cardmarket"),
        "sets": result_sets,
    }


def _history_snapshot_date(price_created_at: str | None) -> str:
    text = nullable_text(price_created_at)
    if text:
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if match:
            return match.group(0)
    return datetime.now(timezone.utc).date().isoformat()


def update_price_history(
    path: Path,
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    *,
    retention_days: int = 90,
) -> tuple[dict, dict]:
    """Append one compact daily valuation snapshot keyed by Cardmarket idProduct."""
    retention_days = max(7, int(retention_days))
    history = load_json(path, default={}) or {}
    if not isinstance(history, dict):
        history = {}
    history.setdefault("schemaVersion", 1)
    history["catalogVersion"] = "3.13.0"
    history["currency"] = "EUR"
    history["valuationPolicy"] = "trend ?? avg7 ?? avg30 ?? avg"
    history["retentionDays"] = retention_days
    snapshots = history.setdefault("snapshots", {})
    if not isinstance(snapshots, dict):
        snapshots = {}
        history["snapshots"] = snapshots

    snapshot_date = _history_snapshot_date(price_created_at)
    values = {}
    for product_id, price in prices_by_id.items():
        valuation = _price_valuation_eur(price)
        if isinstance(valuation, (int, float)):
            values[str(int(product_id))] = round(float(valuation), 6)
    overwritten = snapshot_date in snapshots
    snapshots[snapshot_date] = values

    try:
        newest = datetime.fromisoformat(snapshot_date).date()
        cutoff = newest - timedelta(days=retention_days - 1)
        for date_key in list(snapshots):
            try:
                date_value = datetime.fromisoformat(date_key).date()
            except ValueError:
                continue
            if date_value < cutoff:
                del snapshots[date_key]
    except ValueError:
        pass

    # Stable chronological JSON output makes Git diffs and app parsing predictable.
    history["snapshots"] = {key: snapshots[key] for key in sorted(snapshots)}
    history["updatedAt"] = utc_now_iso()
    stats = {
        "snapshotDate": snapshot_date,
        "productsWithValuation": len(values),
        "daysStored": len(history["snapshots"]),
        "retentionDays": retention_days,
        "overwroteExistingDay": overwritten,
    }
    return history, stats


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_catalog_manifest(
    catalog: dict,
    sets_index: dict,
    price_history: dict | None,
    *,
    generated_at: str,
    catalog_path: Path,
    sets_path: Path,
    price_history_path: Path | None,
) -> dict:
    """Build an integrity manifest for the bytes actually written to disk.

    V3.10 labelled a canonical JSON-object fingerprint as ``sha256``. It was a
    valid content fingerprint, but not the SHA-256 of the saved file bytes. The
    distinction matters if Cardify later verifies downloads. Schema v2 makes
    ``sha256`` byte-accurate and keeps the canonical hash as ``contentSha256``.
    """
    printings = sum(len(card.get("printings", [])) for card in catalog.values() if isinstance(card, dict))
    manifest = {
        "schemaVersion": 2,
        "catalogVersion": "3.13.0",
        "generatedAt": generated_at,
        "sha256Semantics": "raw-file-bytes",
        "backwardCompatibility": {
            "catalogFilenameUnchanged": True,
            "v39IdentityContractPreserved": True,
            "catalogRootShape": "catalogId -> sparse card object",
            "clientSparseSchema": 1,
        },
        "files": {
            "catalog": {
                "path": f"output/{CATALOG_FILENAME}",
                "sha256": file_sha256(catalog_path),
                "contentSha256": hash_payload(catalog),
                "entities": len(catalog),
                "printings": printings,
            },
            "sets": {
                "path": f"output/{SETS_FILENAME}",
                "sha256": file_sha256(sets_path),
                "contentSha256": hash_payload(sets_index),
                "sets": len(sets_index.get("sets", [])),
            },
        },
    }
    if isinstance(price_history, dict) and price_history_path is not None:
        manifest["files"]["priceHistory"] = {
            "path": f"output/{PRICE_HISTORY_FILENAME}",
            "sha256": file_sha256(price_history_path),
            "contentSha256": hash_payload(price_history),
            "days": len(price_history.get("snapshots", {})),
            "currency": price_history.get("currency"),
        }
    return manifest


# ---------------------------------------------------------------------------
# Raw source orchestration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# OPlayTCG multilingual printing catalogue (V3.13.0)
# ---------------------------------------------------------------------------


def _oplay_language_info(value: str | None) -> dict:
    code = str(value or "").strip().lower()
    info = OPLAY_LANGUAGES.get(code) or {"language": code or None, "label": code.upper() if code else None}
    return {"oplayLanguage": code or None, **info}


def _oplay_code_from_page_url(url: str | None) -> str | None:
    match = re.search(r"/cards/((?:OP|EB|ST|PRB)\d{2}-\d{3}|P-\d{3}|DON-\d{3})(?:/|$|[?#])", str(url or ""), re.I)
    return canonical_id(match.group(1)) if match else None


def _oplay_source_printing_id(code: str, variant: str | None) -> str:
    variant = str(variant or "base").strip().lower()
    if variant in {"", "base"}:
        return canonical_id(code)
    if re.fullmatch(r"[pr]\d+", variant):
        return f"{canonical_id(code)}_{variant.upper()}"
    return f"{canonical_id(code)}_{_safe_asset_component(variant).upper()}"


def _oplay_image_quality(url: str | None) -> tuple[int, int]:
    text = str(url or "").lower()
    score = 0
    if "/original/" in text or "/full/" in text or "/large/" in text:
        score += 3
    elif "/medium/" in text:
        score += 2
    elif "/small/" in text:
        score += 1
    ext_score = 1 if text.endswith((".webp", ".png", ".jpg", ".jpeg")) else 0
    return score, ext_score


def _oplay_guess_name(title: str | None, code: str | None) -> str | None:
    text = html_lib.unescape(str(title or "")).strip()
    if not text:
        return None
    text = re.sub(r"\s*[·|-]\s*One Piece Card Game.*$", "", text, flags=re.I).strip()
    if code:
        text = re.sub(r"\s*\(" + re.escape(code) + r"\)\s*$", "", text, flags=re.I).strip()
        text = re.sub(r"\s+" + re.escape(code) + r"\s*$", "", text, flags=re.I).strip()
    if text.casefold() in {"base", "p1", "p2", "p3", "p4", "p5", "r1", "r2"}:
        return None
    return text or None


def _oplay_normalize_image_record(
    page_url: str | None,
    image_url: str | None,
    *,
    title: str | None = None,
    caption: str | None = None,
) -> dict | None:
    image_url = html_lib.unescape(str(image_url or "")).strip()
    if not image_url:
        return None
    parsed = urlparse(image_url)
    if (parsed.hostname or "").casefold() != OPLAY_IMAGE_HOST:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    lang_index = next((idx for idx, part in enumerate(parts) if part.lower() in OPLAY_LANGUAGES), None)
    if lang_index is None or lang_index == 0 or len(parts) < lang_index + 2:
        return None
    release_code = canonical_id(parts[lang_index - 1])
    oplay_language = parts[lang_index].lower()
    filename = parts[-1]
    stem = Path(filename).stem
    code = _oplay_code_from_page_url(page_url)
    if not code:
        code_match = re.match(r"((?:OP|EB|ST|PRB)\d{2}-\d{3}|P-\d{3}|DON-\d{3})(?:_|$)", stem, re.I)
        code = canonical_id(code_match.group(1)) if code_match else None
    if not code:
        return None
    suffix = stem[len(code):] if stem.upper().startswith(code.upper()) else ""
    suffix = suffix.lstrip("_- ")
    variant = suffix.casefold() if suffix else "base"
    # Ignore localization suffixes if the CDN ever appends them to the filename.
    if variant in {"en", "jp", "fr", "th", "tc", "cn", "kr"}:
        variant = "base"
    lang = _oplay_language_info(oplay_language)
    oplay_key = "|".join([code, str(lang.get("language") or oplay_language), release_code, variant])
    return {
        "oplayKey": oplay_key,
        "code": code,
        "sourcePrintingId": _oplay_source_printing_id(code, variant),
        "variant": variant,
        "releaseCode": release_code,
        "language": lang.get("language"),
        "languageLabel": lang.get("label"),
        "oplayLanguage": oplay_language,
        "imageUrl": image_url,
        "pageUrl": str(page_url or "") or None,
        "title": nullable_text(title),
        "caption": nullable_text(caption),
        "name": _oplay_guess_name(title, code) or _oplay_guess_name(caption, code),
        "source": "oplaytcg",
    }


def _parse_oplay_sitemap_xml(xml_text: str, source_url: str) -> tuple[list[str], list[dict], list[str]]:
    child_sitemaps: list[str] = []
    printings: list[dict] = []
    page_urls: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise RuntimeError(f"Sitemap OPlay inválido {source_url}: {error}") from error

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    if local(root.tag) == "sitemapindex":
        for node in root.iter():
            if local(node.tag) != "loc" or not (node.text or "").strip():
                continue
            value = (node.text or "").strip()
            if "sitemap" in value.casefold() and (urlparse(value).hostname or "").casefold() == "oplaytcg.com":
                child_sitemaps.append(value)
        return child_sitemaps, printings, page_urls

    for url_node in [node for node in root.iter() if local(node.tag) == "url"]:
        direct_loc = None
        for child in list(url_node):
            if local(child.tag) == "loc" and (child.text or "").strip():
                direct_loc = (child.text or "").strip()
                break
        if direct_loc:
            page_urls.append(direct_loc)
        for image_node in [node for node in url_node.iter() if local(node.tag) == "image"]:
            image_loc = title = caption = None
            for child in list(image_node):
                name = local(child.tag)
                value = (child.text or "").strip()
                if name == "loc":
                    image_loc = value
                elif name == "title":
                    title = value
                elif name == "caption":
                    caption = value
            record = _oplay_normalize_image_record(direct_loc, image_loc, title=title, caption=caption)
            if record:
                printings.append(record)
    return child_sitemaps, printings, page_urls


def _oplay_card_metadata_from_html(html: str, page_url: str, code: str) -> dict:
    """Extract conservative OPlay card metadata for entities Bandai does not know yet.

    Printing identity comes from the image sitemap. Page metadata is enrichment only;
    missing/changed markup leaves fields null rather than inventing mechanics.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    heading = soup.find("h1")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    name = heading.get_text(" ", strip=True) if heading else _oplay_guess_name(title, code)
    if name and canonical_id(name) == canonical_id(code):
        name = _oplay_guess_name(title, code)

    image_urls = []
    for tag in soup.find_all(["img", "source"]):
        for attr in ("src", "srcset"):
            raw = tag.get(attr)
            if not raw:
                continue
            for candidate in str(raw).split(","):
                url = candidate.strip().split(" ", 1)[0]
                if OPLAY_IMAGE_HOST in url:
                    image_urls.append(url)

    lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()]

    def scalar_after(label: str) -> str | None:
        wanted = label.casefold()
        for idx, line in enumerate(lines[:-1]):
            if line.casefold() == wanted:
                value = lines[idx + 1].strip()
                if value and value.casefold() not in {"set", "effect", "trigger", "printings", "archetype"}:
                    return value
        return None

    def int_after(label: str) -> int | None:
        value = scalar_after(label)
        if not value:
            return None
        match = re.search(r"-?\d+", value.replace(",", ""))
        return int(match.group(0)) if match else None

    type_value = rarity = attribute = None
    known_types = {"leader", "character", "event", "stage", "don!!", "don"}
    known_rarities = {
        "leader", "common", "uncommon", "rare", "super rare", "secret rare",
        "promo", "special", "treasure rare", "don!!", "don",
    }
    for line in lines:
        if "·" not in line:
            continue
        parts = [part.strip() for part in line.split("·") if part.strip()]
        if not parts or parts[0].casefold() not in known_types:
            continue
        type_value = parts[0].title() if parts[0].casefold() not in {"don", "don!!"} else "Don"
        if len(parts) > 1 and parts[1].casefold() in known_rarities:
            rarity = parts[1]
            if len(parts) > 2:
                attribute = parts[2]
        elif len(parts) > 1:
            attribute = parts[1]
        break

    def section_text(label: str) -> str | None:
        for header in soup.find_all(["h2", "h3", "h4"]):
            if header.get_text(" ", strip=True).casefold() != label.casefold():
                continue
            chunks = []
            for sibling in header.next_siblings:
                sibling_name = getattr(sibling, "name", None)
                if sibling_name in {"h2", "h3", "h4"}:
                    break
                if hasattr(sibling, "get_text"):
                    text = sibling.get_text(" ", strip=True)
                else:
                    text = str(sibling).strip()
                if text:
                    chunks.append(text)
            value = " ".join(chunks).strip()
            return value or None
        return None

    set_code = None
    set_value = scalar_after("Set")
    if set_value:
        match = re.search(r"\b((?:OP|EB|ST|PRB)\d{2}|P|DON)\b", set_value, re.I)
        if match:
            set_code = canonical_id(match.group(1))
    if not set_code:
        page_text = "\n".join(lines)
        set_match = re.search(r"(?:^|\n)Set\s*\n?\s*((?:OP|EB|ST|PRB)\d{2}|P|DON)\b", page_text, re.I)
        if set_match:
            set_code = canonical_id(set_match.group(1))

    archetype = section_text("Archetype")
    types = []
    if archetype:
        types = [part.strip() for part in re.split(r"→|/|\|", archetype) if part.strip()]

    counter = int_after("Counter")
    return {
        "code": canonical_id(code),
        "name": nullable_text(name),
        "pageUrl": page_url,
        "title": nullable_text(title),
        "setCode": set_code,
        "type": nullable_text(type_value),
        "rarity": nullable_text(rarity),
        "life": int_after("Life"),
        "cost": int_after("Cost"),
        "power": int_after("Power"),
        "counter": counter,
        "block": int_after("Block"),
        "attributes": [attribute] if attribute else [],
        "types": types,
        "effect": section_text("Effect"),
        "trigger": section_text("Trigger"),
        "imageUrls": sorted(set(image_urls)),
        "fetchedAt": utc_now_iso(),
        "source": "oplaytcg",
    }


def fetch_oplay_raw(
    session: requests.Session,
    *,
    previous_raw: dict | None = None,
    bandai_codes: set[str] | None = None,
    refresh_metadata: bool = False,
    metadata_max_new: int = 0,
    deep_crawl: bool = False,
) -> dict:
    """Fetch the public OPlay printing catalogue without downloading card images.

    Primary path: the site's XML sitemap/image sitemap. OPlay announced an image sitemap
    in its public changelog; using it avoids tens of thousands of card-page requests.
    Only OPlay-only card codes need one metadata page request, and those results are cached
    in raw/oplaytcg_catalog_raw.json on later runs.
    """
    previous_raw = previous_raw if isinstance(previous_raw, dict) else {}
    bandai_codes = {canonical_id(code) for code in (bandai_codes or set()) if code}
    previous_cards = previous_raw.get("cards") if isinstance(previous_raw.get("cards"), dict) else {}
    sitemap_queue = [OPLAY_SITEMAP_URL]
    # Fallback names used by common sitemap generators; queried only if the root does not expose images.
    sitemap_fallbacks = [
        f"{OPLAY_BASE_URL}/sitemap-images.xml",
        f"{OPLAY_BASE_URL}/image-sitemap.xml",
        f"{OPLAY_BASE_URL}/sitemap_images.xml",
    ]
    seen_sitemaps = set()
    sitemap_rows = []
    printings_by_key: dict[str, dict] = {}
    page_urls = set()
    errors = []

    def fetch_sitemap(url: str) -> bool:
        if url in seen_sitemaps or len(seen_sitemaps) >= 96:
            return False
        seen_sitemaps.add(url)
        try:
            response = session.get(url, headers={"Accept": "application/xml,text/xml,*/*;q=0.8"}, timeout=45)
            response.raise_for_status()
            text = response.text
            children, rows, pages = _parse_oplay_sitemap_xml(text, url)
            sitemap_rows.append({"url": url, "httpStatus": response.status_code, "bytes": len(response.content), "children": len(children), "printings": len(rows)})
            sitemap_queue.extend(child for child in children if child not in seen_sitemaps)
            page_urls.update(pages)
            for row in rows:
                key = row["oplayKey"]
                previous = printings_by_key.get(key)
                if previous is None or _oplay_image_quality(row.get("imageUrl")) > _oplay_image_quality(previous.get("imageUrl")):
                    printings_by_key[key] = row
            return bool(rows or children)
        except Exception as error:
            errors.append({"url": url, "error": str(error)[:1000]})
            return False

    while sitemap_queue:
        fetch_sitemap(sitemap_queue.pop(0))

    if not printings_by_key:
        # Discover non-standard sitemap filenames advertised by robots.txt.
        try:
            robots = session.get(f"{OPLAY_BASE_URL}/robots.txt", timeout=30)
            if robots.ok:
                for match in re.finditer(r"(?im)^\s*Sitemap:\s*(https?://\S+)\s*$", robots.text):
                    advertised = match.group(1).strip()
                    if (urlparse(advertised).hostname or "").casefold() == "oplaytcg.com":
                        sitemap_queue.append(advertised)
                while sitemap_queue:
                    fetch_sitemap(sitemap_queue.pop(0))
        except Exception as error:
            errors.append({"url": f"{OPLAY_BASE_URL}/robots.txt", "error": str(error)[:1000]})

    if not printings_by_key:
        for fallback in sitemap_fallbacks:
            fetch_sitemap(fallback)

    # Optional expensive fallback. It exists for resilience but is intentionally opt-in.
    if not printings_by_key and deep_crawl:
        codes = set()
        for lang in OPLAY_LANGUAGES:
            index_url = f"{OPLAY_BASE_URL}/en/library/all/{lang}"
            try:
                response = session.get(index_url, timeout=45)
                response.raise_for_status()
                for match in re.finditer(r"/cards/((?:OP|EB|ST|PRB)\d{2}-\d{3}|P-\d{3}|DON-\d{3})", response.text, re.I):
                    codes.add(canonical_id(match.group(1)))
            except Exception as error:
                errors.append({"url": index_url, "error": str(error)[:1000]})
        for code_index, code in enumerate(sorted(codes), start=1):
            for lang in OPLAY_LANGUAGES:
                page_url = f"{OPLAY_BASE_URL}/en/cards/{code}/{lang}"
                try:
                    response = session.get(page_url, timeout=45)
                    if response.status_code != 200:
                        continue
                    soup = BeautifulSoup(response.text, "html.parser")
                    for tag in soup.find_all(["img", "source"]):
                        raw = tag.get("src") or tag.get("srcset")
                        if not raw:
                            continue
                        for candidate in str(raw).split(","):
                            image_url = candidate.strip().split(" ", 1)[0]
                            record = _oplay_normalize_image_record(page_url, image_url, title=tag.get("alt"))
                            if record:
                                key = record["oplayKey"]
                                previous = printings_by_key.get(key)
                                if previous is None or _oplay_image_quality(record.get("imageUrl")) > _oplay_image_quality(previous.get("imageUrl")):
                                    printings_by_key[key] = record
                except Exception as error:
                    errors.append({"url": page_url, "error": str(error)[:1000]})
            if code_index % 100 == 0:
                print(f"OPlay deep crawl: {code_index}/{len(codes)} códigos")

    printings = sorted(printings_by_key.values(), key=lambda row: (row.get("code") or "", row.get("language") or "", row.get("releaseCode") or "", row.get("variant") or ""))
    codes = sorted({row["code"] for row in printings if row.get("code")})
    cards = {str(key): value for key, value in previous_cards.items() if isinstance(value, dict)}
    metadata_targets = [code for code in codes if code not in bandai_codes and (refresh_metadata or code not in cards)]
    if metadata_max_new:
        metadata_targets = metadata_targets[:metadata_max_new]
    for index, code in enumerate(metadata_targets, start=1):
        page_url = f"{OPLAY_BASE_URL}/en/cards/{code}"
        try:
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            cards[code] = _oplay_card_metadata_from_html(response.text, page_url, code)
        except Exception as error:
            errors.append({"url": page_url, "error": str(error)[:1000]})
        if index % 50 == 0 or index == len(metadata_targets):
            print(f"OPlay metadata: {index}/{len(metadata_targets)} nuevas")

    language_counts = Counter(row.get("language") or "unknown" for row in printings)
    release_counts = Counter(row.get("releaseCode") or "unknown" for row in printings)
    return {
        "schemaVersion": 1,
        "source": "OPlayTCG public sitemap/library",
        "sourceUrl": OPLAY_LIBRARY_URL,
        "fetchedAt": utc_now_iso(),
        "imagePolicy": "remote-url-only; image bytes are not downloaded into raw",
        "languages": OPLAY_LANGUAGES,
        "sitemaps": sitemap_rows,
        "cards": cards,
        "printings": printings,
        "stats": {
            "uniqueCards": len(codes),
            "printings": len(printings),
            "languages": dict(sorted(language_counts.items())),
            "releases": len(release_counts),
            "metadataCards": len(cards),
            "metadataFetchedThisRun": len(metadata_targets),
            "sitemapDocuments": len(sitemap_rows),
        },
        "errors": errors[:500],
    }


def normalize_oplay_printings(raw: dict | None) -> list[dict]:
    rows = (raw or {}).get("printings", []) if isinstance(raw, dict) else []
    result = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = nullable_text(item.get("oplayKey"))
        code = canonical_id(item.get("code"))
        image_url = nullable_text(item.get("imageUrl"))
        if not key or not code or not image_url or key in seen:
            continue
        seen.add(key)
        result.append({**item, "code": code})
    return result



def fetch_live_raw(
    session: requests.Session,
    bandai_delay: float,
    vega_bin: str = "vega",
    *,
    raw_dir: Path = DEFAULT_RAW_DIR,
    include_oplay: bool = True,
    oplay_refresh_metadata: bool = False,
    oplay_metadata_max_new: int = 0,
    oplay_deep_crawl: bool = False,
) -> dict:
    print("Descargando Bandai oficial...")
    bandai = fetch_bandai_raw(session, bandai_delay, vega_bin)

    print("Descargando catálogo público oficial de Cardmarket...")
    cm_products = fetch_json(session, CARDMARKET_PRODUCTS_URL)

    print("Descargando Price Guide público oficial de Cardmarket...")
    cm_prices = fetch_json(session, CARDMARKET_PRICE_GUIDE_URL)

    print("Descargando catálogo público no-singles de Cardmarket (metadata de expansiones)...")
    try:
        cm_nonsingles = fetch_json(session, CARDMARKET_NONSINGLES_URL)
    except Exception as error:
        print(f"AVISO: catálogo no-singles Cardmarket no disponible: {error}")
        cm_nonsingles = {"disabled": True, "fetchedAt": utc_now_iso(), "products": []}

    previous_oplay = load_json(raw_dir / OPLAY_RAW_FILENAME, default={}) or {}
    if include_oplay:
        print("Descargando catálogo multilingüe OPlay (sitemap/metadata; sin descargar imágenes)...")
        bandai_codes = {base_code(card.get("sourcePrintingId")) for card in (bandai.get("cards", []) if isinstance(bandai, dict) else []) if card.get("sourcePrintingId")}
        try:
            oplay = fetch_oplay_raw(
                session,
                previous_raw=previous_oplay,
                bandai_codes=bandai_codes,
                refresh_metadata=oplay_refresh_metadata,
                metadata_max_new=oplay_metadata_max_new,
                deep_crawl=oplay_deep_crawl,
            )
            if not oplay.get("printings") and previous_oplay.get("printings"):
                print("AVISO: OPlay no devolvió printings nuevas; se conserva el RAW OPlay anterior.")
                oplay = previous_oplay
        except Exception as error:
            if previous_oplay.get("printings"):
                print(f"AVISO: OPlay no disponible ({error}); se reutiliza el RAW OPlay anterior.")
                oplay = previous_oplay
            else:
                print(f"AVISO: OPlay no disponible y no existe cache previo: {error}")
                oplay = {
                    "schemaVersion": 1,
                    "source": "OPlayTCG public sitemap/library",
                    "fetchedAt": utc_now_iso(),
                    "disabled": True,
                    "error": str(error)[:1000],
                    "cards": {},
                    "printings": [],
                    "stats": {},
                }
    else:
        oplay = previous_oplay if previous_oplay else {
            "schemaVersion": 1,
            "source": "OPlayTCG public sitemap/library",
            "fetchedAt": utc_now_iso(),
            "disabled": True,
            "cards": {},
            "printings": [],
            "stats": {},
        }

    return {
        "bandai": bandai,
        "cardmarket_products": cm_products,
        "cardmarket_prices": cm_prices,
        "cardmarket_nonsingles": cm_nonsingles,
        "oplay": oplay,
    }


def save_raw(raw_dir: Path, raw_data: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for key, filename in RAW_FILENAMES.items():
        path = raw_dir / filename
        value = raw_data.get(key)
        if value is None and key in OPTIONAL_RAW_KEYS:
            value = {"disabled": True, "fetchedAt": utc_now_iso()}
        if value is None:
            raise RuntimeError(f"Falta RAW requerido en memoria: {key}")
        save_json(path, value)
        print(f"RAW guardado: {path}")


def load_raw(raw_dir: Path) -> dict:
    result = {}
    for key, filename in RAW_FILENAMES.items():
        path = raw_dir / filename
        data = load_json(path)
        if data is None and key in OPTIONAL_RAW_KEYS:
            print(f"RAW opcional no disponible: {path}; se continúa sin esa fuente.")
            data = {"disabled": True, "fetchedAt": None, "don": None, "promos": None}
        elif data is None:
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

        git_run(["commit", "-m", "chore: update Bandai, OPlay and Cardmarket catalogue"])
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
    assert catalog["OP17-001"]["catalogId"] == "OP17-001"
    assert catalog["OP17-001"]["life"] == 5
    assert catalog["OP17-001"]["printings"][0]["cardmarket"]["price"]["trend"] == 1.1
    assert stats["cards"] == 1

    # V3.9 regression: Cardmarket-only standard cards use idMetacard identity,
    # while DON!! remains additive and also uses idMetacard identity.
    # supplements. They never overwrite Bandai cards or enter the Bandai mapping.
    extra_product = normalize_cardmarket_product({
        "idProduct": 124,
        "name": "Future Promo (P-999)",
        "idCategory": 1621,
        "categoryName": "One Piece Single",
        "idExpansion": 777,
        "idMetacard": 9001,
        "dateAdded": "2026-09-10 00:00:00",
    })
    don_product = normalize_cardmarket_product({
        "idProduct": 125,
        "name": "DON!! (Test Design)",
        "idCategory": 1621,
        "categoryName": "One Piece Single",
        "idExpansion": 778,
        "idMetacard": 9002,
        "dateAdded": "2026-09-10 00:00:00",
    })
    supplement_catalog = dict(catalog)
    supplement_stats = add_cardmarket_supplements(
        supplement_catalog,
        {123: products[0], 124: extra_product, 125: don_product},
        {
            124: normalize_price_row({"idProduct": 124, "trend": 2.5}),
            125: normalize_price_row({"idProduct": 125, "avg7": 3.5}),
        },
        "2026-09-10T02:00:00+0200",
    )
    assert supplement_stats["standardCards"] == 1
    assert supplement_stats["standardProducts"] == 1
    assert supplement_stats["donCards"] == 1
    assert supplement_stats["donProducts"] == 1
    assert supplement_catalog["CMCARD-9001"]["catalogOrigin"] == "cardmarket-only"
    assert supplement_catalog["CMCARD-9001"]["catalogId"] == "CMCARD-9001"
    assert supplement_catalog["CMCARD-9001"]["code"] == "P-999"
    assert supplement_catalog["CMCARD-9001"]["printings"][0]["cardmarket"]["price"]["valuationEur"] == 2.5
    assert supplement_catalog["DON-CM-9002"]["type"] == "Don"
    assert supplement_catalog["DON-CM-9002"]["catalogId"] == "DON-CM-9002"
    assert supplement_catalog["DON-CM-9002"]["printings"][0]["cardmarket"]["price"]["valuationEur"] == 3.5
    assert supplement_stats["standardProductsWithPriceGuide"] == 1
    assert supplement_stats["standardProductsWithValuation"] == 1

    # V3.10.2: extract exact Cardmarket product images from HTML without
    # guessing expansion paths such as OPPR / ST-10 / ST-10-JP.
    cm_image_fixture = """
    <html><head>
      <meta property="og:image"
            content="https://product-images.s3.cardmarket.com/1621/OPPR/808800/808800.jpg">
    </head><body>
      <img src="https://product-images.s3.cardmarket.com/1621/OTHER/999999/999999.jpg">
    </body></html>
    """
    extracted = extract_cardmarket_product_image_url(
        cm_image_fixture,
        "https://www.cardmarket.com/en/OnePiece/Products?idProduct=808800",
        808800,
    )
    assert extracted == "https://product-images.s3.cardmarket.com/1621/OPPR/808800/808800.jpg"
    assert extract_cardmarket_product_image_url(
        cm_image_fixture,
        "https://www.cardmarket.com/en/OnePiece/Products?idProduct=123456",
        123456,
    ) is None

    exact_standard = normalize_cardmarket_product({
        "idProduct": 130,
        "name": "Exact Promo (P-996)",
        "idCategory": 1621,
        "categoryName": "One Piece Single",
        "idExpansion": 782,
        "idMetacard": 9301,
    })
    exact_don = normalize_cardmarket_product({
        "idProduct": 131,
        "name": "DON!! (Exact Test)",
        "idCategory": 1621,
        "categoryName": "One Piece Single",
        "idExpansion": 783,
        "idMetacard": 9302,
    })
    exact_standard_url = "https://product-images.s3.cardmarket.com/1621/OPPR/130/130.png"
    exact_don_url = "https://product-images.s3.cardmarket.com/1621/OPPR/131/131.jpg"
    exact_standard_public = "https://raw.githubusercontent.com/example/cardify-data/main/images/cardmarket/130/130.png"
    exact_don_public = "https://raw.githubusercontent.com/example/cardify-data/main/images/cardmarket/131/131.jpg"
    exact_records = {
        130: {
            "productId": 130,
            "status": "found",
            "productPage": "https://www.cardmarket.com/en/OnePiece/Products?idProduct=130",
            "imageUrl": exact_standard_url,
            "publicUrl": exact_standard_public,
            "localPath": "cardmarket/130/130.png",
            "exactProductMatch": True,
            "matchMethod": "idProduct-in-image-url",
        },
        131: {
            "productId": 131,
            "status": "found",
            "productPage": "https://www.cardmarket.com/en/OnePiece/Products?idProduct=131",
            "imageUrl": exact_don_url,
            "publicUrl": exact_don_public,
            "localPath": "cardmarket/131/131.jpg",
            "exactProductMatch": True,
            "matchMethod": "idProduct-in-image-url",
        },
    }
    exact_health = {
        "images": {
            exact_standard_url: {"ok": True, "finalUrl": exact_standard_url},
            exact_don_url: {"ok": True, "finalUrl": exact_don_url},
        }
    }
    exact_catalog = dict(catalog)
    exact_stats = add_cardmarket_supplements(
        exact_catalog,
        {130: exact_standard, 131: exact_don},
        {},
        None,
        cardmarket_exact_images=exact_records,
        image_cache=exact_health,
    )
    standard_printing = exact_catalog["CMCARD-9301"]["printings"][0]
    don_printing = exact_catalog["DON-CM-9302"]["printings"][0]
    assert standard_printing["printingId"] == "CM-130"
    assert standard_printing["imageUrl"] == exact_standard_public
    assert standard_printing["image"]["exactProductMatch"] is True
    assert don_printing["printingId"] == "CM-131"
    assert don_printing["imageUrl"] == exact_don_public
    assert don_printing["image"]["exactProductMatch"] is True
    assert exact_catalog["DON-CM-9302"]["previewImage"]["printingId"] == "CM-131"
    assert exact_stats["cardmarketExactImages"]["standardProductsWithExactImage"] == 1
    assert exact_stats["cardmarketExactImages"]["donProductsWithExactImage"] == 1

    # Same printed code can refer to different Cardmarket metacards. They must
    # never collapse into one catalogue entity.
    same_code_a = normalize_cardmarket_product({
        "idProduct": 126, "name": "Alpha (P-998)", "idCategory": 1621,
        "idExpansion": 779, "idMetacard": 9101,
    })
    same_code_b = normalize_cardmarket_product({
        "idProduct": 127, "name": "Beta (P-998)", "idCategory": 1621,
        "idExpansion": 780, "idMetacard": 9102,
    })
    split_catalog = dict(catalog)
    split_stats = add_cardmarket_supplements(
        split_catalog, {126: same_code_a, 127: same_code_b}, {}, None
    )
    assert split_stats["standardCards"] == 2
    assert split_catalog["CMCARD-9101"]["code"] == "P-998"
    assert split_catalog["CMCARD-9101"]["name"] == "Alpha"
    assert split_catalog["CMCARD-9102"]["code"] == "P-998"
    assert split_catalog["CMCARD-9102"]["name"] == "Beta"

    # A Price Guide row with only `low` exists as market data but does not
    # produce valuationEur under the catalogue valuation policy.
    low_only = normalize_cardmarket_product({
        "idProduct": 129, "name": "Low Only (P-997)", "idCategory": 1621,
        "idExpansion": 781, "idMetacard": 9201,
    })
    low_catalog = dict(catalog)
    low_stats = add_cardmarket_supplements(
        low_catalog, {129: low_only},
        {129: normalize_price_row({"idProduct": 129, "low": 20})},
        "2026-09-10T02:00:00+0200",
    )
    assert low_stats["standardProductsWithPriceGuide"] == 1
    assert low_stats["standardProductsWithValuation"] == 0
    assert low_catalog["CMCARD-9201"]["printings"][0]["cardmarket"]["price"]["valuationEur"] is None

    # A Cardmarket code disagreeing with Bandai is diagnosed explicitly rather
    # than silently accepted or automatically quarantined when other evidence
    # (name/release) still supports the physical product identity.
    mismatch_product = normalize_cardmarket_product({
        "idProduct": 128, "name": "Edward.Newgate (OP17-099)",
        "idCategory": 1621, "idExpansion": 999, "idMetacard": 888,
    })
    mismatch_mapping = {"mappings": {
        "OP17-001": {"productId": 128, "confirmed": False, "source": "test-name-release"}
    }}
    mismatch_catalog, mismatch_stats = build_catalog(
        cards, mismatch_mapping, {128: mismatch_product}, {}, None, image_cache,
        series_expansion_profiles={"569117": {"value": 999}},
    )
    mismatch_evidence = mismatch_catalog["OP17-001"]["printings"][0]["cardmarket"]["mappingEvidence"]
    assert mismatch_evidence["codeMatches"] is False
    assert mismatch_evidence["nameMatches"] is True
    assert mismatch_evidence["releaseMatches"] is True
    assert mismatch_stats["cardmarketCodeDiscrepancies"][0]["cardmarketProductCode"] == "OP17-099"

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

    # V3.5 regression: Bandai release identity can repair legacy suffix drift.
    # Three anchors establish PRB99 -> expansion 900. A missing reprint whose
    # legacy R-number points to ST98 is mapped only to the unique unused product
    # in Bandai's PRB99 expansion. No product-order/V.1 inference is involved.
    release_products = []
    release_mapping = {"schemaVersion": 3, "mappings": {}}
    for pid, code in ((3001, "OP97-001"), (3002, "OP97-002"), (3003, "OP97-003")):
        product = normalize_cardmarket_product({
            "idProduct": pid, "name": f"Anchor ({code})",
            "idCategory": 1621, "idExpansion": 900, "idMetacard": pid + 10000,
        })
        release_products.append(product)
        release_mapping["mappings"][code] = {
            "productId": pid, "legacySet": "PRB99", "legacyName": "Anchor",
        }
    release_products.extend([
        normalize_cardmarket_product({
            "idProduct": 3010, "name": "Target (OP98-010)",
            "idCategory": 1621, "idExpansion": 900, "idMetacard": 13010,
        }),
        normalize_cardmarket_product({
            "idProduct": 3011, "name": "Target (OP98-010)",
            "idCategory": 1621, "idExpansion": 901, "idMetacard": 13010,
        }),
        normalize_cardmarket_product({
            "idProduct": 3020, "name": "Collision (OP98-020)",
            "idCategory": 1621, "idExpansion": 900, "idMetacard": 13020,
        }),
    ])
    release_products = [p for p in release_products if p]
    release_mapping["mappings"]["OP98-010_R1"] = {
        "productId": None, "legacySet": "ST98", "legacyName": "Target",
    }
    release_cards = [
        {
            "sourcePrintingId": "OP98-010_R1", "name": "Target",
            "seriesLabel": "PREMIUM BOOSTER [PRB-99]",
            "cardSetsText": "PREMIUM BOOSTER [PRB-99]",
            "imageUrl": "https://example.invalid/OP98-010_r1.png",
        },
        {
            "sourcePrintingId": "OP98-020_P1", "name": "Collision",
            "seriesLabel": "PREMIUM BOOSTER [PRB-99]",
            "cardSetsText": "PREMIUM BOOSTER [PRB-99]",
            "imageUrl": "https://example.invalid/OP98-020_p1.png",
        },
        {
            "sourcePrintingId": "OP98-020_R1", "name": "Collision",
            "seriesLabel": "PREMIUM BOOSTER [PRB-99]",
            "cardSetsText": "PREMIUM BOOSTER [PRB-99]",
            "imageUrl": "https://example.invalid/OP98-020_r1.png",
        },
    ]
    release_review = auto_map_and_build_review(
        release_cards, release_mapping, release_products, allow_auto_map=True
    )
    assert release_mapping["mappings"]["OP98-010_R1"]["productId"] == 3010
    assert release_mapping["mappings"]["OP98-010_R1"]["legacySet"] == "PRB99"
    assert release_mapping["mappings"]["OP98-010_R1"]["previousLegacySet"] == "ST98"
    assert "OP98-020_P1" not in release_mapping["mappings"]
    assert "OP98-020_R1" not in release_mapping["mappings"]
    assert any(x["printingId"] == "OP98-010_R1" for x in release_review["autoMappingsAdded"])

    # V3.6 regression: an exact historical suffix is NOT authoritative when
    # current Bandai places that suffix in another structured release.
    identity_products = {}
    identity_mapping = {"schemaVersion": 3, "mappings": {}}
    for product_id, code in (
        (4001, "ST97-001"),
        (4002, "ST97-002"),
        (4003, "ST97-003"),
    ):
        product = normalize_cardmarket_product({
            "idProduct": product_id,
            "name": f"Anchor ({code})",
            "idCategory": 1621,
            "idExpansion": 700,
            "idMetacard": 24000 + product_id,
        })
        identity_products[product_id] = product
        identity_mapping["mappings"][code] = {
            "productId": product_id,
            "legacySet": "ST97",
            "legacyName": "Anchor",
        }
    collision_product = normalize_cardmarket_product({
        "idProduct": 4010,
        "name": "Collision (OP96-010)",
        "idCategory": 1621,
        "idExpansion": 701,
        "idMetacard": 28010,
    })
    identity_products[4010] = collision_product
    identity_mapping["mappings"]["OP96-010_P1"] = {
        "productId": 4010,
        "legacySet": "misc-promos",
        "legacyName": "Collision",
    }
    identity_cards = [{
        "cardNo": "OP96-010",
        "sourcePrintingId": "OP96-010_P1",
        "name": "Collision",
        "seriesId": "569097",
        "seriesLabel": "STARTER DECK [ST-97]",
        "cardSetsText": "STARTER DECK [ST-97]",
        "isParallel": True,
        "isReprint": False,
    }]
    identity_result = validate_mapping_against_bandai(
        identity_cards,
        identity_mapping,
        identity_products,
    )
    assert identity_mapping["mappings"]["OP96-010_P1"]["productId"] is None
    assert identity_result["summary"]["quarantined"] == 1

    # V3.6 regression: suffix drift may migrate an orphan historical parallel
    # only when semantic class + structured release + product are one-to-one.
    alias_products = list(identity_products.values())
    alias_product = normalize_cardmarket_product({
        "idProduct": 4020,
        "name": "Alias Target (OP96-020)",
        "idCategory": 1621,
        "idExpansion": 700,
        "idMetacard": 28020,
    })
    alias_products.append(alias_product)
    alias_mapping = {
        "schemaVersion": 4,
        "mappings": {
            "ST97-001": {
                "productId": 4001, "legacySet": "ST97", "legacyName": "Anchor",
            },
            "ST97-002": {
                "productId": 4002, "legacySet": "ST97", "legacyName": "Anchor",
            },
            "ST97-003": {
                "productId": 4003, "legacySet": "ST97", "legacyName": "Anchor",
            },
            "OP96-020_P1": {
                "productId": 4020,
                "legacySet": "ST97",
                "legacyName": "Alias Target",
            },
        },
    }
    alias_cards = [{
        "cardNo": "OP96-020",
        "sourcePrintingId": "OP96-020_P2",
        "name": "Alias Target",
        "seriesId": "569097",
        "seriesLabel": "STARTER DECK [ST-97]",
        "cardSetsText": "STARTER DECK [ST-97]",
        "isParallel": True,
        "isReprint": False,
    }]
    alias_review = auto_map_and_build_review(
        alias_cards,
        alias_mapping,
        alias_products,
        allow_auto_map=True,
    )
    assert alias_mapping["mappings"]["OP96-020_P2"]["productId"] == 4020
    assert alias_mapping["mappings"]["OP96-020_P1"]["productId"] is None
    assert alias_review["semanticAliasesMoved"][0]["donorMappingKey"] == "OP96-020_P1"

    # V3.6 regression: a new release expansion may be learned from the public
    # catalogue only with a large similarity margin. Near-tied twins stay
    # unresolved (e.g. English vs Asia-region/Japanese catalogues).
    infer_cards = []
    infer_products = []
    for number in range(1, 11):
        code = f"OP95-{number:03d}"
        infer_cards.append({
            "cardNo": code,
            "sourcePrintingId": code,
            "name": f"Infer {number}",
            "seriesId": "569096",
            "seriesLabel": "STARTER DECK [ST-96]",
            "cardSetsText": "STARTER DECK [ST-96]",
        })
        infer_products.append(normalize_cardmarket_product({
            "idProduct": 5000 + number,
            "name": f"Infer {number} ({code})",
            "idCategory": 1621,
            "idExpansion": 702,
            "idMetacard": 35000 + number,
        }))
    infer_products.append(normalize_cardmarket_product({
        "idProduct": 5099,
        "name": "Infer 1 (OP95-001)",
        "idCategory": 1621,
        "idExpansion": 703,
        "idMetacard": 35001,
    }))
    inferred_profiles, _ = infer_release_expansions_from_catalog(
        infer_cards,
        [product for product in infer_products if product],
        {},
    )
    assert inferred_profiles["ST96"]["value"] == 702

    twin_cards = []
    twin_products = []
    for number in range(1, 11):
        code = f"OP94-{number:03d}"
        twin_cards.append({
            "cardNo": code,
            "sourcePrintingId": code,
            "name": f"Twin {number}",
            "seriesId": "569095",
            "seriesLabel": "STARTER DECK [ST-95]",
            "cardSetsText": "STARTER DECK [ST-95]",
        })
        for expansion_id, offset in ((704, 0), (705, 100)):
            twin_products.append(normalize_cardmarket_product({
                "idProduct": 5100 + offset + number,
                "name": f"Twin {number} ({code})",
                "idCategory": 1621,
                "idExpansion": expansion_id,
                "idMetacard": 36000 + number,
            }))
    twin_profiles, _ = infer_release_expansions_from_catalog(
        twin_cards,
        [product for product in twin_products if product],
        {},
    )
    assert "ST95" not in twin_profiles


    # V3.7 regression: Zeus OP11-106_P2 moved into the combined OP15-EB04
    # release while the correct OP15 Cardmarket product remained on historical
    # suffix P3. The current structured target must first quarantine the Promo
    # product and then inherit the unique release-specific donor product.
    zeus_products = [
        normalize_cardmarket_product({
            "idProduct": 6101, "name": "Zeus (OP11-106)",
            "idCategory": 1621, "idExpansion": 5303, "idMetacard": 46101,
        }),
        normalize_cardmarket_product({
            "idProduct": 6102, "name": "Zeus (OP11-106)",
            "idCategory": 1621, "idExpansion": 6456, "idMetacard": 46101,
        }),
    ]
    zeus_products = [p for p in zeus_products if p]
    zeus_by_id = {p["idProduct"]: p for p in zeus_products}
    zeus_mapping = {"schemaVersion": 4, "mappings": {
        "OP11-106_P2": {
            "productId": 6101, "legacySet": "misc-promos", "legacyName": "Zeus",
        },
        "OP11-106_P3": {
            "productId": 6102, "legacySet": "OP15", "legacyName": "Zeus",
        },
    }}
    zeus_cards = [{
        "cardNo": "OP11-106", "sourcePrintingId": "OP11-106_P2", "name": "Zeus",
        "seriesId": "569115", "seriesLabel": "BOOSTER PACK [OP15-EB04]",
        "cardSetsText": "BOOSTER PACK [OP15-EB04]", "isParallel": True,
        "isReprint": False,
    }]
    zeus_series_profiles = {"569115": {
        "value": 6456, "count": 193, "total": 194, "ratio": 0.9948,
        "source": "bandai-current-series-mapping-majority", "seriesId": "569115",
        "releaseCodes": ["EB04", "OP15"],
    }}
    zeus_guard = validate_mapping_against_bandai(
        zeus_cards, zeus_mapping, zeus_by_id,
        release_profiles={}, series_profiles=zeus_series_profiles,
        series_diagnostics={},
    )
    assert zeus_mapping["mappings"]["OP11-106_P2"]["productId"] is None
    assert zeus_guard["summary"]["quarantined"] == 1
    zeus_review = auto_map_and_build_review(
        zeus_cards, zeus_mapping, zeus_products, allow_auto_map=True,
        series_expansion_diagnostics={},
        series_expansion_profiles=zeus_series_profiles,
    )
    assert zeus_mapping["mappings"]["OP11-106_P2"]["productId"] == 6102
    assert zeus_mapping["mappings"]["OP11-106_P3"]["productId"] is None
    assert any(
        x["printingId"] == "OP11-106_P2" and x["donorMappingKey"] == "OP11-106_P3"
        for x in zeus_review["semanticAliasesMoved"]
    )

    # V3.7 regression: Kid & Killer kept the OP14 Cardmarket product on a
    # generic Promotion-card suffix while Bandai moved the OP14 special art to
    # P5. The generic donor still exists, so migration must not require it to be
    # orphaned.
    kid_product = normalize_cardmarket_product({
        "idProduct": 6201, "name": "Kid & Killer (EB01-003)",
        "idCategory": 1621, "idExpansion": 6432, "idMetacard": 46201,
    })
    kid_mapping = {"schemaVersion": 4, "mappings": {
        "EB01-003_P3": {
            "productId": 6201, "legacySet": "OP14", "legacyName": "Kid & Killer",
        },
    }}
    kid_cards = [
        {
            "cardNo": "EB01-003", "sourcePrintingId": "EB01-003_P3",
            "name": "Kid & Killer", "seriesId": "569901",
            "seriesLabel": "Promotion card", "cardSetsText": "Promotion card",
            "isParallel": True, "isReprint": False,
        },
        {
            "cardNo": "EB01-003", "sourcePrintingId": "EB01-003_P5",
            "name": "Kid & Killer", "seriesId": "569114",
            "seriesLabel": "BOOSTER PACK -THE AZURE SEA'S SEVEN- [OP14-EB04]",
            "cardSetsText": "BOOSTER PACK -THE AZURE SEA'S SEVEN- [OP14-EB04]",
            "isParallel": True, "isReprint": False,
        },
    ]
    kid_profiles = {"569114": {
        "value": 6432, "count": 194, "total": 194, "ratio": 1.0,
        "source": "bandai-current-series-mapping-majority", "seriesId": "569114",
        "releaseCodes": ["EB04", "OP14"],
    }}
    kid_review = auto_map_and_build_review(
        kid_cards, kid_mapping, [kid_product], allow_auto_map=True,
        series_expansion_diagnostics={}, series_expansion_profiles=kid_profiles,
    )
    assert kid_mapping["mappings"]["EB01-003_P5"]["productId"] == 6201
    assert kid_mapping["mappings"]["EB01-003_P3"]["productId"] is None
    assert any(x["printingId"] == "EB01-003_P5" for x in kid_review["semanticAliasesMoved"])

    # V3.7 regression: OP17 can be regionally ambiguous (two plausible
    # Cardmarket expansions). We still know a Special-Tournaments product is
    # impossible for an OP17 printing, so it must be quarantined without
    # choosing between the two OP17 expansions.
    buggy_products = [
        normalize_cardmarket_product({
            "idProduct": 6301, "name": "Buggy (P-084)",
            "idCategory": 1621, "idExpansion": 5262, "idMetacard": 46301,
        }),
        normalize_cardmarket_product({
            "idProduct": 6302, "name": "Buggy (P-084)",
            "idCategory": 1621, "idExpansion": 6492, "idMetacard": 46301,
        }),
        normalize_cardmarket_product({
            "idProduct": 6303, "name": "Buggy (P-084)",
            "idCategory": 1621, "idExpansion": 6723, "idMetacard": 46301,
        }),
    ]
    buggy_products = [p for p in buggy_products if p]
    buggy_mapping = {"schemaVersion": 4, "mappings": {
        "P-084_P1": {
            "productId": 6301, "legacySet": "prize-cards", "legacyName": "Buggy",
        },
    }}
    buggy_cards = [{
        "cardNo": "P-084", "sourcePrintingId": "P-084_P1", "name": "Buggy",
        "seriesId": "569117",
        "seriesLabel": "BOOSTER PACK -THE WORLD'S STRONGEST WARRIORS- [OP-17]",
        "cardSetsText": "BOOSTER PACK -THE WORLD'S STRONGEST WARRIORS- [OP-17]",
        "isParallel": True, "isReprint": False,
    }]
    buggy_diag = {"569117": {
        "plausibleExpansions": [6492, 6723],
        "releaseCodes": ["OP17"],
    }}
    buggy_guard = validate_mapping_against_bandai(
        buggy_cards, buggy_mapping, {p["idProduct"]: p for p in buggy_products},
        release_profiles={}, series_profiles={}, series_diagnostics=buggy_diag,
    )
    assert buggy_mapping["mappings"]["P-084_P1"]["productId"] is None
    assert buggy_guard["quarantined"][0]["reason"] == "bandai-series-expansion-not-plausible"

    # V3.7 regression: P-057 demonstrates cross-semantic suffix drift. Bandai
    # keeps the old base ID in the generic Promotion bucket and exposes the
    # ST16 printing as P1. A unique release-specific product must move into the
    # already-existing P1 mapping entry even though base/parallel classes differ.
    lullaby_product = normalize_cardmarket_product({
        "idProduct": 6401, "name": "Fleeting Lullaby (P-057)",
        "idCategory": 1621, "idExpansion": 5748, "idMetacard": 46401,
    })
    lullaby_mapping = {"schemaVersion": 4, "mappings": {
        "P-057": {
            "productId": 6401, "legacySet": "uta-deck-battle-participation-pack",
            "legacyName": "Fleeting Lullaby",
        },
        "P-057_P1": {
            "productId": None, "legacySet": "ST16", "legacyName": "Fleeting Lullaby",
            "invalidProductId": 6499, "invalidReason": "old-test-quarantine",
        },
    }}
    lullaby_cards = [
        {
            "cardNo": "P-057", "sourcePrintingId": "P-057", "name": "Fleeting Lullaby",
            "seriesId": "569901", "seriesLabel": "Promotion card",
            "cardSetsText": "Promotion card", "isParallel": False, "isReprint": False,
        },
        {
            "cardNo": "P-057", "sourcePrintingId": "P-057_P1", "name": "Fleeting Lullaby",
            "seriesId": "569016", "seriesLabel": "STARTER DECK -Green Uta- [ST-16]",
            "cardSetsText": "STARTER DECK -Green Uta- [ST-16]",
            "isParallel": True, "isReprint": False,
        },
    ]
    lullaby_profiles = {"569016": {
        "value": 5748, "count": 11, "total": 11, "ratio": 1.0,
        "source": "bandai-current-series-mapping-majority", "seriesId": "569016",
        "releaseCodes": ["ST16"],
    }}
    lullaby_review = auto_map_and_build_review(
        lullaby_cards, lullaby_mapping, [lullaby_product], allow_auto_map=True,
        series_expansion_diagnostics={}, series_expansion_profiles=lullaby_profiles,
    )
    assert lullaby_mapping["mappings"]["P-057_P1"]["productId"] == 6401
    assert lullaby_mapping["mappings"]["P-057"]["productId"] is None
    assert lullaby_mapping["mappings"]["P-057_P1"]["legacySet"] == "ST16"
    assert any(
        x["printingId"] == "P-057_P1"
        and x["donorSemanticClass"] == "base"
        and x["targetSemanticClass"] == "parallel"
        for x in lullaby_review["semanticAliasesMoved"]
    )

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

    # V3.10.2 regression: optional community data can enrich images but can
    # never participate in identity. DON!! matching must respect full release
    # context, not merely a character-name coincidence.
    community_fixture = {
        "don": {
            "endpoint": "https://optcgapi.com/api/allDonCards/",
            "payload": [
                {
                    "card_name": "DON!! Card (Perona)",
                    "optcg_don_name": "DON!! Card (Perona) - Premium Booster -The Best- (PRB-01)",
                    "card_image_id": "don_36",
                    "card_image": "/media/static/Card_Images/perona.jpg",
                },
                {
                    "card_name": "DON!! Card (Nico Robin)",
                    "optcg_don_name": "DON!! Card (Nico Robin) - Extra Booster: One Piece Heroines Edition (EB-03)",
                    "card_image_id": "don_130",
                    "card_image": "/media/static/Card_Images/robin_eb03.jpg",
                },
                {
                    "card_name": "DON!! Card (Robin)",
                    "optcg_don_name": "DON!! Card (Robin) - Premium Booster -The Best- Vol. 2 (PRB-02)",
                    "card_image_id": "don_20",
                    "card_image": "/media/static/Card_Images/robin_prb02.jpg",
                },
                {
                    "card_name": "DON!! Card (Gol.D.Roger)",
                    "optcg_don_name": "DON!! Card (Gol.D.Roger) - Carrying On His Will (OP13)",
                    "card_image_id": "don_5",
                    "card_image": "/media/static/Card_Images/roger_op13.jpg",
                },
                {
                    "card_name": "DON!! Card (Tin Pack Set Vol. 1 -Gol.D.Roger-)",
                    "optcg_don_name": "DON!! Card (Tin Pack Set Vol. 1 -Gol.D.Roger-) - One Piece Promotion Cards (OP-PR)",
                    "card_image_id": "don_65",
                    "card_image": "/media/static/Card_Images/roger_ts01.jpg",
                },
                {
                    "card_name": "DON!! Card (Whitebeard)",
                    "optcg_don_name": "DON!! Card (Whitebeard) - Premium Booster -The Best- (PRB-01)",
                    "card_image_id": "don_80",
                    "card_image": "/media/static/Card_Images/whitebeard_prb01.jpg",
                },
            ],
        },
        "promos": {
            "endpoint": "https://optcgapi.com/api/allPromos/",
            "payload": [
                {
                    "id": 999,
                    "card_name": "Future Promo",
                    "card_set_id": "P-999",
                    "card_image_id": "P-999",
                    "card_image": "https://optcgapi.com/media/static/Card_Images/P-999.jpg",
                }
            ],
        },
    }
    community_rows = normalize_community_image_records(community_fixture)
    assert len(community_rows) == 7, community_rows
    assert any(row.get("fullName", "").endswith("(PRB-02)") for row in community_rows)

    don_match, don_diag = match_community_reference_image(
        kind="don", display_name="DON!! (PRB Perona)", printed_codes=[], community_images=community_rows
    )
    assert don_match and don_match["sourceImageId"] == "don_36", don_diag

    robin_match, robin_diag = match_community_reference_image(
        kind="don", display_name="DON!! (PRB02 - Nico Robin)", printed_codes=[], community_images=community_rows
    )
    assert robin_match and robin_match["sourceImageId"] == "don_20", robin_diag

    # These were real V3.10 false positives and must stay rejected/corrected.
    kumamoto_match, _ = match_community_reference_image(
        kind="don", display_name="DON!! (Kumamoto 2026 Nico Robin)", printed_codes=[], community_images=community_rows
    )
    assert kumamoto_match is None
    roger_match, roger_diag = match_community_reference_image(
        kind="don", display_name="DON!! (Gol D. Roger TS01)", printed_codes=[], community_images=community_rows
    )
    assert roger_match and roger_match["sourceImageId"] == "don_65", roger_diag
    whitebeard_match, _ = match_community_reference_image(
        kind="don", display_name="DON!! (Whitebeard DP05)", printed_codes=[], community_images=community_rows
    )
    assert whitebeard_match is None
    plain_name_match, plain_name_diag = match_community_reference_image(
        kind="don", display_name="DON!! (Sanji)", printed_codes=[], community_images=community_rows
    )
    assert plain_name_match is None
    assert plain_name_diag["status"] == "insufficient-design-qualifier"

    promo_match, promo_diag = match_community_reference_image(
        kind="promo", display_name="Future Promo", printed_codes=["P-999"], community_images=community_rows
    )
    assert promo_match and promo_match["sourceCode"] == "P-999", promo_diag
    wrong_code_match, _ = match_community_reference_image(
        kind="promo", display_name="Future Promo", printed_codes=["P-998"], community_images=community_rows
    )
    assert wrong_code_match is None

    # Community artwork may remain an entity preview, but never becomes a
    # physical Cardmarket printing image, even when only one product exists.
    community_cache = {
        "images": {
            "https://optcgapi.com/media/static/Card_Images/P-999.jpg": {
                "ok": True,
                "finalUrl": "https://optcgapi.com/media/static/Card_Images/P-999.jpg",
            }
        }
    }
    image_catalog = dict(catalog)
    image_stats = add_cardmarket_supplements(
        image_catalog,
        {124: extra_product},
        {124: normalize_price_row({"idProduct": 124, "trend": 2.5})},
        "2026-09-10T02:00:00+0200",
        community_images=community_rows,
        image_cache=community_cache,
    )
    assert image_catalog["CMCARD-9001"]["previewImageUrl"] is not None
    assert image_catalog["CMCARD-9001"]["printings"][0]["imageUrl"] is None
    assert image_catalog["CMCARD-9001"]["printings"][0]["referenceImage"] is None
    assert image_stats["communityImages"]["standardEntitiesWithReferenceImage"] == 1

    duplicate_product = {
        **extra_product,
        "idProduct": 125,
        "idMetacard": 9002,
        "name": "Future Promo (P-999)",
        "website": "https://www.cardmarket.com/en/OnePiece/Products?idProduct=125",
    }
    duplicate_catalog = dict(catalog)
    duplicate_stats = add_cardmarket_supplements(
        duplicate_catalog,
        {124: extra_product, 125: duplicate_product},
        {
            124: normalize_price_row({"idProduct": 124, "trend": 2.5}),
            125: normalize_price_row({"idProduct": 125, "trend": 2.6}),
        },
        "2026-09-10T02:00:00+0200",
        community_images=community_rows,
        image_cache=community_cache,
    )
    assert duplicate_catalog["CMCARD-9001"]["previewImageUrl"] is None
    assert duplicate_catalog["CMCARD-9002"]["previewImageUrl"] is None
    assert "P-999" in duplicate_stats["communityImages"]["ambiguousStandardCodesSuppressed"]

    # V3.10.2 set index exposes base/master targets without changing card identity.
    sets_fixture = build_sets_index(catalog, {"packs": [{**vega_pack, "title_parts": {
        "prefix": "BOOSTER PACK", "title": "THE WORLD'S STRONGEST WARRIORS", "label": "OP-17"
    }}]}, mapping)
    bandai_sets = [item for item in sets_fixture["sets"] if item["source"] == "bandai"]
    assert bandai_sets and bandai_sets[0]["collectionTargets"]["master"] >= 1
    assert bandai_sets[0]["cards"][0]["catalogId"] == "OP17-001"

    # V3.11.4 language regression: only explicit parenthesised qualifiers count,
    # Non-English is never swallowed by English, and expansion evidence must be
    # homogeneous before assigning one language to every physical product.
    assert _explicit_nonsingle_language_marker(
        "Memorial Collection Booster Box (Non-English)"
    ) == "non-en"
    assert _explicit_nonsingle_language_marker(
        "Booster Box (Japanese)"
    ) == "ja"
    assert _explicit_nonsingle_language_marker(
        "Booster Box (English Version)"
    ) == "en"
    assert _explicit_nonsingle_language_marker(
        "Japanese Championship English Collection"
    ) is None

    non_en_evidence = _explicit_nonsingle_language([
        "Memorial Collection Booster Box (Non-English)",
        "Memorial Collection Booster (Non-English)",
    ])
    assert non_en_evidence["language"] is None
    assert non_en_evidence["languageGroup"] == "non-en"
    assert non_en_evidence["languageSource"] != "cardmarket-nonsingles-explicit-english"

    language_nonsingles = [
        {"idExpansion": 5580, "name": "Memorial Collection Booster Box (Non-English)"},
        {"idExpansion": 5580, "name": "Memorial Collection Booster (Non-English)"},
        {"idExpansion": 6018, "name": "Regional Booster Box (Japanese)"},
        {"idExpansion": 6018, "name": "Regional Booster (Japanese)"},
        {"idExpansion": 7001, "name": "Example Booster Box (English Version)"},
        {"idExpansion": 7001, "name": "Example Booster (English Version)"},
        # Mixed/incomplete expansion: one explicitly non-English product plus an
        # unqualified product cannot define the whole expansion language.
        {"idExpansion": 5262, "name": "Promotion Pack"},
        {"idExpansion": 5262, "name": "Promotion Pack (Non-English)"},
        # Explicitly conflicting qualifiers are also unknown.
        {"idExpansion": 7002, "name": "Example Booster Box (English Version)"},
        {"idExpansion": 7002, "name": "Example Booster Box (Japanese)"},
        # Even conflicting evidence cannot beat the verified 5511 override.
        {"idExpansion": 5511, "name": "Promo Box (English Version)"},
        {"idExpansion": 5511, "name": "Promo Box (Non-English)"},
    ]
    language_metadata, _ = build_cardmarket_expansion_metadata(
        {"mappings": {}}, {}, language_nonsingles
    )
    assert language_metadata[5580]["language"] is None
    assert language_metadata[5580]["languageLabel"] == "Non-English"
    assert language_metadata[5580]["languageGroup"] == "non-en"
    assert language_metadata[6018]["language"] == "ja"
    assert language_metadata[7001]["language"] == "en"
    assert language_metadata[5262]["language"] is None
    assert language_metadata[5262]["languageLabel"] is None
    assert language_metadata[7002]["language"] is None
    assert language_metadata[7002]["languageLabel"] is None
    assert language_metadata[5511]["language"] == "ja"
    assert language_metadata[5511]["languageLabel"] == "Japanese"

    # Stable URL/edition coherence is retained, but V3.11.4 no longer treats a
    # Bandai-English slug as proof that the complete Cardmarket expansion is EN.
    slug_products = {}
    slug_mapping = {"mappings": {}}
    for index in range(1, 5):
        code = f"OP01-{index:03d}"
        product_id = 800000 + index
        slug_products[product_id] = normalize_cardmarket_product({
            "idProduct": product_id,
            "name": f"Example {index} ({code})",
            "idExpansion": 7003,
            "idMetacard": 900000 + index,
        })
        slug_mapping["mappings"][code] = {
            "productId": product_id,
            "url": (
                "https://www.cardmarket.com/en/OnePiece/Products/Singles/"
                f"OP01-Romance-Dawn/Example-{index}-{code}"
            ),
        }
    slug_metadata, _ = build_cardmarket_expansion_metadata(
        slug_mapping, slug_products, []
    )
    assert slug_metadata[7003]["editionSlug"] == "op01-romance-dawn"
    assert slug_metadata[7003].get("language") is None


    # V3.11.4 regression: extra Cardmarket products sharing one anchored metacard
    # are attached to the same Bandai entity. P-JP expansion 5511 is explicitly
    # Japanese; version/order is never inferred.
    jp_anchor_product = normalize_cardmarket_product({
        "idProduct": 692222, "name": "Portgas.D.Ace (P-028)",
        "idCategory": 1621, "idExpansion": 5230, "idMetacard": 415755,
    })
    jp_product = normalize_cardmarket_product({
        "idProduct": 707718, "name": "Portgas.D.Ace (P-028)",
        "idCategory": 1621, "idExpansion": 5511, "idMetacard": 415755,
    })
    jp_mapping = {"mappings": {
        "P-028": {
            "productId": 692222,
            "url": "https://www.cardmarket.com/en/OnePiece/Products/Singles/Promos/PortgasDAce-P-028-V1",
        }
    }}
    jp_bandai = [{"sourcePrintingId": "P-028", "name": "Portgas.D.Ace"}]
    jp_candidates, jp_candidate_stats = cardmarket_bandai_variant_candidates(
        jp_bandai, jp_mapping, {692222: jp_anchor_product, 707718: jp_product}
    )
    assert jp_candidates == {707718: "P-028"}, jp_candidate_stats
    jp_expansion_metadata, _ = build_cardmarket_expansion_metadata(
        jp_mapping, {692222: jp_anchor_product, 707718: jp_product}, []
    )
    assert jp_expansion_metadata[5511]["language"] == "ja"
    jp_catalog = {
        "P-028": {
            "catalogId": "P-028", "code": "P-028", "name": "Portgas.D.Ace",
            "rarity": "Promo", "type": "Character", "life": None, "cost": 2,
            "power": 3000, "counter": 1000, "colors": ["Red"], "attributes": [],
            "block": None, "types": [], "effect": None, "trigger": None,
            "sources": ["bandai"], "releaseIds": [], "printings": [],
        }
    }
    jp_added = add_cardmarket_bandai_variants(
        jp_catalog, jp_candidates,
        {692222: jp_anchor_product, 707718: jp_product},
        {707718: normalize_price_row({"idProduct": 707718, "trend": 19.32})},
        "2026-09-14T02:46:38+0200",
        expansion_metadata=jp_expansion_metadata,
    )
    assert jp_added["products"] == 1
    assert jp_catalog["P-028"]["printings"][0]["printingId"] == "CM-707718"
    assert jp_catalog["P-028"]["printings"][0]["language"] == "ja"
    assert jp_catalog["P-028"]["printings"][0]["cardmarket"]["price"]["valuationEur"] == 19.32

    # V3.13.0 regression: a legacy image tied to the same idProduct is reused
    # only as a validated reference, never promoted to exact artwork.
    legacy_url = "https://example.com/P-028_p2_EN.webp"
    legacy_refs, legacy_ref_stats = legacy_mapping_reference_images({"mappings": {
        "P-028_P2": {
            "productId": 740383,
            "legacyImageUrl": legacy_url,
            "legacySet": "gift-collection-01",
        }
    }})
    assert legacy_ref_stats["uniqueProductReferences"] == 1
    assert legacy_refs[740383]["scope"] == "single-cardmarket-product"
    legacy_catalog = {
        "P-028": {
            "catalogId": "P-028", "code": "P-028", "name": "Portgas.D.Ace",
            "rarity": "Promo", "type": "Character", "life": None, "cost": 5,
            "power": 6000, "counter": None, "colors": ["Red"], "attributes": ["Special"],
            "block": 1, "types": ["Whitebeard Pirates"], "effect": "[Double Attack]",
            "trigger": None, "sources": ["bandai"], "releaseIds": [], "printings": [],
        }
    }
    legacy_product = normalize_cardmarket_product({
        "idProduct": 740383, "name": "Portgas.D.Ace (P-028)",
        "idCategory": 1621, "idExpansion": 5230, "idMetacard": 415755,
    })
    legacy_added = add_cardmarket_bandai_variants(
        legacy_catalog, {740383: "P-028"}, {740383: legacy_product}, {}, None,
        linked_reference_images=legacy_refs,
        image_cache={"images": {legacy_url: {
            "ok": True, "finalUrl": legacy_url, "httpStatus": 200, "contentType": "image/webp"
        }}},
    )
    legacy_printing = legacy_catalog["P-028"]["printings"][0]
    assert legacy_added["productsWithReferenceImage"] == 0
    assert legacy_printing["imageUrl"] is None
    assert legacy_printing["image"] is None
    assert legacy_printing["referenceImage"] is None

    # Compact daily history overwrites the same source day instead of duplicating it.
    history_path = Path("/tmp/optcg_v3101_history_test.json")
    if history_path.exists():
        history_path.unlink()
    history, history_stats = update_price_history(
        history_path,
        {123: price},
        "2026-09-10T02:00:00+0200",
        retention_days=30,
    )
    save_json(history_path, history)
    history2, history_stats2 = update_price_history(
        history_path,
        {123: normalize_price_row({"idProduct": 123, "trend": 1.2})},
        "2026-09-10T23:00:00+0200",
        retention_days=30,
    )
    assert history_stats["daysStored"] == 1
    assert history_stats2["overwroteExistingDay"] is True
    assert history2["snapshots"]["2026-09-10"]["123"] == 1.2
    history_path.unlink(missing_ok=True)

    version, version_label = _cardmarket_market_version("<title>Card (V.2)</title>", "https://www.cardmarket.com/en/OnePiece/Products/Singles/X/Card-V2")
    assert version == "2" and version_label == "Version 2"
    dirs = _cardmarket_image_dir_candidates({"idProduct": 838658, "name": "DON!! (PRB02 - Sanji)"}, {"editionCode": "PRB02", "languageGroup": "en"})
    assert "PRB02" in dirs and "PRB02-JP" in dirs

    # V3.12 OPlay sitemap normalization: exact artwork is identified by
    # code + normalized language + release + printing variant.
    oplay_fixture = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
      <url><loc>https://oplaytcg.com/en/cards/ST02-004/tc</loc>
        <image:image><image:loc>https://cards.oplaytcg.com/PRB01/tc/small/ST02-004_p3.webp</image:loc></image:image>
      </url>
      <url><loc>https://oplaytcg.com/en/cards/OP01-001/fr</loc>
        <image:image><image:loc>https://cards.oplaytcg.com/OP01/fr/small/OP01-001.webp</image:loc></image:image>
      </url>
    </urlset>"""
    _, oplay_fixture_rows, _ = _parse_oplay_sitemap_xml(oplay_fixture, OPLAY_SITEMAP_URL)
    assert len(oplay_fixture_rows) == 2
    tc = next(row for row in oplay_fixture_rows if row["code"] == "ST02-004")
    assert tc["language"] == "zh-Hant" and tc["releaseCode"] == "PRB01" and tc["variant"] == "p3"
    assert tc["oplayKey"] == "ST02-004|zh-Hant|PRB01|p3"

    # Unique Cardmarket/OPlay evidence may merge and supply the exact remote image.
    unique_catalog = {
        "OP01-001": {
            "catalogId": "OP01-001", "code": "OP01-001", "name": "Example",
            "sources": ["cardmarket"], "releaseIds": [], "printings": [{
                "id": "CM-999001", "printingId": "CM-999001", "baseCode": "OP01-001",
                "source": "cardmarket", "sources": ["cardmarket"],
                "language": "fr", "languageLabel": "French", "editionCode": "OP01",
                "imageUrl": None, "image": None, "referenceImage": None,
                "releases": [], "cardmarket": {"productId": 999001, "product": {
                    "idProduct": 999001, "name": "Example (OP01-001)", "idExpansion": 1
                }},
            }],
        }
    }
    unique_raw = {"cards": {}, "printings": [next(row for row in oplay_fixture_rows if row["code"] == "OP01-001")]}
    unique_map, unique_review, unique_stats = reconcile_oplay_with_catalog(unique_catalog, unique_raw, {})
    unique_printing = unique_catalog["OP01-001"]["printings"][0]
    assert unique_stats["cardmarketProductsMapped"] == 1 and unique_review["pendingCount"] == 0
    assert unique_printing["imageUrl"] == "https://cards.oplaytcg.com/OP01/fr/small/OP01-001.webp"
    assert unique_printing["displayInCollection"] is True
    assert unique_map["products"]["999001"]["oplayKey"] == "OP01-001|fr|OP01|base"

    # Two physical OPlay candidates for the same insufficiently-described market row
    # are never guessed. The Cardmarket market row stays auditable but hidden, and
    # both real OPlay printings remain visible collection versions.
    ambiguous_catalog = {
        "OP01-001": {
            "catalogId": "OP01-001", "code": "OP01-001", "name": "Example",
            "sources": ["cardmarket"], "releaseIds": [], "printings": [{
                "id": "CM-999002", "printingId": "CM-999002", "baseCode": "OP01-001",
                "source": "cardmarket", "sources": ["cardmarket"],
                "language": "fr", "languageLabel": "French", "editionCode": "OP01",
                "imageUrl": None, "image": None, "referenceImage": None,
                "releases": [], "cardmarket": {"productId": 999002, "product": {
                    "idProduct": 999002, "name": "Example (OP01-001)", "idExpansion": 1
                }},
            }],
        }
    }
    base_row = next(row for row in oplay_fixture_rows if row["code"] == "OP01-001")
    alt_row = {**base_row, "variant": "p1", "sourcePrintingId": "OP01-001_p1",
               "oplayKey": "OP01-001|fr|OP01|p1",
               "imageUrl": "https://cards.oplaytcg.com/OP01/fr/small/OP01-001_p1.webp"}
    _, ambiguous_review, ambiguous_stats = reconcile_oplay_with_catalog(
        ambiguous_catalog, {"cards": {}, "printings": [base_row, alt_row]}, {}
    )
    assert ambiguous_stats["cardmarketProductsMapped"] == 0
    assert ambiguous_stats["cardmarketProductsHiddenAsAmbiguousMarketRows"] == 1
    assert ambiguous_review["pendingCount"] == 1
    cm_hidden = next(p for p in ambiguous_catalog["OP01-001"]["printings"] if p["printingId"] == "CM-999002")
    assert cm_hidden["displayInCollection"] is False
    assert len([p for p in ambiguous_catalog["OP01-001"]["printings"] if p.get("source") == "oplay"]) == 2

    print("SELF-TEST OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------




def _client_release(release: dict) -> dict:
    """Keep only release metadata used by Cardify UI.

    The full reconciliation/audit representation stays in memory and in the
    dedicated mapping/report files. Repeating every release field on ~40k
    printings was one of the largest contributors to the client payload.
    """
    if not isinstance(release, dict):
        return {}
    out = {}
    for key in ("releaseId", "source", "code", "kind", "displayName", "cardmarketExpansionId"):
        value = release.get(key)
        if value is not None and value != "" and value != []:
            out[key] = value
    return out


def _client_cardmarket(cardmarket: dict) -> dict:
    """Compact Cardmarket data to the fields Cardify actually renders.

    Product URLs/names and mapping evidence live in data/review artifacts. The
    app opens a code-level Cardmarket search for every physical printing, so the
    client catalog only needs productId plus the current price metrics.
    """
    if not isinstance(cardmarket, dict):
        return {}
    product = cardmarket.get("product") if isinstance(cardmarket.get("product"), dict) else {}
    product_id = cardmarket.get("productId") or product.get("idProduct")
    out = {}
    if product_id is not None:
        out["productId"] = product_id

    price = cardmarket.get("price")
    if isinstance(price, dict):
        compact_price = {}
        if product_id is not None:
            compact_price["idProduct"] = product_id
        for key in ("createdAt", "low", "trend", "avg7", "avg30", "valuationEur"):
            value = price.get(key)
            if value is not None:
                compact_price[key] = value
        if compact_price:
            out["price"] = compact_price

    evidence = cardmarket.get("mappingEvidence")
    if isinstance(evidence, dict) and evidence.get("sourceCodeDiscrepancy") is True:
        out["mappingEvidence"] = {"sourceCodeDiscrepancy": True}
    return out


def build_client_catalog(catalog: dict) -> tuple[dict, dict]:
    """Build the wire-format catalog consumed by Cardify.

    V3.13 keeps the rich canonical catalog in memory while generating sets,
    mapping QA and reports, but publishes a backwards-compatible sparse object
    with the same ``catalogId -> card`` root shape. Missing printing mechanics
    already fall back to the entity mechanics in Cardify's parser.
    """
    client = {}
    for catalog_id, card in catalog.items():
        if not isinstance(card, dict):
            continue
        entry = {}
        code = card.get("code") or catalog_id
        for key in (
            "code", "name", "rarity", "type", "life", "cost", "power", "counter",
            "colors", "attributes", "block", "types", "effect", "trigger", "sources",
            "catalogOrigin", "bandaiCanonical", "dataCompleteness", "collectibleType",
            "cardmarketMetacardIds", "cardmarketMetacardId", "previewImageUrl",
        ):
            value = card.get(key)
            if value is not None and value != "" and value != []:
                entry[key] = value

        printed_codes = card.get("printedCodes") or []
        if printed_codes and printed_codes != [code]:
            entry["printedCodes"] = printed_codes
        legacy_ids = card.get("legacyCatalogIds") or []
        if legacy_ids:
            entry["legacyCatalogIds"] = legacy_ids

        base_rarity = card.get("rarity")
        printings = []
        for printing in card.get("printings", []) or []:
            if not isinstance(printing, dict):
                continue
            printing_id = printing.get("id") or printing.get("printingId") or printing.get("sourcePrintingId")
            if not printing_id:
                continue
            row = {"id": printing_id}

            source = printing.get("source")
            if source:
                row["source"] = source
            source_printing_id = printing.get("sourcePrintingId")
            if source_printing_id and source_printing_id != printing_id:
                row["sourcePrintingId"] = source_printing_id

            variant_type = printing.get("variantType")
            if variant_type and variant_type != "unknown":
                row["variantType"] = variant_type
            if printing.get("isParallel") is True:
                row["isParallel"] = True
            if printing.get("isReprint") is True:
                row["isReprint"] = True
            if printing.get("physicalVariantUnknown") is True:
                row["physicalVariantUnknown"] = True
            if printing.get("displayInCollection") is False:
                row["displayInCollection"] = False

            rarity = printing.get("rarity")
            if rarity:
                row["rarity"] = rarity
            for key in ("language", "editionCode", "editionName", "marketVersion"):
                value = printing.get(key)
                if value is not None and value != "":
                    row[key] = value
            if not printing.get("language") and printing.get("languageLabel"):
                row["languageLabel"] = printing.get("languageLabel")
            version_label = printing.get("marketVersionLabel")
            if version_label and version_label != printing.get("marketVersion"):
                row["marketVersionLabel"] = version_label

            image_url = printing.get("imageUrl")
            if image_url:
                row["imageUrl"] = image_url

            releases = [_client_release(item) for item in (printing.get("releases") or [])]
            releases = [item for item in releases if item]
            if releases:
                row["releases"] = releases

            different = printing.get("mechanicsDifferFromBase") or []
            if different:
                mechanics = printing.get("mechanics")
                if isinstance(mechanics, dict):
                    row["mechanics"] = mechanics
                row["mechanicsDifferFromBase"] = different

            compact_market = _client_cardmarket(printing.get("cardmarket"))
            if compact_market:
                row["cardmarket"] = compact_market

            printings.append(row)
        entry["printings"] = printings
        client[catalog_id] = entry

    # Regression contract: compaction can remove redundant metadata, never IDs,
    # image URLs, visibility, languages or valuation values.
    if set(client) != set(catalog):
        raise RuntimeError("V3.13 client compaction changed catalog entity IDs")
    full_printings = 0
    client_printings = 0
    for catalog_id, card in catalog.items():
        original_rows = [p for p in (card.get("printings", []) or []) if isinstance(p, dict)]
        compact_rows = client[catalog_id].get("printings", [])
        full_printings += len(original_rows)
        client_printings += len(compact_rows)
        original_by_id = {
            str(p.get("id") or p.get("printingId") or p.get("sourcePrintingId")): p
            for p in original_rows
            if p.get("id") or p.get("printingId") or p.get("sourcePrintingId")
        }
        compact_by_id = {str(p.get("id")): p for p in compact_rows}
        if set(original_by_id) != set(compact_by_id):
            raise RuntimeError(f"V3.13 client compaction changed printing IDs for {catalog_id}")
        for printing_id, original in original_by_id.items():
            compact = compact_by_id[printing_id]
            checks = (
                ("source", original.get("source") or "bandai", compact.get("source") or "bandai"),
                ("variantType", original.get("variantType") or "unknown", compact.get("variantType") or "unknown"),
                ("rarity", original.get("rarity"), compact.get("rarity")),
                ("language", original.get("language"), compact.get("language")),
                ("editionCode", original.get("editionCode"), compact.get("editionCode")),
                ("editionName", original.get("editionName"), compact.get("editionName")),
                ("marketVersion", original.get("marketVersion"), compact.get("marketVersion")),
                ("imageUrl", original.get("imageUrl"), compact.get("imageUrl")),
                ("displayInCollection", original.get("displayInCollection") is not False, compact.get("displayInCollection") is not False),
            )
            for field, before, after in checks:
                if before != after:
                    raise RuntimeError(f"V3.13 client compaction changed {field} for {catalog_id}/{printing_id}")
            original_market = original.get("cardmarket") if isinstance(original.get("cardmarket"), dict) else {}
            original_price = original_market.get("price") if isinstance(original_market.get("price"), dict) else {}
            compact_market = compact.get("cardmarket") if isinstance(compact.get("cardmarket"), dict) else {}
            compact_price = compact_market.get("price") if isinstance(compact_market.get("price"), dict) else {}
            if original_price.get("valuationEur") != compact_price.get("valuationEur"):
                raise RuntimeError(f"V3.13 client compaction changed valuation for {catalog_id}/{printing_id}")

    compact_bytes = len(json.dumps(client, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    full_bytes = len(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    stats = {
        "schemaVersion": 1,
        "entities": len(client),
        "printings": client_printings,
        "internalPrintings": full_printings,
        "internalCompactBytes": full_bytes,
        "clientBytes": compact_bytes,
        "savedBytes": max(0, full_bytes - compact_bytes),
        "reductionPercent": round((1 - compact_bytes / full_bytes) * 100, 3) if full_bytes else 0.0,
        "contract": "same IDs/languages/visibility/images/valuations; redundant audit metadata omitted",
    }
    return client, stats

def main() -> None:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return
    if args.price_history_days < 7:
        raise RuntimeError("--price-history-days debe ser >= 7")
    if args.cardmarket_image_delay < 0:
        raise RuntimeError("--cardmarket-image-delay debe ser >= 0")
    if args.cardmarket_image_max_new < 0:
        raise RuntimeError("--cardmarket-image-max-new debe ser >= 0")
    if args.image_asset_max_new < 0:
        raise RuntimeError("--image-asset-max-new debe ser >= 0")
    if args.oplay_card_metadata_max_new < 0:
        raise RuntimeError("--oplay-card-metadata-max-new debe ser >= 0")

    session = make_session()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.image_dir.mkdir(parents=True, exist_ok=True)

    if args.from_raw:
        raw_data = load_raw(args.raw_dir)
    else:
        raw_data = fetch_live_raw(
            session,
            args.bandai_delay,
            args.vega_bin,
            raw_dir=args.raw_dir,
            include_oplay=not args.no_oplay,
            oplay_refresh_metadata=args.oplay_refresh_metadata,
            oplay_metadata_max_new=args.oplay_card_metadata_max_new,
            oplay_deep_crawl=args.oplay_deep_crawl,
        )
        save_raw(args.raw_dir, raw_data)

    oplay_raw = raw_data.get("oplay") if not args.no_oplay else {"disabled": True, "cards": {}, "printings": [], "stats": {}}
    oplay_printings = normalize_oplay_printings(oplay_raw)
    # OPTCGAPI was retired in V3.13.0. The legacy matching code remains only for
    # rollback/self-test compatibility; no live community records enter the catalog.
    community_images: list[dict] = []

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

    nonsingle_rows_raw = extract_rows(
        raw_data.get("cardmarket_nonsingles") or {},
        ("products", "product", "data"),
    )
    nonsingle_products = [normalize_cardmarket_product(row) for row in nonsingle_rows_raw]
    nonsingle_products = [item for item in nonsingle_products if item]

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

    series_expansion_diagnostics = analyze_bandai_series_expansions_from_catalog(
        bandai_cards, products
    )
    series_expansion_profiles = _stable_bandai_series_expansion_profiles(
        bandai_cards, mapping, products_by_id, series_expansion_diagnostics
    )
    release_profiles = _stable_release_expansion_profiles(mapping, products_by_id)
    inferred_release_profiles, _ = infer_release_expansions_from_catalog(
        bandai_cards, products, release_profiles
    )
    for release_code, profile in inferred_release_profiles.items():
        release_profiles.setdefault(release_code, profile)

    bandai_identity_validation = validate_mapping_against_bandai(
        bandai_cards,
        mapping,
        products_by_id,
        release_profiles=release_profiles,
        series_profiles=series_expansion_profiles,
        series_diagnostics=series_expansion_diagnostics,
    )

    review = auto_map_and_build_review(
        bandai_cards,
        mapping,
        products,
        allow_auto_map=not args.no_auto_map,
        series_expansion_diagnostics=series_expansion_diagnostics,
        series_expansion_profiles=series_expansion_profiles,
    )

    # Final pre-publication firewall: nothing added or migrated above is allowed
    # to reach the catalogue with release/expansion evidence contradicting Bandai.
    final_identity_validation = validate_mapping_against_bandai(
        bandai_cards,
        mapping,
        products_by_id,
        release_profiles=release_profiles,
        series_profiles=series_expansion_profiles,
        series_diagnostics=series_expansion_diagnostics,
    )
    all_identity_quarantined = (
        bandai_identity_validation.get("quarantined", [])
        + final_identity_validation.get("quarantined", [])
    )
    identity_quarantined_final = []
    identity_resolved_during_run = []
    for item in all_identity_quarantined:
        current_entry = mapping.get("mappings", {}).get(item.get("printingId"))
        if (
            isinstance(current_entry, dict)
            and get_number(current_entry.get("productId")) is None
            and current_entry.get("invalidReason") == item.get("reason")
        ):
            identity_quarantined_final.append(item)
        else:
            identity_resolved_during_run.append(item)

    if final_identity_validation.get("quarantined"):
        refreshed = auto_map_and_build_review(
            bandai_cards,
            mapping,
            products,
            allow_auto_map=False,
            series_expansion_diagnostics=series_expansion_diagnostics,
            series_expansion_profiles=series_expansion_profiles,
        )
        refreshed["autoMappingsAdded"] = review.get("autoMappingsAdded", [])
        refreshed["semanticAliasesMoved"] = review.get("semanticAliasesMoved", [])
        refreshed["semanticDriftQuarantined"] = (
            review.get("semanticDriftQuarantined", [])
            + refreshed.get("semanticDriftQuarantined", [])
        )
        review = refreshed

    save_json(args.data_dir / MAPPING_FILENAME, mapping)

    expansion_metadata, expansion_metadata_stats = build_cardmarket_expansion_metadata(
        mapping, products_by_id, nonsingle_products
    )
    bandai_variant_candidates, bandai_variant_candidate_stats = cardmarket_bandai_variant_candidates(
        bandai_cards, mapping, products_by_id
    )
    linked_legacy_reference_images, linked_legacy_reference_stats = legacy_mapping_reference_images(mapping)
    linked_legacy_reference_images = {
        product_id: record
        for product_id, record in linked_legacy_reference_images.items()
        if product_id in bandai_variant_candidates
    }
    linked_legacy_reference_stats["linkedVariantReferences"] = len(linked_legacy_reference_images)

    # V3.12 image policy: remote by default. Exact Bandai/OPlay URLs are catalog data,
    # not Git assets. Cache mode is explicit and optional. Cardmarket HTML image
    # discovery is retained only as a disabled legacy fallback.
    cardmarket_printing_metadata, cardmarket_metadata_stats = load_cardmarket_printing_metadata(
        args.data_dir, mapping
    )
    save_json(args.data_dir / CARDMARKET_PRINTING_METADATA_FILENAME, cardmarket_printing_metadata)
    cardmarket_exact_images = {
        int(pid): record
        for pid, record in (cardmarket_printing_metadata.get("products") or {}).items()
        if str(pid).isdigit() and isinstance(record, dict)
    }

    image_cache = {"images": {}}
    image_asset_manifest = {"schemaVersion": 1, "assets": {}}
    bandai_asset_stats = {"targets": len({canonical_id(c.get("sourcePrintingId")) for c in bandai_cards if c.get("sourcePrintingId")}), "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0}
    cardmarket_asset_stats = {"targets": 0, "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0, "manual": 0}
    oplay_asset_stats = {"targets": len(oplay_printings), "reused": 0, "adopted": 0, "downloaded": 0, "pending": 0, "errors": 0}
    oplay_public_urls: dict[str, str] = {}
    cardmarket_image_discovery_stats = {
        "enabled": False,
        "discovered": 0,
        "cachedFound": 0,
        "errors": 0,
        "stoppedReason": "retired-by-oplay-v3.12",
    }
    discovery_network_enabled = False
    image_report = {
        "totalUrls": 0, "officialUrls": 0, "communityUrls": 0,
        "cardmarketExactUrls": 0, "checkedThisRun": 0, "failed": [],
    }

    if args.image_storage_mode == "cache" and not args.no_persist_images:
        image_cache_path = args.data_dir / IMAGE_CACHE_FILENAME
        image_cache = load_json(image_cache_path, default={}) or {"images": {}}
        image_asset_manifest_path = args.data_dir / IMAGE_ASSET_MANIFEST_FILENAME
        image_asset_manifest = load_json(image_asset_manifest_path, default={}) or {"schemaVersion": 1, "assets": {}}
        bandai_asset_stats = persist_bandai_image_assets(
            session, bandai_cards, args.image_dir, image_asset_manifest, image_cache,
            args.image_public_base_url, allow_downloads=True,
            refresh=args.refresh_image_assets, max_new=args.image_asset_max_new,
        )
        remaining = 0
        if args.image_asset_max_new:
            remaining = max(0, args.image_asset_max_new - bandai_asset_stats.get("downloaded", 0))
        oplay_public_urls, oplay_asset_stats = persist_oplay_image_assets(
            session, oplay_printings, args.image_dir, image_asset_manifest, image_cache,
            args.image_public_base_url, allow_downloads=True,
            refresh=args.refresh_image_assets, max_new=remaining if args.image_asset_max_new else 0,
        )
        save_json(image_cache_path, image_cache)
        save_json(image_asset_manifest_path, image_asset_manifest)

    catalog, catalogue_stats = build_catalog(
        bandai_cards,
        mapping,
        products_by_id,
        prices_by_id,
        price_created_at,
        image_cache,
        series_expansion_profiles=series_expansion_profiles,
    )
    bandai_catalogue_stats = dict(catalogue_stats)
    linked_variant_stats = add_cardmarket_bandai_variants(
        catalog,
        bandai_variant_candidates,
        products_by_id,
        prices_by_id,
        price_created_at,
        expansion_metadata=expansion_metadata,
        cardmarket_exact_images=cardmarket_exact_images,
        linked_reference_images=linked_legacy_reference_images,
        image_cache=image_cache,
    )
    supplemental_stats = add_cardmarket_supplements(
        catalog,
        products_by_id,
        prices_by_id,
        price_created_at,
        community_images=community_images,
        cardmarket_exact_images=cardmarket_exact_images,
        expansion_metadata=expansion_metadata,
        image_cache=image_cache,
    )
    # Integrate OPlay as the canonical multilingual physical-printing layer.
    # Exact Cardmarket links are persisted; ambiguous market rows remain auditable
    # but are hidden from collection/version pickers to avoid duplicate physical cards.
    oplay_mapping_path = args.data_dir / OPLAY_CARDMARKET_MAPPING_FILENAME
    oplay_mapping = load_json(oplay_mapping_path, default={}) or {}
    oplay_mapping, oplay_mapping_review, oplay_stats = reconcile_oplay_with_catalog(
        catalog,
        oplay_raw,
        oplay_mapping,
        oplay_public_urls=oplay_public_urls,
    )
    save_json(oplay_mapping_path, oplay_mapping)
    oplay_review_path = args.output_dir / OPLAY_MAPPING_REVIEW_FILENAME
    save_json(oplay_review_path, oplay_mapping_review)

    catalogue_stats.update({
        "bandaiCards": bandai_catalogue_stats["cards"],
        "bandaiPrintings": bandai_catalogue_stats["printings"],
        "bandaiPrintingsWithCardmarketMapping": bandai_catalogue_stats["printingsWithCardmarketMapping"],
        "bandaiPrintingsWithCardmarketPriceGuide": bandai_catalogue_stats["printingsWithCardmarketPriceGuide"],
        "bandaiPrintingsWithCardmarketValuation": bandai_catalogue_stats["printingsWithCardmarketValuation"],
        "cardmarketOnlyCards": supplemental_stats["standardCards"],
        "cardmarketOnlyProducts": supplemental_stats["standardProducts"],
        "donCards": supplemental_stats["donCards"],
        "donProducts": supplemental_stats["donProducts"],
        "cardmarketDirectProducts": supplemental_stats["standardProducts"] + supplemental_stats["donProducts"],
        "cardmarketLinkedBandaiProducts": linked_variant_stats["products"],
        "oplaySourceCards": oplay_stats.get("sourceCards", 0),
        "oplaySourcePrintings": oplay_stats.get("sourcePrintings", 0),
        "oplayOnlyEntitiesAdded": oplay_stats.get("oplayOnlyEntitiesAdded", 0),
        "oplayPrintingsAdded": oplay_stats.get("oplayPrintingsAdded", 0),
        "oplayCardmarketProductsMapped": oplay_stats.get("cardmarketProductsMapped", 0),
        "cards": len(catalog),
        "printings": sum(len(card.get("printings", [])) for card in catalog.values()),
        "visiblePrintings": sum(
            1 for card in catalog.values() if isinstance(card, dict)
            for printing in card.get("printings", []) or []
            if printing.get("displayInCollection") is not False
        ),
        "hiddenMarketPrintings": sum(
            1 for card in catalog.values() if isinstance(card, dict)
            for printing in card.get("printings", []) or []
            if printing.get("displayInCollection") is False
        ),
        # Market/valuation counts describe Cardmarket-linked rows only. OPlay-only
        # printings intentionally do not inflate these metrics.
        "printingsWithCardmarketPriceGuide": (
            bandai_catalogue_stats["printingsWithCardmarketPriceGuide"]
            + linked_variant_stats["productsWithPriceGuide"]
            + supplemental_stats["standardProductsWithPriceGuide"]
            + supplemental_stats["donProductsWithPriceGuide"]
        ),
        "printingsWithCardmarketValuation": (
            bandai_catalogue_stats["printingsWithCardmarketValuation"]
            + linked_variant_stats["productsWithValuation"]
            + supplemental_stats["standardProductsWithValuation"]
            + supplemental_stats["donProductsWithValuation"]
        ),
        "entitiesWithPreviewImage": sum(
            1 for card in catalog.values() if isinstance(card, dict) and card.get("previewImageUrl")
        ),
    })

    sets_index = build_sets_index(catalog, bandai_root, mapping)
    history_path = args.output_dir / PRICE_HISTORY_FILENAME
    price_history = load_json(history_path, default=None)
    history_stats = {
        "enabled": not args.no_price_history,
        "daysStored": len((price_history or {}).get("snapshots", {})) if isinstance(price_history, dict) else 0,
    }
    if not args.no_price_history:
        price_history, update_stats = update_price_history(
            history_path,
            prices_by_id,
            price_created_at,
            retention_days=args.price_history_days,
        )
        history_stats.update(update_stats)

    client_catalog, client_compaction_stats = build_client_catalog(catalog)

    generated_at = utc_now_iso()
    oplay_raw_stats = (oplay_raw or {}).get("stats", {}) if isinstance(oplay_raw, dict) else {}
    report = {
        "generatedAt": generated_at,
        "schemaVersion": 11,
        "catalogVersion": "3.13.0",
        "sources": {
            "bandai": {
                "url": bandai_root.get("sourceUrl") if isinstance(bandai_root, dict) else None,
                "fetchedAt": bandai_root.get("fetchedAt") if isinstance(bandai_root, dict) else None,
                "records": len(bandai_cards),
                "series": len(bandai_root.get("series", [])) if isinstance(bandai_root, dict) else None,
                "authority": "official",
            },
            "oplay": {
                "enabled": not args.no_oplay,
                "url": OPLAY_LIBRARY_URL,
                "fetchedAt": (oplay_raw or {}).get("fetchedAt") if isinstance(oplay_raw, dict) else None,
                "uniqueCards": oplay_raw_stats.get("uniqueCards", 0),
                "printings": oplay_raw_stats.get("printings", 0),
                "languages": oplay_raw_stats.get("languages", {}),
                "releases": oplay_raw_stats.get("releases", 0),
                "metadataCards": oplay_raw_stats.get("metadataCards", 0),
                "metadataFetchedThisRun": oplay_raw_stats.get("metadataFetchedThisRun", 0),
                "sitemapDocuments": oplay_raw_stats.get("sitemapDocuments", 0),
                "errors": len((oplay_raw or {}).get("errors", [])) if isinstance(oplay_raw, dict) else 0,
                "authority": "community-physical-printing-catalogue",
                "authoritativeForOfficialIdentity": False,
                "authoritativeForPrice": False,
                "imageBytesDownloaded": args.image_storage_mode == "cache" and not args.no_persist_images,
            },
            "cardmarketProducts": {
                "url": CARDMARKET_PRODUCTS_URL,
                "records": len(products),
                "createdAt": raw_data["cardmarket_products"].get("createdAt") if isinstance(raw_data["cardmarket_products"], dict) else None,
                "authority": "official-public-download",
            },
            "cardmarketPriceGuide": {
                "url": CARDMARKET_PRICE_GUIDE_URL,
                "records": len(prices),
                "createdAt": price_created_at,
                "authority": "official-public-download",
            },
            "cardmarketNonSingles": {
                "url": CARDMARKET_NONSINGLES_URL,
                "records": len(nonsingle_products),
                "authority": "official-public-download",
                "purpose": "expansion/language evidence without per-product HTML scraping",
            },
            "optcgapi": {
                "enabled": False,
                "status": "retired-v3.13.0",
                "replacement": "OPlayTCG multilingual physical-printing catalogue",
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
            "bandaiIdentityDetected": all_identity_quarantined,
            "bandaiIdentityQuarantined": identity_quarantined_final,
            "bandaiIdentityResolvedDuringRun": identity_resolved_during_run,
            "bandaiIdentitySummary": {
                "detected": len(all_identity_quarantined),
                "quarantined": len(identity_quarantined_final),
                "resolvedDuringRun": len(identity_resolved_during_run),
                "reasonCounts": dict(sorted(Counter(item.get("reason") for item in all_identity_quarantined).items())),
            },
            "semanticAliasesMoved": len(review.get("semanticAliasesMoved", [])),
            "semanticDriftQuarantined": review.get("semanticDriftQuarantined", []),
            "seriesExpansionProfiles": series_expansion_profiles,
            "inferredReleaseProfiles": review.get("inferredReleaseProfiles", {}),
            "needsReview": len(review.get("needsReview", [])),
        },
        "oplayIntegration": {
            "stats": oplay_stats,
            "mappingReviewPending": oplay_mapping_review.get("pendingCount", 0),
            "mappingFile": f"data/{OPLAY_CARDMARKET_MAPPING_FILENAME}",
            "reviewFile": f"output/{OPLAY_MAPPING_REVIEW_FILENAME}",
            "cardmarketMetadataMigration": cardmarket_metadata_stats,
            "identityPolicy": "merge only deterministic unique matches; never guess by price/date/product order",
        },
        "cardmarketExpansionMetadata": {
            "stats": expansion_metadata_stats,
            "verified": {str(key): value for key, value in sorted(CARDMARKET_VERIFIED_EXPANSIONS.items())},
        },
        "bandaiLinkedCardmarketVariants": {
            "candidateDiscovery": bandai_variant_candidate_stats,
            "legacyReferenceImages": linked_legacy_reference_stats,
            "added": linked_variant_stats,
        },
        "supplementalCardmarket": supplemental_stats,
        "sets": {
            "officialBandaiSetCount": sets_index.get("officialBandaiSetCount"),
            "oplaySetCount": sets_index.get("oplaySetCount", 0),
            "cardmarketExpansionCount": sets_index.get("cardmarketExpansionCount"),
            "total": len(sets_index.get("sets", [])),
        },
        "priceHistory": history_stats,
        "images": {
            "storageMode": args.image_storage_mode,
            "policy": "exact printing image only; OPlay/Bandai remote by default; no sibling/entity fallback",
            "remoteOPlayImages": len({row.get("imageUrl") for row in oplay_printings if row.get("imageUrl")}),
            "oplayExactPrintingImagesInCatalog": oplay_stats.get("printingsWithOPlayImage", 0),
            "bandaiPersistentAssets": bandai_asset_stats,
            "oplayPersistentAssets": oplay_asset_stats,
            "cardmarketDiscovery": cardmarket_image_discovery_stats,
            "cardmarketDiscoveryStatus": "retired in favour of OPlay exact-printing mapping",
        },
        "output": catalogue_stats,
        "clientCatalog": client_compaction_stats,
        "fingerprints": {
            "catalog": hash_payload(client_catalog),
            "mapping": hash_payload(mapping),
            "oplayMapping": hash_payload(oplay_mapping),
            "sets": hash_payload(sets_index),
            "priceHistory": hash_payload(price_history) if isinstance(price_history, dict) else None,
        },
    }

    catalog_path = args.output_dir / CATALOG_FILENAME
    report_path = args.output_dir / REPORT_FILENAME
    review_path = args.output_dir / REVIEW_FILENAME
    sets_path = args.output_dir / SETS_FILENAME
    manifest_path = args.output_dir / MANIFEST_FILENAME
    storage_report_path = args.output_dir / STORAGE_REPORT_FILENAME
    save_json_compact(catalog_path, client_catalog)
    save_json(review_path, review)
    save_json(sets_path, sets_index)
    if isinstance(price_history, dict):
        save_json(history_path, price_history)

    manifest = build_catalog_manifest(
        client_catalog,
        sets_index,
        price_history if isinstance(price_history, dict) else None,
        generated_at=generated_at,
        catalog_path=catalog_path,
        sets_path=sets_path,
        price_history_path=history_path if isinstance(price_history, dict) else None,
    )
    save_json(manifest_path, manifest)
    # Write report once so its size is included in the storage audit, then append a
    # compact storage summary and write it again.
    save_json(report_path, report)
    storage_report = build_storage_report(
        args.raw_dir, args.data_dir, args.output_dir, args.image_dir,
        oplay_raw=oplay_raw, image_storage_mode=args.image_storage_mode,
    )
    save_json(storage_report_path, storage_report)
    report["storage"] = {
        "reportFile": f"output/{STORAGE_REPORT_FILENAME}",
        "workingDataBytes": storage_report.get("workingDataBytes", 0),
        "workingDataMiB": storage_report.get("workingDataMiB", 0),
        "gitRepositoryBytes": (storage_report.get("gitRepository") or {}).get("bytes", 0),
        "folders": {
            key: {"files": value.get("files", 0), "bytes": value.get("bytes", 0), "mib": value.get("mib", 0)}
            for key, value in (storage_report.get("folders") or {}).items()
        },
    }
    save_json(report_path, report)

    print("\nGeneración completada (V3.13.0):")
    print(f"- Cartas totales catálogo: {catalogue_stats['cards']}")
    print(f"- Impresiones físicas totales: {catalogue_stats['printings']} (visibles={catalogue_stats['visiblePrintings']} / market rows ocultas={catalogue_stats['hiddenMarketPrintings']})")
    print(f"- Bandai: {catalogue_stats['bandaiCards']} cartas / {catalogue_stats['bandaiPrintings']} printings")
    print(f"- OPlay fuente: {oplay_stats.get('sourceCards', 0)} cartas / {oplay_stats.get('sourcePrintings', 0)} printings")
    print(f"  · idiomas: {json.dumps(oplay_stats.get('languages', {}), ensure_ascii=False, sort_keys=True)}")
    print(f"  · matches Bandai exactos: {oplay_stats.get('bandaiPrintingsMatched', 0)}")
    print(f"  · matches Cardmarket exactos: {oplay_stats.get('cardmarketProductsMapped', 0)}")
    print(f"  · entidades legacy Cardmarket fusionadas: {oplay_stats.get('legacyMarketEntitiesMergedIntoOPlay', 0)}")
    print(f"  · printings OPlay nuevas: {oplay_stats.get('oplayPrintingsAdded', 0)}")
    print(f"  · entidades OPlay-only nuevas: {oplay_stats.get('oplayOnlyEntitiesAdded', 0)}")
    print(f"  · Cardmarket ambiguas ocultas, sin adivinar: {oplay_stats.get('cardmarketProductsHiddenAsAmbiguousMarketRows', 0)}")
    print(f"  · mappings OPlay pendientes de revisión: {oplay_mapping_review.get('pendingCount', 0)}")
    print(f"- Cardmarket: Price Guide en {catalogue_stats['printingsWithCardmarketPriceGuide']} rows / valoración EUR en {catalogue_stats['printingsWithCardmarketValuation']}")
    print(f"- Imágenes: modo={args.image_storage_mode}; OPlay URLs exactas remotas={len({row.get('imageUrl') for row in oplay_printings if row.get('imageUrl')})}")
    if args.image_storage_mode == "cache":
        print(f"  · Bandai cache: {bandai_asset_stats.get('reused', 0)} reutilizadas / {bandai_asset_stats.get('downloaded', 0)} nuevas")
        print(f"  · OPlay cache: {oplay_asset_stats.get('reused', 0)} reutilizadas / {oplay_asset_stats.get('downloaded', 0)} nuevas")
    print(f"- OPTCGAPI: RETIRADO; no se consulta ni se publica raw/optcgapi_images_raw.json")
    print(f"- Sets/releases indexados: {len(sets_index.get('sets', []))} (OPlay={sets_index.get('oplaySetCount', 0)})")
    if isinstance(price_history, dict):
        print(f"- Histórico precios: {history_stats.get('daysStored', 0)} días; snapshot {history_stats.get('snapshotDate', 'sin cambio')}")
    print(f"- Mappings Bandai/Cardmarket pendientes: {len(review.get('needsReview', []))}")
    print(f"- QA identidad Bandai: {len(all_identity_quarantined)} detectadas / {len(identity_quarantined_final)} en cuarentena / {len(identity_resolved_during_run)} resueltas")
    print(f"- Storage working tree (raw+data+output+images): {storage_report.get('workingDataMiB', 0):.3f} MiB")
    for folder_name, folder_stats in (storage_report.get('folders') or {}).items():
        print(f"  · {folder_name}/: {folder_stats.get('files', 0)} ficheros / {folder_stats.get('mib', 0):.3f} MiB")
    print(f"- Git .git/: {(storage_report.get('gitRepository') or {}).get('mib', 0):.3f} MiB")
    print(f"- Catálogo cliente V3.13: {client_compaction_stats.get('clientBytes', 0) / (1024 * 1024):.3f} MiB (reducción={client_compaction_stats.get('reductionPercent', 0):.1f}% vs representación interna)")
    print(f"- Price Guide createdAt: {price_created_at}")

    if not args.no_push:
        publish_paths = [
            args.raw_dir / RAW_FILENAMES["bandai"],
            args.raw_dir / RAW_FILENAMES["cardmarket_products"],
            args.raw_dir / RAW_FILENAMES["cardmarket_prices"],
            args.raw_dir / RAW_FILENAMES["cardmarket_nonsingles"],
            args.raw_dir / RAW_FILENAMES["oplay"],
            args.data_dir / MAPPING_FILENAME,
            args.data_dir / CARDMARKET_PRINTING_METADATA_FILENAME,
            oplay_mapping_path,
            catalog_path,
            report_path,
            review_path,
            oplay_review_path,
            storage_report_path,
            sets_path,
            manifest_path,
        ]
        if history_path.exists():
            publish_paths.append(history_path)
        if args.image_storage_mode == "cache" and not args.no_persist_images:
            publish_paths.extend([
                args.data_dir / IMAGE_CACHE_FILENAME,
                args.data_dir / IMAGE_ASSET_MANIFEST_FILENAME,
                args.image_dir,
            ])
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
