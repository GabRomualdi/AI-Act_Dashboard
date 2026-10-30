"""
EnerGuard - scenari simulati di deterioramento
==============================================
Nel test set nessun asset con utenza non critica ha P(guasto) >= 0.90 (il massimo e' 0.886): la demo
non mostrerebbe mai una HIC decisa dal solo rischio. Per questo una raccomandazione su cinque del
pulsante "+5" e' uno SCENARIO SIMULATO, costruito cosi':
- si parte da un asset reale del test set con utenza `standard` o `alta`, non ancora mostrato;
- si peggiorano solo vibrazione, temperatura e giorni dall'ultima manutenzione, mai migliorandoli e
  sempre con valori realmente osservati nella flotta (quantili del dataset, al massimo il 98esimo
  percentile: agli estremi il modello ha pochi esempi);
- tra le combinazioni che portano il modello VERO a stimare P(guasto) >= 0.90 si sceglie quella con il
  peggioramento minimo (somma degli aumenti in deviazioni standard);
- confidenza (meta-modello), fattori SHAP, controfattuale e casi simili sono poi calcolati sulle
  letture simulate, come per ogni altra raccomandazione.
Il livello NON e' forzato: lo decide la matrice di routing (R3, oppure R2 per la riduzione del carico
su utenza alta). Lo scenario e' dichiarato sulla card e nell'audit trail (evento `scenario_simulato`).
Gli scenari non entrano nelle metriche calcolate sul test set (fairness, drift, mitigazione).
"""
import itertools
import random
from typing import Optional

import numpy as np
import pandas as pd

from explainer import ETICHETTE
from utils_io import prepara_feature

FEATURE_DETERIORAMENTO = ("vibrazione_indice", "temperatura_media", "giorni_da_ultima_manutenzione")
QUANTILI_DETERIORAMENTO = (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98)
SOGLIA_SCENARIO = 0.90


class GeneratoreScenari:
    def __init__(self, modello, colonne: list, dataset: pd.DataFrame, predizioni: pd.DataFrame,
                 soglia: float = SOGLIA_SCENARIO):
        self.modello, self.colonne, self.soglia = modello, colonne, soglia
        self.colonne_dataset = list(dataset.columns)
        F = list(FEATURE_DETERIORAMENTO)
        cand = predizioni[predizioni["criticita_utenza"] != "critica"]
        # valori realmente osservati (interpolation="higher"): nessuna lettura inventata
        livelli = np.column_stack([dataset[f].quantile(list(QUANTILI_DETERIORAMENTO), interpolation="higher")
                                   .to_numpy() for f in F])
        combo = np.array(list(itertools.product(range(len(QUANTILI_DETERIORAMENTO)), repeat=len(F))))
        valori = livelli[combo, np.arange(len(F))]                                  # (combinazioni, F)
        base = cand[F].to_numpy(dtype=float)                                         # (asset, F)
        nuovi = np.maximum(base[:, None, :], valori[None, :, :])                     # (asset, combinazioni, F)
        sforzo = ((nuovi - base[:, None, :]) / dataset[F].std().to_numpy()).sum(axis=2)

        righe = cand.loc[cand.index.repeat(len(combo)), self.colonne_dataset].reset_index(drop=True)
        righe[F] = nuovi.reshape(-1, len(F))
        p = self.modello.predict_proba(prepara_feature(righe, colonne))[:, 1].reshape(len(cand), len(combo))

        self.opzioni: dict[int, list[dict]] = {}     # indice riga del test set -> scenari possibili
        for k, i in enumerate(cand.index):
            ok = np.flatnonzero(p[k] >= soglia)
            if ok.size:
                self.opzioni[int(i)] = sorted(
                    ({"proba": float(p[k, j]), "sforzo": float(sforzo[k, j]),
                      "letture": dict(zip(F, nuovi[k, j].tolist()))} for j in ok),
                    key=lambda o: (o["sforzo"], -o["proba"]))
        self.aree = {i: str(predizioni.at[i, "area_geografica"]) for i in self.opzioni}

    def __len__(self) -> int:
        return len(self.opzioni)

    def ordine(self, seed: int) -> list[int]:
        """Ordine deterministico che alterna le aree, cosi' gli scenari non cadono tutti nella stessa."""
        rng = random.Random(seed)
        per_area: dict[str, list[int]] = {}
        for i in sorted(self.opzioni):
            per_area.setdefault(self.aree[i], []).append(i)
        gruppi = [rng.sample(v, len(v)) for _, v in sorted(per_area.items())]
        rng.shuffle(gruppi)
        return [i for giro in itertools.zip_longest(*gruppi) for i in giro if i is not None]

    def scegli(self, ordine: list[int], disponibili: set, soglia_rischio: float) -> Optional[tuple[int, dict]]:
        """Primo asset disponibile nell'ordine, con il peggioramento minimo che porta P(guasto) almeno a
        max(0.90, soglia di rischio); se la soglia di rischio e' piu' alta di ogni scenario, il piu' rischioso."""
        obiettivo = max(self.soglia, soglia_rischio)
        candidati = [i for i in ordine if i in disponibili]
        for i in candidati:
            adatte = [o for o in self.opzioni[i] if o["proba"] >= obiettivo]
            if adatte:
                return i, adatte[0]
        if candidati:
            i = max(candidati, key=lambda c: max(o["proba"] for o in self.opzioni[c]))
            return i, max(self.opzioni[i], key=lambda o: o["proba"])
        return None

    def costruisci(self, riga: pd.Series, opzione: dict) -> tuple[pd.Series, pd.Series, dict]:
        """Riga con le letture simulate, vettore delle feature e descrizione dello scenario per card e audit."""
        nuova = riga.copy()
        for f, v in opzione["letture"].items():
            nuova[f] = v
        x = prepara_feature(pd.DataFrame([nuova[self.colonne_dataset]]), self.colonne).iloc[0]
        proba = float(self.modello.predict_proba(x.to_frame().T)[0, 1])
        nuova["proba"] = proba
        # senza meta-modello la confidenza resta vuota e il chiamante ripiega su max(p, 1-p)
        nuova["confidenza"] = (float(self.modello.confidenza(x.to_frame().T)[0])
                               if hasattr(self.modello, "confidenza") else np.nan)
        descr = {
            "tipo": "deterioramento",
            "testo": "Scenario simulato: letture peggiorate rispetto al dato reale dell'asset",
            "letture": [{"feature": f, "etichetta": ETICHETTE.get(f, {}).get("etichetta", f),
                         "unita": ETICHETTE.get(f, {}).get("unita", ""), "prima": _num(riga[f]), "dopo": _num(v)}
                        for f, v in opzione["letture"].items() if v != riga[f]],
            "proba_reale": round(float(riga["proba"]), 3),
            "proba_scenario": round(proba, 3),
            "confidenza_reale": None if pd.isna(riga.get("confidenza")) else round(float(riga["confidenza"]), 3),
        }
        return nuova, x, descr


def _num(v):
    v = float(v)
    return int(v) if v.is_integer() else round(v, 2)
