# PRD – DFSK "Beatrice" Chatbot

## 1. Contesto e obiettivo

Beatrice è l'assistente virtuale DFSK che vive in una pagina web (chat.html) ed espone un backend FastAPI (`main.py`) con:

- raccolta lead (nome, città, cellulare),
- supporto orientativo sui modelli DFSK (Glory 500, 600, F5, E5),
- ricerca delle sedi DFSK più vicine alla città indicata dall'utente usando:
  - API esterna `cap.openapi.it` per risolvere comune e provincia,
  - tabella locale `province_italia` per la mappatura ISTAT, sigla provincia,
  - tabella `dealers` per i concessionari reali.

Il modello OpenAI riceve il contesto e restituisce un JSON strutturato con intent, aggiornamenti al lead e segnali per il frontend (es. `show_model_images`).

---

## 2. Obiettivi

### 2.1 Obiettivi principali

1. **Raccolta lead**
   - Raccogliere `z_name`, `z_city`, `z_cell`.
   - Portare lo stato del lead a `completed` quando tutti i dati sono presenti.
   - Memorizzare la conversazione e lo stato della sessione.

2. **Sedi vicine**
   - Dato un testo dell'utente che chiede "sedi vicino a X", riconoscere l'intent `dealer_nearby`.
   - Usare `cap.openapi.it` per risolvere la città in ISTAT, derivare la provincia, cercare la sigla in `province_italia`.
   - Cercare i dealer nella tabella `dealers` per città, altrimenti per provincia.
   - Gestire anche il caso "nessuna sede in provincia" con un mini stato interno.

3. **Supporto sui modelli**
   - Rispondere a domande sui risultati della knowledge base interna (`KB_DOCS`).
   - Suggerire un modello adeguato alle esigenze espresse.
   - Gestire richieste di foto per singolo modello tramite `show_model_images`.

### 2.2 Non obiettivi

- Gestione prezzi, offerte, finanziamenti o preventivi.
- Gestione agenda o creazione appuntamenti in CRM esterni.
- Gestione di marchi o prodotti non DFSK.

---

## 3. Architettura ad alto livello

- **Backend**
  - `main.py` con FastAPI e SQLAlchemy.
  - Endpoints principali:
    - `POST /session` per creare una nuova sessione.
    - `POST /chat` per processare un messaggio utente.
    - `GET /` per servire `chat.html`.
    - `GET /health` per health check.

- **Persistenza**
  - SQLite (o altro DB via `DATABASE_URL`) con tabelle:
    - `leads`
    - `conversation_state`
    - `dealers`
    - `province_italia`

- **Integrazioni**
  - OpenAI API (embeddings, modello `gpt-4o-mini` con `responses.parse`).
  - API `cap.openapi.it` (`/province` da script offline, `/cerca_comuni` da backend).

---

## 4. Formato di risposta del modello

Il modello deve SEMPRE restituire un JSON di questo tipo:

```jsonc
{
  "assistant_reply": "testo da dire all'utente (max 3 frasi)",
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
```

### 4.1 Campi speciali

#### assistant_reply

Testo finale mostrato all'utente.

Massimo 3 frasi, con stile definito nel system prompt.

#### intent_type

- **lead_answer**: l'utente sta dando un dato richiesto (nome, città, telefono).
- **lead_refusal**: rifiuta di dare un dato.
- **lead_objection**: chiede spiegazioni sul perché del dato, ma non chiude la porta (es. "a che ti serve il numero?").
- **kb_question**: domande sui modelli DFSK e informazioni auto.
- **dealer_nearby**: richiesta di sedi / concessionari vicini.
- **smalltalk**: saluti, chiacchiere, meteo, ecc.
- **other**: tutto ciò che non rientra sopra.

#### lead_updates

I campi non nulli vengono scritti nel record Lead.

Nessun testo di rifiuto o frasi, solo dati grezzi.

#### new_status

