"""
EnerGuard Starter Kit - OversightManager
=========================================
Gestisce il routing delle raccomandazioni AI verso i tre livelli di
supervisione (HIC / HITL / HOTL) e la coda di approvazione umana.

REGOLA D'ORO: nessuna azione con esito "IN_ATTESA" puo' essere eseguita.
Esiste un solo punto di esecuzione, `_esegui()`: controlla esplicitamente
stato, livello, revisione umana motivata ed emergency stop e, se qualcosa non
torna, rifiuta con EsecuzioneNonAutorizzata (controlli espliciti, non assert:
restano attivi anche con `python -O`). `_esegui()` e' chiamata solo da:
  - `sottometti()` -> solo per decisioni HOTL (stato AUTO_ESEGUITA);
  - `revisiona()`  -> solo per esiti APPROVATA / MODIFICATA, dopo la
                      validazione della motivazione obbligatoria.
Una decisione RIFIUTATA, IN_ATTESA, in ESCALATION o BLOCCATA_STOP non la
raggiunge mai, e se ci arrivasse verrebbe respinta.

Implementato rispetto ai TODO dello starter kit:
  1. Matrice di routing dichiarata (MATRICE_ROUTING, deliverable D3) applicata
     da `route()`; il KPI A6 verifica ogni decisione contro la matrice.
  2. Escalation: decisioni HITL/HIC non revisionate entro lo SLA passano a
     ESCALATION (`controlla_sla`) e le chiude solo un supervisore (ID "SUP-").
  3. Ogni transizione di stato e' registrata nell'AuditLogger.
  4. Emergency stop globale, per area, per tipo e combinato (area+tipo): blocca
     le nuove raccomandazioni E quelle gia' in coda. La riattivazione richiede
     motivazione e conferma esplicita e non fa ripartire nulla in automatico:
     le decisioni sospese tornano in coda almeno HITL (regola R6).
  5. Anti rubber-stamping (KPI A2-A4): una motivazione "fotocopia" (quasi
     identica a una gia' usata) RESPINGE la revisione (RubberStamping) e il
     tentativo resta in audit trail e nei KPI; motivazioni brevi e revisioni
     < 10 s sono segnalate; statistiche per revisore.
"""

import logging
import re
import threading
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum
from statistics import median
from typing import Callable, Optional

log_esecuzioni = logging.getLogger("energuard.esecuzioni")


def _adesso() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _da_iso(valore: str) -> datetime:
    dt = datetime.fromisoformat(valore)
    return dt if dt.tzinfo else dt.astimezone()


def _f(x: float) -> str:
    """0.795 -> '0.795', 0.8 -> '0.8' (niente arrotondamenti che falsano i confronti)."""
    return f"{x:.3f}".rstrip("0").rstrip(".")


class LivelloSupervisione(str, Enum):
    HOTL = "HOTL"   # il sistema agisce, l'umano monitora ex post
    HITL = "HITL"   # approvazione umana obbligatoria prima dell'esecuzione
    HIC = "HIC"     # veto umano assoluto: solo l'umano decide


ORDINE_LIVELLI = {LivelloSupervisione.HOTL: 0, LivelloSupervisione.HITL: 1, LivelloSupervisione.HIC: 2}


class StatoDecisione(str, Enum):
    IN_ATTESA = "IN_ATTESA"
    APPROVATA = "APPROVATA"
    MODIFICATA = "MODIFICATA"
    RIFIUTATA = "RIFIUTATA"
    ESCALATION = "ESCALATION"
    AUTO_ESEGUITA = "AUTO_ESEGUITA"   # ammessa SOLO per HOTL
    BLOCCATA_STOP = "BLOCCATA_STOP"   # bloccata da emergency stop


STATI_IN_CODA = (StatoDecisione.IN_ATTESA, StatoDecisione.ESCALATION)
ESITI_FINALI = (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA, StatoDecisione.RIFIUTATA)


class EsecuzioneNonAutorizzata(RuntimeError):
    """Tentativo di eseguire un'azione che non ha superato la supervisione prevista."""


class RubberStamping(ValueError):
    """Revisione respinta perche' riconosciuta come rubber-stamping (motivazione fotocopia)."""


# ----------------------------------------------------------------------
# Politica delle azioni: da P(guasto) e carico all'azione proposta.
# E' dichiarata qui (e mostrata in dashboard) perche' l'azione proposta
# entra nel routing (R2, R7): cambiarla cambia chi decide.
# ----------------------------------------------------------------------
AZIONI = ["nessuna_azione", "ispezione_routine", "programma_manutenzione",
          "riduci_carico", "ispezione_urgente"]
AZIONI_LEGGERE = ("nessuna_azione", "ispezione_routine")
SOGLIA_DECISIONALE = 0.35   # sotto questa soglia il modello non segnala il guasto (vedi model card)

POLITICA_AZIONI = [
    {"id": "P1", "condizione": "P(guasto) >= 0.70 e carico >= 85%", "azione": "riduci_carico",
     "perche": "Rischio alto su asset gia' sotto sforzo: ridurre il carico abbassa lo stress subito."},
    {"id": "P2", "condizione": "P(guasto) >= 0.75", "azione": "ispezione_urgente",
     "perche": "Guasto probabile entro 30 giorni: serve un sopralluogo immediato."},
    {"id": "P3", "condizione": "P(guasto) >= 0.55", "azione": "programma_manutenzione",
     "perche": "Rischio medio-alto: manutenzione da pianificare a breve."},
    {"id": "P4", "condizione": f"P(guasto) >= {SOGLIA_DECISIONALE} (soglia decisionale)",
     "azione": "ispezione_routine",
     "perche": "Soglia bassa per scelta: un guasto non previsto costa piu' di un'ispezione in piu'."},
    {"id": "P5", "condizione": "altrimenti", "azione": "nessuna_azione",
     "perche": "Rischio basso: nessun intervento oltre al piano ordinario."},
]


def azione_raccomandata(prob_guasto: float, carico_pct: float) -> str:
    if prob_guasto >= 0.70 and carico_pct >= 85:
        return "riduci_carico"
    if prob_guasto >= 0.75:
        return "ispezione_urgente"
    if prob_guasto >= 0.55:
        return "programma_manutenzione"
    if prob_guasto >= SOGLIA_DECISIONALE:
        return "ispezione_routine"
    return "nessuna_azione"


def calcola_confidenza(prob_guasto: float) -> float:
    """confidenza = max(p, 1-p): quanto il modello e' lontano dal 'non so' (0.5)."""
    return round(max(prob_guasto, 1 - prob_guasto), 3)


# ----------------------------------------------------------------------
# Matrice di routing dichiarata (Dichiarazione di Oversight, deliverable D3).
# Le regole si leggono dall'alto in basso: vale la prima che si applica.
# `verifica` e' usata SOLO per il controllo di coerenza (KPI A6): il routing
# vero e' scritto in modo indipendente in OversightManager.route().
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RegolaRouting:
    id: str
    condizione: str
    livello: LivelloSupervisione
    perche: str
    verifica: Callable[[dict], bool]

    def descrizione(self) -> dict:
        return {"id": self.id, "condizione": self.condizione,
                "livello": self.livello.value, "perche": self.perche}


