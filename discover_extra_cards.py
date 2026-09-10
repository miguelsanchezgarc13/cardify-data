#!/usr/bin/env python3
"""
One Piece TCG catalogue pipeline v3.7.

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
        "Mozilla/5.0 (compatible; OPTCG-Catalogue/3.7; "
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
# Cardmarket-only supplemental catalogue (standard codes missing in Bandai + DON!!)
# ---------------------------------------------------------------------------

DON_PRODUCT_RE = re.compile(r"^\s*DON!!(?:\s|\(|$)", re.I)


def _is_cardmarket_don_product(product: dict) -> bool:
    return bool(DON_PRODUCT_RE.search(str((product or {}).get("name") or "")))


def _cardmarket_price_payload(
    product_id: int,
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
) -> dict | None:
    price = prices_by_id.get(int(product_id))
    if not price:
        return None
    valuation = (
        price.get("trend")
        if price.get("trend") is not None
        else price.get("avg7")
        if price.get("avg7") is not None
        else price.get("avg30")
        if price.get("avg30") is not None
        else price.get("avg")
    )
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
) -> dict:
    product_id = int(product["idProduct"])
    direct_id = f"CM-{product_id}"
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
        "imageUrl": None,
        "imageSourceUrl": None,
        "imageHealth": None,
        "releases": [
            {
                "seriesId": None,
                "seriesLabel": None,
                "cardSetsText": None,
                "cardmarketExpansionId": product.get("idExpansion"),
            }
        ],
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
            "price": _cardmarket_price_payload(product_id, prices_by_id, price_created_at),
        },
        "source": "cardmarket",
        "catalogOrigin": "cardmarket-only",
        "collectibleType": collectible_type,
    }


def add_cardmarket_supplements(
    catalog: dict,
    products_by_id: dict[int, dict],
    prices_by_id: dict[int, dict],
    price_created_at: str | None,
) -> dict:
    """Add market-only standard codes and DON!! designs without weakening Bandai identity.

    Standard card codes absent from Bandai are grouped by their printed code.  They
    remain explicitly market-only: mechanics and official images are unknown until
    Bandai publishes them.  DON!! has no stable Bandai card code in this pipeline, so
    each Cardmarket idMetacard is used as a stable design identity and each idProduct
    remains a distinct direct Cardmarket product.
    """
    bandai_codes = set(catalog)
    existing_product_ids = {
        int(cm["productId"])
        for card in catalog.values()
        for printing in card.get("printings", [])
        for cm in [printing.get("cardmarket")]
        if isinstance(cm, dict) and get_number(cm.get("productId")) is not None
    }

    standard_groups = defaultdict(list)
    don_groups = defaultdict(list)

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
        if code and code not in bandai_codes:
            standard_groups[code].append(product)

    standard_products = 0
    standard_priced = 0
    for code in sorted(standard_groups):
        rows = sorted(standard_groups[code], key=lambda x: int(x["idProduct"]))
        names = [nullable_text(row.get("name")) for row in rows if nullable_text(row.get("name"))]
        display_name = names[0] if names else code
        # Strip the trailing printed code while preserving the Cardmarket name.
        display_name = re.sub(r"\s*\(" + re.escape(code) + r"\)\s*$", "", display_name, flags=re.I).strip() or code
        printings = [
            _direct_cardmarket_printing(
                row, prices_by_id, price_created_at, code, collectible_type="standard-card"
            )
            for row in rows
        ]
        standard_products += len(printings)
        standard_priced += sum(
            1 for printing in printings
            if isinstance(printing.get("cardmarket"), dict)
            and printing["cardmarket"].get("price") is not None
        )
        catalog[code] = {
            "code": code,
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
            "sources": ["cardmarket"],
            "catalogOrigin": "cardmarket-only",
            "bandaiCanonical": False,
            "dataCompleteness": "market-only",
            "cardmarketMetacardIds": sorted({
                int(v) for row in rows
                for v in [get_number(row.get("idMetacard"))]
                if v is not None
            }),
            "printings": printings,
        }

    don_products = 0
    don_priced = 0
    for metacard in sorted(don_groups):
        rows = sorted(don_groups[metacard], key=lambda x: int(x["idProduct"]))
        key = f"DON-CM-{metacard}"
        name = nullable_text(rows[0].get("name")) or "DON!!"
        printings = [
            _direct_cardmarket_printing(
                row, prices_by_id, price_created_at, key, collectible_type="don"
            )
            for row in rows
        ]
        don_products += len(printings)
        don_priced += sum(
            1 for printing in printings
            if isinstance(printing.get("cardmarket"), dict)
            and printing["cardmarket"].get("price") is not None
        )
        catalog[key] = {
            "code": key,
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
            "sources": ["cardmarket"],
            "catalogOrigin": "cardmarket-only",
            "bandaiCanonical": False,
            "dataCompleteness": "market-only",
            "collectibleType": "don",
            "cardmarketMetacardId": metacard,
            "printings": printings,
        }

    return {
        "standardCards": len(standard_groups),
        "standardProducts": standard_products,
        "standardProductsWithPrice": standard_priced,
        "standardCodes": sorted(standard_groups),
        "donCards": len(don_groups),
        "donProducts": don_products,
        "donProductsWithPrice": don_priced,
    }


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

    # V3.8 regression: Cardmarket-only standard codes and DON!! are additive
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
    assert supplement_catalog["P-999"]["catalogOrigin"] == "cardmarket-only"
    assert supplement_catalog["P-999"]["printings"][0]["cardmarket"]["price"]["valuationEur"] == 2.5
    assert supplement_catalog["DON-CM-9002"]["type"] == "Don"
    assert supplement_catalog["DON-CM-9002"]["printings"][0]["cardmarket"]["price"]["valuationEur"] == 3.5

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
        # Rebuild the review after the firewall removed a late mapping. Safety
        # reconciliation remains active, but no new mappings are introduced.
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
    bandai_catalogue_stats = dict(catalogue_stats)
    supplemental_stats = add_cardmarket_supplements(
        catalog, products_by_id, prices_by_id, price_created_at
    )
    catalogue_stats.update({
        "bandaiCards": bandai_catalogue_stats["cards"],
        "bandaiPrintings": bandai_catalogue_stats["printings"],
        "bandaiPrintingsWithCardmarketMapping": bandai_catalogue_stats["printingsWithCardmarketMapping"],
        "bandaiPrintingsWithCardmarketPrice": bandai_catalogue_stats["printingsWithCardmarketPrice"],
        "cardmarketOnlyCards": supplemental_stats["standardCards"],
        "cardmarketOnlyProducts": supplemental_stats["standardProducts"],
        "donCards": supplemental_stats["donCards"],
        "donProducts": supplemental_stats["donProducts"],
        "cards": len(catalog),
        "printings": sum(len(card.get("printings", [])) for card in catalog.values()),
        "printingsWithCardmarketPrice": (
            bandai_catalogue_stats["printingsWithCardmarketPrice"]
            + supplemental_stats["standardProductsWithPrice"]
            + supplemental_stats["donProductsWithPrice"]
        ),
    })

    report = {
        "generatedAt": utc_now_iso(),
        "schemaVersion": 4,
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
            "bandaiIdentityDetected": all_identity_quarantined,
            "bandaiIdentityQuarantined": identity_quarantined_final,
            "bandaiIdentityResolvedDuringRun": identity_resolved_during_run,
            "bandaiIdentitySummary": {
                "detected": len(all_identity_quarantined),
                "quarantined": len(identity_quarantined_final),
                "resolvedDuringRun": len(identity_resolved_during_run),
                "reasonCounts": dict(sorted(Counter(
                    item.get("reason") for item in all_identity_quarantined
                ).items())),
            },
            "semanticAliasesMoved": len(review.get("semanticAliasesMoved", [])),
            "semanticDriftQuarantined": review.get("semanticDriftQuarantined", []),
            "seriesExpansionProfiles": series_expansion_profiles,
            "inferredReleaseProfiles": review.get("inferredReleaseProfiles", {}),
            "needsReview": len(review.get("needsReview", [])),
        },
        "supplementalCardmarket": supplemental_stats,
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
    print(f"- Cartas totales catálogo: {catalogue_stats['cards']}")
    print(f"- Cartas Bandai: {catalogue_stats['bandaiCards']}")
    print(f"- Cartas solo Cardmarket: {catalogue_stats['cardmarketOnlyCards']}")
    print(f"- Diseños DON!! Cardmarket: {catalogue_stats['donCards']}")
    print(f"- Impresiones/productos totales: {catalogue_stats['printings']}")
    print(f"- Impresiones físicas Bandai: {catalogue_stats['bandaiPrintings']}")
    print(f"- Productos solo Cardmarket: {catalogue_stats['cardmarketOnlyProducts']}")
    print(f"- Productos DON!!: {catalogue_stats['donProducts']}")
    print(f"- Bandai con mapping Cardmarket: {catalogue_stats['bandaiPrintingsWithCardmarketMapping']}")
    print(f"- Con precio Cardmarket actual (total): {catalogue_stats['printingsWithCardmarketPrice']}")
    print(f"- Mappings pendientes de revisión: {len(review.get('needsReview', []))}")
    print(f"- Auto mappings añadidos: {len(review.get('autoMappingsAdded', []))}")
    print(f"- Alias semánticos migrados: {len(review.get('semanticAliasesMoved', []))}")
    print(
        "- Mapping QA: "
        f"{len(mapping_validation.get('repaired', []))} reparados / "
        f"{len(mapping_validation.get('quarantined', []))} en cuarentena"
    )
    print(
        "- QA identidad Bandai V3.8: "
        f"{len(all_identity_quarantined)} detectadas / "
        f"{len(identity_quarantined_final)} siguen en cuarentena / "
        f"{len(identity_resolved_during_run)} resueltas en el run"
    )
    print(
        "- QA drift semántico V3.8: "
        f"{len(review.get('semanticDriftQuarantined', []))} en cuarentena"
    )
    print(
        "- Perfiles release inferidos: "
        f"{len(review.get('inferredReleaseProfiles', {}))}"
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
