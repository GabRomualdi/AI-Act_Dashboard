# Dichiarazione di Oversight – EnerGuard

Sistema di manutenzione predittiva per asset di rete (trasformatori, linee AT, cabine primarie, turbine eoliche).
Classificazione AI Act: **alto rischio** (Annex III, componente di sicurezza nella gestione di infrastrutture critiche).
Questa dichiarazione descrive **chi decide, quando e perché**. La matrice qui sotto è implementata in
`dashboard/backend/oversight_manager.py` (`MATRICE_ROUTING` e `OversightManager.route()`). La dashboard misura con il KPI A6 che il
codice la rispetti. Al momento le decisioni coerenti con la matrice sono il 100%.

---

## 1. Livelli di supervisione

| Livello | Significato | Chi chiude la decisione |
|---|---|---|
| **HIC** (Human-in-Command) | L'AI propone, l'umano ha veto assoluto. Nulla parte senza di lui. | Solo un **supervisore** (ID `SUP-…`), oppure escalation |
| **HITL** (Human-in-the-Loop) | La decisione resta `IN_ATTESA` finché un operatore non approva, modifica, rifiuta o fa escalation. | Qualsiasi operatore. In `ESCALATION` solo `SUP-…` |
| **HOTL** (Human-on-the-Loop) | Il sistema esegue l'azione e l'umano monitora. Può **contestare ex post** con motivazione. | Sistema, con contestazione umana registrata |

Esiste **un solo punto di esecuzione** nel codice: `OversightManager._esegui()`. Prima di eseguire, questo metodo ricontrolla
livello, stato e stop attivi. Se le condizioni non sono rispettate solleva `EsecuzioneNonAutorizzata`.
Una decisione rifiutata, bloccata o ancora in attesa non può raggiungerlo (test T1).

---

## 2. Matrice di routing

Le regole si leggono dall'alto in basso: **vale la prima che si applica**.

| # | Condizione | Livello | Perché |
|---|---|---|---|
| **R1** | Utenza `critica` coinvolta (ospedali, infrastrutture essenziali) | **HIC** | Un errore può interrompere la fornitura a un'utenza critica. Il costo di un'azione sbagliata non è recuperabile: serve il veto umano assoluto e la competenza di un supervisore. |
| **R2** | Azione `riduci_carico` su utenza `alta` o `critica` | **HIC** | Ridurre il carico significa limitare la fornitura a utenze sensibili. È una scelta di servizio, non solo tecnica: decide l'umano. |
| **R3** | P(guasto) ≥ **0.60** (soglia di rischio) | **HITL** | Rischio alto: l'intervento proposto è oneroso (manutenzione, ispezione urgente). Serve un giudizio umano prima di agire. |
| **R4** | Confidenza < **0.80** (0.85 con allerta di drift) | **HITL** | Il modello è incerto, oppure le sue performance recenti sono in calo: l'AI non può agire da sola. |
| **R5** | Area **sotto osservazione** (allerta di calibrazione C2, label bias sospetto, override umano elevato C3, oppure promozione manuale) | **HITL** | Dove le performance del modello non sono affidabili, il sistema **si declassa da solo**. |
| **R6** | Decisione sospesa da un emergency stop e poi riattivata | **HITL** (il livello non scende mai) | Dopo uno stop nessuna azione riparte in automatico. |
| **R7** | Rischio basso, confidenza alta e azione leggera (`nessuna_azione` o `ispezione_routine`) | **HOTL** | L'errore costa poco ed è reversibile. Il sistema agisce e l'umano può contestare ex post. |
| **R8** | Ogni altro caso | **HITL** | Rete di sicurezza: nel dubbio decide l'umano. |

### 2.1 Politica delle azioni (input del routing)

L'azione proposta entra nel routing (R2, R7), quindi anche la politica delle azioni è dichiarata:

| # | Condizione | Azione |
|---|---|---|
| P1 | P ≥ 0.70 e carico ≥ 85% | `riduci_carico` |
| P2 | P ≥ 0.75 | `ispezione_urgente` |
| P3 | P ≥ 0.55 | `programma_manutenzione` |
| P4 | P ≥ **0.35** (soglia decisionale) | `ispezione_routine` |
| P5 | altrimenti | `nessuna_azione` |

