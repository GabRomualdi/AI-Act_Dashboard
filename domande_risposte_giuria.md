# Domande e risposte per la giuria – EnerGuard

Raccolta delle domande presenti nelle guidelines (Guida tecnica, Indicatori di qualità, Istruzioni dello starter kit)
e di quelle che la giuria potrebbe ragionevolmente fare.

**Come leggere i riferimenti**
- I riferimenti al codice sono nella forma `file:riga`, relativi a `dashboard/backend/`.
- I riferimenti alla dashboard indicano la scheda e l'elemento da mostrare.

**Avvio della demo:** `python dashboard/backend/app.py`, poi apri http://127.0.0.1:8000.
Sono disponibili anche `--senza-llm` (solo template) e `--port`.

---

## 0. Le cinque domande dell'operatore (principio guida degli Indicatori)

| Domanda | Risposta in dashboard |
|---|---|
| **Cosa sta succedendo?** | Barra KPI in alto: decisioni in coda ed escalation, distribuzione HIC/HITL/HOTL, integrità del log, motore delle spiegazioni. Poi la scheda **Coda decisioni** e la **Matrice confidenza × rischio**. |
| **Perché il sistema raccomanda questo?** | Ogni card mostra la spiegazione in linguaggio operativo con 3 fattori, la regola di routing (R1–R8), la spiegazione contrastiva e i casi simili. |
| **Quanto posso fidarmi, qui e ora?** | Sulla card: P(guasto), confidenza e frase sull'incertezza. Nella scheda **Fairness & drift**: recall per area, calibrazione e drift. |
| **Cosa posso fare io?** | Bottoni Approva / Modifica / Rifiuta / Escalation con motivazione obbligatoria. Per le HOTL, "Contesta" ex post nelle **Decisioni recenti**. |
| **Come lo fermo?** | Bottone **■ Stop** sempre visibile nell'header, con area e tipo selezionabili: 1 click più la conferma. |

---

## 1. Test dal vivo della giuria (Strato 2)

### T1 · Override reale – "Rifiutate questa decisione HITL e dimostrate che l'azione non è stata eseguita"

**Risposta**
- In coda si sceglie una card HITL, si clicca **Rifiuta** e si scrive una motivazione (almeno 15 caratteri).
- La decisione passa in *Decisioni recenti* con la dicitura **"non eseguita"**.
- Nell'audit trail compare `revisione_RIFIUTATA` e **non** compare `esecuzione_azione`. Lo si vede con *Audit trail → Ricostruisci* sull'asset: il campo "eseguita" è falso.

**Dove**
- `oversight_manager.py:521` (`revisiona`): `_esegui` viene chiamato **solo** per APPROVATA o MODIFICATA.
- Endpoint `POST /api/decisioni/{id}/revisione` (`app.py:632`).

### T1-bis · "Mostrateci nel codice l'unico punto in cui un'azione viene eseguita, e dimostrate che una decisione rifiutata non può raggiungerlo"

**Risposta**
- Il punto unico è `OversightManager._esegui` (`oversight_manager.py:956`). Ha esattamente due chiamanti:
  1. `sottometti` (`:500`), solo per HOTL;
  2. `revisiona` (`:579`), solo per APPROVATA o MODIFICATA.
- Anche se qualcuno lo chiamasse impropriamente, `_motivo_non_eseguibile` (`:973`) ricontrolla lo stato e nega l'esecuzione se:
  - lo stato è diverso da APPROVATA, MODIFICATA o AUTO_ESEGUITA;
  - l'esecuzione automatica riguarda un livello diverso da HOTL;
  - manca una revisione umana motivata;
  - è attivo uno stop.
- In questi casi solleva `EsecuzioneNonAutorizzata` e registra `esecuzione_negata` nel log.

**Dove**
- Scheda **Monitoraggio KPI**: il KPI **A1** (auto-esecuzione impropria) deve valere 0, e il registro delle esecuzioni riporta `punto_unico` (`app.py:521`).
- Prova di ricerca: `grep -n "_esegui(" dashboard/backend/*.py`.

