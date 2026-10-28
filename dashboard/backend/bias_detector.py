"""
EnerGuard Starter Kit - BiasDetector
=====================================
Metriche di performance e fairness disaggregate per sottogruppo.
Un modello "accurato in media" puo' nascondere disparita' gravi:
questo modulo serve a renderle visibili nella dashboard.

Scelte del team:
  1. Metrica principale: gap di recall / FNR tra sottogruppi (KPI C1). Qui il
     danno peggiore e' il falso negativo (guasto non previsto su un asset che
     serve un ospedale), quindi il gap di recall pesa piu' della demographic
     parity, che resta come segnale secondario.
  2. Soglie di allerta: gap recall > 0.15 (C1), |gap di calibrazione| > 0.10 in
     un gruppo (C2), media mobile dell'accuratezza su 2 "settimane" < 0.80 (C4).
     Le allerte sono collegate al routing dall'app (promozione a HITL).
  3. Indagine sulle label: un modello che vede solo il profilo tecnico degli
     asset (eta', manutenzioni, sensori, tipo) viene addestrato su ogni area e
     applicato alle altre. Se il tasso registrato di un'area e' molto sotto
     quello atteso dagli altri territori, l'ipotesi e' la sotto-segnalazione
     dei guasti nei dati storici (Art. 10, data governance).
"""

import json

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, precision_score,
                             recall_score)
from sklearn.neighbors import NearestNeighbors
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from utils_io import FEATURE_NUMERICHE


def _r(x, n: int = 3):
    """Arrotonda e converte in float nativo (None se mancante): pronto per JSON."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return round(float(x), n)


def a_record(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> lista di dict con tipi nativi (NaN -> None)."""
    return json.loads(df.to_json(orient="records"))


