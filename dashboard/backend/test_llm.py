"""
EnerGuard - verifica della connessione all'LLM
===============================================
Esecuzione:  python test_llm.py [--asset AST-00042] [--guasto chiave_errata|rete_assente]

Legge la configurazione LLM (.env o variabili d'ambiente, vedi explainer.py),
prende un asset (di default quello a rischio piu' alto del test set), calcola
fattori, azione e livello di supervisione esattamente come la dashboard e
chiede la spiegazione all'LLM. Stampa fonte, latenza e testo.
Non scrive nell'audit trail della dashboard e non stampa mai la chiave API.
Exit code 0 se la spiegazione arriva dall'LLM (o se l'LLM non e' configurato
e si usa il template per scelta), 1 se scatta il fallback.
"""

import argparse
import sys

import joblib

from explainer import (GUASTI_SIMULABILI, ConfigLLM, SpiegatoreLLM, analizza_riga, calibra_etichette)
from oversight_manager import (OversightManager, Raccomandazione, azione_raccomandata,
                               calcola_confidenza)
from utils_io import (PERCORSO_DATASET, PERCORSO_MODELLO, PERCORSO_PREDIZIONI, carica_csv,
                      prepara_feature)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prova la spiegazione LLM su un asset")
    parser.add_argument("--asset", help="asset_id da spiegare (default: rischio piu' alto del test set)")
    parser.add_argument("--guasto", choices=sorted(GUASTI_SIMULABILI),
                        help="simula un guasto dell'LLM per verificare il fallback al template")
    parser.add_argument("--env", help="percorso esplicito del file .env")
    args = parser.parse_args(argv)

    cfg = ConfigLLM(args.env)
    print("Configurazione LLM:", cfg.dettagli())
    if not cfg.attivo:
        print("LLM non configurato (LLM_PROVIDER/LLM_API_KEY/LLM_MODEL): verra' usato il template.")

    dataset = carica_csv(PERCORSO_DATASET)
    calibra_etichette(dataset)
    modello = joblib.load(PERCORSO_MODELLO)
    colonne = list(modello.feature_names_in_)
    asset_id = args.asset
    if not asset_id:
        pred = carica_csv(PERCORSO_PREDIZIONI)
        asset_id = str(pred.sort_values("proba", ascending=False).iloc[0]["asset_id"])
    righe = dataset[dataset["asset_id"] == asset_id]
    if righe.empty:
        print(f"Asset {asset_id} non presente nel dataset", file=sys.stderr)
        return 2
    asset = righe.iloc[0]
    X = prepara_feature(righe, colonne)
    prob = float(modello.predict_proba(X)[:, 1][0])
    conf = (round(float(modello.confidenza(X)[0]), 3) if hasattr(modello, "confidenza")
            else calcola_confidenza(prob))
    analisi = analizza_riga(modello, X.iloc[0], colonne)

    r = Raccomandazione(asset_id, asset["tipo_asset"], asset["area_geografica"], asset["criticita_utenza"],
                        prob, conf, azione_raccomandata(prob, float(asset["carico_pct"])),
                        [(f.nome, f.contributo) for f in analisi.fattori], carico_pct=float(asset["carico_pct"]))
    manager = OversightManager(audit_logger=None)
    manager.route(r)
    rec = manager.contesto_spiegazione(r)
    print(f"\nAsset {asset_id} ({asset['tipo_asset']}, {asset['area_geografica']}, utenza "
          f"{asset['criticita_utenza']}): P(guasto) {prob:.2f}, azione {r.azione_proposta}, "
          f"livello {r.livello.value} ({r.regola})")
    print(f"\nFattori principali (contributi {analisi.metodo.upper()} alla probabilita' di guasto):")
    for f in analisi.fattori:
        print(f"  {f.nome:<32} valore {f.valore_testo:<12} contributo {f.contributo:+.3f}  "
              f"({f.giudizio}, {f.direzione})")

    spiegatore = SpiegatoreLLM(cfg, audit_logger=None, usa_cache=False)
    tpl = spiegatore.fallback.spiega(rec, analisi.fattori)
    print(f"\n[template] {tpl.testo}\n           {tpl.incertezza}")
    if args.guasto:
        spiegatore.imposta_guasto(args.guasto)
    sp = spiegatore.spiega(rec, analisi.fattori)
    print(f"\nfonte: {sp.fonte}  |  latenza: {sp.latenza_ms} ms")
    print("Spiegazione:", sp.testo)
    print("Incertezza: ", sp.incertezza)
    return 0 if sp.tipo_fonte == "llm" or not cfg.attivo else 1


if __name__ == "__main__":
    sys.exit(main())
