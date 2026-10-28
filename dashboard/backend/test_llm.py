"""
EnerGuard Starter Kit - test dell'explainer
============================================
Esecuzione: python test_llm.py
1) Verifica che il modulo a template funzioni (sempre).
2) Se il file .env configura un LLM, esegue una chiamata reale e mostra il risultato,
   la latenza e l'eventuale motivo di fallback.
Prerequisito: modello.joblib e predizioni.csv (eseguire prima train_baseline.py).
"""
import joblib
import pandas as pd

from explainer import ConfigLLM, SpiegatoreLLM, SpiegatoreTemplate, estrai_fattori
from utils_io import carica_csv

modello = joblib.load("modello.joblib")
pred = carica_csv("predizioni.csv")
df = carica_csv("energuard_dataset.csv")
X = pd.get_dummies(df.drop(columns=["asset_id", "guasto_entro_30gg"]))
feature_names = list(X.columns)

riga = pred[pred["y_pred"] == 1].iloc[0]
x = X.loc[df["asset_id"] == riga["asset_id"]].iloc[0]
fattori = estrai_fattori(modello, x, feature_names)
rec = {"asset_id": riga["asset_id"], "tipo_asset": riga["tipo_asset"], "area_geografica": riga["area_geografica"],
       "criticita_utenza": riga["criticita_utenza"], "prob_guasto": float(riga["proba"]),
       "confidenza": float(riga["confidenza"]), "azione_proposta": "programma_manutenzione", "livello": "HITL"}

print("=== Fattori principali (SHAP) ===")
for f in fattori:
    print(f"  {f.etichetta:<32} {f.valore:>8}  contributo {f.contributo:+.3f}  ({f.giudizio})")

print("\n=== 1. Spiegazione a template ===")
sp = SpiegatoreTemplate().spiega(rec, fattori)
print(sp.testo, "\n", sp.incertezza)

cfg = ConfigLLM()
print(f"\n=== 2. LLM: {cfg.descrizione()} ({'attivo' if cfg.attivo else 'NON configurato: creare .env da .env.example'}) ===")
sp = SpiegatoreLLM(cfg).spiega(rec, fattori)
print("fonte:", sp.fonte, "| latenza:", sp.latenza_ms, "ms")
print(sp.testo, "\n", sp.incertezza)