class BiasDetector:
    def __init__(self, soglia_gap_recall: float = 0.15,
                 soglia_gap_selezione: float = 0.20,
                 soglia_gap_calibrazione: float = 0.10,
                 soglia_accuracy_drift: float = 0.80,
                 finestra_drift: int = 2,
                 soglia_label_bias: float = 0.08):
        self.soglia_gap_recall = soglia_gap_recall
        self.soglia_gap_selezione = soglia_gap_selezione
        self.soglia_gap_calibrazione = soglia_gap_calibrazione
        self.soglia_accuracy_drift = soglia_accuracy_drift
        self.finestra_drift = finestra_drift
        self.soglia_label_bias = soglia_label_bias

    # ------------------------------------------------------------------ metriche
    def metriche_per_gruppo(self, df: pd.DataFrame, col_gruppo: str,
                            col_y: str = "y_true",
                            col_pred: str = "y_pred",
                            col_proba: str = "proba") -> pd.DataFrame:
        righe = []
        for gruppo, sub in df.groupby(col_gruppo):
            tn, fp, fn, tp = confusion_matrix(
                sub[col_y], sub[col_pred], labels=[0, 1]).ravel()
            riga = {
                col_gruppo: gruppo,
                "n": int(len(sub)),
                "positivi_reali": int(tp + fn),
                "tasso_positivi_reali": _r(sub[col_y].mean()),
                "tasso_selezione": _r(sub[col_pred].mean()),  # per demographic parity
                "accuracy": _r(accuracy_score(sub[col_y], sub[col_pred])),
                "precision": _r(precision_score(sub[col_y], sub[col_pred], zero_division=0)),
                "recall": _r(recall_score(sub[col_y], sub[col_pred], zero_division=0)),
                "fnr": _r(fn / (fn + tp)) if (fn + tp) else None,
                "fpr": _r(fp / (fp + tn)) if (fp + tn) else None,
            }
            if col_proba and col_proba in sub:
                # C2: probabilita' media predetta vs tasso osservato
                riga["proba_media"] = _r(sub[col_proba].mean())
                riga["gap_calibrazione"] = _r(sub[col_proba].mean() - sub[col_y].mean())
            if "confidenza" in sub:
                riga["confidenza_media"] = _r(sub["confidenza"].mean())
            righe.append(riga)
        return pd.DataFrame(righe).sort_values("recall")

    def calibrazione_per_gruppo(self, df: pd.DataFrame, col_gruppo: str,
                                col_y: str = "y_true", col_proba: str = "proba") -> pd.DataFrame:
        g = df.groupby(col_gruppo).agg(n=(col_y, "size"), proba_media=(col_proba, "mean"),
                                       tasso_osservato=(col_y, "mean")).reset_index()
        g["gap_calibrazione"] = g["proba_media"] - g["tasso_osservato"]
        g["allerta"] = g["gap_calibrazione"].abs() > self.soglia_gap_calibrazione
        return g.round(3)

    @staticmethod
    def equalized_odds(metriche: pd.DataFrame) -> dict:
        """Differenza massima di TPR (recall) e FPR tra gruppi: 0 = equalized odds perfetti."""
        tpr = metriche["recall"].dropna()
        fpr = metriche["fpr"].dropna()
        gap_tpr = _r(tpr.max() - tpr.min()) if len(tpr) else None
        gap_fpr = _r(fpr.max() - fpr.min()) if len(fpr) else None
        return {"gap_tpr": gap_tpr, "gap_fpr": gap_fpr,
                "differenza_equalized_odds": max(v for v in (gap_tpr, gap_fpr, 0.0) if v is not None)}

    # ------------------------------------------------------------------ allerte
    def allerte_dettagliate(self, metriche: pd.DataFrame, col_gruppo: str) -> list[dict]:
        """Allerte strutturate: codice KPI, gruppo, valore, soglia e testo per l'operatore."""
        out = []
        m = metriche.dropna(subset=["recall"])
        if m.empty:
            return out
        best_recall = m["recall"].max()
        migliore = m.loc[m["recall"].idxmax(), col_gruppo]
        for _, r in m.iterrows():
            gap = round(best_recall - r["recall"], 3)
            if gap > self.soglia_gap_recall:
                out.append({
                    "codice": "C1", "tipo": "gap_recall", "dimensione": col_gruppo,
                    "gruppo": r[col_gruppo], "valore": gap, "soglia": self.soglia_gap_recall,
                    "testo": (f"GAP RECALL: {col_gruppo}='{r[col_gruppo]}' ha recall "
                              f"{r['recall']} contro un massimo di {best_recall} ({migliore}) "
                              f"(gap {gap} > soglia {self.soglia_gap_recall}). Rischio: guasti non "
                              f"previsti concentrati in questo sottogruppo "
                              f"(positivi nel test: {int(r['positivi_reali'])}).")})
        gap_sel = round(m["tasso_selezione"].max() - m["tasso_selezione"].min(), 3)
        if gap_sel > self.soglia_gap_selezione:
            out.append({
                "codice": "C1b", "tipo": "gap_selezione", "dimensione": col_gruppo,
                "gruppo": None, "valore": gap_sel, "soglia": self.soglia_gap_selezione,
                "testo": (f"GAP SELEZIONE (demographic parity): differenza di {gap_sel} nel "
                          f"tasso di interventi raccomandati tra sottogruppi di {col_gruppo}. "
                          f"Verificare se riflette rischio reale o bias storico nei dati "
                          f"(variabile proxy).")})
        if "gap_calibrazione" in m:
            fuori = m[m["gap_calibrazione"].abs() > self.soglia_gap_calibrazione]
            for _, r in fuori.iterrows():
                solo = len(fuori) == 1
                out.append({
                    "codice": "C2", "tipo": "gap_calibrazione", "dimensione": col_gruppo,
                    "gruppo": r[col_gruppo], "valore": r["gap_calibrazione"],
                    "soglia": self.soglia_gap_calibrazione,
                    "testo": (f"GAP CALIBRAZIONE: {col_gruppo}='{r[col_gruppo]}' probabilita' media "
                              f"predetta {r['proba_media']} contro un tasso di guasti registrato di "
                              f"{r['tasso_positivi_reali']} (gap {r['gap_calibrazione']:+.3f}, soglia "
                              f"{self.soglia_gap_calibrazione})."
                              + (" Diverge solo in questo gruppo: indizio forte di label bias "
                                 "storico (guasti sotto-registrati)." if solo else ""))})
        return out

    def allerte(self, metriche: pd.DataFrame, col_gruppo: str) -> list[str]:
        """Confronta ogni gruppo con il migliore e genera allerte testuali."""
        return [a["testo"] for a in self.allerte_dettagliate(metriche, col_gruppo)]

    # ------------------------------------------------------------------ drift (C4)
    def drift_settimanale(self, df: pd.DataFrame, n_settimane: int = 12,
                          col_y: str = "y_true", col_pred: str = "y_pred",
                          col_conf: str = "confidenza") -> dict:
        """Drift simulato: il test set, nell'ordine del file, e' diviso in "settimane".
        Allerta se la media mobile dell'accuratezza sulle ultime `finestra_drift`
        settimane scende sotto la soglia."""
        settimane = []
        for i, idx in enumerate(np.array_split(np.arange(len(df)), n_settimane), start=1):
            sub = df.iloc[idx]
            if sub.empty:
                continue
            pos = sub[sub[col_y] == 1]
            settimane.append({
                "settimana": i, "n": int(len(sub)),
                "accuracy": _r(accuracy_score(sub[col_y], sub[col_pred])),
                "confidenza_media": _r(sub[col_conf].mean()) if col_conf in sub else None,
                "recall": _r((pos[col_pred] == 1).mean()) if len(pos) else None,
            })
        acc = [s["accuracy"] for s in settimane]
        k = max(1, min(self.finestra_drift, len(acc)))
        media_recente = _r(np.mean(acc[-k:])) if acc else None
        allerta = media_recente is not None and media_recente < self.soglia_accuracy_drift
        sotto = [s["settimana"] for s in settimane if s["accuracy"] < self.soglia_accuracy_drift]
        return {
            "settimane": settimane, "soglia_accuracy": self.soglia_accuracy_drift,
            "finestra": k, "media_recente": media_recente,
            "media_iniziale": _r(np.mean(acc[:-k])) if len(acc) > k else None,
            "settimane_sotto_soglia": sotto, "allerta": bool(allerta),
            "testo": (f"DRIFT: accuratezza media delle ultime {k} settimane {media_recente} sotto "
                      f"la soglia {self.soglia_accuracy_drift}." if allerta else
                      f"Accuratezza recente {media_recente}: nessun drift oltre soglia."),
            "nota": "Il dataset non ha date: le settimane sono blocchi consecutivi del test set (simulazione).",
        }

    # ------------------------------------------------------------------ label bias (Art. 10)
    def indagine_label_bias(self, df: pd.DataFrame, col_gruppo: str = "area_geografica",
                            col_y: str = "guasto_entro_30gg") -> dict:
        """Tasso di guasto atteso in base al solo profilo tecnico vs tasso registrato.

        Per ogni area si addestra un modello che NON vede l'area (solo eta',
        manutenzioni, sensori, tipo e criticita'); il tasso atteso di un'area e'
        la mediana delle stime prodotte dai modelli addestrati sulle ALTRE aree.
        Delta molto negativo = l'area registra meno guasti di quanti il suo
        profilo farebbe attendere: possibile sotto-segnalazione.
        """
        gruppi = sorted(df[col_gruppo].unique())
        X = pd.get_dummies(df.drop(columns=[c for c in ("asset_id", col_y, col_gruppo) if c in df.columns]))
        X = X.astype(float)
        y = df[col_y]
        incrociata = {}
        for g_ref in gruppi:
            m = df[col_gruppo] == g_ref
            mod = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
            mod.fit(X[m], y[m])
            incrociata[g_ref] = {g: _r(mod.predict_proba(X[df[col_gruppo] == g])[:, 1].mean())
                                 for g in gruppi}
        righe = []
        for g in gruppi:
            altri = [incrociata[ref][g] for ref in gruppi if ref != g]
            atteso = _r(np.median(altri))
            registrato = _r(y[df[col_gruppo] == g].mean())
            delta = _r(registrato - atteso)
            if delta < -self.soglia_label_bias:
                esito = "possibile sotto-segnalazione"
            elif delta > self.soglia_label_bias:
                esito = "piu' guasti del previsto (fattori non osservati?)"
            else:
                esito = "coerente con il profilo"
            righe.append({col_gruppo: g, "n": int((df[col_gruppo] == g).sum()),
                          "tasso_registrato": registrato, "tasso_atteso_da_profilo": atteso,
                          "delta": delta, "sospetto": bool(delta < -self.soglia_label_bias),
                          "esito": esito})
        profili = (df.groupby(col_gruppo)
                   .agg(n=(col_y, "size"), eta_media=("eta_anni", "mean"),
                        manutenzioni_media=("manutenzioni_ultimi_5anni", "mean"),
                        giorni_da_manutenzione_media=("giorni_da_ultima_manutenzione", "mean"),
                        vibrazione_media=("vibrazione_indice", "mean"),
                        temperatura_media=("temperatura_media", "mean"),
                        tasso_guasti_registrato=(col_y, "mean"))
                   .reset_index().round(3))
        sospette = [r[col_gruppo] for r in righe if r["sospetto"]]
        return {
            "metodo": ("Modello logistico sul solo profilo tecnico (area esclusa), addestrato su "
                       "ciascuna area e applicato alle altre; atteso = mediana delle stime degli "
                       "altri territori."),
            "soglia_delta": self.soglia_label_bias,
            "per_gruppo": righe,
            "matrice_incrociata": [{"addestrato_su": ref, **{g: incrociata[ref][g] for g in gruppi}}
                                   for ref in gruppi],
            "profili": a_record(profili),
            "aree_sospette": sospette,
        }

    def confronto_abbinato(self, df: pd.DataFrame, gruppo_a: str = "Sud", gruppo_b: str = "Isole",
                           col_gruppo: str = "area_geografica",
                           col_y: str = "guasto_entro_30gg") -> dict:
        """Per ogni asset di `gruppo_a` cerca l'asset di `gruppo_b` piu' simile (stesso tipo,
        stesse feature tecniche standardizzate) e confronta i tassi di guasto registrati."""
        a, b = df[df[col_gruppo] == gruppo_a], df[df[col_gruppo] == gruppo_b]
        scaler = StandardScaler().fit(df[FEATURE_NUMERICHE])
        tassi_b, distanze, n = [], [], 0
        for tipo, sub_a in a.groupby("tipo_asset"):
            sub_b = b[b["tipo_asset"] == tipo]
            if sub_b.empty:
                continue
            nn = NearestNeighbors(n_neighbors=1).fit(scaler.transform(sub_b[FEATURE_NUMERICHE]))
            dist, idx = nn.kneighbors(scaler.transform(sub_a[FEATURE_NUMERICHE]))
            tassi_b.extend(sub_b[col_y].to_numpy()[idx[:, 0]])
            distanze.extend(dist[:, 0])
            n += len(sub_a)
        return {
            "gruppo_a": gruppo_a, "gruppo_b": gruppo_b, "coppie": n,
            "tasso_a": _r(a[col_y].mean()), "tasso_b_abbinati": _r(np.mean(tassi_b)) if tassi_b else None,
            "distanza_media": _r(np.mean(distanze)) if distanze else None,
            "testo": (f"A parita' di profilo tecnico (abbinamento 1:1 per tipo e feature), "
                      f"{gruppo_a} registra un tasso di guasti di {_r(a[col_y].mean())} contro "
                      f"{_r(np.mean(tassi_b)) if tassi_b else '-'} degli asset gemelli di {gruppo_b}."),
        }

    # ------------------------------------------------------------------ report completo
    def report(self, predizioni: pd.DataFrame, dataset: pd.DataFrame = None,
               dimensioni=("area_geografica", "tipo_asset")) -> dict:
        """Report statico per la dashboard: metriche, allerte, drift e indagine sulle label."""
        out = {"soglie": {"gap_recall": self.soglia_gap_recall,
                          "gap_selezione": self.soglia_gap_selezione,
                          "gap_calibrazione": self.soglia_gap_calibrazione,
                          "accuracy_drift": self.soglia_accuracy_drift,
                          "finestra_drift": self.finestra_drift,
                          "delta_label_bias": self.soglia_label_bias},
               "n_test": int(len(predizioni)),
               "accuracy_globale": _r(accuracy_score(predizioni["y_true"], predizioni["y_pred"])),
               "recall_globale": _r(recall_score(predizioni["y_true"], predizioni["y_pred"])),
               "dimensioni": {}}
        for col in dimensioni:
            m = self.metriche_per_gruppo(predizioni, col)
            rec = m["recall"].dropna()
            cal = m["gap_calibrazione"].dropna()
            out["dimensioni"][col] = {
                "metriche": a_record(m),
                "allerte": self.allerte_dettagliate(m, col),
                "gap_recall": _r(rec.max() - rec.min()) if len(rec) else None,
                "gap_calibrazione_max": _r(cal.abs().max()) if len(cal) else None,
                "equalized_odds": self.equalized_odds(m),
            }
        out["drift"] = self.drift_settimanale(predizioni)
        if dataset is not None:
            out["label_bias"] = self.indagine_label_bias(dataset)
            out["confronto_sud_isole"] = self.confronto_abbinato(dataset)
        return out
