from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import os
import re
import json
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any, Literal

import requests
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, or_, func
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from openai import OpenAI
from local_geography import find_comune, find_province

# ---------- Config ----------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY environment variable")

client = OpenAI(api_key=OPENAI_API_KEY)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./beatrice.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------- Airtable Config ----------

AIRTABLE_BASE_URL = os.getenv("AIRTABLE_BASE_URL", "https://api.airtable.com/v0")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "app3SpIL2LfWaDGt0")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Lead")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "")


# ---------- DB models ----------


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    z_name = Column(String, nullable=True)
    z_city = Column(String, nullable=True)       # città
    z_cell = Column(String, nullable=True)
    z_email = Column(String, nullable=True)
    z_provincia = Column(String, nullable=True)
    status = Column(String, default="in_progress")  # in_progress, completed, failed
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationState(Base):
    __tablename__ = "conversation_state"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    step = Column(String, default="generic")     # solo etichetta
    attempts_json = Column(Text, default="{}")
    history_json = Column(Text, default="[]")    # [{"role": "user"|"assistant", "content": "..."}]
    extra_json = Column(Text, default="{}")      # la usiamo per lo stato delle province senza sede


class Dealer(Base):
    """
    Tabella concessionarie / sedi.
    Popola questa tabella importando i dati dall'Excel.
    """
    __tablename__ = "dealers"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, nullable=False)          # NOME AZIENDA
    address_showroom = Column(String, nullable=True)       # SHOW ROOM
    address_officina = Column(String, nullable=True)       # OFFICINA
    city = Column(String, nullable=False)                  # es. "Crema"
    provincia = Column(String, nullable=False)             # es. "CR"
    phone_vendita = Column(String, nullable=True)          # Tell. VENDITA
    phone_officina = Column(String, nullable=True)         # Tell. OFFICINA
    email = Column(String, nullable=True)                  # REFERENTE / email generica


class ProvinceItalia(Base):
    """
    Tabella province_italia popolata dallo script import_provinces.py.
    La usiamo solo per mappare l'ISTAT del comune alla sigla di provincia.
    """
    __tablename__ = "province_italia"

    id = Column(Integer, primary_key=True, index=True)
    sigla = Column(String, nullable=False)       # es "BO"
    provincia = Column(String, nullable=False)   # es "Bologna"
    istat = Column(String, nullable=False)       # es "037"
    regione = Column(String, nullable=True)      # es "Emilia-Romagna"


# ---------- Knowledge base piccoli testi sui modelli ----------

KB_DOCS = [
    {
        "id": "glory_500",
        "title": "DFSK Glory 500",
        "content": (
            "DFSK Glory 500 è il modello best seller: un SUV compatto a 5 posti, "
            "versatile per l'uso quotidiano, comodo in città e adatto anche alla famiglia. "
            "È disponibile in versione benzina o GPL."
        ),
    },
    {
        "id": "glory_600",
        "title": "DFSK Glory 600",
        "content": (
            "DFSK Glory 600 è un SUV di segmento superiore a 7 posti, più orientato al comfort "
            "e alla dotazione, pensato per chi vuole un'esperienza di guida più premium. "
            "È disponibile in versione benzina o GPL."
        ),
    },
    {
        "id": "glory_f5",
        "title": "DFSK Glory F5",
        "content": (
            "DFSK Glory F5 è un SUV dal carattere più dinamico, indicato per chi cerca "
            "uno stile più sportivo mantenendo spazio e praticità."
        ),
    },
    {
        "id": "e5_phev",
        "title": "DFSK E5 Phev",
        "content": (
            "DFSK E5 Phev è un SUV 7 posti ibrido plug-in. "
            "È pensato per chi vuole un'autonomia complessiva molto elevata (oltre 1.000 km) "
            "senza rinunciare a spazio e praticità."
        ),
    },
]

KB_EMBEDDINGS: List[List[float]] = []


def embed_texts(texts: List[str]) -> List[List[float]]:
    print(f"[EMBED] Richiesta embedding per {len(texts)} testi")
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    print("[EMBED] Embedding ottenuti")
    return [item.embedding for item in response.data]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_kb(query: str, top_k: int = 3, min_score: float = 0.7) -> List[Dict[str, Any]]:
    print(f"[KB] search_kb query={query!r}")
    if not query.strip():
        print("[KB] query vuota, ritorno []")
        return []
    q_emb = embed_texts([query])[0]
    scored = []
    for doc, emb in zip(KB_DOCS, KB_EMBEDDINGS):
        score = cosine_similarity(q_emb, emb)
        print(f"[KB] doc={doc['id']} score={score:.3f}")
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, doc in scored[:top_k]:
        if score >= min_score:
            results.append({"score": score, **doc})
    print(f"[KB] risultati finali: { [r['id'] for r in results] }")
    return results


# ---------- Helpers ----------


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits


FIELD_ORDER = ["z_name", "z_city", "z_cell", "z_email"]


def all_fields_filled(lead: "Lead") -> bool:
    return all(getattr(lead, f) for f in FIELD_ORDER)


def missing_fields(lead: "Lead") -> List[str]:
    return [f for f in FIELD_ORDER if not getattr(lead, f)]


