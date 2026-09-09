#!/usr/bin/env python3
"""
One Piece TCG catalogue pipeline v3.1.

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
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

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
        "Mozilla/5.0 (compatible; OPTCG-Catalogue/3.1; "
        "+https://github.com/)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

CARD_CODE_RE = re.compile(r"\b(?:OP\d{2}|EB\d{2}|ST\d{2}|P)-\d{3}\b", re.I)
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
        help="Pausa entre peticiones de series Bandai (segundos).",
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


def fetch_bandai_raw(session: requests.Session, delay_seconds: float) -> dict:
    last_error = None
    selected_url = None
    index_html = None
    series = None

    for candidate_url in BANDAI_CARDLIST_URLS:
        try:
            print(f"Probando Bandai oficial: {candidate_url}")
            candidate_html = fetch_text(session, candidate_url)
            candidate_series = discover_bandai_series(candidate_html)
            selected_url = candidate_url
            index_html = candidate_html
            series = candidate_series
            break
        except Exception as error:  # network + HTML shape
            last_error = error
            print(f"Bandai no usable en {candidate_url}: {error}")

    if not selected_url or index_html is None or not series:
        raise RuntimeError(f"No se pudo acceder al cardlist oficial de Bandai: {last_error}")

    all_records = []
    series_reports = []
    prefetched = {}

    # Fail fast against a known, stable booster instead of wasting 60 requests
    # when Bandai has changed its query/HTML contract.
    smoke_item = next((x for x in series if x["id"] == "569116"), series[0])
    print(f"Smoke test Bandai: series={smoke_item['id']}")
    smoke_records, smoke_diag = fetch_bandai_series_records(
        session, selected_url, smoke_item
    )
    if not smoke_records:
        raise RuntimeError(
            "Bandai respondió pero no pudimos extraer cartas del smoke test "
            f"series={smoke_item['id']}. Diagnóstico: "
            + json.dumps(smoke_diag, ensure_ascii=False)
        )
    prefetched[smoke_item["id"]] = (smoke_records, smoke_diag)
    print(f"Smoke test Bandai OK: {len(smoke_records)} printings")

    for position, item in enumerate(series, start=1):
        print(
            f"Bandai {position}/{len(series)}: {item['label']} "
            f"(series={item['id']})"
        )
        if item["id"] in prefetched:
            records, diagnostics = prefetched[item["id"]]
        else:
            records, diagnostics = fetch_bandai_series_records(
                session, selected_url, item
            )

        if not records:
            print(
                f"AVISO: Bandai devolvió 0 cartas utilizables para "
                f"series={item['id']} | diagnóstico="
                + json.dumps(diagnostics, ensure_ascii=False)
            )
        all_records.extend(records)
        series_reports.append(
            {**item, "records": len(records), "diagnostics": diagnostics}
        )
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    nonempty_series = sum(1 for row in series_reports if row["records"] > 0)
    if len(all_records) < 1000 or nonempty_series < max(10, len(series) // 2):
        raise RuntimeError(
            "Bandai devolvió un catálogo anormalmente pequeño; se aborta para "
            "no publicar datos parciales. "
            f"records={len(all_records)}, seriesConCartas={nonempty_series}/"
            f"{len(series)}"
        )

    return {
        "source": "Bandai official One Piece Card Game cardlist",
        "sourceUrl": selected_url,
        "fetchedAt": utc_now_iso(),
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
        "schemaVersion": 2,
        "description": (
            "Persistent mapping from physical printing IDs to Cardmarket idProduct, "
            "including legacy release metadata used to disambiguate Bandai reprints. "
            "Prices are never stored here."
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
    """Bandai art identity, ignoring legacy reprint suffixes such as _R1."""
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

    # Common legacy slug vs official Bandai wording aliases.
    event_match = re.fullmatch(r"eventpack0*(\d+)", legacy)
    if event_match and f"eventpackvol{int(event_match.group(1))}" in haystack:
        return True
    return False


def unresolved_release_key(record: dict) -> str:
    source_pid = canonical_id(record.get("sourcePrintingId"))
    release_text = "|".join(
        str(x or "")
        for x in (record.get("seriesLabel"), record.get("cardSetsText"))
    )
    digest = hashlib.sha1(release_text.encode("utf-8")).hexdigest()[:8].upper()
    return f"{source_pid}~BANDAI-RELEASE~{digest}"


def resolve_mapping_key_for_bandai_record(record: dict, mapping_entries: dict) -> str:
    """
    Resolve a Bandai record to our persistent physical-printing key.

    Reprints can reuse the exact same Bandai image/sourcePrintingId. Legacy
    Cardmarket keys such as OP01-120_P2_R1 preserve that physical distinction.
    We prefer a unique release/set match; if several historical printings share
    the same art and the release cannot be proven, create a stable unresolved
    key rather than attaching the wrong Cardmarket price.
    """
    source_pid = canonical_id(record.get("sourcePrintingId"))
    art = art_identity(source_pid)
    candidates = [
        (canonical_id(key), entry)
        for key, entry in mapping_entries.items()
        if isinstance(entry, dict) and art_identity(key) == art
    ]
    if not candidates:
        return source_pid

    release_matches = [
        key for key, entry in candidates
        if legacy_set_matches_bandai_record(entry.get("legacySet"), record)
    ]
    if len(release_matches) == 1:
        return release_matches[0]

    exact_entry = mapping_entries.get(source_pid)
    if isinstance(exact_entry, dict):
        if legacy_set_matches_bandai_record(exact_entry.get("legacySet"), record):
            return source_pid
        if len(candidates) == 1:
            return source_pid

    if len(candidates) == 1:
        return candidates[0][0]

    return unresolved_release_key(record)


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
        resolved_id = resolve_mapping_key_for_bandai_record(card, mapping_entries)
        bandai_by_base[base_code(card.get("sourcePrintingId"))].append((resolved_id, card))

    auto_added = []
    review_items = []

    for base in sorted(bandai_by_base):
        source_ids = sorted({resolved_id for resolved_id, _ in bandai_by_base[base]})
        missing_ids = [
            pid
            for pid in source_ids
            if pid not in mapping_entries
            or get_number((mapping_entries.get(pid) or {}).get("productId")) is None
        ]
        if not missing_ids:
            continue

        candidates = products_by_base.get(base, [])
        unmapped_candidate_products = [
            p for p in candidates
            if int(p["idProduct"]) not in {
                int(e.get("productId"))
                for e in mapping_entries.values()
                if isinstance(e, dict) and get_number(e.get("productId")) is not None
            }
        ]

        # Only auto-map the genuinely trivial case: one Bandai printing missing,
        # one current Cardmarket product whose own product name contains the exact
        # card number. Anything involving V.1/V.2 ordering is left for review.
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
            auto_added.append({"printingId": printing_id, "productId": product["idProduct"]})
            continue

        for printing_id in missing_ids:
            bandai_examples = [
                c for resolved_id, c in bandai_by_base[base]
                if resolved_id == printing_id
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


def build_catalog(
    bandai_cards: list[dict],
    mapping: dict,
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
    image_cache: dict,
) -> tuple[dict, dict]:
    by_base = defaultdict(list)
    for record in bandai_cards:
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

    for base in sorted(by_base):
        records = by_base[base]
        canonical = choose_canonical_record(records, base)

        mechanics = {}
        for field in ("name", "category", "life", "cost", "power", "counter", "colors", "attributes", "types", "effect", "trigger"):
            values = {
                json.dumps(r.get(field), ensure_ascii=False, sort_keys=True)
                for r in records
                if r.get(field) not in (None, "", [], {})
            }
            if len(values) > 1:
                mechanics[field] = [json.loads(v) for v in sorted(values)]
        if mechanics:
            mechanic_conflicts.append({"baseCode": base, "fields": mechanics})

        grouped_printings = defaultdict(list)
        for record in records:
            resolved_printing_id = resolve_mapping_key_for_bandai_record(
                record, mapping_entries
            )
            grouped_printings[resolved_printing_id].append(record)

        printings = []
        for printing_id in sorted(grouped_printings):
            same_id_records = grouped_printings[printing_id]
            by_image = defaultdict(list)
            for record in same_id_records:
                by_image[str(record.get("imageUrl") or "")].append(record)

            for image_index, (image_url, image_records) in enumerate(sorted(by_image.items()), start=1):
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

                best = max(image_records, key=richness)
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
                    # When image checks are intentionally skipped, keep the official
                    # URL; otherwise a cached explicit failure suppresses it.
                    safe_image_url = image_url
                elif image_status.get("ok"):
                    safe_image_url = image_status.get("finalUrl") or image_url
                else:
                    safe_image_url = None
                    printings_without_valid_image += 1

                map_entry = mapping_entries.get(printing_id)
                cm = None
                if isinstance(map_entry, dict) and get_number(map_entry.get("productId")) is not None:
                    printings_with_mapping += 1
                    product_id = int(get_number(map_entry.get("productId")))
                    product = products_by_id.get(product_id)
                    price = prices_by_id.get(product_id)
                    if price:
                        printings_with_price += 1
                    cm = {
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

                bandai_source_ids = sorted(
                    {canonical_id(r.get("sourcePrintingId")) for r in image_records}
                )
                printings.append(
                    {
                        "id": internal_id,
                        "printingId": printing_id,
                        "sourcePrintingId": bandai_source_ids[0] if bandai_source_ids else None,
                        "bandaiSourcePrintingIds": bandai_source_ids,
                        "baseCode": base,
                        "variantType": variant_type(printing_id, best.get("displayName"), best.get("rarity")),
                        "isParallel": bool(best.get("isParallel")) or bool(re.search(r"_P\d+", printing_id)),
                        "isReprint": bool(best.get("isReprint")) or bool(re.search(r"_R\d+", printing_id)),
                        "rarity": best.get("rarity"),
                        "imageUrl": safe_image_url,
                        "imageSourceUrl": image_url or None,
                        "imageHealth": image_status,
                        "releases": releases,
                        "cardmarket": cm,
                        "source": "bandai",
                    }
                )

        printings.sort(key=natural_printing_sort_key)
        output[base] = {
            "code": base,
            "game": "One Piece",
            "name": canonical.get("name") or base,
            "rarity": canonical.get("rarity"),
            "type": canonical.get("category"),
            "life": canonical.get("life"),
            "cost": canonical.get("cost"),
            "power": canonical.get("power"),
            "counter": canonical.get("counter"),
            "colors": canonical.get("colors") or [],
            "attributes": canonical.get("attributes") or [],
            "block": canonical.get("block"),
            "types": canonical.get("types") or [],
            "effect": canonical.get("effect"),
            "trigger": canonical.get("trigger"),
            "sources": ["bandai", *( ["cardmarket"] if any(p.get("cardmarket") for p in printings) else [] )],
            "printings": printings,
        }

    stats = {
        "cards": len(output),
        "printings": sum(len(card["printings"]) for card in output.values()),
        "printingsWithCardmarketMapping": printings_with_mapping,
        "printingsWithCardmarketPrice": printings_with_price,
        "printingsWithoutValidatedImage": printings_without_valid_image,
        "bandaiPrintingIdImageCollisions": collisions,
        "mechanicConflicts": mechanic_conflicts,
    }
    return output, stats


# ---------------------------------------------------------------------------
# Raw source orchestration
# ---------------------------------------------------------------------------


def fetch_live_raw(session: requests.Session, bandai_delay: float) -> dict:
    print("Descargando Bandai oficial...")
    bandai = fetch_bandai_raw(session, bandai_delay)

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

    # Regression: a Bandai art reused in PRB01 must resolve to the legacy
    # reprint key instead of inheriting the original product's price.
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
        "sourcePrintingId": "OP01-120_P2",
        "seriesLabel": "ONE PIECE CARD THE BEST",
        "cardSetsText": "-ONE PIECE CARD THE BEST- [PRB-01]",
    }
    assert resolve_mapping_key_for_bandai_record(original, reprint_mapping) == "OP01-120_P2"
    assert resolve_mapping_key_for_bandai_record(reprint, reprint_mapping) == "OP01-120_P2_R1"
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
        raw_data = fetch_live_raw(session, args.bandai_delay)
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