- **in_progress**: conversazione in corso.
- **completed**: tutti i dati raccolti, richiesta appuntamento pronta.
- **failed**: l'utente non vuole procedere o nega più volte i dati necessari.

Nel ramo dealer_nearby il backend forza new_status allo stato attuale del lead, non si chiude un lead "solo chiedendo sedi".

#### show_model_images

null di default.

Valori validi: "Glory 500", "Glory 600", "Glory F5", "Glory E5".

Il frontend usa questo campo per mostrare le immagini del modello richiesto.

#### dealer_city_raw

Nome città così come interpretato dal modello, es. "Budrio", "Catania".

Usato solo quando intent_type = "dealer_nearby".

---

## 5. Intents e flussi principali

### 5.1 Intent dealer_nearby

**Trigger**: frasi tipo "sedi vicino a X", "dove siete in zona Y", "avete sedi ad Augusta?".

**Comportamento backend /chat**:

1. Recupero ConversationState e Lead.

2. Recupero extra_json che contiene:
   - `dealer_no_sede_stage` (0, 1, 2)
   - `dealer_last_provincia`

3. Chiamata al modello, ottenimento JSON.

4. Se `intent_type = dealer_nearby`:
   - `new_status` viene forzato a `lead.status` corrente.
   - `show_model_images` viene azzerato.
   - Si decide la città di lavoro `city_text`:
     - se `dealer_city_raw` non è null, usare quello,
     - altrimenti usare `lead.z_city` se esiste.
   - Se non c'è una città, l'assistente risponde chiedendo esplicitamente "In che città sei?".
   - Se la città esiste, si chiama `resolve_city(city_text, db)` che:
     - chiama `/cerca_comuni?comune=<nome>` su cap.openapi.it con Bearer token,
     - estrae il primo istat del comune,
     - prende le prime 3 cifre come codice provincia,
     - cerca nella tabella `province_italia` un record con istat uguale,
     - produce un dict `{"city": nome_comune, "provincia": sigla_provincia}`.
   - Con `city_info` si chiama `find_dealers_near(db, city_info, limit=3)`:
     - prima cerca dealer per `Dealer.city ilike city`,
     - se zero risultati e provincia presente, cerca per `Dealer.provincia == sigla`.
   - In base ai risultati:
     - **se ci sono dealer**:
       - `extra["dealer_no_sede_stage"] = 0`
       - `extra["dealer_last_provincia"] = sigla`
       - risposta formattata con elenco sedi.
     - **se non ci sono dealer e non abbiamo sigla provincia**:
       - se `dealer_no_sede_stage == 0` chiede "provincia più vicina".
       - altrimenti messaggio finale "nessuna sede, possiamo farti ricontattare da un consulente".
     - **se non ci sono dealer ma abbiamo sigla**:
       - se `dealer_no_sede_stage == 0` messaggio "nella tua provincia XX non abbiamo sedi, indicaci provincia più vicina", passa a stage 1.
       - altrimenti messaggio finale "non abbiamo sedi in provincia XX, possiamo farti ricontattare".

### 5.2 Intent kb_question

Risposte informative sui modelli DFSK, usando KB_DOCS.

Può settare `show_model_images` quando l'utente chiede foto di un modello specifico.

Il backend non modifica nulla nel DB oltre a eventuali `lead_updates`.

### 5.3 Intent lead_answer, lead_refusal, lead_objection

#### lead_answer

Dati in `lead_updates` vengono salvati.

Se, dopo l'update, non ci sono più campi mancanti (`z_name`, `z_city`, `z_cell`), il modello deve impostare `new_status = "completed"`.

#### lead_refusal

Il modello lascia il campo relativo a null in `lead_updates`.

Il testo spiega che senza quel dato non si può completare la richiesta.

#### lead_objection

Il modello spiega la motivazione della richiesta del dato.

Ripete la richiesta dello stesso dato nella stessa risposta.

### 5.4 Intent smalltalk e other

#### smalltalk

Risposte brevi, poi si torna su auto o appuntamento.

