"""EnerGuard Starter Kit - lettura CSV robusta.
Accetta sia il CSV standard (separatore virgola, decimale punto) sia la
versione per Excel italiano (separatore punto e virgola, decimale virgola).
"""
import pandas as pd


def carica_csv(percorso: str) -> pd.DataFrame:
    df = pd.read_csv(percorso, encoding="utf-8-sig")
    if df.shape[1] == 1:  # tutto in una colonna: e' la versione ';' con decimale ','
        df = pd.read_csv(percorso, sep=";", decimal=",", encoding="utf-8-sig")
    return df
