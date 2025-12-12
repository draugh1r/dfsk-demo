from pathlib import Path
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import os
import json
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any, Literal

import requests
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from openai import OpenAI

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

# Endpoint dell'API "cerca comuni" (usa COMUNI_API_TOKEN già esistente)
COMUNI_API_BASE = os.getenv("COMUNI_API_BASE", "https://cap.openapi.it").rstrip("/")
COMUNI_API_TOKEN = os.getenv("COMUNI_API_TOKEN", "").strip()

# Cache semplice in memoria per i comuni risolti
CITY_CACHE: Dict[str, Dict[str, str]] = {}


# ---------- DB models ----------


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    z_name = Column(String, nullable=True)
    z_city = Column(String, nullable=True)       # città
    z_cell = Column(String, nullable=True)
    # campi legacy che non usiamo più nella logica, li lasciamo per compatibilità DB
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
            "versatile per l'uso quotidiano, comodo in città e adatto anche alla famiglia."
        ),
    },
    {
        "id": "glory_600",
        "title": "DFSK Glory 600",
        "content": (
            "DFSK Glory 600 è un SUV di segmento superiore, più orientato al comfort "
            "e alla dotazione, pensato per chi vuole un'esperienza di guida più premium."
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
        "id": "glory_e5",
        "title": "DFSK Glory E5",
        "content": (
            "DFSK Glory E5 è la versione ibrida della gamma DFSK, ideale per chi vuole "
            "ridurre consumi ed emissioni senza rinunciare alla praticità di un SUV."
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


FIELD_ORDER = ["z_name", "z_city", "z_cell"]


def all_fields_filled(lead: "Lead") -> bool:
    return all(getattr(lead, f) for f in FIELD_ORDER)


def missing_fields(lead: "Lead") -> List[str]:
    return [f for f in FIELD_ORDER if not getattr(lead, f)]


def _extract_prov_istat_from_comune_istat(istat_code: str) -> Optional[str]:
    """
    L'API sui comuni restituisce istat del comune spesso a 5 cifre (es. '37008'),
    mentre la tabella province_italia usa codici ISTAT provincia a 3 cifre (es. '037').

    Convenzione che funziona con i dati di cap.openapi.it:
    - se len == 6: prime 3 cifre sono il codice provincia (PPPCCC)
    - se len == 5: il codice provincia è '0' + prime 2 cifre (es. '37008' -> '037')
    - altrimenti: prendo comunque le prime 3 cifre come fallback.
    """
    istat_code = (istat_code or "").strip()
    if not istat_code:
        return None

    if len(istat_code) == 6:
        return istat_code[:3]
    if len(istat_code) == 5:
        return "0" + istat_code[:2]
    if len(istat_code) >= 3:
        return istat_code[:3]
    return None


def resolve_city(raw_city: str, db: Optional[Session] = None) -> Optional[Dict[str, str]]:
    """
    Risolve una città italiana usando l'API /cerca_comuni di cap.openapi.it.

    Passi:
    - chiama /cerca_comuni?comune=<nome>
    - prende il primo risultato e l'ISTAT del comune
    - ricava il codice provincia ISTAT a 3 cifre
    - cerca nella tabella province_italia quella provincia
    - ritorna {"city": <nome comune>, "provincia": <sigla provincia>} oppure None

    Se qualcosa va storto, restituisce None oppure fa fallback minimale (solo city, provincia vuota).
    """
    print(f"[CITY] resolve_city input={raw_city!r}")
    if not raw_city:
        print("[CITY] raw_city vuota")
        return None

    name = raw_city.strip()
    if not name:
        print("[CITY] raw_city solo spazi")
        return None

    cache_key = name.lower()
    if cache_key in CITY_CACHE:
        print(f"[CITY] cache hit per {cache_key}, valore={CITY_CACHE[cache_key]}")
        return CITY_CACHE[cache_key]

    # Se manca base o token, fai solo normalizzazione minima
    if not COMUNI_API_BASE or not COMUNI_API_TOKEN:
        info = {"city": name.title(), "provincia": ""}
        CITY_CACHE[cache_key] = info
        print(f"[CITY] COMUNI_API_BASE o COMUNI_API_TOKEN non configurati, uso fallback {info}")
        return info

    # Costruisco l'URL corretto per /cerca_comuni
    base = COMUNI_API_BASE
    if "cerca_comuni" in base:
        url = base
    else:
        url = f"{base}/cerca_comuni"

    headers = {
        "Authorization": f"Bearer {COMUNI_API_TOKEN}",
        "Accept": "application/json",
    }

    try:
        print(f"[CITY] chiamata a {url} per comune={name!r}")
        resp = requests.get(
            url,
            params={"comune": name},
            headers=headers,
            timeout=4,
        )
        print(f"[CITY] cerca_comuni status_code={resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        print(f"[CITY] cerca_comuni response_json={data!r}")

        if not data.get("success"):
            print("[CITY] success=false nella risposta API")
            return None

        data_block = data.get("data") or {}
        results = data_block.get("result") or []
        if not results:
            print("[CITY] nessun comune in data.result")
            return None

        first = results[0]
        city_name = first.get("comune") or name.title()
        istat_code = first.get("istat")

        provincia_sigla = ""

        # Se abbiamo l'istat del comune e una sessione DB, proviamo a risalire alla provincia
        if istat_code and db is not None:
            prov_istat = _extract_prov_istat_from_comune_istat(istat_code)
            print(f"[CITY] istat comune={istat_code!r}, prov_istat derivato={prov_istat!r}")
            if prov_istat:
                try:
                    prov_row = (
                        db.query(ProvinceItalia)
                        .filter(ProvinceItalia.istat == prov_istat)
                        .first()
                    )
                    if prov_row:
                        provincia_sigla = (prov_row.sigla or "").upper()
                        print(
                            f"[CITY] trovata provincia in DB per istat {prov_istat}: "
                            f"{prov_row.provincia} ({provincia_sigla})"
                        )
                    else:
                        print(f"[CITY] nessuna provincia trovata in DB per istat {prov_istat}")
                except Exception as e:
                    print(f"[CITY] errore query province_italia: {e!r}")

        info = {"city": city_name, "provincia": provincia_sigla}
        CITY_CACHE[cache_key] = info
        print(f"[CITY] resolve_city finale info={info}")
        return info

    except Exception as e:
        print(f"[CITY] errore chiamando cerca_comuni: {e!r}, uso fallback minimale")
        info = {"city": name.title(), "provincia": ""}
        CITY_CACHE[cache_key] = info
        return info


def find_dealers_near(db: Session, city_info: Dict[str, str], limit: int = 3) -> List[Dealer]:
    """
    Ritorna una lista di Dealer vicini:
    - prima prova match esatto sulla città
    - se non trova nulla, prova sulla provincia (sigla, es. 'BO')
    """
    city = (city_info.get("city") or "").strip()
    provincia = (city_info.get("provincia") or "").strip().upper()

    print(f"[DEALER] find_dealers_near city={city!r}, provincia={provincia!r}")

    dealers: List[Dealer] = []

    if city:
        dealers = (
            db.query(Dealer)
            .filter(Dealer.city.ilike(city))
            .order_by(Dealer.id.asc())
            .limit(limit)
            .all()
        )
        print(f"[DEALER] trovati {len(dealers)} dealer per città")
        if dealers:
            return dealers

    if provincia:
        dealers = (
            db.query(Dealer)
            .filter(Dealer.provincia == provincia)
            .order_by(Dealer.city.asc())
            .limit(limit)
            .all()
        )
        print(f"[DEALER] trovati {len(dealers)} dealer per provincia")

    return dealers


def format_dealers_for_reply(city_info: Dict[str, str], dealers: List[Dealer]) -> str:
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

    lines.append(f"Le sedi DFSK più vicine alla zona di {city_label} sono:")

    for d in dealers:
        desc_parts: List[str] = [d.company_name]

        if d.address_showroom:
            desc_parts.append(d.address_showroom)
        elif d.address_officina:
            desc_parts.append(d.address_officina)

        if d.city or d.provincia:
            loc = (d.city or "").strip()
            if d.provincia:
                loc += f" ({d.provincia})"
            desc_parts.append(loc)

        line = " – ".join(p for p in desc_parts if p)
        phone = d.phone_vendita or d.phone_officina
        if phone:
            line += f" – Tel. {phone}"

        lines.append(f"- {line}")

    lines.append(
        "Se vuoi, posso aiutarti a fissare un appuntamento nella sede che preferisci tra queste."
    )
    return "\n".join(lines)


# ---------- OpenAI call ----------

SYSTEM_PROMPT = """
Sei Beatrice, l'assistente virtuale di DFSK, produttore di automobili.

OBIETTIVO PRINCIPALE
- Aiutare l'utente a fissare un appuntamento nella concessionaria DFSK più vicina raccogliendo:
  - nome (z_name)
  - città (z_city)
  - numero di cellulare (z_cell)

OBIETTIVO SECONDARIO
- Aiutare l'utente a orientarsi tra i modelli DFSK (solo quelli elencati nella knowledge base).

FLOW INIZIALE
- All'inizio presentati brevemente e chiedi il nome: "Con chi ho il piacere di parlare?".
- Quando conosci il nome, usalo nelle risposte successive.
- Tono amichevole, massimo 3 frasi per risposta, niente dettagli tecnici o numeri precisi (prezzi, cavalli, consumi, ecc.).

MODELLI DI CUI PUOI PARLARE
- Puoi parlare SOLO dei modelli presenti nell'elenco "Modelli DFSK presenti nella knowledge base".
- Modelli: Glory 500 (best seller), Glory 600, Glory F5, Glory E5 (ibrida).
- Se l'utente chiede "che modelli avete", cita questi quattro e metti in evidenza Glory 500 come il più scelto.
- Non inventare altri modelli.

RICHIESTE DI FOTO
- Se chiede foto in generale, chiedi di quale modello vuole vedere le immagini, e imposta "show_model_images" = null.
- Se chiede foto di un modello specifico, imposta "show_model_images" in modo esatto:
  - "Glory 500" | "Glory 600" | "Glory F5" | "Glory E5"
- Conferma nel testo che gli stai mostrando le foto di quel modello.
- Puoi aggiungere frasi tipo: "Se poi sei interessato, possiamo fissare un appuntamento nella sede più vicina".

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
- Obiettivo: raccogliere z_name, z_city, z_cell quando l'utente è interessato all'appuntamento.
- Non inserire mai nei campi del lead frasi come "non voglio dirlo", insulti o testo non valido.
- Se rifiuta di dare un dato:
  - nel JSON il campo corrispondente resta null,
  - nel testo spieghi che senza il dato non puoi completare la richiesta.
- Se rifiuta più volte in modo chiaro di proseguire, imposta new_status = "failed".

CAMPI MANCANTI
- Il contesto contiene "campi_mancanti".
- Se vedi "nessuno (tutti i dati necessari sono presenti)":
  - NON chiedere più nome, città o cellulare.
  - Ringrazia e conferma che la richiesta di appuntamento è stata registrata.
  - Imposta new_status = "completed".

INTENTI
- lead_answer: l'utente risponde a domande sui dati (nome, città, numero).
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

FORMATO DI RISPOSTA (OBBLIGATORIO)
Devi restituire SEMPRE e SOLO un oggetto JSON con questa struttura:

{
  "assistant_reply": "testo da dire all'utente (massimo 3 frasi)",
  "intent_type": "lead_answer | lead_refusal | lead_objection | kb_question | dealer_nearby | smalltalk | other",
  "lead_updates": {
    "z_name": "oppure null",
    "z_city": "oppure null",
    "z_cell": "oppure null"
  },
  "new_status": "in_progress | completed | failed",
  "show_model_images": "Glory 500 | Glory 600 | Glory F5 | Glory E5 | null",
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
  - Nome esatto del modello quando mostri le immagini, altrimenti null.
- dealer_city_raw:
  - Nome città capito dal messaggio quando parli di sedi, altrimenti null.

NON includere mai testo fuori da questo JSON.
""".strip()


def build_llm_input(
    lead: Lead,
    history: List[Dict[str, str]],
    kb_matches: List[Dict[str, Any]],
    user_message: str,
) -> str:
    lines: List[str] = []

    lines.append("Storico conversazione (ultimi massimo 8 messaggi):")
    for msg in history[-8:]:
        prefix = "Utente" if msg["role"] == "user" else "Beatrice"
        lines.append(f"{prefix}: {msg['content']}")

    lines.append("\nStato attuale del lead:")
    lines.append(f"- status: {lead.status}")
    lines.append(
        f"- dati_raccolti: nome={lead.z_name or ''}, città={lead.z_city or ''}, cell={lead.z_cell or ''}"
    )

    missing = missing_fields(lead)
    if missing:
        lines.append(f"- campi_mancanti: {', '.join(missing)}")
    else:
        lines.append("- campi_mancanti: nessuno (tutti i dati necessari sono presenti)")
        lines.append(
            "IMPORTANTE: campi_mancanti è 'nessuno (tutti i dati necessari sono presenti)'. "
            "In questo caso NON devi più chiedere dati personali (nome, città, cellulare), "
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
    show_model_images: Optional[str] = None  # "Glory 500" | "Glory 600" | "Glory F5" | "Glory E5" | None
    dealer_city_raw: Optional[str] = None


def call_beatrice(
    lead: Lead,
    history: List[Dict[str, str]],
    kb_matches: List[Dict[str, Any]],
    user_message: str,
) -> Dict[str, Any]:
    prompt = build_llm_input(lead, history, kb_matches, user_message)

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
    status: str


class ChatResponse(BaseModel):
    reply: str
    step: str
    lead: LeadOut
    show_model_images: Optional[str] = None


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
        "status": lead.status,
    })

    llm_data = call_beatrice(lead, history, kb_matches, req.message)

    print("[CHAT] llm_data grezzo:", json.dumps(llm_data, indent=2, ensure_ascii=False))

    assistant_reply = llm_data.get("assistant_reply", "Ok.")
    lead_updates = llm_data.get("lead_updates") or {}
    new_status = llm_data.get("new_status", "in_progress")
    show_model_images = llm_data.get("show_model_images")
    intent_type = llm_data.get("intent_type", "other")
    dealer_city_raw = llm_data.get("dealer_city_raw")

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
        "status": lead.status,
    })

    # logica sedi vicine / province
    if intent_type == "dealer_nearby":
        print("[CHAT] Entrato in ramo dealer_nearby")
        # niente immagini in un turno dedicato alle sedi
        show_model_images = None

        # città di lavoro: prima quella estratta dal modello, poi quella eventualmente già salvata
        city_text: Optional[str] = None
        if dealer_city_raw:
            city_text = str(dealer_city_raw)
        elif lead.z_city:
            city_text = lead.z_city

        print(f"[CHAT] dealer_nearby: city_text={city_text!r}")

        if not city_text:
            # non abbiamo ancora una città -> chiediamo la città
            assistant_reply = (
                "Per indicarti le sedi DFSK nella tua zona ho bisogno di sapere in che città ti trovi. "
                "In che città sei?"
            )
            print("[CHAT] dealer_nearby: nessuna city -> chiedo città esplicitamente")
        else:
            city_info = resolve_city(city_text, db)
            if not city_info:
                assistant_reply = (
                    "Non sono riuscita a riconoscere con precisione la città. "
                    "Puoi ripetermi il nome della città, magari indicando anche la provincia?"
                )
                print("[CHAT] dealer_nearby: resolve_city ha restituito None")
            else:
                dealers = find_dealers_near(db, city_info, limit=3)
                provincia = (city_info.get("provincia") or "").upper()
                print(f"[CHAT] dealer_nearby: provincia risolta={provincia!r}, num_dealers={len(dealers)}")

                if dealers:
                    # abbiamo trovato sedi -> reset logica "nessuna sede"
                    extra["dealer_no_sede_stage"] = 0
                    extra["dealer_last_provincia"] = provincia
                    assistant_reply = format_dealers_for_reply(city_info, dealers)
                    print("[CHAT] dealer_nearby: trovate sedi, reply formattata")
                    # se non avevamo ancora città nel lead e il resolver l'ha capita, salviamola
                    if not lead.z_city and city_info.get("city"):
                        lead.z_city = city_info["city"]
                        print(f"[CHAT] dealer_nearby: salvo z_city nel lead = {lead.z_city!r}")
                else:
                    # nessuna sede trovata
                    print("[CHAT] dealer_nearby: nessuna sede trovata per city/provincia")
                    if not provincia:
                        # caso raro: niente provincia nota dall'API
                        if dealer_no_sede_stage == 0:
                            assistant_reply = (
                                "Al momento non trovo sedi DFSK registrate nella zona che hai indicato. "
                                "Puoi indicarmi la provincia più vicina a te?"
                            )
                            extra["dealer_no_sede_stage"] = 1
                            print("[CHAT] dealer_nearby: no prov, primo giro -> chiedo provincia vicina")
                        else:
                            assistant_reply = (
                                "Al momento non abbiamo sedi DFSK registrate nella zona che hai indicato.\n\n"
                                "Se vuoi possiamo farti ricontattare da un nostro consulente."
                            )
                            extra["dealer_no_sede_stage"] = 2
                            print("[CHAT] dealer_nearby: no prov, secondo giro -> messaggio finale consulente")
                    else:
                        # abbiamo una provincia dal resolver
                        if dealer_no_sede_stage == 0:
                            # primo tentativo senza sedi in questa provincia
                            assistant_reply = (
                                f"Nella tua provincia {provincia} non abbiamo sedi DFSK registrate, "
                                "puoi indicarci la provincia più vicina a te?"
                            )
                            extra["dealer_no_sede_stage"] = 1
                            extra["dealer_last_provincia"] = provincia
                            print("[CHAT] dealer_nearby: prima provincia senza sedi -> chiedo provincia vicina")
                        else:
                            # secondo tentativo senza sedi (anche se la provincia è diversa, per l'utente è comunque 'secondo giro')
                            assistant_reply = (
                                f"Al momento non abbiamo sedi DFSK registrate in provincia {provincia}.\n\n"
                                "Se vuoi possiamo farti ricontattare da un nostro consulente."
                            )
                            extra["dealer_no_sede_stage"] = 2
                            extra["dealer_last_provincia"] = provincia
                            print("[CHAT] dealer_nearby: seconda provincia senza sedi -> messaggio finale consulente")

    lead.status = new_status
    print(f"[CHAT] lead.status finale={lead.status}")

    history.append({"role": "assistant", "content": assistant_reply})
    state.history_json = json.dumps(history)
    state.step = lead.status
    state.extra_json = json.dumps(extra)

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