MATRICE_ROUTING = [
    RegolaRouting("R1", "Utenza critica coinvolta (ospedali, infrastrutture essenziali)",
                  LivelloSupervisione.HIC,
                  "Un errore puo' interrompere la fornitura a utenze critiche: veto umano assoluto.",
                  lambda c: c["criticita_utenza"] == "critica"),
    RegolaRouting("R2", "Riduzione del carico su utenza alta o critica",
                  LivelloSupervisione.HIC,
                  "Ridurre il carico limita la fornitura a utenze sensibili: decide solo l'operatore.",
                  lambda c: c["azione"] == "riduci_carico" and c["criticita_utenza"] != "standard"),
    RegolaRouting("R3", "P(guasto) >= soglia di rischio (default 0.60)",
                  LivelloSupervisione.HITL,
                  "Rischio alto: serve il giudizio umano prima di agire.",
                  lambda c: c["prob_guasto"] >= c["soglia_rischio"]),
    RegolaRouting("R4", "Confidenza < soglia di confidenza (default 0.80, +0.05 con allerta di drift)",
                  LivelloSupervisione.HITL,
                  "Modello incerto o performance recenti in calo: decide l'umano.",
                  lambda c: c["confidenza"] < c["soglia_confidenza"]),
    RegolaRouting("R5", "Area sotto osservazione (allerta di calibrazione, fairness o override, o promozione manuale)",
                  LivelloSupervisione.HITL,
                  "Dove le performance non sono affidabili il sistema si declassa da solo.",
                  lambda c: c["area_promossa"]),
    RegolaRouting("R6", "Decisione sospesa da un emergency stop e poi riattivata",
                  LivelloSupervisione.HITL,
                  "Dopo uno stop nessuna azione riparte in automatico.",
                  lambda c: c["post_stop"]),
    RegolaRouting("R7", "Rischio basso + confidenza alta + azione leggera (nessuna azione o ispezione di routine)",
                  LivelloSupervisione.HOTL,
                  "Il sistema agisce, l'umano monitora e puo' contestare ex post.",
                  lambda c: c["azione"] in AZIONI_LEGGERE),
    RegolaRouting("R8", "Ogni altro caso (es. manutenzione programmata a rischio medio)",
                  LivelloSupervisione.HITL,
                  "Default prudente: nel dubbio decide l'umano.",
                  lambda c: True),
]


def valuta_matrice(contesto: dict) -> RegolaRouting:
    """Applica la matrice dichiarata a un contesto di routing."""
    for regola in MATRICE_ROUTING:
        if regola.verifica(contesto):
            return regola
    raise RuntimeError("Matrice di routing incompleta")


@dataclass
class Raccomandazione:
    asset_id: str
    tipo_asset: str
    area_geografica: str
    criticita_utenza: str          # standard | alta | critica
    prob_guasto: float             # output del modello, 0-1
    confidenza: float              # 0-1, max(p, 1-p): vedi nota in fondo
    azione_proposta: str
    spiegazione: list              # fattori principali, es. [("vibrazione_indice", 0.41), ...]
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    creata_il: str = field(default_factory=lambda: _iso(_adesso()))
    livello: Optional[LivelloSupervisione] = None
    stato: StatoDecisione = StatoDecisione.IN_ATTESA
    revisore: Optional[str] = None
    motivazione: Optional[str] = None
    # --- tracciabilita' del routing (KPI A6)
    carico_pct: Optional[float] = None
    azione_originale: Optional[str] = None      # azione proposta dall'AI (resta anche se l'umano la modifica)
    regola: Optional[str] = None
    motivo_routing: Optional[str] = None
    contesto_routing: dict = field(default_factory=dict)
    # --- esecuzione
    eseguita: bool = False
    eseguita_il: Optional[str] = None
    # --- coda e revisione (KPI A3, A4, A5)
    in_coda_dal: Optional[str] = None
    aperta_il: Optional[str] = None
    aperta_da: Optional[str] = None
    chiusa_il: Optional[str] = None
    durata_revisione_s: Optional[float] = None
    flag_revisione: list = field(default_factory=list)
    escalation_il: Optional[str] = None
    escalation_da: Optional[str] = None
    motivo_escalation: Optional[str] = None
    escalation_sla: bool = False
    # --- emergency stop
    bloccata_da: Optional[str] = None
    stato_pre_blocco: Optional[StatoDecisione] = None
    sospesa_da_stop: bool = False
    # --- HOTL: intervento ex post
    contestazione: Optional[dict] = None


