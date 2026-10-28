"""
EnerGuard Starter Kit - OversightManager
=========================================
Gestisce il routing delle raccomandazioni AI verso i tre livelli di
supervisione (HIC / HITL / HOTL) e la coda di approvazione umana.

REGOLA D'ORO: nessuna azione con esito "IN_ATTESA" puo' essere eseguita.
Il vostro sistema deve dimostrarlo (la giuria lo testera' dal vivo).

TODO per il team:
  1. Completare la matrice di routing in `route()` giustificando le soglie
     nella Dichiarazione di Oversight (deliverable D3).
  2. Implementare l'escalation: una decisione HITL non revisionata entro
     `sla_minuti` deve cambiare stato, non essere eseguita in silenzio.
  3. Collegare ogni transizione di stato all'AuditLogger.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class LivelloSupervisione(str, Enum):
    HOTL = "HOTL"   # il sistema agisce, l'umano monitora ex post
    HITL = "HITL"   # approvazione umana obbligatoria prima dell'esecuzione
    HIC = "HIC"     # veto umano assoluto: solo l'umano decide


class StatoDecisione(str, Enum):
    IN_ATTESA = "IN_ATTESA"
    APPROVATA = "APPROVATA"
    MODIFICATA = "MODIFICATA"
    RIFIUTATA = "RIFIUTATA"
    ESCALATION = "ESCALATION"
    AUTO_ESEGUITA = "AUTO_ESEGUITA"   # ammessa SOLO per HOTL
    BLOCCATA_STOP = "BLOCCATA_STOP"   # bloccata da emergency stop


AZIONI = ["nessuna_azione", "ispezione_routine", "programma_manutenzione",
          "riduci_carico", "ispezione_urgente"]


@dataclass
class Raccomandazione:
    asset_id: str
    tipo_asset: str
    area_geografica: str
    criticita_utenza: str          # standard | alta | critica
    prob_guasto: float             # output del modello, 0-1
    confidenza: float              # 0-1 (vedi nota in fondo)
    azione_proposta: str
    spiegazione: list              # es. [("vibrazione_indice", 0.41), ...]
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    creata_il: str = field(default_factory=lambda: datetime.now().isoformat())
    livello: Optional[LivelloSupervisione] = None
    stato: StatoDecisione = StatoDecisione.IN_ATTESA
    revisore: Optional[str] = None
    motivazione: Optional[str] = None


class OversightManager:
    def __init__(self, audit_logger, soglia_confidenza_alta: float = 0.80,
                 soglia_rischio_alto: float = 0.60, sla_minuti: int = 30):
        self.audit = audit_logger
        self.soglia_conf = soglia_confidenza_alta
        self.soglia_rischio = soglia_rischio_alto
        self.sla_minuti = sla_minuti
        self.coda: list[Raccomandazione] = []
        self.stop_attivi: set[str] = set()   # es. {"area:Sud", "tipo:linea_AT", "GLOBALE"}

    # ------------------------------------------------------------------
    # ROUTING - il cuore dell'esercizio
    # ------------------------------------------------------------------
    def route(self, r: Raccomandazione) -> LivelloSupervisione:
        """Assegna il livello di supervisione. COMPLETARE E GIUSTIFICARE.

        Logica minima di partenza (da estendere):
          - utenza critica (ospedali, infrastrutture) o azione "riduci_carico"
            su utenza alta/critica  -> HIC, sempre
          - rischio alto O confidenza bassa                     -> HITL
          - rischio basso E confidenza alta E azione leggera    -> HOTL
        """
        if r.criticita_utenza == "critica" or (
                r.azione_proposta == "riduci_carico" and r.criticita_utenza != "standard"):
            livello = LivelloSupervisione.HIC
        elif r.prob_guasto >= self.soglia_rischio or r.confidenza < self.soglia_conf:
            livello = LivelloSupervisione.HITL
        elif r.azione_proposta in ("nessuna_azione", "ispezione_routine"):
            livello = LivelloSupervisione.HOTL
        else:
            livello = LivelloSupervisione.HITL   # default prudente
        r.livello = livello
        return livello

    # ------------------------------------------------------------------
    def sottometti(self, r: Raccomandazione):
        """Instrada la raccomandazione e la esegue o la mette in coda."""
        if self._stop_applicabile(r):
            r.stato = StatoDecisione.BLOCCATA_STOP
            self.audit.log("SISTEMA", "blocco_emergency_stop", r)
            return r

        self.route(r)
        if r.livello == LivelloSupervisione.HOTL:
            r.stato = StatoDecisione.AUTO_ESEGUITA
            self._esegui(r)
            self.audit.log("SISTEMA", "auto_esecuzione_HOTL", r)
        else:
            self.coda.append(r)
            self.audit.log("SISTEMA", f"in_coda_{r.livello.value}", r)
        return r

    def revisiona(self, decision_id: str, esito: StatoDecisione,
                  revisore: str, motivazione: str,
                  azione_modificata: Optional[str] = None):
        """Registra il giudizio umano. La motivazione e' OBBLIGATORIA."""
        if not motivazione or len(motivazione.strip()) < 15:
            raise ValueError("Motivazione obbligatoria (minimo 15 caratteri). "
                             "Senza motivazione non c'e' audit trail.")
        r = self._trova(decision_id)
        if r.stato != StatoDecisione.IN_ATTESA:
            raise ValueError(f"Decisione {decision_id} gia' chiusa: {r.stato}")
        r.stato, r.revisore, r.motivazione = esito, revisore, motivazione
        if azione_modificata:
            r.azione_proposta = azione_modificata
        if esito in (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA):
            self._esegui(r)
        self.audit.log(revisore, f"revisione_{esito.value}", r)
        return r

    # ------------------------------------------------------------------
    # EMERGENCY STOP - deve bloccare DAVVERO (la giuria lo verifica)
    # ------------------------------------------------------------------
    def attiva_stop(self, ambito: str, operatore: str, motivazione: str):
        """ambito: 'GLOBALE' | 'area:<nome>' | 'tipo:<tipo_asset>'"""
        self.stop_attivi.add(ambito)
        self.audit.log(operatore, f"emergency_stop_ON:{ambito}",
                       None, extra={"motivazione": motivazione})
        # TODO: le decisioni gia' in coda che ricadono nell'ambito
        # devono passare a BLOCCATA_STOP, non restare eseguibili.

    def disattiva_stop(self, ambito: str, operatore: str, motivazione: str):
        self.stop_attivi.discard(ambito)
        self.audit.log(operatore, f"emergency_stop_OFF:{ambito}",
                       None, extra={"motivazione": motivazione})

    def _stop_applicabile(self, r: Raccomandazione) -> bool:
        return ("GLOBALE" in self.stop_attivi
                or f"area:{r.area_geografica}" in self.stop_attivi
                or f"tipo:{r.tipo_asset}" in self.stop_attivi)

    # ------------------------------------------------------------------
    def _esegui(self, r: Raccomandazione):
        """Punto unico di esecuzione. Qualunque azione DEVE passare da qui.
        La giuria verifichera' che non esistano altri percorsi di esecuzione."""
        assert r.stato in (StatoDecisione.APPROVATA, StatoDecisione.MODIFICATA,
                           StatoDecisione.AUTO_ESEGUITA), \
            f"Tentata esecuzione con stato non valido: {r.stato}"
        assert not (r.livello == LivelloSupervisione.HIC
                    and r.stato == StatoDecisione.AUTO_ESEGUITA), \
            "Violazione: una decisione HIC non puo' mai essere auto-eseguita"
        print(f"[ESECUZIONE] {r.asset_id}: {r.azione_proposta} ({r.livello.value})")

    def _trova(self, decision_id: str) -> Raccomandazione:
        for r in self.coda:
            if r.id == decision_id:
                return r
        raise KeyError(decision_id)

    # ------------------------------------------------------------------
    # KPI per il pannello di monitoraggio (vedi Indicatori di Qualita')
    # ------------------------------------------------------------------
    def kpi(self) -> dict:
        chiuse = [r for r in self.coda if r.stato != StatoDecisione.IN_ATTESA]
        override = [r for r in chiuse if r.stato in
                    (StatoDecisione.RIFIUTATA, StatoDecisione.MODIFICATA)]
        return {
            "in_attesa": sum(1 for r in self.coda if r.stato == StatoDecisione.IN_ATTESA),
            "tasso_override": round(len(override) / len(chiuse), 3) if chiuse else None,
            "stop_attivi": sorted(self.stop_attivi),
            # TODO: tempo medio di revisione, distribuzione HIC/HITL/HOTL,
            #       % motivazioni sotto i 30 caratteri (proxy di rubber-stamping)
        }


# NOTA SU "CONFIDENZA": per un classificatore binario una scelta semplice e
# difendibile e' confidenza = max(p, 1-p) oppure 1 - entropia normalizzata.
# Qualunque scelta va dichiarata nella model card.