### T2 · Motivazione – "Approvate con motivazione vuota, poi con 'ok'"

**Risposta**
- Entrambe vengono rifiutate con errore 400 e il messaggio appare sotto il campo.
- `_valida_motivazione` (`oversight_manager.py:672`) richiede:
  - almeno **15 caratteri**;
  - almeno 2 parole;
  - almeno 5 lettere diverse.
  Testi come "aaaaaaaaaaaaaaaa" o "ok ok ok ok ok" quindi non passano.
- **Anche la motivazione "fotocopia" è bloccante.** `_blocca_fotocopia` (`:596`) è chiamata da `revisiona` (`:568`) prima di qualsiasi cambio di stato. `_trova_fotocopia` (`:650`) confronta la motivazione con quelle già usate per altre decisioni, ignorando maiuscole, accenti, punteggiatura e numeri (cambiare solo l'ID asset non basta). Se la similarità testuale è ≥ 0.85 (`SOGLIA_FOTOCOPIA`, `:304`):
  - la revisione è **respinta** con errore 400 (`RubberStamping`, `:95`) e il messaggio indica da quale asset è copiata;
  - la decisione **resta in coda** (IN_ATTESA) e **non viene eseguita**;
  - il testo dell'errore resta visibile sulla card finché l'operatore non riscrive la motivazione;
  - il tentativo è registrato nell'audit trail come `rubber_stamping_bloccato` (con esito tentato, motivazione, asset d'origine, similarità), così non si perde traccia.
- Restano **segnalazioni non bloccanti** (conteggiate in A3/A4, ma la revisione viene accettata): motivazione sotto i 30 caratteri e revisione sotto i 10 secondi (`_flag_revisione`, `:516`).
- **Dove si vede:** toast e messaggio rosso sulla card al momento del blocco; KPI **A4** che sale (conta le fotocopie respinte); colonna **Respinte** nella tabella per revisore; evento `rubber_stamping_bloccato` in *Audit trail*.
- **Precedenze:** il controllo dei permessi viene prima (un operatore su una HIC riceve 403, non il messaggio di fotocopia). Le **escalation non sono bloccate**: serve a passare la decisione a un supervisore, non a chiuderla.

**Dove**
- Scheda **Monitoraggio KPI** → A3, A4 e la tabella per revisore (`statistiche_revisori`, `:950`).

### T3 · Emergency stop parziale – "Fermate le linee AT del Sud senza fermare il resto"

**Risposta**
- Nell'header: area **Sud**, tipo **linea_AT**, poi **■ Stop**.
- La finestra di conferma mostra l'anteprima delle decisioni coinvolte e chiede la motivazione: 1 click più la conferma (B4).
- Effetti:
  - le decisioni già in coda nell'ambito diventano `BLOCCATA_STOP` e sono **non eseguibili**;
  - le nuove raccomandazioni nell'ambito nascono già bloccate;
  - il resto continua normalmente.
- L'attivazione è registrata (`emergency_stop_ON`) con motivazione. Lo stop resta visibile come chip rosso nell'header.
- La **riattivazione** richiede motivazione e conferma esplicita. Le decisioni sbloccate tornano in coda **almeno HITL** (R6) e niente riparte in automatico.

**Dove**
- `oversight_manager.py:770` (`attiva_stop`), `:793` (`disattiva_stop`), `:831` (`_reinstrada_post_stop`), `:725` (ambiti `GLOBALE` / `area:` / `tipo:` / `area:+tipo:`).
- Controllo nel punto di esecuzione: `:871`.
- Dashboard: chip "STOP …" nell'header e sezione "Bloccate da emergency stop" sotto la coda.

### T4 · 60 secondi – "Un giurato non tecnico deve capire cosa raccomanda, perché e quanto è sicuro"

**Risposta.** Ogni card contiene:
1. azione raccomandata in italiano (es. "Programmare una manutenzione");
2. spiegazione in linguaggio da control room, con 3 fattori: valore, valori tipici della flotta, giudizio ("molto sopra la norma") e direzione ("aumenta il rischio");
3. **incertezza** sempre visibile ("La confidenza è 68%, inferiore alla soglia…");
4. motivo del livello di supervisione (regola R1–R8);
5. spiegazione **contrastiva** ("se il carico passasse da 100% a 67.8%, la probabilità scenderebbe al 37%…");
6. **casi simili** dallo storico, con avvertenza se provengono dal Sud.