Nessuna modifica di stato lead se non eventuali `lead_updates` validi.

#### other

Catch all per domande fuori contesto.

L'assistente spiega che può occuparsi solo di auto DFSK in questa chat.

---

## 6. Schema database

### 6.1 Tabella leads

```sql
leads
- id              Integer, PK
- session_id      String, unique, index
- z_name          String, nullable
- z_city          String, nullable
- z_cell          String, nullable
- z_email         String, nullable (legacy, non usato nella logica)
- z_provincia     String, nullable (legacy)
- status          String, default "in_progress" ("in_progress", "completed", "failed")
- created_at      DateTime, default datetime.utcnow
```

### 6.2 Tabella conversation_state

```sql
conversation_state
- id              Integer, PK
- session_id      String, unique, index
- step            String, es. "generic" o stato lead ("in_progress", "completed", "failed")
- attempts_json   Text, default "{}" (non usato nella logica attuale)
- history_json    Text, JSON con lista messaggi [{role: "user"|"assistant", content: "..."}]
- extra_json      Text, JSON per stato aggiuntivo:
                  {
                    "dealer_no_sede_stage": 0|1|2,
                    "dealer_last_provincia": "BO" | "SR" | ...
                  }
```

### 6.3 Tabella dealers

```sql
dealers
- id              Integer, PK
- company_name    String, not null, NOME AZIENDA
- address_showroom String, nullable, SHOW ROOM
- address_officina String, nullable, OFFICINA
- city            String, not null, città (es. "Crema", "Catania")
- provincia       String, not null, sigla provincia (es. "CR", "CT")
- phone_vendita   String, nullable, telefono vendita
- phone_officina  String, nullable, telefono officina
- email           String, nullable, referente o email generica
```

### 6.4 CSV / Excel dealer

Il CSV/Excel importato in `dealers` deve avere, o essere mappato su:

- NOME AZIENDA → `company_name`
- SHOW ROOM → `address_showroom`
- OFFICINA → `address_officina`
- CITTÀ → `city`
- PROVINCIA → `provincia` (sigla, es. "BO")
- TEL VENDITA → `phone_vendita`
- TEL OFFICINA → `phone_officina`
- EMAIL → `email`

Gli script di import (non descritti qui) devono normalizzare la città e la provincia coerenti con come vengono usate nelle query.

### 6.5 Tabella province_italia

Popolata dallo script `import_provinces.py`.

```sql
province_italia
- id              Integer, PK
- sigla           String, not null, sigla provincia (es. "BO", "CT", "SR")
- provincia       String, not null, nome provincia (es. "Bologna", "Catania")
- istat           String, not null, codice ISTAT provincia (3 cifre, es. "037", "087", "089")
- regione         String, nullable, nome regione (es. "Emilia-Romagna")
```

---

## 7. Script import_provinces.py

Funzioni principali:

- Usa `COMUNI_API_BASE` (default `https://cap.openapi.it`) e `COMUNI_API_TOKEN`.
- Droppa e ricrea solo la tabella `province_italia`.
- Chiama:

```http
GET {COMUNI_API_BASE}/province
Authorization: Bearer {COMUNI_API_TOKEN}
Accept: application/json
```

Si aspetta una risposta con:

```json
{
  "data": [
    {
      "sigla": "AG",
      "provincia": "Agrigento",
      "superficie": 3052.59,
      "residenti": 446081,
      "num_comuni": 43,
      "istat": "084",
      "regione": "Sicilia"
    },
    ...
  ],
  "success": true,
  "message": "",
  "error": null
}
```

Inserisce in `province_italia` solo le righe con sigla, provincia, istat non vuoti.

---

## 8. Integrazione cap.openapi.it in produzione

Nel backend (`main.py`):

**Variabili ambiente**:

- `COMUNI_API_BASE`, default `https://cap.openapi.it`
- `COMUNI_API_TOKEN`, token già configurato, obbligatorio per le chiamate.

### 8.1 Funzione resolve_city(raw_city, db)

**Passi**:

