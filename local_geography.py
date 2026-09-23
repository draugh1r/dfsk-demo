"""Lookup geografico offline basato sull'elenco dei comuni italiani ISTAT."""

import csv
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional


DATA_PATH = Path(__file__).parent / "data" / "comuni_italiani.csv"


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", without_accents.casefold()))


@lru_cache(maxsize=1)
def _indexes():
    comuni_exact = {}
    comuni_normalized = {}
    province = {}
    with DATA_PATH.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            city = row["comune"].strip()
            code = row["provincia"].strip().upper()
            province_name = row["provincia_nome"].strip()
            comuni_exact.setdefault(city.casefold(), set()).add((city, code))
            comuni_normalized.setdefault(_key(city), set()).add((city, code))
            province.setdefault(_key(province_name), set()).add((province_name, code))
            province.setdefault(_key(code), set()).add((province_name, code))
    return comuni_exact, comuni_normalized, province


def find_province(raw_name: str) -> Optional[dict[str, str]]:
    matches = _indexes()[2].get(_key(raw_name), set())
    if len(matches) != 1:
        return None
    name, code = next(iter(matches))
    return {"city": name, "provincia": code, "scope": "province"}


def find_comune(raw_name: str) -> Optional[dict[str, str]]:
    name = (raw_name or "").strip()
    explicit_code = None
    match = re.fullmatch(r"(.+?)\s+\(([A-Za-z]{2})\)", name)
    if not match:
        match = re.fullmatch(r"(.+?)\s+([A-Z]{2})", name)
    if match:
        name, explicit_code = match.group(1), match.group(2).upper()

    exact, normalized, _ = _indexes()
    matches = exact.get(name.casefold()) or normalized.get(_key(name), set())
    if explicit_code:
        matches = {(city, code) for city, code in matches if code == explicit_code}
    if len(matches) != 1:
        return None
    city, code = next(iter(matches))
    return {"city": city, "provincia": code, "scope": "city"}