**Dove**
- `explainer.py:373` (`SpiegatoreTemplate`), `:405` (`_incertezza`), `:457` (`controfattuale`), `:522` (`CasiSimili`).
- Composizione nella card: `app.py:171` (`_sottometti_riga`).

### T4-bis (con LLM) · "Cosa succede se stacchiamo la rete o mettiamo una chiave errata? Quali guardrail impediscono all'LLM di inventare numeri?"

**Risposta: fallback**
- Scheda **Soglie & LLM**: "Simula chiave errata" o "Simula rete assente", poi "Rigenera spiegazione" su una card (oppure "+5 nuove raccomandazioni").
- La card riceve comunque la spiegazione dal template, con fonte dichiarata `template(fallback:…)`. **Nessuna card resta mai senza spiegazione.**
- La spiegazione a template viene mostrata subito; quella dell'LLM arriva in background e la sostituisce.

**Risposta: guardrail** (`explainer.py:923`)
1. **Ogni numero** citato deve essere presente nei dati passati all'LLM. Sono ammessi arrotondamenti e la forma percentuale.
2. Niente **gergo tecnico**: lista `GERGO` (`:696`), es. "shap", "feature", "coefficient".
3. Il **fattore principale** deve essere citato.
4. L'**azione proposta** deve essere citata.
5. È imposto un limite di lunghezza.

Qualunque violazione, o qualunque errore (rete, quota, timeout, JSON malformato), porta al fallback sul template (`:748`, `:780`). L'evento è registrato nel log.

**Dove**
- KPI **B3b** (fonte dichiarata) e **B3c** (tasso di fallback < 5%).
- `POST /api/llm/guasto` (`app.py:671`).

### T5 · Bias nascosto – "Cosa avete scoperto sui dati storici di quest'azienda?"

**Risposta**
- **Anomalia.** Sud e Isole hanno profili tecnici simili: età media 23.2 contro 24.9 anni, manutenzioni in 5 anni 2.1 contro 1.7, giorni dall'ultima manutenzione 343 contro 386. Eppure i guasti registrati sono **24% contro 45%**.
- **Confronto a parità di profilo.** Con un abbinamento 1:1 tra asset gemelli, il Sud registra 0.242 contro 0.364.
- **Atteso contro registrato.** Il profilo tecnico stima per il Sud 0.368 contro 0.242 registrato.
- **Calibrazione.** Al Sud il gap è **+0.159**, l'unico gruppo oltre la soglia di 0.10 (C2).
- **Ipotesi.**
  1. Sotto-segnalazione dei guasti (riparazioni non registrate).
  2. Manutenzione correttiva affidata a terzi e fuori dai sistemi aziendali.
  3. Minore copertura dei sensori.
- **Mitigazione implementata e misurata.**
  - Il Sud è promosso automaticamente a HITL (R5).
  - Le card del Sud mostrano un avviso.
  - I casi simili segnalano quando i confronti provengono dal Sud.
  - Prima/dopo: i guasti reali che finirebbero in HOTL scendono **da 9 a 3**.

**Dove**
- `bias_detector.py:201` (`indagine_label_bias`), `:259` (`confronto_abbinato`), `:98` (`calibrazione_per_gruppo`).
- `app.py:226` (`_aggiorna_promozioni`), `app.py:406` (`misura_mitigazione`).
- Scheda **Fairness & drift**: box "Cosa abbiamo scoperto (T5)", tabella label bias e tabella mitigazione prima/dopo.

### T6 · Audit a ritroso – "Ricostruite in un minuto una decisione chiusa un'ora fa. Poi dimostrate che il log non è manomissibile in silenzio"

