"""
EnerGuard Starter Kit - Scheletro dashboard Streamlit
======================================================
Esecuzione:  streamlit run app.py
Prerequisito: aver eseguito train_baseline.py (genera predizioni.csv)

Lo scheletro definisce le SEZIONI OBBLIGATORIE della dashboard (vedi
documento "Indicatori di Qualita'"). Il come le realizzate e' scelta
vostra: Streamlit e' solo il default comodo, potete usare React/Gradio.
"""

import pandas as pd
import streamlit as st

from audit_logger import AuditLogger
from explainer import ConfigLLM, crea_spiegatore, estrai_fattori
from bias_detector import BiasDetector
from utils_io import carica_csv
from oversight_manager import (OversightManager, Raccomandazione,
                               StatoDecisione, AZIONI)

st.set_page_config(page_title="EnerGuard | Supervisione Umana",
                   layout="wide", page_icon="⚡")


# ----------------------------------------------------------------------
@st.cache_resource
def bootstrap():
    audit = AuditLogger("audit_trail.jsonl")
    om = OversightManager(audit)
    spiegatore = crea_spiegatore(audit)   # LLM se .env e' configurato, altrimenti template
    pred = carica_csv("predizioni.csv")
    # Trasforma le prime N predizioni positive in raccomandazioni in coda
    for _, riga in pred[pred["y_pred"] == 1].head(40).iterrows():
        r = Raccomandazione(
            asset_id=riga["asset_id"],
            tipo_asset=riga["tipo_asset"],
            area_geografica=riga["area_geografica"],
            criticita_utenza=riga["criticita_utenza"],
            prob_guasto=round(float(riga["proba"]), 3),
            confidenza=round(float(riga["confidenza"]), 3),
            azione_proposta="programma_manutenzione",  # TODO: derivare da regole
            spiegazione=[],  # TODO: riempire con SHAP/feature importance
        )
        om.sottometti(r)
    return om, audit, pred, spiegatore


om, audit, pred, spiegatore = bootstrap()

st.title("EnerGuard · Dashboard di Supervisione Umana")
operatore = st.sidebar.text_input("ID operatore", value="OP-001")

# --- EMERGENCY STOP: sempre visibile, mai a piu' di un click ---
st.sidebar.divider()
_cfg = ConfigLLM()
st.sidebar.caption(
    f"Motore spiegazioni: **{'LLM · ' + _cfg.descrizione() if _cfg.attivo else 'template locale'}**"
    + ("" if _cfg.attivo else "  \nConfigurate .env (vedi .env.example) per usare un LLM esterno."))

st.sidebar.subheader("Emergency stop")
ambito = st.sidebar.selectbox("Ambito", ["GLOBALE", "area:Sud", "area:Nord",
                                         "area:Centro", "area:Isole",
                                         "tipo:linea_AT", "tipo:trasformatore"])
mot_stop = st.sidebar.text_input("Motivazione stop")
c1, c2 = st.sidebar.columns(2)
if c1.button("ATTIVA", type="primary"):
    om.attiva_stop(ambito, operatore, mot_stop or "non fornita")  # TODO: rendere obbligatoria
if c2.button("Disattiva"):
    om.disattiva_stop(ambito, operatore, mot_stop or "non fornita")
if om.stop_attivi:
    st.sidebar.error(f"STOP ATTIVI: {', '.join(sorted(om.stop_attivi))}")

@st.cache_data(show_spinner=False)
def _fattori_asset(asset_id: str):
    """Estrae i fattori SHAP per un asset. In cache: SHAP e' costoso."""
    import joblib
    modello = joblib.load("modello.joblib")
    df = carica_csv("energuard_dataset.csv")
    X = pd.get_dummies(df.drop(columns=["asset_id", "guasto_entro_30gg"]))
    x = X.loc[df["asset_id"] == asset_id].iloc[0]
    return estrai_fattori(modello, x, list(X.columns))


