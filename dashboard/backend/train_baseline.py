"""
EnerGuard Starter Kit - Baseline Tier 1
========================================
Modello di partenza + metriche disaggregate. Con questi parametri e il
dataset ufficiale dovreste ottenere AUC intorno a 0.85. Da qui in poi il
punteggio NON si vince migliorando l'AUC: si vince costruendo l'oversight.

Esecuzione:  python train_baseline.py
Output:      modello.joblib, predizioni.csv (input per la dashboard)
"""

from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split

from bias_detector import BiasDetector
from utils_io import carica_csv

SOGLIA = 0.35   # soglia decisionale: da discutere e giustificare in model card

ROOT = Path(__file__).resolve().parents[2]
df = carica_csv(ROOT / "dataset" / "energuard_dataset.csv")
y = df["guasto_entro_30gg"]
X = pd.get_dummies(df.drop(columns=["asset_id", "guasto_entro_30gg"]))

X_tr, X_te, y_tr, y_te, df_tr, df_te = train_test_split(
    X, y, df, test_size=0.3, random_state=0, stratify=y)

modello = RandomForestClassifier(
    n_estimators=300, random_state=0, class_weight="balanced")
modello.fit(X_tr, y_tr)

proba = modello.predict_proba(X_te)[:, 1]
pred = (proba >= SOGLIA).astype(int)

print("AUC:", round(roc_auc_score(y_te, proba), 3))
print(classification_report(y_te, pred, digits=3))

# --- Metriche disaggregate: il deliverable chiave del Tier 1 ---
valut = df_te.copy()
valut["y_true"], valut["y_pred"], valut["proba"] = y_te.values, pred, proba

bd = BiasDetector()
for col in ["area_geografica", "tipo_asset"]:
    m = bd.metriche_per_gruppo(valut, col)
    print(f"\n=== Metriche per {col} ===")
    print(m.to_string(index=False))
    for a in bd.allerte(m, col):
        print("  [ALLERTA]", a)

# Confidenza: scelta semplice, da dichiarare in model card
valut["confidenza"] = proba.round(3)
valut["confidenza"] = valut["proba"].apply(lambda p: round(max(p, 1 - p), 3))

joblib.dump(modello, "modello.joblib")
valut.to_csv("predizioni.csv", index=False)
print("\nSalvati: modello.joblib, predizioni.csv")

# DOMANDA GUIDA (Tier 1): confrontate i profili di rischio di Sud e Isole
# (eta', manutenzioni, giorni dall'ultima manutenzione) con i rispettivi
# tassi di guasto registrati. Notate qualcosa di strano? Cosa potrebbe
# significare per la qualita' delle label storiche?