**Risposta: ricostruzione**
- Scheda **Audit trail**: si inserisce l'asset_id e si clicca **Ricostruisci**.
- Per ogni decisione si vede:
  - **chi** ha deciso, **cosa** e **quando**;
  - il **perché** (la motivazione);
  - se l'AI è stata **approvata o corretta** (azione AI contro azione finale);
  - se l'azione è stata eseguita;
  - eventuali escalation e contestazioni;
  - la fonte della spiegazione;
  - la cronologia degli eventi con gli hash.

**Risposta: integrità**
- Il log è append-only con catena SHA-256: ogni record contiene l'hash del precedente.
- L'indicatore di integrità è sempre visibile.
- **"Simula manomissione"** altera un record su una copia in memoria e mostra quale record viene rilevato come non valido.
- La verifica confronta anche la coda del file con l'ultimo hash scritto dal processo, quindi rileva troncamenti e catene ricalcolate da zero.

**Dove**
- `audit_logger.py:214` (`ricostruisci`), `:230` (`_sintesi`), `:112` (`verifica`), `:150` (`simula_manomissione`), `:342` (`_hash`), `:293` (`esporta`).
- **Esporta JSONL** per l'audit esterno.

---

## 2. Domande della Guida tecnica (Tier 1–4)

### "Costa di più un falso positivo o un falso negativo? La soglia deve riflettere l'asimmetria."

**Risposta**
- Un falso negativo (guasto non previsto, magari su una cabina che serve un ospedale) costa molto più di un'ispezione inutile.
- Per questo la **soglia decisionale è 0.20** (su probabilità calibrate, equivalente a 0.35 sul modello non calibrato) e non 0.50 (`oversight_manager.py:107`, usata anche in `train_baseline.py:31`).
- Il modello è un RandomForest (300 alberi, `class_weight="balanced"`, `train_baseline.py:42`).
- Risultati sul test set (720 asset): **AUC 0.862**, accuracy 0.774, **recall 0.752**.

### "Annotate ogni gap superiore a 15 punti di recall (per area e per tipo di asset)."

**Risposta**
- **Per area:** Nord 0.536 · Centro 0.542 · Sud 0.794 · Isole 0.953. Il **gap massimo è 0.417**, con allerta C1 per Nord, Centro e Sud.
- **Per tipo di asset:** trasformatore 0.698 · linea_AT 0.706 · cabina_primaria 0.762 · turbina_eolica 0.839. Il gap è 0.141: sotto soglia, ma vicino.
- **Dove:** `bias_detector.py:69`, `:117`. Scheda **Fairness & drift**.

### "Sud e Isole: formulate almeno due ipotesi."

Vedi T5: sotto-segnalazione, manutenzione correttiva di terzi non registrata, minore copertura dei sensori.
Sono riportate anche in `DichiarazioneDiOversight.md` §4.

### "Perché proprio quelle soglie?" (la giuria non chiede quali, chiede perché)

**Risposta**
- **Rischio 0.85 → HIC (non 0.60 → HITL della guida):** da 0.85 il guasto è molto probabile e anche non intervenire è una scelta pesante, quindi decide solo l'umano, anche se il modello è affidabile (a destra della curva la decisione passa da HITL a HIC). Sul test set R3 riguarda 5 asset su 720 (HIC totale 9.3%). Sotto 0.85 le azioni con P ≥ 0.55 non sono mai leggere e restano comunque HITL.
- **Confidenza 0.80 con curva (criticità ≤ 0.20):** R3, R4 e R7 sono calcolate da un'unica funzione di zona (`zona_matrice`) basata sulla curva f(P) = 0.80 + 0.10 × P(guasto), che va da (0.80, 0) a (0.90, 1) e divide la matrice per tutta l'altezza. Sotto la curva: zona gialla (HITL, R4) o rossa se P ≥ 0.85 (HIC, R3). Sulla curva o a destra: zona verde (HOTL, R7) o HIC se P ≥ 0.85. La criticità (1 − confidenza) + 0.10 × P(guasto) è la stessa curva espressa come numero. Più è alto il rischio, più deve essere affidabile il modello. Sul test set i guasti mancati che finiscono in HOTL sono 9 (16 con la soglia piatta a 0.78), gli errori eseguiti in autonomia 15 (erano 29), HOTL al 49% (era 54%). Un peso extra per le utenze `alta` è stato valutato e scartato: toglieva autonomia senza togliere guasti mancati.
- **Margine di drift +0.05:** sposta la curva a destra (da 0.85 a 0.95, criticità ≤ 0.15) quando l'accuratezza recente scende sotto 0.80 (oggi attivo): HOTL 42%, guasti mancati in HOTL 5.
- **SLA 30 min:** alla scadenza la decisione va in escalation, mai in esecuzione.
- **Motivazione:** minimo 15 caratteri (bloccante); sotto i 30 viene segnalata (A4); una motivazione fotocopia (similarità ≥ 0.85) è respinta e conta in A4.
- **Revisione rapida:** sotto i 10 secondi viene segnalata (A3), senza bloccare.
- **Allerte:** C1 > 0.15, C2 > 0.10; override (C3) ≥ 40% su almeno 5 giudizi.
- La giustificazione completa è in `DichiarazioneDiOversight.md` §3.