def _clean_location_name(raw_location: str) -> str:
    """Ripulisce input come 'sedi in provincia di Mantova' lasciando solo il luogo."""
    name = (raw_location or "").strip()
    name = re.sub(
        r"^\s*(?:sedi(?:\s+dfsk)?\s+)?(?:in|nella|della)?\s*provincia\s+(?:di\s+)?",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.strip(" \t,.;:!?-")


def resolve_province_from_db(db: Optional[Session], raw_location: str) -> Optional[Dict[str, str]]:
    """Riconosce localmente nome o sigla di provincia, senza dipendere da API esterne."""
    if db is None:
        return None

    name = _clean_location_name(raw_location)
    if not name:
        return None

    normalized_code = name.upper()
    try:
        province = (
            db.query(ProvinceItalia)
            .filter(
                or_(
                    func.upper(func.trim(ProvinceItalia.sigla)) == normalized_code,
                    ProvinceItalia.provincia.ilike(name),
                )
            )
            .first()
        )
    except Exception as exc:
        print(f"[CITY] errore query locale province_italia: {exc!r}")
        return None

    if not province:
        return None

    return {
        "city": (province.provincia or name).strip(),
        "provincia": (province.sigla or "").strip().upper(),
        "scope": "province",
    }


def resolve_city(raw_city: str, db: Optional[Session] = None) -> Optional[Dict[str, str]]:
    """Risolve provincia o comune dall'elenco ISTAT salvato nel progetto."""
    print(f"[CITY] resolve_city input={raw_city!r}")
    if not raw_city:
        print("[CITY] raw_city vuota")
        return None

    name = _clean_location_name(raw_city)
    if not name:
        print("[CITY] raw_city solo spazi")
        return None

    local_province = find_province(name) or resolve_province_from_db(db, name)
    if local_province:
        print(f"[CITY] provincia riconosciuta localmente: {local_province}")
        return local_province

    info = find_comune(name)
    print(f"[CITY] comune riconosciuto localmente: {info}")
    return info


def resolve_requested_locations(
    user_message: str,
    dealer_city_raw: Optional[str],
    saved_city: Optional[str],
    db: Session,
) -> List[Dict[str, str]]:
    """Dà precedenza ai luoghi scritti ora rispetto alla città salvata nel lead."""
    for text in (user_message, dealer_city_raw):
        if not text:
            continue
        parts = re.split(r"\s+(?:o|oppure|e)\s+|[,;/]", str(text), flags=re.IGNORECASE)
        if 1 < len(parts) <= 3:
            locations = [resolve_city(part, db) for part in parts]
            if all(locations) and all(item["scope"] == "province" for item in locations):
                # Mantiene l'ordine richiesto e non mostra due volte la stessa provincia.
                return list({item["provincia"]: item for item in locations}.values())

        location = resolve_city(str(text), db)
        if location:
            return [location]

    if dealer_city_raw:
        return []
    saved_location = resolve_city(saved_city, db) if saved_city else None
    return [saved_location] if saved_location else []


def extract_city_from_address(address: str) -> str:
    """Estrae il comune da indirizzi tipo '..., 26013 Crema CR'."""
    address = (address or "").strip()
    if not address:
        return ""
    match = re.search(r"\b\d{5}\s+(.+?)\s+\(?[A-Z]{2}\)?\s*$", address, re.IGNORECASE)
    if match:
        return match.group(1).strip().title()
    return ""


def _dealer_city_filters(city: str):
    """Match città su campo city o come comune finale negli indirizzi (CAP Comune PR)."""
    city = (city or "").strip()
    end_pattern = f"% {city} __"
    parenthesized_end_pattern = f"% {city} (__)"
    return or_(
        Dealer.city.ilike(city),
        Dealer.address_showroom.ilike(end_pattern),
        Dealer.address_showroom.ilike(parenthesized_end_pattern),
        Dealer.address_officina.ilike(end_pattern),
        Dealer.address_officina.ilike(parenthesized_end_pattern),
    )


def _address_province_suffix(address: Optional[str]) -> str:
    """Estrae la sigla finale da indirizzi che terminano con 'MI' o '(MI)'."""
    match = re.search(r"(?:\s|\()([A-Z]{2})\)?\s*$", address or "", re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _dealer_location_is_consistent(dealer: Dealer) -> bool:
    """Evita di mostrare una sede se l'indirizzo contraddice la provincia censita."""
    displayed_address = dealer.address_showroom or dealer.address_officina
    address_province = _address_province_suffix(displayed_address)
    dealer_province = (dealer.provincia or "").strip().upper()
    return not address_province or address_province == dealer_province


def _valid_dealers(dealers: List[Dealer], limit: int) -> List[Dealer]:
    return [dealer for dealer in dealers if _dealer_location_is_consistent(dealer)][:limit]


def find_dealers_near(db: Session, city_info: Dict[str, str], limit: int = 3) -> List[Dealer]:
    """
    Ritorna una lista di Dealer vicini:
    - prima prova match sulla città (campo city o comune negli indirizzi)
    - se non trova nulla, prova sulla provincia (sigla, es. 'BO')
    """
    city = (city_info.get("city") or "").strip()
    provincia = (city_info.get("provincia") or "").strip().upper()
    province_scope = city_info.get("scope") == "province"

    print(f"[DEALER] find_dealers_near city={city!r}, provincia={provincia!r}")

    dealers: List[Dealer] = []

    if city and not province_scope:
        dealers = (
            db.query(Dealer)
            .filter(
                _dealer_city_filters(city),
                func.upper(func.trim(Dealer.provincia)) == provincia,
            )
            .order_by(Dealer.id.asc())
            .all()
        )
        dealers = _valid_dealers(dealers, limit)
        print(f"[DEALER] trovati {len(dealers)} dealer per città/indirizzo")
        if dealers:
            return dealers

    if provincia:
        dealers = (
            db.query(Dealer)
            .filter(func.upper(func.trim(Dealer.provincia)) == provincia)
            .order_by(Dealer.city.asc(), Dealer.id.asc())
            .all()
        )
        dealers = _valid_dealers(dealers, limit)
        print(f"[DEALER] trovati {len(dealers)} dealer per provincia")

    return dealers


def format_dealers_for_reply(
    city_info: Dict[str, str], dealers: List[Dealer], include_closing: bool = True
) -> str:
    """
    Converte la lista di Dealer in un testo da mandare all'utente.
    Qui assumiamo che dealers NON sia vuoto.
    """
    if not dealers:
        return "Al momento non trovo sedi DFSK registrate nella zona che hai indicato."

    lines: List[str] = []

    city_label = city_info.get("city") or ""
    prov = city_info.get("provincia") or ""
    if prov:
        city_label = f"{city_label} ({prov})"

    lines.append(f"Le sedi DFSK disponibili per la zona di {city_label} sono:")

    for d in dealers:
        desc_parts: List[str] = [d.company_name]

        if d.address_showroom:
            desc_parts.append(d.address_showroom)
        elif d.address_officina:
            desc_parts.append(d.address_officina)

        if d.city:
            loc = (d.city or "").strip()
            if d.provincia:
                loc += f" ({d.provincia})"
            desc_parts.append(loc)

        line = " – ".join(p for p in desc_parts if p)
        phone = d.phone_vendita or d.phone_officina
        if phone:
            line += f" – Tel. {phone}"

        lines.append(f"- {line}")

    if include_closing:
        lines.append(
            "Se vuoi, posso aiutarti a fissare un appuntamento nella sede che preferisci tra queste."
        )
    return "\n".join(lines)

### lead su airtable ###

def _format_chat_history(history: List[Dict[str, str]]) -> str:
    """Formatta la history JSON in testo leggibile per Airtable."""
    lines: List[str] = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        prefix = "Utente" if role == "user" else "Beatrice"
        lines.append(f"{prefix}: {content}")
    return "\n\n".join(lines)


def push_lead_to_airtable(lead: Lead, db: Session) -> None:
    """
    Invia il lead a Airtable quando lo stato passa a 'completed'.

    Mappa i campi così:
    - Stato Lead   -> "Da contattare"
    - Nome         -> lead.z_name
    - Cellulare    -> lead.z_cell
    - Email        -> lead.z_email
    - Provincia    -> ricavata da resolve_city(z_city) se possibile
    - Origine Lead -> "Chatbot"
    - chat         -> conversazione completa formattata
    """
    if not AIRTABLE_API_KEY:
        print("[AIRTABLE] API key non configurata, salto export lead")
        return

    if not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        print("[AIRTABLE] BASE_ID o TABLE_NAME non configurati, salto export lead")
        return

    try:
        provincia = ""

        if lead.z_city:
            info = resolve_city(lead.z_city, db)
            if info and info.get("provincia"):
                provincia = info["provincia"]

        chat_text = ""
        conv_state = db.query(ConversationState).filter_by(session_id=lead.session_id).first()
        if conv_state and conv_state.history_json:
            try:
                history = json.loads(conv_state.history_json)
                chat_text = _format_chat_history(history)
            except Exception as e:
                print(f"[AIRTABLE] Errore parsing history_json: {e!r}")

        payload = {
            "records": [
                {
                    "fields": {
                        "Stato Lead": "Da contattare",
                        "Nome": lead.z_name or "",
                        "Cellulare": lead.z_cell or "",
                        "Email": lead.z_email or "",
                        "Provincia": provincia,
                        "Città": lead.z_city,
                        "Origine Lead": "Chatbot",
                        "Chat": chat_text,
                    }
                }
            ]
        }

        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        }

        url = f"{AIRTABLE_BASE_URL}/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
        print(f"[AIRTABLE] POST {url} payload={payload}")

        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        print(f"[AIRTABLE] response status={resp.status_code}, body={resp.text[:400]!r}")
        resp.raise_for_status()

    except Exception as e:
        # Non blocchiamo la chat se Airtable ha problemi
        print(f"[AIRTABLE] Errore durante export lead: {e!r}")



# ---------- OpenAI call ----------

SYSTEM_PROMPT = """
Sei Beatrice, l'assistente virtuale di DFSK, produttore di automobili.

OBIETTIVO PRINCIPALE
- Aiutare l'utente a fissare un appuntamento nella concessionaria DFSK più vicina raccogliendo:
  - nome (z_name)
  - città (z_city)
  - numero di cellulare (z_cell)
  - email (z_email)

OBIETTIVO SECONDARIO
- Aiutare l'utente a orientarsi tra i modelli DFSK (solo quelli elencati nella knowledge base).

FLOW INIZIALE
- All'inizio presentati brevemente e chiedi il nome: "Con chi ho il piacere di parlare?".
- Quando conosci il nome, usalo nelle risposte successive.
- Tono amichevole, massimo 3 frasi per risposta, niente dettagli tecnici o numeri precisi (prezzi, cavalli, consumi, ecc.).
- Eccezione consentita: per DFSK E5 Phev puoi citare "autonomia complessiva oltre 1.000 km" se utile alla domanda dell'utente.

MODELLI DI CUI PUOI PARLARE
- Puoi parlare SOLO dei modelli presenti nell'elenco "Modelli DFSK presenti nella knowledge base".
- Modelli: Glory 500 (best seller), Glory 600, Glory F5, E5 Phev (ibrida).
- Se l'utente chiede "che modelli avete", cita questi quattro e metti in evidenza Glory 500 come il più scelto.
- Non inventare altri modelli.

POSTI E CAPACITÀ DEI MODELLI
- Glory 500: SUV compatto a 5 posti.
- Glory 600: SUV a 7 posti (benzina o GPL).
- E5 Phev: SUV a 7 posti ibrido plug-in.
- Glory F5: SUV a 5 posti.
- Se l'utente chiede un'auto a 7 posti, proponi Glory 600 o E5 Phev.
- Se l'utente chiede un'auto a 5 posti, proponi Glory 500 o Glory F5.

RICHIESTE DI FOTO
- Se chiede foto in generale, chiedi di quale modello vuole vedere le immagini, e imposta "show_model_images" = null.
- Se chiede foto di un modello specifico, imposta "show_model_images" in modo esatto:
  - ["Glory 500"] | ["Glory 600"] | ["Glory F5"] | ["E5 Phev"]
- Se chiede foto di più modelli (es. "500 e F5"), imposta "show_model_images" con la lista dei modelli richiesti:
  - ["Glory 500", "Glory F5"]
- Conferma nel testo che gli stai mostrando le foto di quel modello.
- Puoi aggiungere frasi tipo: "Se poi sei interessato, possiamo fissare un appuntamento nella sede più vicina".
- Quando mostri le foto di PIÙ modelli nello stesso turno (show_model_images con 2 o più modelli):
  - Struttura assistant_reply in frasi nello stesso ordine della lista, così le foto possono comparire subito dopo la frase del modello.
  - Esempio con due modelli (massimo 3 frasi totali):
    - 1) frase introduttiva generale
    - 2) frase su primo modello della lista (es. Glory 500)
    - 3) frase su secondo modello della lista (es. Glory F5)
- Non chiedere mai nome/città/cellulare per poter mostrare le foto: puoi mostrarle anche senza dati personali.
- Se nel contesto trovi "foto_gia_mostrate_ultimo_turno" (non null):
  - NON ripetere le stesse foto a meno che l'utente non le richieda esplicitamente di nuovo (es. "rimandami le foto", "fammi rivedere").
  - Se l'utente risponde solo "sì/ok" dopo che le foto sono state già mostrate, interpreta come interesse e passa oltre: proponi appuntamento e chiedi il nome (se manca) o la città (se serve), lasciando show_model_images = null.

RICHIESTE SEDI / CONCESSIONARIE VICINE
- Se chiede dove sono le sedi, le concessionarie nella sua zona, la sede più vicina, ecc.:
  - imposta intent_type = "dealer_nearby".
- Se nel messaggio è presente una città, mettila in dealer_city_raw (es. "Milano", "Roma").
- Se parla solo di "vicino a me", senza città:
  - dealer_city_raw = null,
  - nel testo chiedi esplicitamente in che città si trova.
- In questo turno NON chiedere numero di telefono, concentrati sulle sedi.
- NON inventare mai indirizzi o numeri: il backend aggiungerà la lista corretta.
- Le tue frasi devono essere del tipo:
  - "Ora ti indico le sedi DFSK nella tua zona."

GESTIONE DATI PERSONALI
- Obiettivo: raccogliere z_name, z_city, z_cell, z_email quando l'utente è interessato all'appuntamento.
- Non inserire mai nei campi del lead frasi come "non voglio dirlo", insulti o testo non valido.
- Se rifiuta di dare un dato:
  - nel JSON il campo corrispondente resta null,
  - nel testo spieghi che senza il dato non puoi completare la richiesta.
- Se rifiuta più volte in modo chiaro di proseguire, imposta new_status = "failed".

CAMPI MANCANTI
- Il contesto contiene "campi_mancanti".
- Se vedi "nessuno (tutti i dati necessari sono presenti)":
  - NON chiedere più nome, città, cellulare o email.
  - Ringrazia e conferma che la richiesta di appuntamento è stata registrata.
  - Imposta new_status = "completed".

INTENTI
- lead_answer: l'utente risponde a domande sui dati (nome, città, numero, email).
- lead_refusal: rifiuta di dare dati.
- lead_objection: chiede chiarimenti sui dati ("a che ti serve il mio numero?").
- kb_question: chiede informazioni sui modelli DFSK.
- dealer_nearby: chiede sedi / concessionarie nella sua zona.
- smalltalk: saluti, chiacchiere generiche, meteo, ecc.
- other: tutto il resto.

RICHIESTE FUORI CONTESTO (altre marche, moto, ecc.)
- Spiega che in questa chat puoi parlare solo dei modelli DFSK indicati.
- Non chiedere subito dati personali.
- Fai una domanda per capire che tipo di auto cerca (città, famiglia, viaggi, ecc.).

DOMANDE FREQUENTI (FAQ) E RISPOSTE OTTIMIZZATE

Quando riconosci una delle domande qui sotto, usa il contenuto indicato come guida per la risposta, adattando il tono al resto della conversazione e mantenendo tutte le regole su stile, lunghezza e raccolta dati che hai già ricevuto.

1) Domanda: "Dove sono prodotte le vetture?"
   - Risposta guida:
     - Spiega che le vetture DFSK sono prodotte in Cina e trasportate direttamente in Italia.
     - Sottolinea che qualità e assistenza sono comunque rapide e garantite sul territorio italiano.
   - Esempio di formulazione:
     - "Le vetture DFSK sono prodotte in Cina e arrivano direttamente in Italia, dove garantiamo qualità, assistenza e supporto rapido tramite la nostra rete di concessionarie."

2) Domanda: "Dove posso trovare le officine service?"
   - Obiettivo: indirizzare l'utente verso l'officina autorizzata più vicina.
   - Chiedi in che città si trova.
   - Risposta guida:
     - "Per indicarle l'officina autorizzata più vicina ho bisogno di sapere dove si trova. Mi può indicare la città in cui risiede?"

3) Domanda: "Dove avete i ricambi?"
   - Risposta guida:
     - "In Italia abbiamo disponibilità di ricambi: molti pezzi vengono spediti ai centri ufficiali entro 48 ore. Se vuoi, puoi dirmi la tua città e ti indirizzo al centro assistenza più vicino."
   - Non dare dettagli logistici complessi, resta semplice e rassicurante.

4) Domanda: "Fate permuta?"
   - Concetto chiave: dipende dal singolo concessionario.
   - Risposta guida:
     - "In molti casi la permuta è possibile, dipende dal concessionario. Se vuole, posso aiutarla a trovare il concessionario più vicino che può valutare la sua vettura in permuta."
   - Poi collega alla ricerca sedi:
     - Chiedi città con frasi tipo:
       - "Mi indica la città in cui si trova, così posso suggerirle la sede più comoda per valutare la permuta?"
   - Non promettere mai condizioni economiche specifiche.

5) Domanda: "Cosa posso fare se succede qualche problema alla vettura?"
   - Risposta guida:
     - "In caso di problemi può contattare direttamente la concessionaria presso cui ha acquistato la vettura, oppure la sede DFSK più vicina. Il team di assistenza sarà a disposizione per supportarla."

6) Domanda: "Che GPL utilizzate?"
   - Risposta guida:
     - "Utilizziamo impianti GPL della marca Landi Renzo, garantiti e certificati per le nostre vetture."
   - Mantieni il tono rassicurante, senza entrare in dati tecnici profondi.

7) Domanda: "Quanto costa la vettura?" o domande sui prezzi in generale
   - Ricorda le regole generali sui prezzi:
     - Di base non dare numeri precisi su listini e offerte.
   - Risposta guida:
     - "I prezzi aggiornati sono visibili sul sito ufficiale DFSK e possono variare leggermente a seconda del concessionario. Se vuole, posso aiutarla a trovare il concessionario più vicino per ricevere un preventivo dettagliato."
   - Aggiungi sempre una domanda per capire meglio:
     - "A quale modello è interessato in particolare?"
   - Se l'utente specifica il modello, continua in modo discorsivo e, se ha senso, proponi l'appuntamento in sede.

8) Domanda: "Possono guidarla i neopatentati?" (o richieste tipo "auto per neopatentati")
   - Risposta guida:
     - Spiega che alcuni modelli della gamma DFSK sono adatti ai neopatentati, evitando di far intendere che lo siano tutti.
     - Indica come opzioni principali Glory 500 e Glory F5, poi fai una mini presentazione di entrambe e una differenza chiara.
   - Struttura tipica (massimo 3 frasi):
     - "Alcuni dei nostri modelli sono guidabili anche dai neopatentati, in particolare DFSK Glory 500 e Glory F5. Glory 500 è il SUV compatto più scelto, pratico e versatile. Glory F5 ha uno stile più sportivo e un carattere più dinamico."
   - Foto:
     - Se l'utente chiede neopatentati e sembra interessato, mostra le immagini di entrambi i modelli.

9) Domanda: "Posso parlare con un operatore umano?"
   - Risposta guida:
     - Conferma che è possibile parlare con un responsabile umano.
     - Fornisci il link ufficiale ai contatti DFSK.
   - Esempio di formulazione:
     - "Certamente, se preferisce può parlare direttamente con un nostro responsabile. Può contattarci tramite la pagina ufficiale dei contatti DFSK: https://dfskcar.it/contatti"
   - Dopo questa risposta non insistere subito sulla raccolta dati, a meno che l'utente torni a parlare di appuntamento o sede vicina.


FORMATO DI RISPOSTA (OBBLIGATORIO)
Devi restituire SEMPRE e SOLO un oggetto JSON con questa struttura:

{
  "assistant_reply": "testo da dire all'utente (massimo 3 frasi)",
  "intent_type": "lead_answer | lead_refusal | lead_objection | kb_question | dealer_nearby | smalltalk | other",
  "lead_updates": {
    "z_name": "oppure null",
    "z_city": "oppure null",
    "z_cell": "oppure null",
    "z_email": "oppure null"
  },
  "new_status": "in_progress | completed | failed",
  "show_model_images": ["Glory 500" | "Glory 600" | "Glory F5" | "E5 Phev"] oppure null,
  "dealer_city_raw": "nome città oppure null"
}

- assistant_reply: risposta naturale all'utente (massimo 3 frasi).
- lead_updates:
  - Metti un valore NON nullo solo se il messaggio dell'utente contiene chiaramente un dato valido.
  - Per gli altri campi lascia esplicitamente null.
- new_status:
  - "in_progress" di default,
  - "completed" se hai già tutti i dati,
  - "failed" se l'utente non vuole proseguire.
- show_model_images:
  - Lista con uno o più nomi esatti dei modelli quando mostri le immagini, altrimenti null.
- dealer_city_raw:
  - Nome città capito dal messaggio quando parli di sedi, altrimenti null.

NON includere mai testo fuori da questo JSON.
""".strip()


def build_llm_input(
    lead: Lead,
    history: List[Dict[str, str]],
    kb_matches: List[Dict[str, Any]],
    user_message: str,
    last_shown_models: Optional[List[str]] = None,
) -> str:
    lines: List[str] = []

    lines.append("Storico conversazione (ultimi massimo 8 messaggi):")
    for msg in history[-8:]:
        prefix = "Utente" if msg["role"] == "user" else "Beatrice"
        lines.append(f"{prefix}: {msg['content']}")

    lines.append("\nStato attuale del lead:")
    lines.append(f"- status: {lead.status}")
    lines.append(
        f"- dati_raccolti: nome={lead.z_name or ''}, città={lead.z_city or ''}, cell={lead.z_cell or ''}, email={lead.z_email or ''}"
    )
    lines.append(
        f"- foto_gia_mostrate_ultimo_turno: {last_shown_models if last_shown_models else 'null'}"
    )

    missing = missing_fields(lead)
    if missing:
        lines.append(f"- campi_mancanti: {', '.join(missing)}")
    else:
        lines.append("- campi_mancanti: nessuno (tutti i dati necessari sono presenti)")
        lines.append(
            "IMPORTANTE: campi_mancanti è 'nessuno (tutti i dati necessari sono presenti)'. "
            "In questo caso NON devi più chiedere dati personali (nome, città, cellulare, email), "
            "devi solo ringraziare e confermare la richiesta di appuntamento, "
            'e impostare new_status = "completed".'
        )

    lines.append("\nModelli DFSK presenti nella knowledge base (puoi parlare solo di questi):")
    for doc in KB_DOCS:
        lines.append(f"- {doc['title']}")

    if kb_matches:
        lines.append("\nKnowledge base rilevante sulle auto DFSK:")
        for m in kb_matches:
            lines.append(f"Titolo: {m['title']}")
            lines.append(f"Contenuto: {m['content']}")
            lines.append("")

    lines.append("\nNuovo messaggio dell'utente:")
    lines.append(user_message)

    lines.append(
        "\nIn base a tutto questo, genera SOLO l'oggetto JSON nella forma richiesta nel system prompt."
    )

    prompt = "\n".join(lines)
    print(f"[LLM] build_llm_input lunghezza: {len(prompt)}")
    print("[LLM] build_llm_input preview:\n", prompt[:400], "\n--- fine preview ---\n")
    return prompt


class LeadPatch(BaseModel):
    z_name: Optional[str] = None
    z_city: Optional[str] = None
    z_cell: Optional[str] = None
    z_email: Optional[str] = None


class BeatriceLLMOutput(BaseModel):
    assistant_reply: str
    intent_type: Literal[
        "lead_answer",
        "lead_refusal",
        "lead_objection",
        "kb_question",
        "dealer_nearby",
        "smalltalk",
        "other",
    ]
    lead_updates: LeadPatch = Field(default_factory=LeadPatch)
    new_status: Literal["in_progress", "completed", "failed"] = "in_progress"
    show_model_images: Optional[
        List[Literal["Glory 500", "Glory 600", "Glory F5", "E5 Phev"]]
    ] = None
    dealer_city_raw: Optional[str] = None

    @field_validator("show_model_images", mode="before")
    @classmethod
    def normalize_show_model_images(cls, v):
        # compatibilità: se il modello restituisce per errore una stringa, la convertiamo in lista
        if v is None:
            return None
        if isinstance(v, str):
            vv = v.strip()
            return [vv] if vv else None
        return v


def call_beatrice(
    lead: Lead,
    history: List[Dict[str, str]],
    kb_matches: List[Dict[str, Any]],
    user_message: str,
    last_shown_models: Optional[List[str]] = None,
) -> Dict[str, Any]:
    prompt = build_llm_input(lead, history, kb_matches, user_message, last_shown_models)

    try:
        print(f"[LLM] CHIAMATA AL MODELLO gpt-4o-mini")
        resp = client.responses.parse(
            model="gpt-4o-mini",
            instructions=SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            text_format=BeatriceLLMOutput,
        )

        parsed: BeatriceLLMOutput = resp.output_parsed
        data = parsed.model_dump()
        print("[LLM] RISPOSTA PARSATA:", json.dumps(data, indent=2, ensure_ascii=False))

    except Exception as e:
        print(f"[LLM] ERRORE nella chiamata al modello: {e!r}")
        data = {
            "assistant_reply": "Mi dispiace, c'è stato un problema tecnico, puoi ripetere per favore?",
            "intent_type": "other",
            "lead_updates": {
                "z_name": None,
                "z_city": None,
                "z_cell": None,
                "z_email": None,
            },
            "new_status": "in_progress",
            "show_model_images": None,
            "dealer_city_raw": None,
        }

    return data


# ---------- FastAPI app ----------

app = FastAPI(title="DFSK Beatrice chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# static per chat.html e cartella cars
app.mount("/static", StaticFiles(directory=Path(__file__).parent), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SessionInitResponse(BaseModel):
    session_id: str
    first_message: str = Field(
        default=(
            "Ciao, sono Beatrice, l'assistente DFSK.\n\n"
            "Ti aiuto a scoprire i nostri modelli e, se vuoi, a fissare un appuntamento in concessionaria.\n\n"
            "Con chi ho il piacere di parlare?"
        )
    )


class ChatRequest(BaseModel):
    session_id: str
    message: str


class LeadOut(BaseModel):
    z_name: Optional[str] = None
    z_city: Optional[str] = None
    z_cell: Optional[str] = None
    z_email: Optional[str] = None
    status: str


class ChatResponse(BaseModel):
    reply: str
    step: str
    lead: LeadOut
    show_model_images: Optional[List[str]] = None


@app.on_event("startup")
def startup_event():
    print("[STARTUP] Creazione tabelle DB (se non esistono)")
    Base.metadata.create_all(bind=engine)

    print("[STARTUP] Calcolo embeddings KB")
    global KB_EMBEDDINGS
    KB_EMBEDDINGS = embed_texts([doc["content"] for doc in KB_DOCS])
    print(f"[STARTUP] Embeddings KB pronti, n_doc = {len(KB_DOCS)}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def root():
    html_path = Path(__file__).parent / "chat.html"
    return html_path.read_text(encoding="utf-8")


@app.post("/session", response_model=SessionInitResponse)
def create_session(db: Session = Depends(get_db)):
    session_id = str(uuid.uuid4())
    print(f"[SESSION] nuova session_id={session_id}")

    lead = Lead(session_id=session_id, status="in_progress")
    state = ConversationState(
        session_id=session_id,
        step="generic",
        attempts_json=json.dumps({}),
        history_json=json.dumps(
            [
                {
                    "role": "assistant",
                    "content": (
                        "Ciao, sono Beatrice, l'assistente DFSK.\n\n"
                        "Ti aiuto a scoprire i nostri modelli e, se vuoi, a fissare un appuntamento in concessionaria.\n\n"
                        "Con chi ho il piacere di parlare?"
                    ),
                }
            ]
        ),
        extra_json=json.dumps({}),
    )
    db.add(lead)
    db.add(state)
    db.commit()

    return SessionInitResponse(session_id=session_id)


def load_state(db: Session, session_id: str) -> ConversationState:
    state = db.query(ConversationState).filter_by(session_id=session_id).first()
    if not state:
        raise HTTPException(status_code=404, detail="Sessione non trovata")
    return state


def load_lead(db: Session, session_id: str) -> Lead:
    lead = db.query(Lead).filter_by(session_id=session_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead non trovato")
    return lead


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    print("\n================= /chat REQUEST =================")
    print(f"[CHAT] session_id={req.session_id}")
    print(f"[CHAT] user_message={req.message!r}")

    state = load_state(db, req.session_id)
    lead = load_lead(db, req.session_id)

    # extra state per la logica "nessuna sede / seconda provincia"
    try:
        extra = json.loads(state.extra_json or "{}")
    except Exception:
        extra = {}

    dealer_no_sede_stage = int(extra.get("dealer_no_sede_stage", 0))  # 0 = nessun problema, 1 = prima provincia senza sedi, 2 = già spiegato
    last_prov = extra.get("dealer_last_provincia")
    last_shown_models = extra.get("last_shown_models")
    if not isinstance(last_shown_models, list):
        last_shown_models = None

    print(f"[CHAT] extra iniziale: {extra}")
    print(f"[CHAT] dealer_no_sede_stage={dealer_no_sede_stage}, last_prov={last_prov}")

    history: List[Dict[str, str]] = json.loads(state.history_json or "[]")
    print(f"[CHAT] history_len prima={len(history)}")
    print(f"[CHAT] ultimi 3 messaggi prima: {history[-3:]}")

    # ultimo messaggio dell'assistente (per capire se stavamo chiedendo la città per le sedi)
    last_assistant_msg: Optional[str] = None
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            last_assistant_msg = msg.get("content")
            break
    print(f"[CHAT] last_assistant_msg={last_assistant_msg!r}")

    # aggiungiamo il messaggio utente alla history che passeremo al modello
    history.append({"role": "user", "content": req.message})

    kb_matches = search_kb(req.message)

    print("[CHAT] lead PRIMA di call_beatrice:", {
        "z_name": lead.z_name,
        "z_city": lead.z_city,
        "z_cell": lead.z_cell,
        "z_email": lead.z_email,
        "status": lead.status,
    })

    llm_data = call_beatrice(lead, history, kb_matches, req.message, last_shown_models)

    print("[CHAT] llm_data grezzo:", json.dumps(llm_data, indent=2, ensure_ascii=False))

    assistant_reply = llm_data.get("assistant_reply", "Ok.")
    lead_updates = llm_data.get("lead_updates") or {}
    new_status = llm_data.get("new_status", "in_progress")
    show_model_images = llm_data.get("show_model_images")
    intent_type = llm_data.get("intent_type", "other")
    dealer_city_raw = llm_data.get("dealer_city_raw")

    # compatibilità: se arriva una stringa, normalizziamo in lista
    if isinstance(show_model_images, str):
        show_model_images = [show_model_images.strip()] if show_model_images.strip() else None
    if isinstance(show_model_images, list) and len(show_model_images) == 0:
        show_model_images = None

    print(f"[CHAT] intent_type={intent_type}")
    print(f"[CHAT] dealer_city_raw={dealer_city_raw}")
    print(f"[CHAT] new_status dal modello={new_status}")
    print(f"[CHAT] show_model_images={show_model_images}")
    print(f"[CHAT] lead_updates (prima di cast)={lead_updates}")

    # --- OVERRIDE INTENT PER LE SEDI ---
    # Se il modello non ha messo dealer_nearby ma:
    # - abbiamo dealer_city_raw
    # - l'ultimo messaggio dell'assistente era chiaramente una DOMANDA sulle sedi
    #   in base alla città o alla provincia
    # allora forziamo intent_type = dealer_nearby.
    if intent_type != "dealer_nearby" and dealer_city_raw and last_assistant_msg:
        last_lower = last_assistant_msg.lower()
        if (
            "sedi dfsk" in last_lower
            and ("città" in last_lower or "zona" in last_lower or "provincia" in last_lower)
            and "?" in last_assistant_msg
        ):
            print("[CHAT] Override intent_type -> dealer_nearby (stavamo aspettando la città per le sedi)")
            intent_type = "dealer_nearby"

    # se non è dealer_nearby, resettiamo lo stato "nessuna sede"
    if intent_type != "dealer_nearby":
        extra["dealer_no_sede_stage"] = 0
        extra["dealer_last_provincia"] = last_prov

    # se è una richiesta sedi, non permettiamo di cambiare lo stato del lead in questo turno
    if intent_type == "dealer_nearby":
        print("[CHAT] intent_type=dealer_nearby -> forzo new_status = stato attuale lead")
        new_status = lead.status or "in_progress"
    else:
        if new_status not in ("in_progress", "completed", "failed"):
            new_status = "in_progress"

    # applica aggiornamenti lead (anche se intent_type è dealer_nearby, perché
    # la frase può contenere nome/città utili tipo "sono Francesco di Crema")
    if isinstance(lead_updates, dict):
        updates_dict = lead_updates
    else:
        updates_dict = dict(lead_updates)

    print(f"[CHAT] updates_dict normalizzato: {updates_dict}")

    for field_name in FIELD_ORDER:
        if field_name in updates_dict:
            value = updates_dict[field_name]
            print(f"[CHAT] update field {field_name} -> {value!r}")
            if value is not None:
                value_str = str(value)
                if field_name == "z_cell":
                    value_str = normalize_phone(value_str)
                setattr(lead, field_name, value_str)

    print("[CHAT] lead DOPO updates:", {
        "z_name": lead.z_name,
        "z_city": lead.z_city,
        "z_cell": lead.z_cell,
        "z_email": lead.z_email,
        "status": lead.status,
    })

    # logica sedi vicine / province
    if intent_type == "dealer_nearby":
        print("[CHAT] Entrato in ramo dealer_nearby")
        # niente immagini in un turno dedicato alle sedi
        show_model_images = None
        extra["last_shown_models"] = None

        locations = resolve_requested_locations(req.message, dealer_city_raw, lead.z_city, db)
        print(f"[CHAT] dealer_nearby: locations={locations!r}")

        if not locations:
            if not dealer_city_raw and not lead.z_city:
                assistant_reply = (
                    "Per indicarti le sedi DFSK nella tua zona ho bisogno di sapere in che città ti trovi. "
                    "In che città sei?"
                )
            else:
                assistant_reply = (
                    "Non sono riuscita a riconoscere con precisione la località. "
                    "Puoi ripetere il comune, magari indicando anche la provincia?"
                )
            print("[CHAT] dealer_nearby: località non riconosciuta")
        else:
            matches = [(info, find_dealers_near(db, info, limit=3)) for info in locations]
            found = [(info, dealers) for info, dealers in matches if dealers]
            print(f"[CHAT] dealer_nearby: risultati per provincia={[(i['provincia'], len(d)) for i, d in matches]}")

            if found:
                extra["dealer_no_sede_stage"] = 0
                extra["dealer_last_provincia"] = found[0][0]["provincia"]
                if len(locations) == 1:
                    assistant_reply = format_dealers_for_reply(*found[0])
                else:
                    sections = [
                        format_dealers_for_reply(info, dealers, include_closing=False)
                        if dealers else f"Non risultano sedi DFSK in provincia di {info['city']} ({info['provincia']})."
                        for info, dealers in matches
                    ]
                    assistant_reply = "\n\n".join(sections) + (
                        "\n\nSe vuoi, posso aiutarti a fissare un appuntamento nella sede che preferisci."
                    )
                print("[CHAT] dealer_nearby: trovate sedi, reply formattata")
                if not lead.z_city and locations[0].get("scope") == "city":
                    lead.z_city = locations[0]["city"]
            else:
                provincia = locations[0]["provincia"]
                if dealer_no_sede_stage == 0:
                    assistant_reply = (
                        f"Nella tua provincia {provincia} non abbiamo sedi DFSK registrate, "
                        "puoi indicarci la provincia più vicina a te?"
                    )
                    extra["dealer_no_sede_stage"] = 1
                else:
                    assistant_reply = (
                        f"Al momento non abbiamo sedi DFSK registrate in provincia {provincia}.\n\n"
                        "Se vuoi possiamo farti ricontattare da un nostro consulente."
                    )
                    extra["dealer_no_sede_stage"] = 2
                extra["dealer_last_provincia"] = provincia
                print("[CHAT] dealer_nearby: nessuna sede nelle province richieste")

     # salviamo lo stato precedente per capire se è appena passato a "completed"
    prev_status = lead.status
    lead.status = new_status
    print(f"[CHAT] lead.status finale={lead.status}")

    # memorizziamo quali foto sono state mostrate in questo turno (per evitare ripetizioni al turno successivo)
    extra["last_shown_models"] = show_model_images if show_model_images else None

    history.append({"role": "assistant", "content": assistant_reply})
    state.history_json = json.dumps(history)
    state.step = lead.status
    state.extra_json = json.dumps(extra)

    # se il lead è appena passato a completed, lo spediamo ad Airtable
    # (dopo aver aggiornato history_json così la chat è completa)
    if prev_status != "completed" and lead.status == "completed":
        print("[CHAT] lead appena completato, invio ad Airtable")
        push_lead_to_airtable(lead, db)

    db.add(lead)
    db.add(state)
    db.commit()
    db.refresh(lead)
    db.refresh(state)

    print(f"[CHAT] history_len dopo= {len(history)}")
    print(f"[CHAT] ultimo messaggio assistant: {assistant_reply}")
    print(f"[CHAT] extra finale: {extra}")

    lead_out = LeadOut(
        z_name=lead.z_name,
        z_city=lead.z_city,
        z_cell=lead.z_cell,
        z_email=lead.z_email,
        status=lead.status,
    )

    print("[CHAT] RESPONSE -> reply, step, lead, show_model_images")
    print(f"  reply= {assistant_reply}")
    print(f"  step = {state.step}")
    print(f"  lead = {lead_out.model_dump()}")
    print(f"  show_model_images = {show_model_images}")
    print("=================================================\n")

    return ChatResponse(
        reply=assistant_reply,
        step=state.step,
        lead=lead_out,
        show_model_images=show_model_images,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
