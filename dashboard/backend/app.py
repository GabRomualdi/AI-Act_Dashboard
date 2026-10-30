"""
EnerGuard - server della dashboard di supervisione umana
=========================================================
Esecuzione (dalla root del progetto o da dashboard/backend):

    python train_baseline.py          # una volta: produce modello.joblib e predizioni.csv
    python app.py [--port 8000] [--senza-llm]
    -> http://127.0.0.1:8000

In alternativa: `streamlit run app.py` avvia lo stesso server e mostra la dashboard in un iframe.

Il browser e' solo una vista: routing, motivazioni, emergency stop, esecuzione e audit
vivono qui e nei moduli oversight_manager / explainer / bias_detector / audit_logger.

Limiti dichiarati della demo: identita' dell'operatore auto-dichiarata (prefisso SUP- = supervisore),
decisioni in memoria (il log su file persiste), catena di hash non firmata, flusso di
raccomandazioni simulato a partire dal test set (una ogni 5 dal pulsante "+5" e' uno scenario
simulato di deterioramento, dichiarato sulla card e nel log: vedi scenari.py).
"""
import argparse
import json
import math
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from audit_logger import AuditLogger  # noqa: E402
from bias_detector import BiasDetector  # noqa: E402
from explainer import (AZIONI_ETICHETTA, GUASTI_SIMULABILI, CasiSimili, ConfigLLM,  # noqa: E402
                       SpiegatoreLLM, analizza_riga, calibra_etichette, controfattuale,
                       descrivi_confidenza)
from oversight_manager import (AZIONI, STATI_IN_CODA, EsecuzioneNonAutorizzata,  # noqa: E402
                               LivelloSupervisione, OversightManager, Raccomandazione,
                               RubberStamping, StatoDecisione, azione_raccomandata,
                               calcola_confidenza)
from scenari import GeneratoreScenari  # noqa: E402
from utils_io import (PERCORSO_AUDIT, PERCORSO_DASHBOARD, PERCORSO_DATASET,  # noqa: E402
                      PERCORSO_MODELLO, PERCORSO_PREDIZIONI, carica_csv, prepara_feature)

ORDINE_AREE = ["Nord", "Centro", "Sud", "Isole"]
MARGINE_DRIFT = 0.05
SCENARIO_OGNI = 5   # nel flusso simulato, una raccomandazione ogni 5 e' uno scenario di deterioramento (scenari.py)
AVVISO_LABEL = ("Nei dati storici di quest'area i guasti risultano sotto-registrati: "
                "il rischio reale potrebbe essere più alto di quanto stimato.")