**Dove**
- Scheda **Soglie & LLM**: le soglie sono modificabili con ID operatore e motivazione, e ogni modifica è registrata come `modifica_soglie` (`oversight_manager.py:849`).

### "Come è definita la confidenza?"

**Risposta**
- **Meta-modello di affidabilità** (`modello_calibrato.py`, `MetaAffidabilita`): un secondo Random Forest, addestrato sul training set a prevedere se il modello (valutato out-of-fold) ha dato la risposta giusta. La confidenza è la sua stima di P(previsione corretta).
- **Perché non `max(p, 1−p)`:** dipendeva solo da p, quindi nella matrice i punti stavano su due rette e R3/R4 erano di fatto una sola dimensione. Anche il disaccordo tra gli alberi del RandomForest è stato scartato: con foglie pure è esattamente `√(v(1−v))`, ancora una funzione di p.
- **Perché il meta-modello non vede p:** con p tra gli input prevede meglio gli errori in generale (AUC 0.90), ma impara che "p basso = quasi sempre giusto" e lascia in autonomia fino a 27 guasti mancati. Usa quindi solo le caratteristiche dell'asset (pesano di più vibrazione, giorni dall'ultima manutenzione, età).
- **Prova che funziona:** prevede gli errori con AUC 0.83 (contro 0.75 del metodo precedente basato sui 30 casi più simili) e sul test set l'accuratezza reale va dal 52% nel quartile di confidenza più basso al 100% in quello più alto. La correlazione con `max(p, 1−p)` è 0.83: in parte segue il rischio, ma i punti nella matrice restano sparsi.
- **Limiti:** tra 0.6 e 0.8 è un po' ottimista (dichiara circa 0.75, ha ragione circa nel 63% dei casi); stima l'affidabilità da errori passati, non su situazioni mai viste. Per questo resta affiancata a calibrazione per gruppo e drift.

### "Mostrate la matrice di routing e dimostrate che il codice fa ciò che il documento promette."

**Risposta**
- La matrice R1–R8 è in `oversight_manager.py:206` (`MATRICE_ROUTING`, con condizione, livello e motivazione). L'indice di criticità (`indice_criticita`, `PENDENZA_CRITICITA`) è a `:150`; la curva e le zone (`frontiera_hotl`, `zona_matrice`) a `:164` e `:169`.
- Il routing vero è scritto in modo **indipendente** in `route()` (`:373`).
- Il KPI **A6** confronta le due implementazioni decisione per decisione (`verifica_routing`, `:431`). Target: 100%.

**Dove**
- Scheda **Matrice confidenza × rischio**: la curva f(P) divide la matrice per tutta l'altezza. A sinistra: zona rossa sopra la soglia di rischio 0.85 (HIC, R3) e zona gialla sotto (HITL, R4). A destra: zona verde (HOTL, R7) e, sopra 0.85, un'area tratteggiata in cui la decisione passa da HITL a HIC (R3). Ogni punto è una decisione, colorata per livello, con la regola nel tooltip: un punto di colore diverso dalla sua zona è stato deciso da una regola prioritaria (R1, R2, R5, R6, R8). Cliccandolo si apre la scheda completa della segnalazione. La scheda include la tabella R1–R8 valutata sui valori e sulle soglie in vigore al momento del routing: per ogni regola mostra se si applica e quale ha deciso il livello (`valutazione_routing` in `oversight_manager.py`).
- `DichiarazioneDiOversight.md` §2.

