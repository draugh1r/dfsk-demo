import os
import json
import requests

from sqlalchemy import text
from sqlalchemy.orm import Session

from main import Base, engine, SessionLocal
from main import ProvinceItalia  # model con __tablename__ = "province_italia"

COMUNI_API_BASE = os.getenv("COMUNI_API_BASE", "https://cap.openapi.it").strip()
COMUNI_API_TOKEN = os.getenv("COMUNI_API_TOKEN", "").strip()


def import_provinces():
    if not COMUNI_API_TOKEN:
        raise RuntimeError(
            "COMUNI_API_TOKEN non impostato nell'ambiente, impossibile chiamare l'API cap.openapi.it"
        )

    db: Session = SessionLocal()
    try:
        print("[PROV] Drop e recreate tabella 'province_italia'")

        # 1, droppiamo SOLO la tabella delle province
        Base.metadata.drop_all(bind=engine, tables=[ProvinceItalia.__table__])

        # 2, la ricreiamo con lo schema definito nel model ProvinceItalia
        Base.metadata.create_all(bind=engine, tables=[ProvinceItalia.__table__])

        # 3, chiamiamo l'endpoint /province
        url = COMUNI_API_BASE.rstrip("/") + "/province"
        print(f"[PROV] Scarico elenco province da '{url}'")

        headers = {
            "Authorization": f"Bearer {COMUNI_API_TOKEN}",
            "Accept": "application/json",
        }

        resp = requests.get(url, headers=headers, timeout=10)
        print(f"[PROV] HTTP status={resp.status_code}")
        resp.raise_for_status()

        data = resp.json()
        print(f"[PROV] Response JSON: {json.dumps(data, ensure_ascii=False)[:400]}...")

        if not isinstance(data, dict) or "data" not in data:
            raise RuntimeError("Formato risposta inatteso da /province")

        items = data.get("data") or []
        if not isinstance(items, list):
            raise RuntimeError("Campo 'data' inatteso da /province")

        print(f"[PROV] Province ricevute: {len(items)}")

        inserted = 0
        for item in items:
            if not isinstance(item, dict):
                continue

            sigla = (item.get("sigla") or "").strip()
            provincia = (item.get("provincia") or "").strip()
            istat = (item.get("istat") or "").strip()
            regione = (item.get("regione") or "").strip() or None

            if not sigla or not provincia or not istat:
                print(
                    f"[PROV] Riga saltata per dati mancanti: "
                    f"sigla={sigla!r}, provincia={provincia!r}, istat={istat!r}"
                )
                continue

            row = ProvinceItalia(
                sigla=sigla,
                provincia=provincia,
                istat=istat,
                regione=regione,
            )
            db.add(row)
            inserted += 1

        db.commit()
        print(f"[PROV] Import completato, province inserite: {inserted}")

        # verifichiamo conteggio
        count = db.query(ProvinceItalia).count()
        print(f"[PROV] Totale record in province_italia: {count}")

    finally:
        db.close()


if __name__ == "__main__":
    print("[PROV] Creazione tabelle DB (se non esistono)")
    # In realtà la crea già sopra, ma tenerlo non fa male
    Base.metadata.create_all(bind=engine)

    import_provinces()
