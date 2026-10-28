# ⚡ Attivare i commenti "intelligenti" (LLM) — guida passo‑passo

---

## In una frase

La dashboard sa già scrivere una spiegazione dei dati per l'operatore.
Di default usa frasi predefinite ("template"). Seguendo questa guida farai scrivere
quelle frasi da un'**intelligenza artificiale (GPT)**, che le rende più naturali.

> ✅ **Non devi scrivere nessuna riga di codice.** Devi solo creare un piccolo file di
> configurazione (si chiama `.env`) e incollarci 5 valori che ti diamo noi.
> È come inserire una password del Wi‑Fi: copi e incolli, non "programmi".

⏱️ Tempo necessario: **circa 5 minuti.**

---

## Cosa ti serve prima di iniziare

1. Aver scaricato e aperto la cartella del kit sul tuo computer.
2. Un **terminale** aperto **dentro quella cartella**.
   - Windows: apri la cartella, click nella barra dell'indirizzo, scrivi `cmd`, Invio.
   - Mac: tasto destro sulla cartella → "Servizi" → "Nuovo terminale nella cartella".
3. Un **editor di testo** qualsiasi (va benissimo il Blocco Note / TextEdit, oppure VS Code).

> 💡 Come capisci di essere "dentro la cartella giusta"? Nel terminale scrivi `dir`
> (Windows) o `ls` (Mac) e premi Invio: devi vedere elencati file come `app.py` e
> `.env.example`. Se non li vedi, non sei nella cartella giusta.

---

## Passo 1 — Installa i componenti (una volta sola)

Nel terminale scrivi questa riga e premi Invio:

```bash
pip install -r requirements.txt
```

Aspetta che finisca (può metterci un minuto). Se vedi scorrere del testo e alla fine
torna la riga di comando libera, è andato tutto bene.

---

## Passo 2 — Crea il file `.env`

Nella cartella c'è un file di esempio chiamato **`.env.example`**.
Devi farne una **copia** e chiamarla **`.env`** (senza `.example`).

Nel terminale:

```bash
cp .env.example .env
```

> Su **Windows** usa invece: `copy .env.example .env`

Fatto: ora nella cartella c'è un nuovo file `.env`.

---

## Passo 3 — Scrivi i valori dentro `.env`

Apri il file **`.env`** con l'editor di testo. Cancella tutto quello che c'è dentro e
**incolla esattamente questo**:

```ini
LLM_PROVIDER=azure
LLM_API_KEY=INCOLLA_QUI_LA_CHIAVE
LLM_MODEL=gpt-5.6-luna
LLM_BASE_URL=https://aoi-enelhackaton-sandbox.openai.azure.com
LLM_API_VERSION=2024-12-01-preview
LLM_TIMEOUT=20
```

Poi **sostituisci** `INCOLLA_QUI_LA_CHIAVE` con la **chiave** che ti abbiamo consegnato
(è una stringa lunga di lettere e numeri). Attenzione a **non lasciare spazi** né prima
né dopo la chiave, e a non aggiungere virgolette.

**Salva** il file e chiudilo.

> 🔐 La chiave è **segreta**, come una password. Non pubblicarla, non metterla su internet,
> non inviarla in chat pubbliche. Serve solo per l'hackathon. Il file `.env` è già
> configurato per **non** finire mai nella consegna finale, quindi va tutto bene.

Cosa sono quei valori (te li spieghiamo, ma **non devi cambiarli**, tranne la chiave):

| Valore | Significato semplice |
|--------|----------------------|
| `LLM_PROVIDER` | Il fornitore dell'AI che usiamo (Azure). |
| `LLM_API_KEY` | La tua "password" per usare l'AI. **← l'unico da incollare** |
| `LLM_MODEL` | Quale AI usare (`gpt-5.6-luna`). |
| `LLM_BASE_URL` | L'indirizzo internet a cui la dashboard si collega. |
| `LLM_API_VERSION` | La versione tecnica (già corretta). |
| `LLM_TIMEOUT` | Quanti secondi aspettare una risposta prima di rinunciare. |

---

## Passo 4 — Controlla che funzioni

Nel terminale scrivi:

```bash
python test_llm.py
```

Nel testo che compare, cerca la riga che inizia con **`fonte:`**. È il tuo semaforo:

| Cosa leggi | Significato | Cosa fare |
|-----------|-------------|-----------|
| 🟢 `fonte: llm:azure/gpt-5.6-luna ...` | **Funziona! Sta usando l'AI.** | Vai al Passo 5 |
| 🟠 `fonte: template(fallback:...)` | Ha provato l'AI ma è tornato alle frasi predefinite | Leggi il testo tra parentesi e vedi la tabella "Problemi" sotto |
| ⚪ `fonte: template` (senza "fallback") | L'AI **non è configurata** | Controlla di aver fatto il Passo 2 e 3 (file `.env`, non `.env.example`) |

---

## Passo 5 — Avvia la dashboard

```bash
streamlit run app.py
```

Si aprirà la dashboard nel browser. Guarda la **barra a sinistra**, in alto:

- *"Motore spiegazioni: **LLM · azure/gpt-5.6-luna**"* → 🎉 stai usando l'AI!
- *"Motore spiegazioni: **template locale**"* → manca la configurazione (rivedi Passo 2‑3).

Sotto ogni scheda di raccomandazione c'è la scritta *"Fonte spiegazione: …"*: ti dice,
caso per caso, se quella frase l'ha scritta l'AI o il template.

Per **fermare** la dashboard: torna al terminale e premi `Ctrl + C`.

---

## Problemi frequenti (e come risolverli)

| Cosa vedi | Probabile causa | Soluzione |
|-----------|-----------------|-----------|
| `fonte: template(fallback: ...401...)` o `...403...` | Chiave sbagliata o incollata male | Ricontrolla `LLM_API_KEY` nel `.env`: niente spazi, niente virgolette |
| `fonte: template(fallback: ...timeout...)` | Internet lento o assente | Controlla la connessione; riprova |
| `fonte: template` (senza fallback) | File `.env` non trovato | Verifica che si chiami esattamente `.env` (non `.env.txt` né `.env.example`) |
| `python: command not found` | Python non installato/non nel percorso | Prova `python3` al posto di `python` |
| `pip: command not found` | idem | Prova `pip3` al posto di `pip` |
| La dashboard non ha dati | Manca il passaggio dati | Esegui prima `python train_baseline.py`, poi riprova |

> 🛟 **Nota tranquillizzante:** anche se l'AI non parte, **la dashboard funziona lo stesso**.
> Torna automaticamente alle frasi predefinite ("template") e non resta mai senza
> spiegazione. Quindi non puoi "romperla": puoi solo aggiungere l'AI o no.

---

## Riepilogo (i 5 comandi in ordine)

```bash
pip install -r requirements.txt      # 1. installa i componenti
cp .env.example .env                 # 2. crea il file di configurazione
#    ...poi apri .env e incolla i valori + la chiave (Passo 3)
python train_baseline.py             # 3. prepara i dati per la dashboard
python test_llm.py                   # 4. controlla che l'AI risponda (riga "fonte:")
streamlit run app.py                 # 5. avvia la dashboard
```

Buon hackathon! ⚡