### "Il revisore può approvare, modificare, rifiutare, fare escalation?"

**Risposta**
- Sì: `revisiona` (`oversight_manager.py:521`).
- **Modifica:** l'azione alternativa deve essere diversa da quella proposta. L'azione dell'AI resta registrata in `azione_originale`.
- **Escalation:** la chiude solo un supervisore (`SUP-…`).
- **Decisioni HIC:** può chiuderle solo un supervisore (revisori differenziati per competenza). Un operatore può solo metterle in escalation.

### "Cosa succede alle decisioni HITL non revisionate entro lo SLA?"

**Risposta**
- `controlla_sla` (`oversight_manager.py:707`), eseguito a ogni polling della dashboard, le porta in **ESCALATION** con log `escalation_SLA`.
- Non vengono **mai** eseguite in silenzio.
- KPI **A5**: escalation per SLA scaduto, target < 15%.

### "Ogni card mostra i 3 fattori in linguaggio da control room?"

**Risposta**
- Sì. I fattori sono calcolati con SHAP TreeExplainer (`explainer.py:298`, `:323`) e tradotti in valore, valori tipici, giudizio e direzione.
- Esempio: "età dell'asset 25 anni, sopra la norma (13–23): aumenta il rischio".
- KPI **B1** (copertura ≥ 3 fattori) = 100%.

### "Perché avete scelto (o non scelto) un LLM? Lo avete motivato nella model card?"

**Risposta**
- Usiamo l'LLM (Azure, configurato in `dataset/.env`) **solo per tradurre** in italiano operativo i numeri già calcolati: non decide e non calcola.
- **Costi accettati:** non determinismo, latenza (~3 s), costo, dipendenza da un fornitore esterno.
- **Come li governiamo:**
  - il template viene mostrato subito e l'LLM arriva in background;
  - guardrail;
  - fallback automatico;
  - fonte dichiarata;
  - log di ogni chiamata;
  - cache;
  - tasso di fallback monitorato (B3c).
- Senza `.env`, o con `--senza-llm`, il sistema funziona interamente a template.

**Dove**
- `explainer.py:691` (`SpiegatoreLLM`), `app.py:200` (`_genera_spiegazione`), `app.py:211` (`_job_llm`).

### "Mostrate la calibrazione per gruppo, il drift nel tempo e il tasso di override per area."

**Risposta**
- **Calibrazione:** probabilità media predetta contro tasso osservato, per area (C2).
- **Drift:** test set diviso in 12 "settimane", con andamento di accuratezza e confidenza media. Soglia 0.80; ultime 2 settimane a 0.692, quindi **allerta** (C4).
- **Override per area:** C3, calcolato su rifiuti, modifiche e contestazioni HOTL.

**Dove**
- `bias_detector.py:98`, `:166`; `oversight_manager.py:1108`.
- Scheda **Fairness & drift**.

### "C'è almeno un alert automatico che modifica il routing?"

**Risposta.** Sì, quattro (`app.py:226` → `oversight_manager.py:908`):
1. C2 promuove l'area a HITL;
2. il label bias promuove l'area a HITL;
3. l'override ≥ 40% (C3) promuove l'area a HITL;
4. il drift (C4) aggiunge +0.05 alla soglia di confidenza (la curva HOTL si sposta a destra).

Promozioni e revoche sono registrate nel log. È possibile anche la promozione o revoca manuale con motivazione (scheda **Fairness & drift** → "Promuovi / revoca").

### "Perché il gap di recall (C1) non promuove automaticamente le aree?"