def spiegazione_per(r):
    """Spiegazione in linguaggio operativo per una raccomandazione in coda."""
    rec = {"asset_id": r.asset_id, "tipo_asset": r.tipo_asset, "area_geografica": r.area_geografica,
           "criticita_utenza": r.criticita_utenza, "prob_guasto": r.prob_guasto,
           "confidenza": r.confidenza, "azione_proposta": r.azione_proposta,
           "livello": getattr(r.livello, "value", None), "soglia_confidenza": om.soglia_conf}
    return spiegatore.spiega(rec, _fattori_asset(r.asset_id))


tab_coda, tab_matrice, tab_bias, tab_audit = st.tabs(
    ["Coda decisioni", "Matrice confidenza × rischio", "Bias & drift", "Audit trail"])

# ----------------------------------------------------------------------
with tab_coda:
    pendenti = [r for r in om.coda if r.stato == StatoDecisione.IN_ATTESA]
    st.metric("Decisioni in attesa di revisione umana", len(pendenti))
    for r in sorted(pendenti, key=lambda x: -x.prob_guasto)[:10]:
        with st.expander(
                f"{'🔴' if r.livello.value == 'HIC' else '🟠'} {r.asset_id} · "
                f"{r.tipo_asset} · {r.area_geografica} · "
                f"P(guasto)={r.prob_guasto} · conf={r.confidenza} · {r.livello.value}"):
            st.write(f"Azione proposta: **{r.azione_proposta}** · "
                     f"Utenza: {r.criticita_utenza}")
            sp = spiegazione_per(r)
            st.markdown(sp.testo)
            st.warning(sp.incertezza)
            st.caption(f"Fonte spiegazione: {sp.fonte} · {sp.latenza_ms} ms")
            mot = st.text_area("Motivazione (obbligatoria)", key=f"m{r.id}")
            az = st.selectbox("Azione", AZIONI,
                              index=AZIONI.index(r.azione_proposta), key=f"a{r.id}")
            b1, b2, b3 = st.columns(3)
            try:
                if b1.button("Approva", key=f"ok{r.id}"):
                    om.revisiona(r.id, StatoDecisione.APPROVATA, operatore, mot)
                if b2.button("Modifica e approva", key=f"mod{r.id}"):
                    om.revisiona(r.id, StatoDecisione.MODIFICATA, operatore, mot,
                                 azione_modificata=az)
                if b3.button("Rifiuta", key=f"no{r.id}"):
                    om.revisiona(r.id, StatoDecisione.RIFIUTATA, operatore, mot)
            except ValueError as e:
                st.error(str(e))

# ----------------------------------------------------------------------
with tab_matrice:
    st.subheader("Dove decide l'AI e dove serve l'umano")
    st.scatter_chart(pred.rename(columns={"proba": "rischio"}),
                     x="confidenza", y="rischio", color="area_geografica")
    st.caption("TODO: sovrapporre le soglie di routing HIC/HITL/HOTL e colorare "
               "le zone. L'operatore deve capire A COLPO D'OCCHIO in quale "
               "regime opera ogni decisione.")

# ----------------------------------------------------------------------
with tab_bias:
    bd = BiasDetector()
    valut = pred.rename(columns={})
    m = bd.metriche_per_gruppo(valut, "area_geografica")
    st.dataframe(m, use_container_width=True)
    for a in bd.allerte(m, "area_geografica"):
        st.warning(a)
    st.caption("TODO: aggiungere trend temporale (drift), tasso di override "
               "umano per area, calibrazione per gruppo.")

# ----------------------------------------------------------------------
with tab_audit:
    ok, n = audit.verifica_catena()
    st.metric("Integrità catena audit", "VERIFICATA" if ok else "COMPROMESSA",
              delta=f"{n} record")
    st.caption("TODO: tabella filtrabile del log per asset/operatore/periodo.")