class OversightManager:
    MIN_MOTIVAZIONE = 15               # la guida: rifiutare motivazioni sotto i 15 caratteri
    MAX_MOTIVAZIONE = 1000
    SOGLIA_MOTIVAZIONE_BREVE = 30      # KPI A4
    SOGLIA_REVISIONE_RAPIDA_S = 10     # KPI A3: < 10 s = approvazione senza lettura
    SOGLIA_FOTOCOPIA = 0.85            # KPI A4: similarita' oltre cui la motivazione e' "fotocopia" (revisione respinta)
    PREFISSO_SUPERVISORE = "SUP-"
    LIMITI_SOGLIE = {"soglia_confidenza": (0.50, 0.99), "soglia_rischio": (0.30, 0.95),
                     "sla_minuti": (1, 240)}

    def __init__(self, audit_logger, soglia_confidenza_alta: float = 0.80,
                 soglia_rischio_alto: float = 0.60, sla_minuti: int = 30,
                 orologio: Callable[[], datetime] = None, aree=None, tipi=None, lock=None):
        self.audit = audit_logger
        self.soglia_conf = soglia_confidenza_alta
        self.soglia_rischio = soglia_rischio_alto
        self.sla_minuti = sla_minuti
        self.margine_drift = 0.0               # aggiunto alla soglia di confidenza se c'e' drift
        self.hic_solo_supervisori = True       # revisori differenziati per competenza
        self.aree = set(aree) if aree else None
        self.tipi = set(tipi) if tipi else None
        self._ora = orologio or _adesso
        self._lock = lock or threading.RLock()
        self.decisioni: list[Raccomandazione] = []
        self._indice: dict[str, Raccomandazione] = {}
        self.stop_attivi: dict[str, dict] = {}     # es. {"area:Sud+tipo:linea_AT": {operatore, motivazione, dal}}
        self.promozioni: dict[str, dict] = {}      # area -> {"automatiche": {politica: {...}}, "manuale": {...}}
        self.esecuzioni: list[dict] = []           # registro delle azioni realmente eseguite
        self._motivazioni_usate: list[tuple[str, str]] = []   # (testo confrontabile, decision_id)
        self.respinte: list[dict] = []             # revisioni respinte per rubber-stamping (KPI A4)

    # ------------------------------------------------------------------
    # Coda
    # ------------------------------------------------------------------
    @property
    def coda(self) -> list[Raccomandazione]:
        """Decisioni che attendono un umano (IN_ATTESA o ESCALATION)."""
        return [r for r in self.decisioni if r.stato in STATI_IN_CODA]

    def trova(self, decision_id: str) -> Raccomandazione:
        return self._trova(decision_id)

    def _trova(self, decision_id: str) -> Raccomandazione:
        try:
            return self._indice[decision_id]
        except KeyError:
            raise KeyError(f"Decisione {decision_id} inesistente") from None

    # ------------------------------------------------------------------
    # ROUTING - il cuore dell'esercizio
    # ------------------------------------------------------------------
    @property
    def soglia_conf_effettiva(self) -> float:
        return round(self.soglia_conf + self.margine_drift, 3)

    def _contesto(self, r: Raccomandazione, post_stop: bool) -> dict:
        return {
            "criticita_utenza": r.criticita_utenza,
            "azione": r.azione_proposta,
            "prob_guasto": float(r.prob_guasto),
            "confidenza": float(r.confidenza),
            "soglia_rischio": self.soglia_rischio,
            "soglia_confidenza": self.soglia_conf_effettiva,
            "margine_drift": self.margine_drift,
            "area_promossa": self.area_promossa(r.area_geografica),
            "motivi_promozione": self.motivi_promozione(r.area_geografica),
            "post_stop": bool(post_stop),
        }

    def route(self, r: Raccomandazione, post_stop: bool = False) -> LivelloSupervisione:
        """Assegna il livello di supervisione secondo la matrice dichiarata (R1-R8)."""
        c = self._contesto(r, post_stop)
        if c["criticita_utenza"] == "critica":
            livello, regola = LivelloSupervisione.HIC, "R1"
        elif c["azione"] == "riduci_carico" and c["criticita_utenza"] != "standard":
            livello, regola = LivelloSupervisione.HIC, "R2"
        elif c["prob_guasto"] >= c["soglia_rischio"]:
            livello, regola = LivelloSupervisione.HITL, "R3"
        elif c["confidenza"] < c["soglia_confidenza"]:
            livello, regola = LivelloSupervisione.HITL, "R4"
        elif c["area_promossa"]:
            livello, regola = LivelloSupervisione.HITL, "R5"
        elif c["post_stop"]:
            livello, regola = LivelloSupervisione.HITL, "R6"
        elif c["azione"] in AZIONI_LEGGERE:
            livello, regola = LivelloSupervisione.HOTL, "R7"
        else:
            livello, regola = LivelloSupervisione.HITL, "R8"   # default prudente
        r.livello, r.regola, r.contesto_routing = livello, regola, c
        r.motivo_routing = self._testo_regola(regola, c, r)
        return livello

    @staticmethod
    def _testo_regola(regola: str, c: dict, r: Raccomandazione) -> str:
        if regola == "R1":
            return "Utenza critica coinvolta: veto umano assoluto, decide solo l'operatore."
        if regola == "R2":
            return f"Riduzione del carico su utenza {c['criticita_utenza']}: decisione riservata all'operatore."
        if regola == "R3":
            return (f"P(guasto) {_f(c['prob_guasto'])} pari o sopra la soglia di rischio "
                    f"{_f(c['soglia_rischio'])}: serve una revisione umana prima di agire.")
        if regola == "R4":
            extra = (f" (inclusi +{_f(c['margine_drift'])} per l'allerta di drift)"
                     if c["margine_drift"] else "")
            return (f"Confidenza {_f(c['confidenza'])} sotto la soglia {_f(c['soglia_confidenza'])}"
                    f"{extra}: il modello e' incerto, decide l'umano.")
        if regola == "R5":
            return (f"Area {r.area_geografica} sotto osservazione ({'; '.join(c['motivi_promozione'])}): "
                    f"decisione promossa a HITL.")
        if regola == "R6":
            return "Decisione sospesa da un emergency stop: alla riattivazione serve una revisione umana."
        if regola == "R7":
            return "Rischio basso, confidenza alta e azione leggera: esecuzione automatica monitorata (HOTL)."
        return f"Azione '{c['azione']}' non leggera: serve l'approvazione umana (default prudente)."

    def verifica_routing(self, r: Raccomandazione) -> bool:
        """KPI A6: il livello assegnato coincide con la matrice dichiarata?"""
        if not r.contesto_routing or r.livello is None:
            return False
        return valuta_matrice(r.contesto_routing).livello == r.livello

    # ------------------------------------------------------------------
    def sottometti(self, r: Raccomandazione) -> Raccomandazione:
        """Instrada la raccomandazione e la esegue (solo HOTL) o la mette in coda."""
        with self._lock:
            if r.id in self._indice:
                raise ValueError(f"Decisione {r.id} gia' sottomessa")
            if r.azione_proposta not in AZIONI:
                raise ValueError(f"Azione sconosciuta: {r.azione_proposta}")
            r.azione_originale = r.azione_originale or r.azione_proposta
            r.stato = StatoDecisione.IN_ATTESA
            self.decisioni.append(r)
            self._indice[r.id] = r
            self.route(r)

            ambito = self._stop_applicabile(r)
            if ambito:
                # nuova raccomandazione dentro uno stop attivo: non entra in coda e non si esegue
                r.stato, r.bloccata_da, r.sospesa_da_stop = StatoDecisione.BLOCCATA_STOP, ambito, True
                self.audit.log("SISTEMA", "blocco_emergency_stop", r, extra={
                    "ambito": ambito, "motivazione": "nuova raccomandazione nell'ambito di uno stop attivo"})
                return r

            if r.livello == LivelloSupervisione.HOTL:
                r.stato = StatoDecisione.AUTO_ESEGUITA
                self.audit.log("SISTEMA", "auto_esecuzione_HOTL", r, extra={"motivo_routing": r.motivo_routing})
                self._esegui(r, "SISTEMA")
            else:
                r.in_coda_dal = _iso(self._ora())
                self.audit.log("SISTEMA", f"in_coda_{r.livello.value}", r,
                               extra={"motivo_routing": r.motivo_routing, "sla_minuti": self.sla_minuti})
            return r

    # ------------------------------------------------------------------
    # REVISIONE UMANA
    # ------------------------------------------------------------------
    def apri(self, decision_id: str, operatore: str) -> Raccomandazione:
        """L'operatore apre la card: da qui si misura il tempo di revisione (KPI A3)."""
        with self._lock:
            operatore = self._valida_operatore(operatore)
            r = self._trova(decision_id)
            if r.stato not in STATI_IN_CODA or (r.aperta_da == operatore and r.aperta_il):
                return r
            r.aperta_il, r.aperta_da = _iso(self._ora()), operatore
            self.audit.log(operatore, "apertura_card", r)
            return r

    def revisiona(self, decision_id: str, esito: StatoDecisione,
                  revisore: str, motivazione: str,
                  azione_modificata: Optional[str] = None) -> Raccomandazione:
        """Registra il giudizio umano. La motivazione e' OBBLIGATORIA."""
        with self._lock:
            revisore = self._valida_operatore(revisore)
            motivazione = self._valida_motivazione(motivazione)
            try:
                esito = StatoDecisione(esito)
            except ValueError:
                raise ValueError(f"Esito non valido: {esito}") from None
            if esito not in ESITI_FINALI + (StatoDecisione.ESCALATION,):
                raise ValueError("Esiti ammessi: APPROVATA, MODIFICATA, RIFIUTATA, ESCALATION")
            r = self._trova(decision_id)
            if r.stato == StatoDecisione.BLOCCATA_STOP:
                raise ValueError(f"Decisione {decision_id} bloccata da emergency stop ({r.bloccata_da}): "
                                 f"non revisionabile finche' lo stop e' attivo")
            if r.stato not in STATI_IN_CODA:
                raise ValueError(f"Decisione {decision_id} gia' chiusa: {r.stato.value}")
            if esito == StatoDecisione.MODIFICATA:
                if azione_modificata not in AZIONI:
                    raise ValueError(f"Azione modificata non valida: scegliere tra {', '.join(AZIONI)}")
                if azione_modificata == r.azione_proposta:
                    raise ValueError("L'azione modificata coincide con quella proposta: usare Approva")
            elif azione_modificata:
                raise ValueError("Un'azione alternativa si indica solo con l'esito MODIFICATA")

            ora = self._ora()
            if esito == StatoDecisione.ESCALATION:
                if r.stato == StatoDecisione.ESCALATION:
                    raise ValueError("Decisione gia' in escalation: la chiude un supervisore")
                r.stato = StatoDecisione.ESCALATION
                r.escalation_il, r.escalation_da = _iso(ora), revisore
                r.motivo_escalation, r.escalation_sla = motivazione, False
                self._registra_motivazione(motivazione, r.id)
                self.audit.log(revisore, "revisione_ESCALATION", r, extra={"motivazione": motivazione})
                return r

            # revisori differenziati per competenza
            if r.stato == StatoDecisione.ESCALATION and not self.e_supervisore(revisore):
                raise PermissionError("Decisione in ESCALATION: puo' chiuderla solo un supervisore (ID che "
                                      f"inizia con {self.PREFISSO_SUPERVISORE}).")
            if (r.livello == LivelloSupervisione.HIC and self.hic_solo_supervisori
                    and not self.e_supervisore(revisore)):
                raise PermissionError("Decisione HIC (utenza critica): puo' chiuderla solo un supervisore "
                                      f"(ID che inizia con {self.PREFISSO_SUPERVISORE}) oppure va messa in escalation.")

            self._blocca_fotocopia(r, esito, revisore, motivazione)
            durata = self._durata_apertura(r, revisore, ora)
            flag = self._flag_revisione(durata, motivazione)
            r.stato, r.revisore, r.motivazione = esito, revisore, motivazione
            r.chiusa_il, r.durata_revisione_s, r.flag_revisione = _iso(ora), durata, flag
            if esito == StatoDecisione.MODIFICATA:
                r.azione_proposta = azione_modificata
            self._registra_motivazione(motivazione, r.id)
            self.audit.log(revisore, f"revisione_{esito.value}", r, extra={
                "azione_ai": r.azione_originale, "durata_revisione_s": durata, "flag_revisione": flag})
            if esito in (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA):
                self._esegui(r, revisore)
            return r

    def contesta(self, decision_id: str, operatore: str, motivazione: str) -> Raccomandazione:
        """HOTL: l'umano monitora e puo' intervenire ex post contestando l'esecuzione automatica."""
        with self._lock:
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            r = self._trova(decision_id)
            if r.stato != StatoDecisione.AUTO_ESEGUITA:
                raise ValueError("Si contestano ex post solo le decisioni HOTL eseguite automaticamente")
            if r.contestazione:
                raise ValueError("Decisione gia' contestata")
            r.contestazione = {"operatore": operatore, "motivazione": motivazione, "il": _iso(self._ora())}
            self.audit.log(operatore, "contestazione_HOTL", r, extra={"motivazione": motivazione})
            return r

    def _blocca_fotocopia(self, r: Raccomandazione, esito: StatoDecisione, revisore: str,
                          motivazione: str) -> None:
        """Rubber-stamping (KPI A4): una motivazione fotocopia RESPINGE la revisione, non la segnala soltanto.
        La decisione resta in coda e il tentativo resta in audit trail e nei KPI."""
        trovata = self._trova_fotocopia(motivazione, escludi=r.id)
        if not trovata:
            return
        origine_id, similarita = trovata
        origine = self._indice.get(origine_id)
        asset = origine.asset_id if origine else origine_id
        self.respinte.append({"il": _iso(self._ora()), "operatore": revisore, "decisione_id": r.id,
                              "motivo": "motivazione_fotocopia"})
        self.audit.log(revisore, "rubber_stamping_bloccato", r, extra={
            "motivo": "motivazione_fotocopia", "esito_tentato": esito.value, "motivazione": motivazione,
            "fotocopia_di": origine_id, "asset_origine": asset, "similarita": round(similarita, 3)})
        raise RubberStamping(
            f"Rubber-stamping bloccato: motivazione fotocopia (simile al {similarita:.0%} a quella gia' usata "
            f"per {asset}). Scrivi cosa hai verificato su questa decisione: la revisione NON e' stata "
            f"registrata e la decisione resta in coda.")

    @staticmethod
    def _durata_apertura(r: Raccomandazione, revisore: str, ora: datetime) -> Optional[float]:
        if r.aperta_il and r.aperta_da == revisore:
            return round((ora - _da_iso(r.aperta_il)).total_seconds(), 1)
        return None

    def _flag_revisione(self, durata: Optional[float], motivazione: str) -> list:
        """Segnali di rubber-stamping che non bloccano ma contano nei KPI A3/A4."""
        flag = []
        if len(motivazione) < self.SOGLIA_MOTIVAZIONE_BREVE:
            flag.append("motivazione_breve")
        if durata is None:
            flag.append("apertura_non_registrata")
        elif durata < self.SOGLIA_REVISIONE_RAPIDA_S:
            flag.append("revisione_rapida")
        return flag

    @staticmethod
    def _normalizza(testo: str) -> str:
        t = unicodedata.normalize("NFKD", testo.lower())
        t = "".join(ch for ch in t if not unicodedata.combining(ch))
        return " ".join(re.findall(r"[a-z0-9]+", t))

    @classmethod
    def _confrontabile(cls, testo: str) -> str:
        # senza numeri: cambiare solo l'ID asset o una cifra non rende diversa la motivazione
        return " ".join(p for p in cls._normalizza(testo).split() if not any(c.isdigit() for c in p))

    def _trova_fotocopia(self, motivazione: str, escludi: str = None) -> Optional[tuple[str, float]]:
        """Motivazione fotocopia: identica o quasi a una gia' usata. Ritorna (decisione, similarita') o None."""
        t = self._confrontabile(motivazione)
        if not t:
            return None
        sm = SequenceMatcher(None, autojunk=False)
        sm.set_seq2(t)
        migliore = None
        for prec, did in self._motivazioni_usate:
            if did == escludi:
                continue
            sm.set_seq1(prec)
            if sm.real_quick_ratio() < self.SOGLIA_FOTOCOPIA or sm.quick_ratio() < self.SOGLIA_FOTOCOPIA:
                continue
            s = sm.ratio()
            if s >= self.SOGLIA_FOTOCOPIA and (migliore is None or s > migliore[1]):
                migliore = (did, s)
        return migliore

    def _registra_motivazione(self, motivazione: str, decision_id: str) -> None:
        self._motivazioni_usate.append((self._confrontabile(motivazione), decision_id))

    def _valida_motivazione(self, motivazione: Optional[str]) -> str:
        m = re.sub(r"\s+", " ", motivazione or "").strip()
        if len(m) < self.MIN_MOTIVAZIONE:
            raise ValueError(f"Motivazione obbligatoria: almeno {self.MIN_MOTIVAZIONE} caratteri "
                             f"(inseriti {len(m)}). Senza motivazione non c'e' audit trail.")
        if len(m) > self.MAX_MOTIVAZIONE:
            raise ValueError(f"Motivazione troppo lunga (massimo {self.MAX_MOTIVAZIONE} caratteri).")
        parole = [p for p in re.findall(r"[^\W\d_]+", m) if len(p) >= 2]
        lettere = {ch.lower() for ch in m if ch.isalpha()}
        if len(parole) < 2 or len(lettere) < 5:
            raise ValueError("Motivazione non significativa: scrivere una frase che spieghi il giudizio "
                             "(cosa si e' verificato e perche').")
        return m

    def _valida_operatore(self, operatore: Optional[str]) -> str:
        op = (operatore or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{1,31}", op):
            raise ValueError("ID operatore non valido: usare ad es. OP-ROSSI o SUP-BIANCHI "
                             "(2-32 caratteri: lettere, cifre, '.', '_', '-').")
        if op == "SISTEMA":
            raise ValueError("'SISTEMA' e' riservato alle azioni automatiche")
        return op

    def e_supervisore(self, operatore: str) -> bool:
        return (operatore or "").upper().startswith(self.PREFISSO_SUPERVISORE)

    # ------------------------------------------------------------------
    # SLA ED ESCALATION
    # ------------------------------------------------------------------
    def scadenza_sla(self, r: Raccomandazione) -> Optional[str]:
        if r.stato != StatoDecisione.IN_ATTESA or not r.in_coda_dal:
            return None
        return _iso(_da_iso(r.in_coda_dal) + timedelta(minutes=self.sla_minuti))

    def controlla_sla(self) -> list[Raccomandazione]:
        """Le decisioni non revisionate entro lo SLA vanno in ESCALATION, mai eseguite in silenzio."""
        with self._lock:
            ora, scadute = self._ora(), []
            for r in self.decisioni:
                if (r.stato == StatoDecisione.IN_ATTESA and r.in_coda_dal
                        and ora - _da_iso(r.in_coda_dal) >= timedelta(minutes=self.sla_minuti)):
                    r.stato = StatoDecisione.ESCALATION
                    r.escalation_il, r.escalation_da, r.escalation_sla = _iso(ora), "SISTEMA", True
                    r.motivo_escalation = f"SLA di {self.sla_minuti} minuti scaduto senza revisione"
                    self.audit.log("SISTEMA", "escalation_SLA", r, extra={
                        "motivazione": r.motivo_escalation, "in_coda_dal": r.in_coda_dal})
                    scadute.append(r)
            return scadute

    # ------------------------------------------------------------------
    # EMERGENCY STOP - deve bloccare DAVVERO (la giuria lo verifica)
    # ------------------------------------------------------------------
    def normalizza_ambito(self, ambito: str) -> str:
        """'GLOBALE' | 'area:<nome>' | 'tipo:<tipo_asset>' | 'area:<nome>+tipo:<tipo_asset>'"""
        testo = (ambito or "").strip()
        if testo.upper() == "GLOBALE":
            return "GLOBALE"
        parti = {}
        for pezzo in testo.split("+"):
            chiave, sep, valore = pezzo.partition(":")
            chiave, valore = chiave.strip().lower(), valore.strip()
            if not sep or chiave not in ("area", "tipo") or not valore or chiave in parti:
                raise ValueError(f"Ambito di stop non valido: '{ambito}'. Usare GLOBALE, area:<nome>, "
                                 f"tipo:<tipo_asset> oppure area:<nome>+tipo:<tipo_asset>")
            parti[chiave] = valore
        if "area" in parti and self.aree is not None and parti["area"] not in self.aree:
            raise ValueError(f"Area sconosciuta: {parti['area']}")
        if "tipo" in parti and self.tipi is not None and parti["tipo"] not in self.tipi:
            raise ValueError(f"Tipo di asset sconosciuto: {parti['tipo']}")
        return "+".join(f"{k}:{parti[k]}" for k in ("area", "tipo") if k in parti)

    @staticmethod
    def _in_ambito(r: Raccomandazione, ambito: str) -> bool:
        if ambito == "GLOBALE":
            return True
        for pezzo in ambito.split("+"):
            chiave, _, valore = pezzo.partition(":")
            if chiave == "area" and r.area_geografica != valore:
                return False
            if chiave == "tipo" and r.tipo_asset != valore:
                return False
        return True

    def _stop_applicabile(self, r: Raccomandazione) -> Optional[str]:
        """Ritorna l'ambito di stop che copre la decisione (None se nessuno)."""
        if "GLOBALE" in self.stop_attivi:
            return "GLOBALE"
        for ambito in self.stop_attivi:
            if self._in_ambito(r, ambito):
                return ambito
        return None

    def in_ambito(self, ambito: str) -> list[Raccomandazione]:
        """Decisioni ancora eseguibili che uno stop su `ambito` bloccherebbe (anteprima per la conferma)."""
        amb = self.normalizza_ambito(ambito)
        return [r for r in self.decisioni if r.stato in STATI_IN_CODA and self._in_ambito(r, amb)]

    def attiva_stop(self, ambito: str, operatore: str, motivazione: str) -> list[Raccomandazione]:
        with self._lock:
            ambito = self.normalizza_ambito(ambito)
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            if ambito in self.stop_attivi:
                raise ValueError(f"Stop gia' attivo su {ambito}")
            self.stop_attivi[ambito] = {"operatore": operatore, "motivazione": motivazione,
                                        "dal": _iso(self._ora())}
            self.audit.log(operatore, "emergency_stop_ON", None,
                           extra={"ambito": ambito, "motivazione": motivazione})
            # le decisioni gia' in coda nell'ambito diventano NON eseguibili
            bloccate = []
            for r in self.decisioni:
                if r.stato in STATI_IN_CODA and self._in_ambito(r, ambito):
                    r.stato_pre_blocco, r.stato = r.stato, StatoDecisione.BLOCCATA_STOP
                    r.bloccata_da, r.sospesa_da_stop = ambito, True
                    self.audit.log("SISTEMA", "blocco_emergency_stop", r, extra={
                        "ambito": ambito, "motivazione": f"emergency stop attivato da {operatore}",
                        "stato_precedente": r.stato_pre_blocco.value})
                    bloccate.append(r)
            return bloccate

    def disattiva_stop(self, ambito: str, operatore: str, motivazione: str,
                       conferma: bool = False) -> list[Raccomandazione]:
        """Riattivazione: motivazione + conferma esplicita. Niente riparte in automatico."""
        with self._lock:
            ambito = self.normalizza_ambito(ambito)
            if ambito not in self.stop_attivi:
                raise KeyError(f"Nessuno stop attivo su {ambito}")
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            if conferma is not True:
                raise ValueError("La riattivazione richiede una conferma esplicita")
            info = self.stop_attivi.pop(ambito)
            self.audit.log(operatore, "emergency_stop_OFF", None, extra={
                "ambito": ambito, "motivazione": motivazione,
                "attivato_da": info["operatore"], "attivo_dal": info["dal"]})
            sbloccate = []
            for r in self.decisioni:
                if r.stato != StatoDecisione.BLOCCATA_STOP or r.bloccata_da != ambito:
                    continue
                altro = self._stop_applicabile(r)
                if altro:
                    r.bloccata_da = altro
                    self.audit.log("SISTEMA", "blocco_emergency_stop", r, extra={
                        "ambito": altro, "motivazione": f"resta bloccata: ricade anche nello stop {altro}"})
                    continue
                if r.stato_pre_blocco == StatoDecisione.ESCALATION:
                    r.stato = StatoDecisione.ESCALATION
                else:
                    self._reinstrada_post_stop(r)
                    r.stato = StatoDecisione.IN_ATTESA
                    r.in_coda_dal, r.aperta_il, r.aperta_da = _iso(self._ora()), None, None
                r.bloccata_da, r.stato_pre_blocco = None, None
                self.audit.log("SISTEMA", "sblocco_post_stop", r, extra={
                    "ambito": ambito, "motivazione": f"stop disattivato da {operatore}",
                    "motivo_routing": r.motivo_routing})
                sbloccate.append(r)
            return sbloccate

    def _reinstrada_post_stop(self, r: Raccomandazione):
        """Dopo uno stop la decisione torna in coda almeno HITL; il livello non scende mai."""
        prima = (r.livello, r.regola, r.motivo_routing, r.contesto_routing)
        self.route(r, post_stop=True)
        if prima[0] is not None and ORDINE_LIVELLI[r.livello] < ORDINE_LIVELLI[prima[0]]:
            r.livello, r.regola, r.motivo_routing, r.contesto_routing = prima

    # ------------------------------------------------------------------
    # SOGLIE E PROMOZIONI (il sistema si declassa da solo)
    # ------------------------------------------------------------------
    def soglie(self) -> dict:
        return {"soglia_confidenza": self.soglia_conf, "soglia_rischio": self.soglia_rischio,
                "sla_minuti": self.sla_minuti, "margine_drift": self.margine_drift,
                "soglia_confidenza_effettiva": self.soglia_conf_effettiva,
                "hic_solo_supervisori": self.hic_solo_supervisori}

    def imposta_soglie(self, operatore: str, motivazione: str, soglia_confidenza: float = None,
                       soglia_rischio: float = None, sla_minuti: int = None) -> dict:
        """Le nuove soglie valgono per le nuove decisioni: quelle in coda non vengono declassate.
        Lo SLA invece vale subito per tutta la coda."""
        with self._lock:
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            nuove = {"soglia_confidenza": soglia_confidenza, "soglia_rischio": soglia_rischio,
                     "sla_minuti": sla_minuti}
            attuali = {"soglia_confidenza": self.soglia_conf, "soglia_rischio": self.soglia_rischio,
                       "sla_minuti": self.sla_minuti}
            cambi = {}
            for nome, valore in nuove.items():
                if valore is None:
                    continue
                lo, hi = self.LIMITI_SOGLIE[nome]
                valore = int(valore) if nome == "sla_minuti" else round(float(valore), 3)
                if not lo <= valore <= hi:
                    raise ValueError(f"{nome} fuori dall'intervallo ammesso [{lo}, {hi}]")
                if valore != attuali[nome]:
                    cambi[nome] = {"prima": attuali[nome], "dopo": valore}
            if not cambi:
                raise ValueError("Nessuna soglia modificata")
            for nome, v in cambi.items():
                if nome == "soglia_confidenza":
                    self.soglia_conf = v["dopo"]
                elif nome == "soglia_rischio":
                    self.soglia_rischio = v["dopo"]
                else:
                    self.sla_minuti = v["dopo"]
            self.audit.log(operatore, "modifica_soglie", extra={
                "modifiche": cambi, "motivazione": motivazione,
                "nota": "le soglie di routing valgono per le nuove decisioni; lo SLA per tutta la coda"})
            return self.soglie()

    def imposta_margine_drift(self, margine: float, motivo: str) -> bool:
        with self._lock:
            margine = round(float(margine), 3)
            if margine == self.margine_drift:
                return False
            prima, self.margine_drift = self.margine_drift, margine
            self.audit.log("SISTEMA", "modifica_margine_drift", extra={
                "prima": prima, "dopo": margine, "motivazione": motivo,
                "soglia_confidenza_effettiva": self.soglia_conf_effettiva})
            return True

    def area_promossa(self, area: str) -> bool:
        p = self.promozioni.get(area)
        return bool(p and (p["automatiche"] or p["manuale"]))

    def motivi_promozione(self, area: str) -> list[str]:
        p = self.promozioni.get(area)
        if not p:
            return []
        out = [v["motivo"] for v in p["automatiche"].values()]
        if p["manuale"]:
            out.append(f"promozione manuale di {p['manuale']['operatore']}: {p['manuale']['motivazione']}")
        return out

    def aggiorna_promozioni_automatiche(self, attese: dict) -> list[dict]:
        """`attese` = {area: {politica: motivo}} con le condizioni vere ADESSO.
        Le promozioni nuove vengono attivate, quelle non piu' giustificate revocate (tutto nel log)."""
        with self._lock:
            cambi, ora = [], _iso(self._ora())
            for area in sorted(set(self.promozioni) | set(attese)):
                p = self.promozioni.setdefault(area, {"automatiche": {}, "manuale": None})
                correnti, nuove = p["automatiche"], attese.get(area, {})
                for politica, motivo in nuove.items():
                    if politica in correnti:
                        correnti[politica]["motivo"] = motivo
                        continue
                    correnti[politica] = {"motivo": motivo, "dal": ora}
                    self.audit.log("SISTEMA", "promozione_automatica_HITL", extra={
                        "area": area, "politica": politica, "motivazione": motivo})
                    cambi.append({"area": area, "politica": politica, "azione": "promossa"})
                for politica in [k for k in correnti if k not in nuove]:
                    info = correnti.pop(politica)
                    self.audit.log("SISTEMA", "revoca_promozione_automatica", extra={
                        "area": area, "politica": politica, "attiva_dal": info["dal"],
                        "motivazione": "condizione rientrata o politica disattivata"})
                    cambi.append({"area": area, "politica": politica, "azione": "revocata"})
            return cambi

    def promuovi_area(self, area: str, operatore: str, motivazione: str):
        with self._lock:
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            if self.aree is not None and area not in self.aree:
                raise KeyError(f"Area sconosciuta: {area}")
            p = self.promozioni.setdefault(area, {"automatiche": {}, "manuale": None})
            if p["manuale"]:
                raise ValueError(f"Area {area} gia' promossa manualmente")
            p["manuale"] = {"operatore": operatore, "motivazione": motivazione, "dal": _iso(self._ora())}
            self.audit.log(operatore, "promozione_manuale_HITL", extra={"area": area, "motivazione": motivazione})

    def revoca_promozione_manuale(self, area: str, operatore: str, motivazione: str):
        with self._lock:
            operatore = self._valida_operatore(operatore)
            motivazione = self._valida_motivazione(motivazione)
            p = self.promozioni.get(area)
            if not p or not p["manuale"]:
                raise ValueError(f"Nessuna promozione manuale attiva sull'area {area}")
            info, p["manuale"] = p["manuale"], None
            self.audit.log(operatore, "revoca_promozione_manuale", extra={
                "area": area, "motivazione": motivazione, "promossa_da": info["operatore"]})

    # ------------------------------------------------------------------
    def _esegui(self, r: Raccomandazione, attore: str):
        """Punto unico di esecuzione. Qualunque azione DEVE passare da qui.
        La giuria verifichera' che non esistano altri percorsi di esecuzione."""
        motivo = self._motivo_non_eseguibile(r)
        if motivo:
            self.audit.log(attore, "esecuzione_negata", r, extra={"motivazione": motivo})
            raise EsecuzioneNonAutorizzata(f"Esecuzione negata per {r.id} ({r.asset_id}): {motivo}")
        ora = _iso(self._ora())
        r.eseguita, r.eseguita_il = True, ora
        self.esecuzioni.append({"decisione_id": r.id, "asset_id": r.asset_id, "azione": r.azione_proposta,
                                "azione_ai": r.azione_originale, "livello": r.livello.value,
                                "stato": r.stato.value, "autorizzata_da": r.revisore or "SISTEMA (HOTL)",
                                "eseguita_il": ora})
        self.audit.log(attore, "esecuzione_azione", r, extra={"azione": r.azione_proposta})
        log_esecuzioni.info("[ESECUZIONE] %s: %s (%s, %s)", r.asset_id, r.azione_proposta,
                            r.livello.value, r.stato.value)

    def _motivo_non_eseguibile(self, r: Raccomandazione) -> Optional[str]:
        if r.eseguita:
            return "azione gia' eseguita"
        if r.stato not in (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA,
                           StatoDecisione.AUTO_ESEGUITA):
            return f"stato {r.stato.value}: si eseguono solo decisioni approvate/modificate o HOTL"
        if r.stato == StatoDecisione.AUTO_ESEGUITA and r.livello != LivelloSupervisione.HOTL:
            return f"l'esecuzione automatica e' ammessa solo per HOTL (livello {r.livello.value})"
        if r.stato != StatoDecisione.AUTO_ESEGUITA and (
                not r.revisore or r.revisore == "SISTEMA" or not r.motivazione):
            return "manca la revisione umana motivata"
        if r.azione_proposta not in AZIONI:
            return f"azione sconosciuta: {r.azione_proposta}"
        ambito = self._stop_applicabile(r)
        if ambito:
            return f"emergency stop attivo ({ambito})"
        return None

    # ------------------------------------------------------------------
    # KPI per il pannello di monitoraggio (vedi Indicatori di Qualita')
    # ------------------------------------------------------------------
    def kpi(self) -> dict:
        with self._lock:
            dec = list(self.decisioni)
            umane = [r for r in dec if r.livello in (LivelloSupervisione.HITL, LivelloSupervisione.HIC)]
            improprie = [r for r in umane if r.eseguita and not (
                r.stato in (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA)
                and r.revisore and r.revisore != "SISTEMA" and r.motivazione)]
            finali = [r for r in dec if r.stato in ESITI_FINALI]
            override = [r for r in finali if r.stato != StatoDecisione.APPROVATA]
            durate = [r.durata_revisione_s for r in finali if r.durata_revisione_s is not None]
            brevi = [r for r in finali if "motivazione_breve" in r.flag_revisione]
            copie = [x for x in self.respinte if x["motivo"] == "motivazione_fotocopia"]
            rapide = [r for r in finali if {"revisione_rapida", "apertura_non_registrata"} & set(r.flag_revisione)]
            entrate = [r for r in umane if r.in_coda_dal]
            scadute = [r for r in dec if r.escalation_sla]
            con_livello = [r for r in dec if r.livello is not None]
            coerenti = [r for r in con_livello if self.verifica_routing(r)]

            def quota(a, b):
                return round(len(a) / len(b), 3) if b else None

            tasso_override = quota(override, finali)
            mediana = round(median(durate), 1) if durate else None
            # A4 misura il comportamento: le fotocopie respinte contano come tentativi di rubber-stamping
            tentativi = len(finali) + len(copie)
            a4 = round((len(brevi) + len(copie)) / tentativi, 3) if tentativi else None
            a5, a6 = quota(scadute, entrate), quota(coerenti, con_livello)
            indicatori = [
                _indicatore("A1", "Auto-esecuzione impropria", quota(improprie, umane), "pct", "0 assoluto",
                            "ok" if not improprie else "critico",
                            f"{len(improprie)} decisioni HIC/HITL eseguite senza revisione su {len(umane)}"),
                _indicatore("A2", "Tasso di override umano", tasso_override, "pct", "tra 5% e 40%",
                            _stato_a2(tasso_override, len(finali)),
                            f"{len(override)} rifiutate o modificate su {len(finali)} revisionate"),
                _indicatore("A3", "Tempo mediano di revisione", mediana, "s", "30 s - 5 min",
                            _stato_a3(mediana, len(durate)),
                            f"{len(durate)} revisioni con apertura registrata; "
                            f"{len(rapide)} sotto 10 s o senza apertura"),
                _indicatore("A4", "Indice di rubber-stamping", a4, "pct", "< 10%",
                            _stato_soglia(a4, 0.10, 0.30, tentativi),
                            f"{len(brevi)} motivazioni < {self.SOGLIA_MOTIVAZIONE_BREVE} caratteri accettate "
                            f"+ {len(copie)} fotocopie respinte su {tentativi} tentativi di revisione"),
                _indicatore("A5", "Escalation per SLA scaduto", a5, "pct", "< 15%",
                            _stato_soglia(a5, 0.15, 0.15, len(entrate)),
                            f"{len(scadute)} scadute su {len(entrate)} entrate in coda umana "
                            f"(SLA {self.sla_minuti} min)"),
                _indicatore("A6", "Copertura del routing dichiarato", a6, "pct", "100%",
                            "n/d" if a6 is None else ("ok" if a6 == 1 else "critico"),
                            f"{len(coerenti)} decisioni su {len(con_livello)} coerenti con la matrice R1-R8"),
            ]
            stati = Counter(r.stato.value for r in dec)
            return {
                # chiavi originali dello starter kit
                "in_attesa": stati.get(StatoDecisione.IN_ATTESA.value, 0),
                "tasso_override": tasso_override,
                "stop_attivi": sorted(self.stop_attivi),
                # estensioni
                "totale_decisioni": len(dec),
                "in_escalation": stati.get(StatoDecisione.ESCALATION.value, 0),
                "bloccate_stop": stati.get(StatoDecisione.BLOCCATA_STOP.value, 0),
                "eseguite": sum(1 for r in dec if r.eseguita),
                "distribuzione_livelli": {lv.value: sum(1 for r in dec if r.livello == lv)
                                          for lv in LivelloSupervisione},
                "distribuzione_stati": dict(stati),
                "distribuzione_regole": dict(Counter(r.regola for r in dec if r.regola)),
                "tempo_mediano_revisione_s": mediana,
                "pct_revisioni_rapide": quota(rapide, finali),
                "revisioni_respinte": len(copie),
                "indicatori": indicatori,
            }

    def statistiche_revisori(self) -> list[dict]:
        with self._lock:
            per: dict[str, list] = {}
            for r in self.decisioni:
                if r.stato in ESITI_FINALI and r.revisore:
                    per.setdefault(r.revisore, []).append(r)
            escalation = Counter(r.escalation_da for r in self.decisioni
                                 if r.escalation_da and r.escalation_da != "SISTEMA" and not r.escalation_sla)
            respinte = Counter(x["operatore"] for x in self.respinte)
            out = []
            for rev in sorted(set(per) | set(escalation) | set(respinte)):
                lst = per.get(rev, [])
                durate = [x.durata_revisione_s for x in lst if x.durata_revisione_s is not None]
                n, copie = len(lst), respinte.get(rev, 0)
                tentativi = n + copie
                brevi = sum(1 for x in lst if "motivazione_breve" in x.flag_revisione) + copie
                rapide = sum(1 for x in lst if {"revisione_rapida", "apertura_non_registrata"} & set(x.flag_revisione))
                override = sum(1 for x in lst if x.stato != StatoDecisione.APPROVATA)
                segnali = []
                if n >= 3 and override == 0:
                    segnali.append("approva sempre")
                if n and rapide / n > 0.5:
                    segnali.append("revisioni troppo rapide")
                if tentativi and brevi / tentativi > 0.3:
                    segnali.append("motivazioni brevi o fotocopia")
                if copie:
                    segnali.append(f"{copie} {'revisione respinta' if copie == 1 else 'revisioni respinte'} (motivazione fotocopia)")
                out.append({
                    "revisore": rev, "ruolo": "supervisore" if self.e_supervisore(rev) else "operatore",
                    "revisioni": n,
                    "approvate": sum(1 for x in lst if x.stato == StatoDecisione.APPROVATA),
                    "modificate": sum(1 for x in lst if x.stato == StatoDecisione.MODIFICATA),
                    "rifiutate": sum(1 for x in lst if x.stato == StatoDecisione.RIFIUTATA),
                    "escalation_richieste": escalation.get(rev, 0),
                    "revisioni_respinte": copie,
                    "tasso_override": round(override / n, 3) if n else None,
                    "tempo_mediano_s": round(median(durate), 1) if durate else None,
                    "pct_brevi_o_duplicate": round(brevi / tentativi, 3) if tentativi else None,
                    "pct_rapide": round(rapide / n, 3) if n else None,
                    "segnali": segnali,
                })
            return out

    def override_per_area(self) -> list[dict]:
        """KPI C3: override (e contestazioni HOTL) per area."""
        with self._lock:
            out = []
            for area in sorted({r.area_geografica for r in self.decisioni}):
                dec = [r for r in self.decisioni if r.area_geografica == area]
                finali = [r for r in dec if r.stato in ESITI_FINALI]
                override = [r for r in finali if r.stato != StatoDecisione.APPROVATA]
                contestate = [r for r in dec if r.contestazione]
                giudizi = len(finali) + len(contestate)
                out.append({
                    "area": area, "decisioni": len(dec), "revisionate": len(finali),
                    "override": len(override), "contestazioni_hotl": len(contestate),
                    "tasso_override": round(len(override) / len(finali), 3) if finali else None,
                    "giudizi_umani": giudizi,
                    "tasso_disaccordo": round((len(override) + len(contestate)) / giudizi, 3) if giudizi else None,
                    "promossa": self.area_promossa(area),
                    "quota_coda_umana": round(sum(1 for r in dec if r.livello != LivelloSupervisione.HOTL)
                                              / len(dec), 3) if dec else None,
                })
            return out

    def carico_lavoro(self) -> dict:
        with self._lock:
            ora = self._ora()
            in_coda = self.coda
            attese = [(ora - _da_iso(r.in_coda_dal)).total_seconds() / 60 for r in in_coda if r.in_coda_dal]
            vicine = [r for r in in_coda if r.stato == StatoDecisione.IN_ATTESA and r.in_coda_dal and
                      (ora - _da_iso(r.in_coda_dal)) >= timedelta(minutes=self.sla_minuti * 0.8)]
            ultima_ora = [r for r in self.decisioni if r.chiusa_il and
                          ora - _da_iso(r.chiusa_il) <= timedelta(hours=1)]
            return {
                "in_coda": len(in_coda),
                "per_livello": {lv.value: sum(1 for r in in_coda if r.livello == lv)
                                for lv in (LivelloSupervisione.HIC, LivelloSupervisione.HITL)},
                "in_escalation": sum(1 for r in in_coda if r.stato == StatoDecisione.ESCALATION),
                "attesa_max_min": round(max(attese), 1) if attese else None,
                "vicine_scadenza_sla": len(vicine),
                "revisioni_ultima_ora": len(ultima_ora),
                "quota_coda_umana": round(sum(1 for r in self.decisioni if r.livello != LivelloSupervisione.HOTL)
                                          / len(self.decisioni), 3) if self.decisioni else None,
            }

    # ------------------------------------------------------------------
    def contesto_spiegazione(self, r: Raccomandazione) -> dict:
        """Dati della decisione passati agli spiegatori (template e LLM): solo valori gia' calcolati,
        con le soglie in vigore al momento del routing."""
        c = r.contesto_routing or {}
        return {
            "decisione_id": r.id, "asset_id": r.asset_id, "tipo_asset": r.tipo_asset,
            "area_geografica": r.area_geografica, "criticita_utenza": r.criticita_utenza,
            "prob_guasto": float(r.prob_guasto), "confidenza": float(r.confidenza),
            "azione_proposta": r.azione_originale or r.azione_proposta,
            "livello": r.livello.value if r.livello else None, "regola": r.regola,
            "motivo_routing": r.motivo_routing,
            "soglia_confidenza": c.get("soglia_confidenza", self.soglia_conf_effettiva),
            "soglia_rischio": c.get("soglia_rischio", self.soglia_rischio),
            "margine_drift": c.get("margine_drift", self.margine_drift),
            "area_sotto_osservazione": bool(c.get("area_promossa")),
        }

    def a_dict(self, r: Raccomandazione) -> dict:
        d = asdict(r)
        for chiave in ("livello", "stato", "stato_pre_blocco"):
            v = getattr(r, chiave)
            d[chiave] = v.value if isinstance(v, Enum) else v
        d["spiegazione"] = [[str(f), float(v)] for f, v in (r.spiegazione or [])]
        d["scadenza_sla"] = self.scadenza_sla(r)
        d["routing_coerente"] = self.verifica_routing(r)
        d["richiede_supervisore"] = (r.stato == StatoDecisione.ESCALATION or (
            r.livello == LivelloSupervisione.HIC and self.hic_solo_supervisori))
        return d


def _indicatore(codice, nome, valore, unita, target, stato, dettaglio) -> dict:
    return {"codice": codice, "nome": nome, "valore": valore, "unita": unita,
            "target": target, "stato": stato, "dettaglio": dettaglio}


def _stato_a2(x, n) -> str:
    if x is None:
        return "n/d"
    if n < 5:
        return "info"
    if 0.05 <= x <= 0.40:
        return "ok"
    return "allerta" if (x == 0 or x > 0.60) else "attenzione"


def _stato_a3(x, n) -> str:
    if x is None:
        return "n/d"
    if x < 10:
        return "allerta"
    if n < 5:
        return "info"
    return "ok" if 30 <= x <= 300 else "attenzione"


def _stato_soglia(x, target, allarme, n) -> str:
    if x is None:
        return "n/d"
    if x < target:
        return "ok"
    if n < 5:
        return "info"
    return "allerta" if x > allarme or target == allarme else "attenzione"


# NOTA SU "CONFIDENZA": per un classificatore binario una scelta semplice e
# difendibile e' confidenza = max(p, 1-p) oppure 1 - entropia normalizzata.
# Qui usiamo max(p, 1-p) (calcola_confidenza), dichiarato in model card: e'
# alta vicino a 0 e a 1, minima (0.5) quando il modello "tira a indovinare".