**Risposta**
- Tre aree su quattro superano la soglia 0.15: promuoverle tutte eliminerebbe l'HOTL.
- La coda umana ne risulterebbe insostenibile, con rischio di escalation (A5) e di rubber-stamping (A4).
- Scelta dichiarata: allerta visibile, più soglia decisionale bassa, più drift, più promozione manuale a discrezione del supervisore.

### "Nel mondo reale chi è il provider e chi il deployer, e quali obblighi hanno?"

**Risposta**
- **Provider:** chi sviluppa il sistema e lo immette sul mercato o in servizio con il proprio nome. Può essere il fornitore tecnologico, oppure il gestore di rete stesso se lo sviluppa internamente.
  Obblighi principali:
  - gestione del rischio (Art. 9);
  - dati e bias (Art. 10);
  - documentazione tecnica (Art. 11);
  - logging (Art. 12);
  - istruzioni e trasparenza (Art. 13);
  - progettazione per l'oversight (Art. 14);
  - accuratezza, robustezza e cybersicurezza (Art. 15);
  - sistema di gestione della qualità;
  - valutazione di conformità, marcatura CE e registrazione nella banca dati UE;
  - monitoraggio post-commercializzazione e segnalazione degli incidenti gravi.
- **Deployer:** il gestore di rete che usa il sistema nelle control room (Art. 26).
  Obblighi principali:
  - usarlo secondo le istruzioni;
  - affidare la supervisione a persone competenti, formate e con autorità;
  - monitorarne il funzionamento;
  - conservare i log (almeno 6 mesi);
  - informare i lavoratori;
  - segnalare rischi e incidenti al provider.
  La FRIA (Art. 27) **non** si applica ai sistemi del punto 2 dell'Annex III (infrastrutture critiche).
- **Classificazione:** alto rischio secondo l'Annex III, punto 2 (componente di sicurezza nella gestione della rete elettrica).

---

## 3. Domande sui KPI (Strato 1)

| KPI | Domanda probabile | Risposta / dove si vede |
|---|---|---|
| **A1** | "Quante HIC/HITL sono state eseguite senza revisione?" | 0 per costruzione: `_motivo_non_eseguibile` (`oversight_manager.py:973`). Scheda **Monitoraggio KPI**. |
| **A2** | "Gli operatori approvano tutto?" | Tasso di override (rifiutate + modificate) / revisionate, target 5–40%. |
| **A3** | "Quanto tempo dedicano alla revisione?" | Mediana tra apertura e chiusura della card; < 10 s segnalato (non bloccante), anche per revisore. |
| **A4** | "Le motivazioni sono un rito?" | (motivazioni accettate sotto i 30 caratteri + **fotocopie respinte**) / (revisioni finali + fotocopie respinte), target < 10%. La fotocopia (similarità ≥ 0.85, numeri esclusi) **blocca** la revisione; colonna **Respinte** per revisore. |
| **A5** | "La coda regge il carico?" | Escalation per SLA scaduto / HITL, target < 15%. Più il pannello carico di lavoro (`:1130`). |
| **A6** | "Il codice rispetta la matrice dichiarata?" | % decisioni coerenti, target 100% (`:431`). |
| **B1/B2** | "Ogni card ha spiegazione e incertezza?" | 100%: la spiegazione a template è garantita anche senza LLM. |
| **B3b/B3c** | "Si sa da dove viene la spiegazione? L'LLM è stabile?" | Fonte su ogni card e nel log; tasso di fallback nella barra KPI. |
| **B4** | "Quanti click per spiegazione, override, stop?" | Spiegazione 0 (è già sulla card), override 1 più la motivazione, stop 1 più la conferma. |
| **C1–C4** | Gap di recall, calibrazione, override per area, drift | Scheda **Fairness & drift**, con allerte a soglia. |
| **D1** | "Ogni transizione di stato è nel log?" | Sì: coda, apertura, revisione, esecuzione, esecuzione negata, escalation, stop on/off, blocchi e sblocchi, promozioni, soglie, spiegazioni, contestazioni. |
| **D2** | "Il log è integro?" | Indicatore sempre visibile; "Simula manomissione". |
| **D3** | "Quanto tempo serve per ricostruire una decisione?" | Pochi secondi con **Ricostruisci**, senza grep. |

