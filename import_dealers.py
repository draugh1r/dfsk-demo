import os
import re
import argparse
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker, declarative_base, Session


# =========================
# Config DB
# =========================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./beatrice.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# =========================
# Modello Dealer (stesso schema del main)
# =========================

class Dealer(Base):
    __tablename__ = "dealers"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, nullable=False)          # NOME AZIENDA
    address_showroom = Column(String, nullable=True)       # SHOW ROOM
    address_officina = Column(String, nullable=True)       # OFFICINA
    city = Column(String, nullable=False, default="")      # non la usiamo per la logica, ok vuota
    provincia = Column(String, nullable=False)             # es. "CR"
    phone_vendita = Column(String, nullable=True)          # Tell. VENDITA
    phone_officina = Column(String, nullable=True)         # Tell. OFFICINA
    email = Column(String, nullable=True)                  # REFERENTE / email generica


# =========================
# Funzione import
# =========================

def safe_get(row, col_name: str) -> str:
    """Prende una cella come stringa pulita (o "")."""
    if col_name not in row:
        return ""
    val = row[col_name]
    if val is None:
        return ""
    return str(val).strip()


def pick_email(referente: str, rif_vendita: str, rif_servizio: str) -> Optional[str]:
    """
    Prova a ricavare una mail dai campi di riferimento.
    Se non trova '@' da nessuna parte, restituisce None.
    """
    for candidate in (rif_vendita, rif_servizio, referente):
        if candidate and "@" in candidate:
            return candidate
    return None


def extract_city_from_address(address: str) -> str:
    """Estrae il comune da indirizzi tipo '..., 26013 Crema CR'."""
    address = (address or "").strip()
    if not address:
        return ""
    match = re.search(r"\b\d{5}\s+(.+?)\s+\(?[A-Z]{2}\)?\s*$", address, re.IGNORECASE)
    if match:
        return match.group(1).strip().title()
    return ""


def import_dealers(xlsx_path: str, truncate: bool = False) -> None:
    print(f"[IMPORT] Leggo file Excel: {xlsx_path}")

    # Qui il fix importante: niente NaN automatici tipo "NA"
    df = pd.read_excel(
        xlsx_path,
        dtype=str,            # tutto stringa
        keep_default_na=False # non trasformare "NA", "N/A" ecc. in NaN
    )

    print("[IMPORT] Colonne trovate nel file:")
    print(list(df.columns))

    # Adatta qui i nomi colonna alle intestazioni reali del tuo file
    col_azienda = "NOME AZIENDA"
    col_sede_legale = "SEDE LEGALE"
    col_provincia = "Provincia (formula)"
    col_showroom = "SHOW ROOM"
    col_officina = "OFFICINA"
    col_tel_vendita = "Tell. VENDITA"
    col_tel_officina = "Tell. OFFICINA"
    col_referente = "REFERENTE"
    col_rif_vendita = "RIFERIMENTO VENDITA"
    col_rif_servizio = "RIFERIMENTO SERVIZIO"

    # Crea tabelle se non esistono
    Base.metadata.create_all(bind=engine)

    session: Session = SessionLocal()

    try:
        if truncate:
            print("[IMPORT] Pulisco la tabella dealers...")
            session.query(Dealer).delete()
            session.commit()

        inserted = 0

        for idx, row in df.iterrows():
            # idx parte da 0 ma in Excel la prima riga con dati è di solito la 2
            row_num = idx + 2

            azienda = safe_get(row, col_azienda)
            sede_legale = safe_get(row, col_sede_legale)
            showroom = safe_get(row, col_showroom)
            officina = safe_get(row, col_officina)

            provincia = safe_get(row, col_provincia).upper()
            tel_vendita = safe_get(row, col_tel_vendita)
            tel_officina = safe_get(row, col_tel_officina)
            referente = safe_get(row, col_referente)
            rif_vendita = safe_get(row, col_rif_vendita)
            rif_servizio = safe_get(row, col_rif_servizio)

            # Normalizzazione telefoni (opzionale, togli spazi)
            tel_vendita = tel_vendita.replace(" ", "") if tel_vendita else ""
            tel_officina = tel_officina.replace(" ", "") if tel_officina else ""

            # Controlli minimi

            if not azienda:
                print(
                    f"[IMPORT] Riga {row_num} saltata: nome azienda mancante "
                    f"(sede_legale={sede_legale}, provincia={provincia})"
                )
                continue

            if not provincia:
                print(
                    f"[IMPORT] Riga {row_num} saltata: provincia mancante "
                    f"(azienda={azienda}, sede_legale={sede_legale}, showroom={showroom}, officina={officina})"
                )
                continue

            if not (sede_legale or showroom or officina):
                print(
                    f"[IMPORT] Riga {row_num} saltata: nessun indirizzo presente "
                    f"(azienda={azienda}, provincia={provincia})"
                )
                continue

            # City: estrai dal showroom, officina o sede legale
            city = (
                extract_city_from_address(showroom)
                or extract_city_from_address(officina)
                or extract_city_from_address(sede_legale)
            )

            # Decidiamo email (se c'è)
            email = pick_email(referente, rif_vendita, rif_servizio)

            dealer = Dealer(
                company_name=azienda,
                address_showroom=showroom or None,
                address_officina=officina or None,
                city=city,
                provincia=provincia,
                phone_vendita=tel_vendita or None,
                phone_officina=tel_officina or None,
                email=email,
            )

            session.add(dealer)
            inserted += 1

        session.commit()
        print(f"[IMPORT] Import completato, righe inserite: {inserted}")

    finally:
        session.close()

    # Conteggio totale in tabella
    session2: Session = SessionLocal()
    try:
        total = session2.query(Dealer).count()
    finally:
        session2.close()

    print(f"[IMPORT] Totale record in dealers: {total}")


# =========================
# CLI
# =========================

def main():
    parser = argparse.ArgumentParser(
        description="Importa la tabella dealers da un file Excel DFSK."
    )
    parser.add_argument(
        "xlsx_path",
        help="Percorso al file .xlsx delle concessionarie (es. ./concessionarie_dfskt.xlsx)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Svuota la tabella dealers prima di importare",
    )

    args = parser.parse_args()

    import_dealers(args.xlsx_path, truncate=args.reset)


if __name__ == "__main__":
    main()
