"""EnerGuard Starter Kit - lettura CSV robusta e percorsi del progetto.
Accetta sia il CSV standard (separatore virgola, decimale punto) sia la
versione per Excel italiano (separatore punto e virgola, decimale virgola).

I percorsi sono calcolati a partire da questo file, quindi gli script
funzionano da qualunque cartella vengano lanciati.
"""
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parents[1]
DATASET_DIR = ROOT_DIR / "dataset"
FRONTEND_DIR = ROOT_DIR / "dashboard" / "frontend"

PERCORSO_DATASET = DATASET_DIR / "energuard_dataset.csv"
PERCORSO_MODELLO = BACKEND_DIR / "modello.joblib"
PERCORSO_PREDIZIONI = BACKEND_DIR / "predizioni.csv"
PERCORSO_AUDIT = BACKEND_DIR / "audit_trail.jsonl"
PERCORSO_DASHBOARD = FRONTEND_DIR / "EnerGuard_Dashboard_Demo.html"

COLONNE_NON_FEATURE = ["asset_id", "guasto_entro_30gg"]
FEATURE_NUMERICHE = ["eta_anni", "temperatura_media", "vibrazione_indice", "carico_pct",
                     "umidita_media", "manutenzioni_ultimi_5anni", "giorni_da_ultima_manutenzione"]


def carica_csv(percorso) -> pd.DataFrame:
    df = pd.read_csv(percorso, encoding="utf-8-sig")
    if df.shape[1] == 1:  # tutto in una colonna: e' la versione ';' con decimale ','
        df = pd.read_csv(percorso, sep=";", decimal=",", encoding="utf-8-sig")
    return df


def prepara_feature(df: pd.DataFrame, colonne_modello=None) -> pd.DataFrame:
    """One-hot encoding identico a quello usato in addestramento.

    Se `colonne_modello` e' indicato (es. modello.feature_names_in_), le colonne
    vengono riallineate nello stesso ordine: una categoria assente vale 0.
    """
    X = pd.get_dummies(df.drop(columns=[c for c in COLONNE_NON_FEATURE if c in df.columns]))
    if colonne_modello is not None:
        X = X.reindex(columns=list(colonne_modello), fill_value=0)
    return X.astype(float)