---

## 3. Perché proprio queste soglie

| Parametro | Valore | Giustificazione |
|---|---|---|
| Soglia decisionale | **0.35** (invece di 0.50) | In questo dominio un **falso negativo** (guasto non previsto, magari su una cabina che serve un ospedale) costa molto più di un **falso positivo** (un'ispezione in più). Abbassare la soglia sposta l'errore sul lato meno grave. |
| Soglia di rischio (R3) | **0.60** | Default della guida, mantenuto. Sopra 0.55 la politica propone già interventi onerosi. Da 0.60 in su la probabilità di guasto supera quella del "non guasto" con un margine sufficiente da giustificare sempre un giudizio umano. |
| Confidenza | `max(p, 1−p)` | Misura la distanza del modello dal "non so" (0.5). È semplice, spiegabile a un non tecnico e coerente con la card ("il modello è sicuro al X%"). |
| Soglia di confidenza (R4) | **0.80** | Con questa definizione, confidenza < 0.80 equivale a 0.20 < p < 0.80. Tutta la zona ambigua va all'umano. |
| Margine di drift | **+0.05** (→ 0.85) | Si attiva automaticamente quando l'accuratezza delle ultime 2 settimane scende sotto 0.80 (oggi è 0.692). Se il modello peggiora, gli si lascia meno autonomia. |
| SLA HITL | **30 min** | La coda è dimensionata per la demo. Alla scadenza la decisione va in `ESCALATION` e **non viene mai eseguita in silenzio** (KPI A5). |
| Motivazione minima | **15 caratteri** e almeno 2 parole significative | Requisito della guida. Testi come `""` o `"ok"` vengono rifiutati (test T2). Sotto i 30 caratteri la revisione viene segnalata, non bloccata (KPI A4). Se la motivazione è una "fotocopia" (quasi identica, similarità ≥ 0.85, a una già usata per un'altra decisione, anche cambiando solo l'ID asset) la revisione è **bloccata**: errore 400, la decisione resta in coda e non viene eseguita, il tentativo è registrato come `rubber_stamping_bloccato` e conta in A4. Il controllo è solo testuale: una parafrasi semantica non viene intercettata. Le escalation non sono bloccate. |
| Revisione rapida | **< 10 s** | Una revisione così veloce indica un'approvazione senza lettura: viene segnalata per revisore (KPI A3). |
| Allerta gap di recall (C1) | **> 0.15** | Soglia indicata dalla guida e dagli indicatori di qualità. |
| Allerta calibrazione (C2) | **> 0.10** in un singolo gruppo | Una divergenza in un solo gruppo è l'indizio tipico di label bias storico. |
| Promozione per override (C3) | ≥ 5 giudizi e disaccordo ≥ **40%** | Sotto i 5 giudizi il segnale è rumore. Il 40% è vicino alla soglia di allarme del KPI A2 (> 60% = modello inutilizzabile) e permette di intervenire prima di raggiungerla. |

**Effetto combinato, dichiarato onestamente.** Con l'allerta di drift attiva l'HOTL resta riservato ai casi con p ≤ 0.15,
azione `nessuna_azione`, utenza non critica e area non promossa. Nella sessione dimostrativa (40 decisioni) la distribuzione è
**HOTL 8 · HITL 29 · HIC 3**. È una scelta prudente: in questo momento il modello è in drift e i dati del Sud non sono affidabili.

Ogni modifica alle soglie fatta dalla dashboard richiede ID operatore e motivazione, e finisce nell'audit trail
(`modifica_soglie`). Le nuove soglie valgono per le nuove decisioni: quelle già in coda non vengono mai declassate.

---

## 4. Bias identificati e come incidono sul routing

Le guide indicano tre bias da cercare. Tutti i numeri qui sotto sono calcolati dal `BiasDetector` sul test set
(720 asset, accuracy 0.783, recall 0.744).

### Bias 1 – Label bias storico: sotto-segnalazione dei guasti al Sud (Art. 10)

*"Il target è ciò che è stato registrato, non necessariamente ciò che è accaduto"* (Guida tecnica).

- **Evidenza.** Sud e Isole hanno profili tecnici simili, ma tassi di guasto registrati molto diversi:

  | Area | Età media | Manutenzioni (5 anni) | Giorni dall'ultima manutenzione | Guasti registrati |
  |---|---|---|---|---|
  | Sud | 23.2 | 2.1 | 343 | **24%** |
  | Isole | 24.9 | 1.7 | 386 | **45%** |

- **Confronto a parità di profilo.** Con un abbinamento 1:1 tra asset gemelli, il Sud registra 0.242 contro 0.364 delle Isole.
  Il tasso atteso dal profilo per il Sud è 0.368: uno scarto di **−0.126**. Il Sud è l'unica area sospetta.
- **Calibrazione (C2).** Al Sud la probabilità media predetta è 0.396, mentre il tasso registrato è 0.238: un gap di **+0.159**,
  oltre la soglia di 0.10 e l'unico gruppo che la supera.
- **Ipotesi.**
  1. Processi di segnalazione dei guasti non omogenei: guasti riparati sul posto e mai registrati.
  2. Manutenzione correttiva gestita da terzi, fuori dai sistemi aziendali.
  3. Meno sensori o minore copertura del monitoraggio.
- **Mitigazioni.**
  - Il Sud è **promosso automaticamente a HITL** (R5) per C2 e per label bias.
  - Le card del Sud mostrano un avviso sull'affidabilità dei dati.
  - I "casi simili" segnalano quando il confronto poggia su dati del Sud.

### Bias 2 – Disparità di performance per area: l'area geografica come variabile proxy (C1)

L'area geografica è segnalata dalla guida come *"possibile variabile proxy"*: è correlata a età e manutenzione, e quindi alle etichette distorte del Bias 1.

- **Recall per area:** Nord 0.536 · Centro 0.542 · Sud 0.794 · Isole 0.953.
  Il **gap massimo è 0.417**, quasi il triplo della soglia 0.15. Al Nord e al Centro il modello perde circa **1 guasto su 2**.
- Il gap di **selezione** (tasso di interventi raccomandati) tra le aree è **0.569**.
- **Per tipo di asset** il gap di recall è 0.141 (trasformatore 0.698 contro turbina eolica 0.839): sotto soglia, ma vicino.
- **Mitigazioni.**
  - Allerta C1 sempre visibile nel pannello bias.
  - Soglia decisionale bassa (0.35) e routing più severo con il drift.
  - **Misura prima/dopo:** i guasti reali che finirebbero in HOTL scendono **da 9 a 3** (Nord 4→2, Centro 3→1, Sud 1→0, Isole 1→0).
- **Scelta dichiarata.** Il C1 **non** promuove automaticamente le aree. Tre aree su quattro superano la soglia: promuoverle
  tutte eliminerebbe di fatto l'HOTL e scaricherebbe sugli operatori una coda insostenibile (rischio A5 e rubber-stamping). Un
  supervisore può comunque promuovere un'area manualmente, con motivazione registrata.

### Bias 3 – Automation bias, lato umano (Art. 14(4)(b))

Il rischio è che l'operatore approvi "perché lo dice l'AI".

- **Contromisure nella dashboard.**
  - L'**incertezza è sempre visibile** accanto alla raccomandazione (KPI B2).
  - Ogni card mostra 3 fattori in linguaggio operativo, una **spiegazione contrastiva** ("cosa dovrebbe cambiare") e i **casi simili**.
  - La **fonte della spiegazione** è sempre dichiarata: template, LLM o fallback (KPI B3b).
- **Contromisure sul processo.**
  - Motivazione obbligatoria e significativa.
  - Blocco delle motivazioni fotocopia (A4, tentativi respinti per revisore), segnalazione delle motivazioni brevi (A4) e delle revisioni sotto i 10 secondi (A3).
  - Tasso di override monitorato in modo globale (A2) e per area (C3), con promozione automatica oltre il 40%.
  - Revisori differenziati: HIC ed escalation solo `SUP-…`.
- **Regola sull'LLM.** L'LLM **traduce, non decide**. Guardrail: niente numeri inventati, niente gergo, il fattore principale
  deve essere citato. In caso di errore si torna al template e il fallback viene dichiarato.

---

## 5. Emergency stop

- **Granularità:** globale, per area, per tipo di asset, oppure area + tipo (es. `area:Sud+tipo:linea_AT`, test T3).
  Il comando è raggiungibile con 1 click dalla schermata principale più una conferma.
- **Effetto:** blocca le nuove raccomandazioni nell'ambito **e** rende non eseguibili quelle già in coda (`BLOCCATA_STOP`).
  Le decisioni fuori dall'ambito continuano normalmente.
- **Tracciabilità:** attivazione e disattivazione richiedono ID operatore e motivazione, e sono registrate nel log
  (`emergency_stop_ON` / `emergency_stop_OFF`).
- **Riattivazione:** richiede motivazione e conferma esplicita. Le decisioni sbloccate tornano in coda **almeno HITL** (R6),
  oppure restano bloccate se ricadono in un altro stop ancora attivo.

---

## 6. Monitoraggio e auto-declassamento

| Segnale | Effetto automatico sul routing |
|---|---|
| Gap di calibrazione > 0.10 in un'area (C2) | Area promossa a HITL (R5) |
| Label bias sospetto (tasso registrato molto sotto quello atteso dal profilo) | Area promossa a HITL (R5) |
| Override umano ≥ 40% su almeno 5 giudizi in un'area (C3) | Area promossa a HITL (R5) |
| Drift: accuratezza recente < 0.80 (C4) | Soglia di confidenza +0.05 (R4 più severa) |
| Gap di recall > 0.15 (C1) | Solo allerta visibile; promozione manuale a discrezione del supervisore |

Ogni promozione e ogni revoca viene registrata dal sistema nell'audit trail. Quando la condizione rientra, la promozione automatica viene revocata.

---

## 7. Tracciabilità

L'audit trail è un log append-only con **catena di hash SHA-256**. Registra ogni transizione di stato: proposta, routing,
apertura della card, revisione, esecuzione, escalation, stop, promozioni, soglie e spiegazioni.

Dalla dashboard si può:
- verificare l'integrità del log, che è sempre visibile (D2);
- simulare una manomissione e vederla rilevata;
- filtrare i record;
- esportare il log in JSONL;
- **ricostruire per asset** chi ha deciso, cosa, quando e perché, e se l'AI è stata approvata o corretta (D3, test T6).

---

## 8. Limiti dichiarati (cosa NON è risolto)

1. **Le etichette del Sud restano distorte.** La mitigazione aumenta la supervisione, ma non corregge il dato: il modello è
   addestrato su guasti sotto-registrati. Servirebbe una bonifica dei dati alla fonte.
2. **Il gap di recall tra Nord/Centro e Isole (0.417) non è risolto.** È solo esposto e mitigato indirettamente. Al Nord e al
   Centro il modello resta debole sui guasti reali.
3. **Il gap di recall per tipo di asset (0.141)** è sotto soglia ma vicino: va monitorato.
4. **Identità del supervisore.** Il ruolo è riconosciuto dal prefisso `SUP-` e non c'è autenticazione: in produzione servono
   SSO e ruoli verificati.
5. **Ambiente dimostrativo.** Lo SLA, il drift (test set diviso in "settimane") e il flusso di raccomandazioni sono simulati.
6. **La promozione per override (C3)** richiede almeno 5 giudizi umani: all'inizio della giornata non può scattare.
7. **LLM esterno.** Introduce non determinismo, latenza e dipendenza da un fornitore. È mitigato dai guardrail e dal fallback
   al template, ma il testo generato non è riproducibile al 100%.

---

## 9. Ruoli AI Act

- **Provider:** chi sviluppa e immette sul mercato il sistema. Ha gli obblighi di sistema di gestione del rischio, governance
  dei dati (Art. 10), documentazione tecnica, logging (Art. 12), trasparenza (Art. 13), progettazione per l'oversight
  (Art. 14) e marcatura CE.
- **Deployer:** il gestore di rete che lo usa nelle proprie control room. Deve assegnare la supervisione a persone competenti
  e formate, usare il sistema secondo le istruzioni, conservare i log, monitorarne il funzionamento e segnalare gli incidenti
  gravi.
