"""Aggiorna il CSV offline dei comuni dall'elenco ufficiale ISTAT.

Eseguire solo quando si desidera aggiornare la fotografia dei comuni nel repository.
Il chatbot non chiama ISTAT durante l'uso.
"""

import csv
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import requests


SOURCE_URL = "https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.xlsx"
DESTINATION = Path(__file__).resolve().parents[1] / "data" / "comuni_italiani.csv"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _sheet_rows(document: bytes):
    with zipfile.ZipFile(io.BytesIO(document)) as workbook:
        strings_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        strings = [
            "".join(node.text or "" for node in entry.iter(f"{NS}t"))
            for entry in strings_root.findall(f"{NS}si")
        ]
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        for row in sheet_root.iter(f"{NS}row"):
            cells = {}
            for cell in row.findall(f"{NS}c"):
                column = re.match(r"[A-Z]+", cell.attrib["r"]).group()
                value = cell.find(f"{NS}v")
                if value is None:
                    continue
                text = value.text or ""
                cells[column] = strings[int(text)] if cell.attrib.get("t") == "s" else text
            yield cells


def main():
    response = requests.get(SOURCE_URL, timeout=30)
    response.raise_for_status()
    rows = _sheet_rows(response.content)
    header = next(rows)
    columns = {name: column for column, name in header.items()}
    city_column = columns["Denominazione in italiano"]
    code_column = columns["Sigla automobilistica"]
    province_column = next(
        column for name, column in columns.items()
        if name.startswith("Denominazione dell'Unità territoriale sovracomunale")
    )

    records = sorted(
        (
            row.get(city_column, "").strip(),
            row.get(code_column, "").strip().upper(),
            row.get(province_column, "").strip(),
        )
        for row in rows
    )
    if not 7800 <= len(records) <= 8500:
        raise ValueError(f"Numero di comuni inatteso: {len(records)}")
    if any(not city or len(code) != 2 or not province for city, code, province in records):
        raise ValueError("Elenco ISTAT con nomi o sigle provinciali mancanti")
    if not any(city == "Reggello" and code == "FI" for city, code, _ in records):
        raise ValueError("Controllo Reggello/FI fallito")

    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    with DESTINATION.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("comune", "provincia", "provincia_nome"))
        writer.writerows(records)
    print(f"Aggiornati {len(records)} comuni in {DESTINATION}")


if __name__ == "__main__":
    main()