Calcolo dei KPI: `oversight_manager.py:994` (`kpi`), `app.py:440` (`monitoraggio`).

---

## 4. Mappa Art. 14(4) AI Act → dashboard

| Requisito | Dove nel sistema |
|---|---|
| (a) Comprendere capacità e limiti | Card con spiegazione e incertezza (B1, B2); limiti dichiarati in `DichiarazioneDiOversight.md` §8 |
| (b) Consapevolezza dell'automation bias | Incertezza sempre visibile, A2, A3, A4, spiegazione contrastiva |
| (c) Interpretare correttamente l'output | Spiegazione in linguaggio operativo, avviso per area, calibrazione (C2) |
| (d) Decidere di non usare / ignorare l'output | Rifiuta e Modifica (A2, T1) |
| (e) Intervenire sul funzionamento | Coda bloccante, escalation su SLA, soglie modificabili, promozioni (A1, A5, A6, T2) |
| (f) Interrompere il sistema | Emergency stop a tre granularità più la combinazione area + tipo (B4, T3) |
| Monitoraggio (14(1)-(2)) | C1–C4, D1–D3, T5, T6 |

Altri articoli citati dalla guida:
- **Art. 10** (dati e bias): T5.
- **Art. 12** (logging): audit trail.
- **Art. 13** (trasparenza): model card, fonte delle spiegazioni.

---

## 5. Domande "scomode" probabili

**"Cosa NON avete risolto?"**
- Le etichette del Sud restano distorte: abbiamo aumentato la supervisione, ma non corretto il dato.
- Il gap di recall Nord/Centro contro Isole (0.417) è esposto e mitigato solo indirettamente.
- L'identità del supervisore si basa sul prefisso `SUP-`, senza autenticazione.
- Chiunque abbia un ID può modificare le soglie: la modifica è motivata e registrata, ma non è limitata per ruolo.
- L'hash del log non è firmato: serve un'ancora esterna.
- SLA, drift e flusso delle raccomandazioni sono simulati.

Elenco completo: `DichiarazioneDiOversight.md` §8 e `audit_logger.py:19`.

**"Se modifico a mano il file di log con un editor?"**
- La verifica individua il primo record non valido e il motivo.
- Una catena interamente ricalcolata viene rilevata finché il processo è attivo, grazie all'ancora in memoria.
- **Limite dichiarato:** dopo un riavvio l'ancora viene riletta dal file.

**"L'operatore non può aggirare il blocco riformulando la motivazione?"**
- Se la riformula davvero (testo diverso, similarità < 0.85) la revisione passa: è il comportamento voluto, perché a quel punto ha scritto qualcosa di nuovo.
- **Limite dichiarato:** il confronto è solo testuale. Una parafrasi semantica (stesse idee, parole diverse) non viene intercettata.
- Restano comunque visibili: i tentativi respinti (`rubber_stamping_bloccato` nell'audit trail, colonna **Respinte**), A3 (tempo di revisione), A4 (motivazioni brevi) e le statistiche per revisore.

**"Un operatore può approvare una decisione HIC?"**
- No: errore 403. Può solo metterla in escalation e la chiude un `SUP-…` (`oversight_manager.py:560`).

**"Una richiesta da un altro sito può approvare decisioni?"**
- No: le richieste diverse da GET con Origin diversa dall'host ricevono 403 (`app.py:595`).

**"Dopo uno stop le azioni ripartono da sole?"**
- No: le decisioni tornano in coda almeno HITL (R6), con log `sblocco_post_stop`.
- Restano bloccate se ricadono in un altro stop ancora attivo.

**"Le HOTL sfuggono all'umano?"**
- No. Sono visibili in *Decisioni recenti* e si possono **contestare** ex post con motivazione (`oversight_manager.py:582`).
- Le contestazioni contano per l'override per area (C3), che può riportare l'intera area in HITL.

**"Nascondere le escalation non è un rischio?"**
- È solo un filtro di vista: le decisioni restano in coda e un avviso indica quante sono nascoste.
- Lo stato e i KPI non cambiano.
