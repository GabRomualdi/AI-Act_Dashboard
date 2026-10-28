"""
EnerGuard Starter Kit - Baseline Tier 1
========================================
Modello di partenza + metriche disaggregate. Con questi parametri e il
dataset ufficiale dovreste ottenere AUC intorno a 0.85. Da qui in poi il
punteggio NON si vince migliorando l'AUC: si vince costruendo l'oversight.

Esecuzione:  python train_baseline.py [--output CARTELLA]
Output:      modello.joblib, predizioni.csv (input per la dashboard), salvati
             nella cartella del backend qualunque sia la cartella di lancio.

Oltre alle metriche per gruppo stampa le verifiche usate dalla dashboard:
gap di calibrazione (C2), drift simulato (C4) e indagine sulle label storiche
(Sud vs Isole: profili simili, tassi registrati molto diversi).
"""

from pathlib import Path

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split

from bias_detector import BiasDetector
from oversight_manager import SOGLIA_DECISIONALE, calcola_confidenza
from utils_io import BACKEND_DIR, PERCORSO_DATASET, carica_csv, prepara_feature

# Soglia decisionale bassa per scelta: un guasto non previsto costa piu' di un'ispezione
# in piu' (vedi model card). E' la stessa usata dalla politica delle azioni.
SOGLIA = SOGLIA_DECISIONALE


def addestra(percorso_dataset=PERCORSO_DATASET, cartella_output=BACKEND_DIR, verboso: bool = True) -> dict:
    df = carica_csv(percorso_dataset)
    y = df["guasto_entro_30gg"]
    X = prepara_feature(df)

    X_tr, X_te, y_tr, y_te, df_tr, df_te = train_test_split(
        X, y, df, test_size=0.3, random_state=0, stratify=y)

    modello = RandomForestClassifier(n_estimators=300, random_state=0, class_weight="balanced")
    modello.fit(X_tr, y_tr)

    proba = modello.predict_proba(X_te)[:, 1]
    pred = (proba >= SOGLIA).astype(int)
    auc = roc_auc_score(y_te, proba)

    # --- Metriche disaggregate: il deliverable chiave del Tier 1 ---
    valut = df_te.copy()
    valut["y_true"], valut["y_pred"], valut["proba"] = y_te.values, pred, proba
    # Confidenza: max(p, 1-p), dichiarata in model card (stessa funzione usata dal routing)
    valut["confidenza"] = valut["proba"].apply(calcola_confidenza)

    bd = BiasDetector()
    report = bd.report(valut, df)
    if verboso:
        print("AUC:", round(auc, 3))
        print(classification_report(y_te, pred, digits=3))
        for col in ["area_geografica", "tipo_asset"]:
            m = bd.metriche_per_gruppo(valut, col)
            print(f"\n=== Metriche per {col} ===")
            print(m.to_string(index=False))
            for a in bd.allerte(m, col):
                print("  [ALLERTA]", a)
        print("\n=== Drift simulato (C4) ===")
        print(" ", report["drift"]["testo"])
        print("\n=== Indagine sulle label storiche (Art. 10) ===")
        for r in report["label_bias"]["per_gruppo"]:
            print(f"  {r['area_geografica']:<7} registrato {r['tasso_registrato']:.3f}  atteso dal profilo "
                  f"{r['tasso_atteso_da_profilo']:.3f}  delta {r['delta']:+.3f}  -> {r['esito']}")
        print(" ", report["confronto_sud_isole"]["testo"])

    cartella = Path(cartella_output)
    cartella.mkdir(parents=True, exist_ok=True)
    joblib.dump(modello, cartella / "modello.joblib")
    valut.to_csv(cartella / "predizioni.csv", index=False)
    if verboso:
        print(f"\nSalvati: {cartella / 'modello.joblib'}, {cartella / 'predizioni.csv'}")
    return {"auc": round(float(auc), 3), "n_test": int(len(valut)), "cartella": str(cartella),
            "aree_sospette": report["label_bias"]["aree_sospette"]}


def main():
    parser = argparse.ArgumentParser(description="Addestra il modello baseline EnerGuard")
    parser.add_argument("--output", default=str(BACKEND_DIR),
                        help="cartella in cui salvare modello.joblib e predizioni.csv")
    args = parser.parse_args()
    addestra(cartella_output=args.output)


if __name__ == "__main__":
    main()

# DOMANDA GUIDA (Tier 1): confrontate i profili di rischio di Sud e Isole
# (eta', manutenzioni, giorni dall'ultima manutenzione) con i rispettivi
# tassi di guasto registrati. Notate qualcosa di strano? Cosa potrebbe
# significare per la qualita' delle label storiche?
# RISPOSTA DEL TEAM: a parita' di profilo tecnico il Sud registra molti meno
# guasti di quanti ne farebbe attendere (vedi "Indagine sulle label storiche"):
# ipotesi di sotto-segnalazione storica. Il modello la eredita (stime basse e
# mal calibrate per il Sud), quindi la dashboard promuove a HITL le decisioni
# del Sud e lo segnala nelle spiegazioni.