def _pulisci(x):
    """Rende JSON-serializzabile qualunque struttura (numpy, enum, date, NaN)."""
    if isinstance(x, dict):
        return {str(k): _pulisci(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_pulisci(v) for v in x]
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if math.isnan(x) or math.isinf(x) else float(x)
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if x is None or isinstance(x, str):
        return x
    if hasattr(x, "a_dict"):
        return _pulisci(x.a_dict())
    return str(x)


def risposta(dati, status: int = 200) -> JSONResponse:
    return JSONResponse(_pulisci(dati), status_code=status)


# ---------------------------------------------------------------------------
# SISTEMA: stato condiviso tra le richieste
# ---------------------------------------------------------------------------
class Sistema:
    def __init__(self, percorso_audit=PERCORSO_AUDIT, usa_llm: bool = True, n_iniziali: int = 40,
                 seed: int = 7):
        mancanti = [p.name for p in (PERCORSO_DATASET, PERCORSO_MODELLO, PERCORSO_PREDIZIONI) if not p.exists()]
        if mancanti:
            raise FileNotFoundError(f"File mancanti: {', '.join(mancanti)}. "
                                    "Eseguire prima `python train_baseline.py` in dashboard/backend.")
        self.lock = threading.RLock()
        self.audit = AuditLogger(percorso_audit)
        self.dataset = carica_csv(PERCORSO_DATASET)
        calibra_etichette(self.dataset)
        self.modello = joblib.load(PERCORSO_MODELLO)
        self.colonne = list(self.modello.feature_names_in_)
        self.pred = carica_csv(PERCORSO_PREDIZIONI).reset_index(drop=True)
        self.X = prepara_feature(self.pred[list(self.dataset.columns)], self.colonne)
        self.aree = [a for a in ORDINE_AREE if a in set(self.dataset["area_geografica"])]
        self.tipi = sorted(self.dataset["tipo_asset"].unique())

        self.report = BiasDetector().report(self.pred, self.dataset)
        self.aree_sospette = list(self.report.get("label_bias", {}).get("aree_sospette", []))
        storico = self.dataset[~self.dataset["asset_id"].isin(set(self.pred["asset_id"]))]
        self.casi = CasiSimili(storico, aree_sospette=self.aree_sospette)
        self.scenari = GeneratoreScenari(self.modello, self.colonne, self.dataset, self.pred)

        self.config_llm = ConfigLLM() if usa_llm else ConfigLLM.nessuno()
        self.cache_llm: dict = {}
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.n_iniziali, self.seed = n_iniziali, seed
        self.sessione, self.versione = 0, 0
        self.audit.log("SISTEMA", "avvio_sistema", extra={
            "modello": type(self.modello).__name__, "n_test": len(self.pred),
            "motore_spiegazioni": self.config_llm.descrizione() if self.config_llm.attivo else "template",
            "aree_sospette": self.aree_sospette})
        self._nuova_sessione()

    # ------------------------------------------------------------------ sessione
    def _nuova_sessione(self, archivio: Optional[dict] = None):
        with self.lock:
            self.sessione += 1
            self.versione += 1
            self.om = OversightManager(self.audit, aree=self.aree, tipi=self.tipi, lock=self.lock)
            self.spiegatore = SpiegatoreLLM(self.config_llm, self.audit)
            self.spiegatore.cache = self.cache_llm
            self.dettagli: dict = {}
            ordine = self._ordine_flusso()
            self.flusso = list(ordine[self.n_iniziali:])
            self.ordine_scenari = self.scenari.ordine(self.seed)
            self.audit.log("SISTEMA", "avvio_sessione", extra={
                "sessione": self.sessione, "raccomandazioni_iniziali": self.n_iniziali,
                "soglie": self.om.soglie(), "archivio_precedente": archivio})
            self._aggiorna_promozioni()
            for i in ordine[:self.n_iniziali]:
                self._sottometti_riga(i)

    def _ordine_flusso(self) -> list[int]:
        """Ordine deterministico; nelle prime n almeno 3 linee AT del Sud (test T3) e 2 utenze critiche."""
        idx = list(range(len(self.pred)))
        random.Random(self.seed).shuffle(idx)
        p = self.pred
        prioritari = ([i for i in idx if p.at[i, "area_geografica"] == "Sud" and p.at[i, "tipo_asset"] == "linea_AT"][:3]
                      + [i for i in idx if p.at[i, "criticita_utenza"] == "critica"][:2])
        prioritari = list(dict.fromkeys(prioritari))
        resto = [i for i in idx if i not in set(prioritari)]
        testa = resto[:self.n_iniziali - len(prioritari)] + prioritari
        random.Random(self.seed + 1).shuffle(testa)
        return testa + resto[self.n_iniziali - len(prioritari):]

    def _nuovo_id(self) -> str:
        import uuid
        while True:
            did = uuid.uuid4().hex[:8]
            try:
                self.om.trova(did)
            except KeyError:
                return did

    @staticmethod
    def _confidenza(riga, prob: float) -> float:
        """P(previsione corretta) dal meta-modello, calcolata in addestramento (predizioni.csv); se manca, max(p, 1-p)."""
        c = riga.get("confidenza")
        return round(float(c), 3) if c is not None and c == c else calcola_confidenza(prob)

    def _sottometti_riga(self, i: int) -> Raccomandazione:
        return self._sottometti(self.pred.iloc[i], self.X.iloc[i])

    def _sottometti_scenario(self) -> Optional[Raccomandazione]:
        """Scenario simulato di deterioramento (scenari.py): asset reale non critico ancora nel flusso,
        letture peggiorate finche' il modello stima P(guasto) >= 0.90. Il livello lo decide il routing."""
        scelta = self.scenari.scegli(self.ordine_scenari, set(self.flusso), self.om.soglia_rischio)
        if scelta is None:
            return None
        i, opzione = scelta
        self.flusso.remove(i)
        riga, x, scenario = self.scenari.costruisci(self.pred.iloc[i], opzione)
        return self._sottometti(riga, x, scenario)

    def _sottometti(self, riga, x, scenario: Optional[dict] = None) -> Raccomandazione:
        prob = round(float(riga["proba"]), 3)
        carico = float(riga["carico_pct"])
        analisi = analizza_riga(self.modello, x.to_numpy(), self.colonne)
        r = Raccomandazione(str(riga["asset_id"]), str(riga["tipo_asset"]), str(riga["area_geografica"]),
                            str(riga["criticita_utenza"]), prob, self._confidenza(riga, prob),
                            azione_raccomandata(prob, carico),
                            [(f.nome, round(f.contributo, 4)) for f in analisi.fattori],
                            id=self._nuovo_id(), carico_pct=carico)
        if scenario:
            # dichiarato nel log prima del routing: chi ricostruisce la decisione sa che i dati sono simulati
            self.audit.log("SISTEMA", "scenario_simulato", r, extra=scenario)
        self.om.sottometti(r)
        avviso = None
        if r.area_geografica in self.aree_sospette:
            avviso = AVVISO_LABEL
            contrib = analisi.contributi_gruppi.get("area_geografica", 0.0)
            if contrib <= -0.02:
                avviso += (f" Il solo fatto di trovarsi in quest'area abbassa la stima di "
                           f"{round(abs(contrib) * 100)} punti percentuali.")
        self.dettagli[r.id] = {
            "fattori": analisi.fattori, "metodo": analisi.metodo, "avviso_area": avviso,
            "controfattuale": controfattuale(self.modello, x, analisi.fattori, r.azione_proposta, prob),
            "casi_simili": self.casi.trova(riga.to_dict()),
            "scenario": scenario,
            "gen": 0, "llm": None, "spiegazione": None,
        }
        self._genera_spiegazione(r)
        return r

    # ------------------------------------------------------------------ spiegazioni
    def _genera_spiegazione(self, r: Raccomandazione):
        d = self.dettagli[r.id]
        d["gen"] += 1
        rec = {**self.om.contesto_spiegazione(r), "avviso_area": d["avviso_area"]}
        # template subito: nessuna card resta senza spiegazione
        d["spiegazione"] = self.spiegatore.spiega_template(rec, d["fattori"])
        d["llm"] = None
        if self.spiegatore.configurato and r.stato in STATI_IN_CODA:
            d["llm"] = "in_corso"
            self.executor.submit(self._job_llm, self.sessione, self.spiegatore, r.id, d["gen"], rec, d["fattori"])

    def _job_llm(self, sessione, spiegatore, did, gen, rec, fattori):
        try:
            sp, esito = spiegatore.spiega(rec, fattori), "completata"
        except Exception:  # lo spiegatore gestisce gia' i fallback; qui solo difesa
            sp, esito = None, "errore"
        with self.lock:
            d = self.dettagli.get(did)
            if sessione != self.sessione or d is None or d["gen"] != gen:
                return
            if sp is not None:
                d["spiegazione"] = sp
            d["llm"] = esito
            self.versione += 1

    # ------------------------------------------------------------------ promozioni automatiche
    def _aggiorna_promozioni(self):
        """Il sistema si declassa da solo: calibrazione fuori soglia (C2), label bias e override (C3)
        -> area in HITL; drift (C4) -> soglia di confidenza piu' severa. Il gap di recall (C1) resta
        un'allerta visibile: promuovere ogni area con recall bassa eliminerebbe del tutto l'HOTL."""
        attese: dict = {}
        for a in self.report["dimensioni"]["area_geografica"]["allerte"]:
            if a["codice"] == "C2" and a.get("gruppo"):
                attese.setdefault(a["gruppo"], {})["C2"] = f"calibrazione fuori soglia {a['valore']:+.2f} (C2)"
        for area in self.aree_sospette:
            attese.setdefault(area, {})["label_bias"] = "sospetta sotto-registrazione storica dei guasti"
        for o in self.om.override_per_area():
            if o["giudizi_umani"] >= 5 and (o["tasso_disaccordo"] or 0) >= 0.40:
                attese.setdefault(o["area"], {})["C3"] = (
                    f"override umano {o['tasso_disaccordo']:.0%} su {o['giudizi_umani']} giudizi (C3)")
        self.om.aggiorna_promozioni_automatiche(attese)
        drift = self.report["drift"]
        self.om.imposta_margine_drift(MARGINE_DRIFT if drift["allerta"] else 0.0,
                                      drift["testo"] if drift["allerta"] else "drift rientrato")

    # ------------------------------------------------------------------ operazioni
    def _modifica(self):
        self.versione += 1

    def apri(self, did, operatore):
        with self.lock:
            r = self.om.apri(did, operatore)
            self._modifica()
            return r

    def revisiona(self, did, operatore, esito, motivazione, azione_modificata=None):
        with self.lock:
            try:
                r = self.om.revisiona(did, esito, operatore, motivazione, azione_modificata)
            except RubberStamping:
                self._modifica()  # il tentativo respinto cambia audit trail, KPI A4 e segnali per revisore
                raise
            self._aggiorna_promozioni()
            self._modifica()
            return r

    def contesta(self, did, operatore, motivazione):
        with self.lock:
            r = self.om.contesta(did, operatore, motivazione)
            self._aggiorna_promozioni()
            self._modifica()
            return r

    def anteprima_stop(self, ambito):
        with self.lock:
            amb = self.om.normalizza_ambito(ambito)
            dentro = self.om.in_ambito(amb)
            return {"ambito": amb, "gia_attivo": amb in self.om.stop_attivi, "in_coda": len(dentro),
                    "asset": [r.asset_id for r in dentro][:12],
                    "altre_in_coda": len(self.om.coda) - len(dentro)}

    def attiva_stop(self, ambito, operatore, motivazione):
        with self.lock:
            bloccate = self.om.attiva_stop(ambito, operatore, motivazione)
            self._modifica()
            return {"ambito": self.om.normalizza_ambito(ambito), "bloccate": len(bloccate)}

    def disattiva_stop(self, ambito, operatore, motivazione, conferma):
        with self.lock:
            sbloccate = self.om.disattiva_stop(ambito, operatore, motivazione, conferma)
            for r in sbloccate:
                self._genera_spiegazione(r)   # il livello puo' essere cambiato (R6)
            self._modifica()
            return {"ambito": self.om.normalizza_ambito(ambito), "sbloccate": len(sbloccate)}

    def imposta_soglie(self, operatore, motivazione, **soglie):
        with self.lock:
            out = self.om.imposta_soglie(operatore, motivazione, **soglie)
            self.om.controlla_sla()
            self._modifica()
            return out

    def promozione_manuale(self, area, attiva, operatore, motivazione):
        with self.lock:
            if attiva:
                self.om.promuovi_area(area, operatore, motivazione)
            else:
                self.om.revoca_promozione_manuale(area, operatore, motivazione)
            self._modifica()
            return {"area": area, "promossa": self.om.area_promossa(area),
                    "motivi": self.om.motivi_promozione(area)}

    def simula(self, n: int = 5):
        with self.lock:
            if not 1 <= n <= 20:
                raise ValueError("n deve essere tra 1 e 20")
            if not self.flusso:
                raise ValueError("Flusso esaurito: usare Reset demo")
            # prima lo scenario (il suo asset esce dal flusso), poi le altre dal flusso del test set;
            # a scenari esauriti la generazione resta di n raccomandazioni normali
            scenari = [r for r in (self._sottometti_scenario() for _ in range(n // SCENARIO_OGNI)) if r]
            nuove = scenari + [self._sottometti_riga(self.flusso.pop(0))
                               for _ in range(min(n - len(scenari), len(self.flusso)))]
            self._modifica()
            return {"generate": len(nuove), "scenari": len(scenari), "per_livello": {
                lv: sum(1 for r in nuove if r.livello and r.livello.value == lv) for lv in ("HIC", "HITL", "HOTL")},
                "bloccate": sum(1 for r in nuove if r.stato == StatoDecisione.BLOCCATA_STOP)}

    def guasto_llm(self, modalita, operatore):
        with self.lock:
            if not self.spiegatore.configurato:
                raise ValueError("LLM non configurato: le spiegazioni usano gia' il template locale")
            operatore = self.om._valida_operatore(operatore)
            self.spiegatore.imposta_guasto(modalita or None)
            self.audit.log(operatore, "simulazione_guasto_llm", extra={"modalita": modalita or "nessuno"})
            self._modifica()
            return self.spiegatore.statistiche()

    def rigenera(self, did):
        with self.lock:
            r = self.om.trova(did)
            self._genera_spiegazione(r)
            self._modifica()
            return {"id": did, "llm": self.dettagli[did]["llm"]}

    def reset(self, operatore, motivazione):
        with self.lock:
            operatore = self.om._valida_operatore(operatore)
            motivazione = self.om._valida_motivazione(motivazione)
            self.audit.log(operatore, "reset_demo", extra={"motivazione": motivazione})
            self.spiegatore.audit = None
            archivio = self.audit.archivia(operatore, motivazione)
            self._nuova_sessione(archivio)
            return {"sessione": self.sessione, "archivio": archivio}

    def chiudi(self):
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ letture
    def _decisione(self, r: Raccomandazione) -> dict:
        d = self.om.a_dict(r)
        d["valutazione_routing"] = self.om.valutazione_routing(r)
        d.pop("contesto_routing", None)
        det = self.dettagli.get(r.id, {})
        sp = det.get("spiegazione")
        d.update({
            "spiegazione_operativa": sp.a_dict() if sp else None,
            "fonte_spiegazione": sp.fonte if sp else None,
            "llm": det.get("llm"),
            "controfattuale": det.get("controfattuale"),
            "casi_simili": det.get("casi_simili"),
            "avviso_area": det.get("avviso_area"),
            "scenario": det.get("scenario"),
            "metodo_fattori": det.get("metodo"),
            "descrizione_confidenza": descrivi_confidenza(r.confidenza),
        })
        return d

    def stato(self, versione: Optional[int] = None) -> dict:
        with self.lock:
            if self.om.controlla_sla():
                self._modifica()
            base = {"versione": self.versione, "sessione": self.sessione,
                    "ora_server": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "integrita": self.audit.verifica()}
            if versione is not None and versione == self.versione:
                return {**base, "invariato": True}
            stop = [{"ambito": k, **v, "bloccate": sum(1 for r in self.om.decisioni
                                                      if r.stato == StatoDecisione.BLOCCATA_STOP and r.bloccata_da == k)}
                    for k, v in sorted(self.om.stop_attivi.items(), key=lambda kv: kv[1]["dal"])]
            return {**base,
                    "decisioni": [self._decisione(r) for r in self.om.decisioni],
                    "kpi": self.om.kpi(), "soglie": self.om.soglie(), "stop": stop,
                    "promozioni": {a: {"promossa": self.om.area_promossa(a), "motivi": self.om.motivi_promozione(a),
                                       "manuale": bool((self.om.promozioni.get(a) or {}).get("manuale"))}
                                   for a in self.aree},
                    "llm": {**self.spiegatore.statistiche(),
                            "in_corso": sum(1 for d in self.dettagli.values() if d["llm"] == "in_corso")},
                    "flusso": {"rimanenti": len(self.flusso), "totale": len(self.pred)},
                    "drift_allerta": bool(self.report["drift"]["allerta"])}

    def configurazione(self) -> dict:
        return {"aree": self.aree, "tipi": self.tipi, "azioni": AZIONI, "etichette_azioni": AZIONI_ETICHETTA,
                "limiti_soglie": OversightManager.LIMITI_SOGLIE,
                "min_motivazione": OversightManager.MIN_MOTIVAZIONE,
                "min_motivazione_revisione": OversightManager.MIN_MOTIVAZIONE_REVISIONE,
                "prefisso_supervisore": OversightManager.PREFISSO_SUPERVISORE,
                "guasti_llm": GUASTI_SIMULABILI, "llm": self.config_llm.dettagli(),
                "modello": {"tipo": type(self.modello).__name__, "n_test": self.report["n_test"],
                            "accuracy": self.report["accuracy_globale"], "recall": self.report["recall_globale"]}}

    def misura_mitigazione(self) -> dict:
        """Guasti (registrati) che finirebbero in HOTL senza e con le promozioni attuali, per area."""
        with self.lock:
            promozioni, margine = json.loads(json.dumps(self.om.promozioni)), self.om.margine_drift
            soglie = (self.om.soglia_conf, self.om.soglia_rischio)
        prima = OversightManager(None, soglie[0], soglie[1], aree=self.aree, tipi=self.tipi)
        dopo = OversightManager(None, soglie[0], soglie[1], aree=self.aree, tipi=self.tipi)
        dopo.promozioni, dopo.margine_drift = promozioni, margine
        per_area = {a: {"area": a, "n": 0, "guasti": 0, "hotl_prima": 0, "hotl_dopo": 0,
                        "guasti_hotl_prima": 0, "guasti_hotl_dopo": 0} for a in self.aree}
        for _, riga in self.pred.iterrows():
            p = round(float(riga["proba"]), 3)
            args = (riga["asset_id"], riga["tipo_asset"], riga["area_geografica"], riga["criticita_utenza"],
                    p, self._confidenza(riga, p), azione_raccomandata(p, float(riga["carico_pct"])), [])
            v = per_area[riga["area_geografica"]]
            guasto = int(riga["y_true"]) == 1
            v["n"] += 1
            v["guasti"] += guasto
            for nome, om in (("prima", prima), ("dopo", dopo)):
                if om.route(Raccomandazione(*args)) == LivelloSupervisione.HOTL:
                    v[f"hotl_{nome}"] += 1
                    v[f"guasti_hotl_{nome}"] += guasto
        righe = list(per_area.values())
        return {"per_area": righe,
                "nota": "Guasti registrati nel test set: al Sud quelli reali sono probabilmente di piu'.",
                "guasti_hotl_prima": sum(r["guasti_hotl_prima"] for r in righe),
                "guasti_hotl_dopo": sum(r["guasti_hotl_dopo"] for r in righe)}

    def bias(self) -> dict:
        with self.lock:
            override = self.om.override_per_area()
        return {"report": self.report, "override_area": override, "mitigazione": self.misura_mitigazione(),
                "aree_sospette": self.aree_sospette}

    def monitoraggio(self) -> dict:
        """KPI A1-A6 (oversight_manager), B1-B4 (spiegazioni), C1-C4 (fairness), D1-D3 (audit)."""
        with self.lock:
            k = self.om.kpi()
            coda = self.om.coda
            sp = [self.dettagli[r.id]["spiegazione"] for r in coda if r.id in self.dettagli]
            b1 = sum(1 for s in sp if s and s.testo and len(s.fattori) >= 3)
            b2 = sum(1 for s in sp if s and s.incertezza)
            stats = self.spiegatore.statistiche()
            override = self.om.override_per_area()
            esecuzioni = list(reversed(self.om.esecuzioni[-50:]))
            record = self.audit.record()
            n_esec_log = sum(1 for x in record if x["evento"] == "esecuzione_azione")
            n_esec = len(self.om.esecuzioni)
            revisori, carico = self.om.statistiche_revisori(), self.om.carico_lavoro()
            ids_spiegati = {(x.get("extra") or {}).get("decisione_id") for x in record
                            if x["evento"].startswith("spiegazione_")}
            tracciate = sum(1 for r in self.om.decisioni if r.id in ids_spiegati)
        integ = self.audit.verifica()
        t0 = time.perf_counter()
        asset = esecuzioni[0]["asset_id"] if esecuzioni else (self.om.decisioni[0].asset_id
                                                               if self.om.decisioni else None)
        if asset:
            self.audit.ricostruisci(asset)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        rep = self.report
        dim = rep["dimensioni"]["area_geografica"]
        cal = [m.get("gap_calibrazione") for m in dim["metriche"] if m.get("gap_calibrazione") is not None]
        tassi = [o["tasso_override"] for o in override if o["tasso_override"] is not None]
        n_coda, tf = len(sp), stats["tasso_fallback"]

        def ind(codice, nome, valore, unita, target, stato, dettaglio):
            return {"codice": codice, "nome": nome, "valore": valore, "unita": unita,
                    "target": target, "stato": stato, "dettaglio": dettaglio}

        b = [
            ind("B1", "Copertura delle spiegazioni", round(b1 / n_coda, 3) if n_coda else None, "pct", "100%",
                "ok" if b1 == n_coda else "critico", f"{b1} card in coda su {n_coda} con testo e 3 fattori"),
            ind("B2", "Esplicitazione dell'incertezza", round(b2 / n_coda, 3) if n_coda else None, "pct", "100%",
                "ok" if b2 == n_coda else "critico", f"{b2} card su {n_coda} con frase sull'incertezza"),
            ind("B3", "Tracciabilita' della fonte", round(tracciate / len(self.om.decisioni), 3)
                if self.om.decisioni else None, "pct", "100%",
                "ok" if tracciate == len(self.om.decisioni) else "attenzione",
                f"{tracciate} decisioni con fonte della spiegazione registrata nel log"),
            ind("B3c", "Tasso di fallback LLM", tf, "pct", "< 5%",
                "n/d" if tf is None else ("ok" if tf < 0.05 else "attenzione"),
                f"motore {stats['descrizione'] if stats['configurato'] else 'template locale'}; "
                f"{stats['ok']} ok, {stats['fallback']} fallback ({stats['fallback_simulati']} simulati)"),
            ind("B4", "Click per le azioni chiave", None, "info", "<= 2",
                "ok", "Spiegazione completa 1 click; override 2 (motivazione + pulsante); stop 1 + conferma"),
        ]
        c = [
            ind("C1", "Gap di recall tra aree", dim["gap_recall"], "num", f"<= {rep['soglie']['gap_recall']}",
                "attenzione" if any(a["codice"] == "C1" for a in dim["allerte"]) else "ok",
                "; ".join(a["testo"] for a in dim["allerte"] if a["codice"] == "C1") or "nessuna allerta"),
            ind("C2", "Gap di calibrazione massimo", dim["gap_calibrazione_max"], "num",
                f"<= {rep['soglie']['gap_calibrazione']}",
                "attenzione" if any(a["codice"] == "C2" for a in dim["allerte"]) else "ok",
                f"scarti per area: {', '.join(f'{v:+.2f}' for v in cal)}"),
            ind("C3", "Override per area (live)", round(max(tassi) - min(tassi), 3) if len(tassi) > 1 else None,
                "num", "differenza < 0.20", "n/d" if len(tassi) < 2 else
                ("ok" if max(tassi) - min(tassi) < 0.20 else "attenzione"),
                ", ".join(f"{o['area']} {o['tasso_override']:.0%}" for o in override if o["tasso_override"] is not None)
                or "nessuna revisione ancora"),
            ind("C4", "Drift di performance", rep["drift"]["media_recente"], "num",
                f">= {rep['drift']['soglia_accuracy']}", "attenzione" if rep["drift"]["allerta"] else "ok",
                rep["drift"]["testo"] + (f" Mitigazione: soglia di confidenza +{MARGINE_DRIFT}."
                                          if rep["drift"]["allerta"] else "")),
        ]
        d = [
            ind("D1", "Completezza del log", 1.0 if n_esec_log >= n_esec else round(n_esec_log / n_esec, 3), "pct",
                "100%", "ok" if n_esec_log >= n_esec else "critico",
                f"{n_esec} esecuzioni, {n_esec_log} record di esecuzione nel log"),
            ind("D2", "Integrita' della catena", 1.0 if integ["integra"] else 0.0, "pct", "100%",
                "ok" if integ["integra"] else "critico",
                f"{integ['n_record']} record verificati" + ("" if integ["integra"] else f": {integ['motivo']}")),
            ind("D3", "Tempo di ricostruzione", ms, "ms", "< 60 s", "ok",
                f"ricostruzione completa dell'asset {asset}" if asset else "nessuna decisione"),
        ]
        return {"indicatori": k["indicatori"] + b + c + d, "revisori": revisori, "carico": carico,
                "esecuzioni": esecuzioni, "override_area": override, "llm": stats,
                "punto_unico": "oversight_manager.OversightManager._esegui (chiamato solo da sottometti per "
                               "HOTL e da revisiona per APPROVATA/MODIFICATA)"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class ConOperatore(BaseModel):
    operatore: str = ""
    motivazione: str = ""


class Revisione(ConOperatore):
    esito: str
    azione_modificata: Optional[str] = None


class Stop(ConOperatore):
    ambito: str
    conferma: bool = False


class Soglie(ConOperatore):
    soglia_confidenza: Optional[float] = None
    soglia_rischio: Optional[float] = None
    sla_minuti: Optional[int] = None


class Promozione(ConOperatore):
    attiva: bool = True


class Simulazione(BaseModel):
    n: int = 5


class GuastoLLM(ConOperatore):
    modalita: Optional[str] = None


def crea_app(sistema: Optional[Sistema] = None, usa_llm: bool = True) -> FastAPI:
    app = FastAPI(title="EnerGuard API", docs_url="/api/docs", openapi_url="/api/openapi.json")
    stato_app = {"s": sistema, "lock": threading.Lock()}

    def S() -> Sistema:
        with stato_app["lock"]:
            if stato_app["s"] is None:
                stato_app["s"] = Sistema(usa_llm=usa_llm)
            return stato_app["s"]

    app.state.sistema = S

    @app.exception_handler(ValueError)
    def _e400(_req, exc):
        return risposta({"errore": str(exc)}, 400)

    @app.exception_handler(PermissionError)
    def _e403(_req, exc):
        return risposta({"errore": str(exc)}, 403)

    @app.exception_handler(KeyError)
    def _e404(_req, exc):
        return risposta({"errore": str(exc.args[0]) if exc.args else "non trovato"}, 404)

    @app.exception_handler(EsecuzioneNonAutorizzata)
    def _e409(_req, exc):
        return risposta({"errore": str(exc)}, 409)

    @app.exception_handler(RequestValidationError)
    def _e422(_req, exc):
        msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return risposta({"errore": f"Richiesta non valida: {msg}"}, 400)

    @app.middleware("http")
    async def _origine(request: Request, call_next):
        # protezione CSRF minimale: le modifiche arrivano solo dalla pagina servita da questo server
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and origin.split("://", 1)[-1] != host:
                return risposta({"errore": "Origine non consentita"}, 403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(PERCORSO_DASHBOARD, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/config")
    def config():
        return risposta(S().configurazione())

    @app.get("/api/stato")
    def stato(versione: Optional[int] = None):
        return risposta(S().stato(versione))

    @app.get("/api/decisioni/{did}")
    def decisione(did: str):
        s = S()
        with s.lock:
            return risposta(s._decisione(s.om.trova(did)))

    @app.post("/api/decisioni/{did}/apertura")
    def apertura(did: str, b: ConOperatore):
        return risposta(S()._decisione(S().apri(did, b.operatore)))

    @app.post("/api/decisioni/{did}/revisione")
    def revisione(did: str, b: Revisione):
        s = S()
        return risposta(s._decisione(s.revisiona(did, b.operatore, b.esito, b.motivazione, b.azione_modificata)))

    @app.post("/api/decisioni/{did}/contestazione")
    def contestazione(did: str, b: ConOperatore):
        s = S()
        return risposta(s._decisione(s.contesta(did, b.operatore, b.motivazione)))

    @app.post("/api/decisioni/{did}/spiegazione")
    def rigenera(did: str):
        return risposta(S().rigenera(did))

    @app.get("/api/stop/anteprima")
    def anteprima(ambito: str):
        return risposta(S().anteprima_stop(ambito))

    @app.post("/api/stop")
    def stop_on(b: Stop):
        return risposta(S().attiva_stop(b.ambito, b.operatore, b.motivazione))

    @app.post("/api/stop/disattiva")
    def stop_off(b: Stop):
        return risposta(S().disattiva_stop(b.ambito, b.operatore, b.motivazione, b.conferma))

    @app.post("/api/soglie")
    def soglie(b: Soglie):
        return risposta(S().imposta_soglie(b.operatore, b.motivazione, soglia_confidenza=b.soglia_confidenza,
                                           soglia_rischio=b.soglia_rischio, sla_minuti=b.sla_minuti))

    @app.post("/api/aree/{area}/promozione")
    def promozione(area: str, b: Promozione):
        return risposta(S().promozione_manuale(area, b.attiva, b.operatore, b.motivazione))

    @app.post("/api/raccomandazioni")
    def simula(b: Simulazione):
        return risposta(S().simula(b.n))

    @app.post("/api/llm/guasto")
    def guasto(b: GuastoLLM):
        return risposta(S().guasto_llm(b.modalita, b.operatore))

    @app.post("/api/reset")
    def reset(b: ConOperatore):
        return risposta(S().reset(b.operatore, b.motivazione))

    @app.get("/api/bias")
    def bias():
        return risposta(S().bias())

    @app.get("/api/monitoraggio")
    def monitoraggio():
        return risposta(S().monitoraggio())

    @app.get("/api/audit")
    def audit(testo: Optional[str] = None, asset: Optional[str] = None, attore: Optional[str] = None,
              evento: Optional[str] = None, decisione: Optional[str] = None, da: Optional[str] = None,
              a: Optional[str] = None, limite: int = 300):
        return risposta(S().audit.cerca(testo=testo, asset_id=asset, attore=attore, evento=evento,
                                        decisione_id=decisione, da=da, a=a, limite=max(1, min(limite, 2000))))

    @app.get("/api/audit/verifica")
    def verifica():
        return risposta(S().audit.verifica())

    @app.post("/api/audit/simula-manomissione")
    def manomissione():
        return risposta(S().audit.simula_manomissione())

    @app.get("/api/audit/export")
    def export():
        nome = f"audit_trail_export_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        return Response(S().audit.esporta(), media_type="application/x-ndjson",
                        headers={"Content-Disposition": f'attachment; filename="{nome}"'})

    @app.get("/api/audit/ricostruisci/{asset_id}")
    def ricostruisci(asset_id: str):
        t0 = time.perf_counter()
        out = S().audit.ricostruisci(asset_id)
        return risposta({**out, "ms": round((time.perf_counter() - t0) * 1000, 1)})

    return app


def main(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description="Server della dashboard EnerGuard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--senza-llm", action="store_true", help="usa solo le spiegazioni a template")
    args = parser.parse_args(argv)
    try:
        s = Sistema(usa_llm=not args.senza_llm)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"EnerGuard pronto: http://{args.host}:{args.port}  (spiegazioni: "
          f"{s.config_llm.descrizione() if s.config_llm.attivo else 'template locale'})")
    try:
        uvicorn.run(crea_app(s), host=args.host, port=args.port, log_level="warning")
    finally:
        s.chiudi()
    return 0


def _avvia_streamlit():
    import socket

    import streamlit as st
    import uvicorn

    st.set_page_config(page_title="EnerGuard", layout="wide")

    @st.cache_resource
    def server():
        with socket.socket() as sock:
            porta = 8000 if sock.connect_ex(("127.0.0.1", 8000)) else 0
        if porta == 0:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                porta = sock.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(crea_app(), host="127.0.0.1", port=porta, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(600):
            if srv.started:
                break
            time.sleep(0.1)
        return f"http://127.0.0.1:{porta}"

    url = server()
    st.link_button("Apri la dashboard in una nuova scheda", url)
    st.components.v1.iframe(url, height=1400, scrolling=True)


def _in_streamlit() -> bool:
    if "streamlit" not in sys.modules:
        return False
    try:
        from streamlit import runtime
        return runtime.exists()
    except Exception:
        return False


if _in_streamlit():
    _avvia_streamlit()
elif __name__ == "__main__":
    sys.exit(main())
