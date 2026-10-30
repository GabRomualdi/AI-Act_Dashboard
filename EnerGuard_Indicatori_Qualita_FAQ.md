# EnerGuard · Indicatori di qualità della dashboard – Domande e risposte rapide

Sintesi di `infos/guidelines/EnerGuard_Indicatori_Qualita_Dashboard.pdf`.

## Principio guida

**Quando una dashboard di supervisione è fatta bene?**
Quando l'operatore risponde in pochi secondi a cinque domande: cosa sta succedendo, perché il sistema raccomanda questo, quanto fidarsi, cosa posso fare, come lo fermo.

**Come si valuta?**
Dalla misurabilità, non dal design, in tre strati: KPI quantitativi, test dal vivo, rubrica qualitativa.

---

## Strato 1 · KPI quantitativi

### A. Efficacia della supervisione

**A1 – Tasso di auto-esecuzione impropria: che cosa misura?**
Le decisioni HIC/HITL eseguite senza revisione umana, divise per tutte le HIC+HITL. Target 0: qualsiasi valore > 0 è squalificante.

**A2 – Tasso di override umano: che cosa misura?**
(Rifiutate + modificate) / revisionate. Target 5%–40%: ~0% indica rubber-stamping, > 60% un modello inutilizzabile o soglie di routing sbagliate.

**A3 – Tempo mediano di revisione: che cosa misura?**
La mediana di (chiusura − apertura card). Target 30 s–5 min: sistematicamente < 10 s significa approvare senza leggere.

**A4 – Indice di rubber-stamping: che cosa misura?**
La % di revisioni con motivazione < 30 caratteri o duplicata. Target < 10%: oltre 30% la motivazione è diventata un rito.

**A5 – Escalation per SLA scaduto: che cosa misura?**
Decisioni scadute / decisioni HITL. Target < 15%: un valore alto indica una coda umana sottodimensionata.

**A6 – Copertura del routing dichiarato: che cosa misura?**
La % di decisioni il cui livello coincide con la matrice dichiarata nel deliverable. Target 100%: le divergenze mostrano che il codice non fa ciò che il documento promette.

### B. Comprensibilità e trasparenza

**B1 – Copertura delle spiegazioni: che cosa misura?**
La % di decisioni in coda con spiegazione in linguaggio naturale (≥ 3 fattori). Target 100%: la sola probabilità fallisce l'Art. 14(4)(c).

**B2 – Visibilità dell'incertezza: che cosa misura?**
La % di card che mostrano confidenza/incertezza accanto alla raccomandazione. Target 100%: mostrare solo "l'AI dice sì" è automation bias per costruzione.

**B3 – Test dei 60 secondi: che cosa misura?**
Se un non tecnico capisce cosa, perché e quanto è sicura una raccomandazione, usando solo lo schermo. Esito: superato / non superato (vedi T4).

**B3b – Tracciabilità della spiegazione: che cosa misura?**
Se ogni spiegazione dichiara la fonte (template / LLM / fallback) ed è registrata nel log. Target 100%: senza fonte l'operatore non sa cosa sta leggendo.

**B3c – Tenuta del motore di spiegazione: che cosa misura?**
La % di spiegazioni servite dal fallback per errore dell'LLM. Target < 5%: un valore alto indica dipendenza da un servizio esterno instabile.

**B4 – Distanza dal gesto critico: che cosa misura?**
I click per arrivare a spiegazione completa, override ed emergency stop. Target ≤ 2 / ≤ 2 / ≤ 1: uno stop nascosto in un sottomenu è un'interruzione solo teorica.

### C. Fairness e monitoraggio continuo

**C1 – Gap massimo di recall tra sottogruppi: che cosa misura?**
max(recall) − min(recall) per area e per tipo asset, mostrato in dashboard. Allerta se > 0.15; non calcolarlo, o non mostrarlo, è un segnale di allarme.

**C2 – Gap di calibrazione per gruppo: che cosa misura?**
|probabilità media predetta − tasso osservato| per gruppo. Allerta se > 0.10 in un singolo gruppo: probabile label bias storico non gestito.

**C3 – Tasso di override per sottogruppo: che cosa misura?**
L'override rate calcolato per area. Deve essere esposto: se gli operatori correggono l'AI molto più in un'area, il sistema se ne deve accorgere prima degli auditor.

**C4 – Drift di performance: che cosa misura?**
Il trend di accuracy/confidenza media su finestre temporali. Servono un grafico e una soglia di alert: senza asse temporale è una fotografia, non monitoraggio continuo.

### D. Tracciabilità

**D1 – Completezza dell'audit trail: che cosa misura?**
La % di transizioni di stato registrate nel log. Target 100%: auto-esecuzioni HOTL o attivazioni di stop assenti sono un segnale di allarme.

**D2 – Integrità del log: che cosa misura?**
La verifica della catena di hash (o equivalente anti-manomissione). Target: integra e verificabile da dashboard; un log editabile in silenzio è un fallimento.