1. Normalizza nome città, verifica cache `CITY_CACHE`.

2. Se mancano `COMUNI_API_BASE` o `COMUNI_API_TOKEN`:
   - Fallback minimale `{"city": name.title(), "provincia": ""}`.

3. Costruisce `url = COMUNI_API_BASE.rstrip("/") + "/cerca_comuni"`.

4. Chiama:

```http
GET {url}?comune={name}
Authorization: Bearer {COMUNI_API_TOKEN}
Accept: application/json
```

5. Parsing risposta:

```json
{
  "data": {
    "result": [
      {"istat": "37008", "comune": "Budrio", "suppressed": false},
      ...
    ],
    "suppressed": [...]
  },
  "success": true,
  "message": "",
  "error": null
}
```

6. Prende il primo elemento di `data.result`.

7. `istat_code = first["istat"]`, `prov_istat = istat_code[:3]`.

8. Usa `db.query(ProvinceItalia).filter(ProvinceItalia.istat == prov_istat).first()` per mappare provincia e sigla.

9. Ritorna `{"city": city_name, "provincia": sigla_provincia}`.

10. In caso di errore o formato inatteso, fallback a `{"city": name.title(), "provincia": ""}`.

---

## 9. Flusso immagini modelli

**Utente chiede "mi fai vedere le foto" senza modello**:

- Il modello risponde chiedendo quale modello, `show_model_images = null`.

**Utente specifica "della Glory 500"**:

- Intent tipicamente `kb_question`.
- `show_model_images = "Glory 500"`.
- L'assistente dice che sta mostrando le foto del modello scelto, e facoltativamente propone l'appuntamento.

**Il frontend deve**:

- se `show_model_images` è non null, sostituire o affiancare la risposta con la galleria immagini del modello corrispondente.

---

## 10. Backlog e miglioramenti futuri

### Forzare il modello a valorizzare sempre dealer_city_raw

Rafforzare il system prompt con esempi concreti tipo:

- "invece vicino ad Augusta?" ⇒ `dealer_city_raw = "Augusta"`.
- "e a Catania?" ⇒ `dealer_city_raw = "Catania"`.

Obiettivo: evitare casi in cui l'intent resta `dealer_nearby` ma `dealer_city_raw` rimane null e il backend riusa solo `lead.z_city`.

### Gestione esplicita del cambio città per sedi

Migliorare la logica del backend in `dealer_nearby` per casi tipo:

- "no, sono in un altra città" senza nuova città esplicitata.

Possibile regola futura:

- se l'utente smentisce chiaramente la città precedente ma non indica una nuova città, la risposta deve chiedere la nuova città e non rilanciare direttamente i dealer della vecchia.

### Gestione multi città in una conversazione continua

Caso reale già visto nei log:

- l'utente chiede prima Budrio, poi Augusta, poi Siracusa, poi Catania.

Il motore tecnico è già corretto, ma si può:

- rendere più esplicito nel testo che si è passati a un'altra zona (es. "Per Augusta non abbiamo sedi, vedo invece cosa c'è a Catania"),
- evitare di reimpostare `z_city` del lead in modo ambiguo se si usano più città solo per esplorare.

### Micro refined della UX nel ramo "nessuna sede in provincia"

Al secondo giro senza sedi (stage 2) inserire nel prompt del modello indicazioni più precise su come proporre il ricontatto da un consulente senza confondere l'utente.

Valutare se alla chiusura del giro province si debba:

- proporre in modo più esplicito la raccolta dati per un ricontatto remoto (es. video call o altra sede distante),
- oppure chiudere con un messaggio di servizio più chiaro.

### Validazioni lato backend sui dati lead

Aggiungere controlli hard, per esempio:

- `z_cell` deve contenere almeno X cifre dopo normalizzazione,
- lunghezza massima dei campi `z_name`, `z_city`.

In caso di dato non valido scartare l'update e chiedere conferma, per ridurre il rischio che il modello inserisca stringhe rumorose.