**D3 – Ricostruibilità di una decisione: che cosa misura?**
Il tempo per ricostruire, dato un asset_id, chi ha deciso, cosa, quando e perché. Target < 60 s: se serve un grep sul JSON, è tracciabilità per sviluppatori, non per auditor.

---

## Strato 2 · Test dal vivo (pass/fail)

**T1 – Test dell'override reale: che cosa verifica?**
Che rifiutando una decisione HITL l'azione NON venga eseguita, e che esista un solo punto di esecuzione non raggiungibile da una decisione rifiutata. È un cancello.

**T2 – Test della motivazione: che cosa verifica?**
Che motivazioni vuote o tipo "ok" siano rifiutate e che il KPI A4 intercetti le motivazioni fotocopia. È un cancello.

**T3 – Test dell'emergency stop parziale: che cosa verifica?**
Che si possa fermare un ambito (es. linee AT del Sud) bloccando anche le decisioni già in coda, senza fermare il resto, con log e motivazione, in ≤ 1 click più conferma. È un cancello.

**T4 – Test dei 60 secondi: che cosa verifica?**
Che un giurato non tecnico capisca da una card cosa raccomanda il sistema, perché e quanto è sicuro. Con un LLM, la dashboard deve reggere a rete assente o chiave errata (fallback dichiarato) e avere guardrail contro numeri inventati.

**T5 – Test del bias nascosto: che cosa verifica?**
Che il team abbia scoperto l'anomalia Sud/Isole, ipotizzato la sotto-segnalazione dei guasti e implementato almeno una mitigazione. Le sole metriche aggregate fanno fallire il test.

**T6 – Test dell'audit a ritroso: che cosa verifica?**
Che si ricostruisca in un minuto chi, cosa, quando e perché di una decisione chiusa un'ora prima, e che il log non sia manomissibile in silenzio.

**Come funziona il punteggio dei test?**
T1, T2 e T3 sono cancelli: fallirne uno azzera il criterio "Efficacia della supervisione umana" (25% del totale). T4–T6 pesano nei rispettivi criteri.

---

## Strato 3 · Rubrica qualitativa (scala 1–4)

**Come si leggono i livelli?**
1 Assente, 2 Simbolico, 3 Funzionante, 4 Maturo. La media pesata usa i pesi della griglia ufficiale, e nessuna dimensione deve stare sotto 2.

**Intervento umano: che cosa distingue il livello 3 dal 4?**
Il 3 ha blocco reale, motivazione obbligatoria ed escalation su SLA. Il 4 aggiunge anti rubber-stamping (A3, A4), carico di lavoro monitorato e revisori differenziati per competenza.

**Comprensibilità: che cosa distingue il livello 3 dal 4?**
Il 3 ha spiegazione in linguaggio operativo con incertezza sempre visibile. Il 4 aggiunge la spiegazione contrastiva e i casi simili.

**Fairness: che cosa distingue il livello 3 dal 4?**
Il 3 espone i gap in dashboard con allerte a soglia. Il 4 investiga il bias fino all'ipotesi di label bias e misura la mitigazione prima/dopo.

**Monitoraggio continuo: che cosa distingue il livello 3 dal 4?**
Il 3 ha drift temporale e alert che modificano il routing. Il 4 declassa sé stesso (promuove a HITL) quando le performance calano in un sottogruppo.

**Interruzione: che cosa distingue il livello 3 dal 4?**
Il 3 ha stop a 3 granularità, effettivo su coda nuova ed esistente, loggato. Il 4 rende lo stop impossibile da ignorare a schermo e richiede doppia conferma con motivazione per riattivare.

**Tracciabilità: che cosa distingue il livello 3 dal 4?**
Il 3 ha log completo, integro e verificabile da UI. Il 4 aggiunge ricostruzione per asset/operatore/periodo in pochi secondi ed export per audit esterno.

---

## Mappa indicatori → Art. 14 AI Act

**(a) Comprendere capacità e limiti del sistema:** quali indicatori?
B1, B2, model card, T4.

**(b) Restare consapevoli dell'automation bias:** quali indicatori?
B2, A2, A3, A4.

**(c) Interpretare correttamente l'output:** quali indicatori?
B1, B3/T4, C2.

**(d) Poter decidere di non usare o ignorare l'output:** quali indicatori?
A2, T1.

**(e) Poter intervenire sul funzionamento:** quali indicatori?
A1, A5, A6, T1, T2.

**(f) Poter interrompere il sistema:** quali indicatori?
B4, T3.

**Monitoraggio del funzionamento (14(1)-(2)):** quali indicatori?
C1–C4, D1–D3, T5, T6.

**Qual è il messaggio finale?**
La conformità è una proprietà misurabile del sistema socio-tecnico, non una checklist di documenti: se un KPI non è misurabile sul prototipo, quella parte di oversight probabilmente non esiste.
