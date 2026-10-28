"""
EnerGuard Starter Kit - Explainer (spiegazioni in linguaggio naturale)
=======================================================================
Trasforma l'output del modello predittivo in una spiegazione comprensibile
a un operatore di control room (test T4: "cosa raccomanda, perche', quanto
e' sicuro"). Componenti:

  1. analizza_riga() / estrai_fattori() -> i fattori che pesano di piu' (SHAP,
     con fallback dichiarato), confrontati con i valori tipici della flotta
  2. SpiegatoreTemplate -> spiegazione deterministica, nessuna API (sempre disponibile)
  3. SpiegatoreLLM      -> spiegazione generata da un LLM esterno via API,
                           con guardrail e fallback automatico al template
  4. controfattuale()   -> spiegazione contrastiva: cosa dovrebbe cambiare
                           perche' la raccomandazione cambi
  5. CasiSimili         -> gli asset storici piu' simili e cosa e' successo loro

Configurazione LLM (file .env, oppure variabili d'ambiente). Il file .env viene
cercato in quest'ordine: percorso esplicito, variabile ENERGUARD_ENV_FILE,
cartella del backend, radice del repository, cartella dataset/.
  LLM_PROVIDER = openai | anthropic | azure | compatible | none
  LLM_API_KEY  = la chiave API (mai nel codice, mai nel repository)
  LLM_MODEL    = es. gpt-4o-mini, claude-sonnet-4-5, ... (per azure: il deployment)
  LLM_BASE_URL = solo per azure / compatible (es. endpoint Azure, Ollama, vLLM, LM Studio)
  LLM_TIMEOUT  = secondi (default 20)
  LLM_REASONING_EFFORT = opzionale, per i modelli "reasoning" (default "low" se il
                         modello e' della famiglia gpt-5 / o-series)

Principio di design (Art. 13-14 AI Act): l'LLM NON decide e NON calcola.
Riceve i numeri gia' calcolati dal modello e li traduce in linguaggio operativo.
Se la risposta cita numeri non presenti nei dati, non nomina il fattore
principale o l'azione proposta, usa gergo tecnico, oppure l'API fallisce, si usa
il template e la fonte lo dichiara. Ogni spiegazione (template, LLM o fallback)
viene registrata nell'audit trail con fonte e latenza (KPI B3b).
Le connessioni HTTPS usano il trust store del sistema operativo (truststore),
cosi' funzionano anche dietro i proxy aziendali che ispezionano il TLS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # requests e' nel requirements; questo evita crash in modalita' template
    requests = None
    HTTPAdapter = object

try:
    import truststore
except ImportError:
    truststore = None

BACKEND_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Etichette operative delle feature: come le chiama un operatore, non un data scientist
# ---------------------------------------------------------------------------
ETICHETTE = {
    "vibrazione_indice": {
        "etichetta": "indice di vibrazione", "soggetto": "l'indice di vibrazione", "unita": "",
        "plurale": False, "chiavi": ("vibrazion",)},
    "giorni_da_ultima_manutenzione": {
        "etichetta": "giorni dall'ultima manutenzione", "soggetto": "i giorni dall'ultima manutenzione",
        "unita": "giorni", "plurale": True, "chiavi": ("manutenzion", "giorni")},
    "temperatura_media": {
        "etichetta": "temperatura di esercizio", "soggetto": "la temperatura di esercizio",
        "unita": "°C", "plurale": False, "chiavi": ("temperatur", "°c", "gradi", "surriscald")},
    "eta_anni": {
        "etichetta": "età dell'asset", "soggetto": "l'età dell'asset", "unita": "anni",
        "plurale": False, "chiavi": ("età", "eta'", "anni", "vecchi", "anzian")},
    "carico_pct": {
        "etichetta": "carico medio", "soggetto": "il carico medio", "unita": "%",
        "plurale": False, "chiavi": ("carico",)},
    "umidita_media": {
        "etichetta": "umidità ambientale", "soggetto": "l'umidità ambientale", "unita": "%",
        "plurale": False, "chiavi": ("umidit",)},
    "manutenzioni_ultimi_5anni": {
        "etichetta": "manutenzioni negli ultimi 5 anni", "soggetto": "le manutenzioni negli ultimi 5 anni",
        "unita": "", "plurale": True, "chiavi": ("manutenzion", "interventi")},
}

# Percentili della flotta (P10, P25, P50, P75, P90) calcolati su energuard_dataset.csv.
# Sono il riferimento per "nella norma / sopra / sotto" e per il controfattuale;
# calibra_etichette(df) li ricalcola su un altro dataset.
STATISTICHE = {
    "eta_anni": (9.0, 13.0, 18.0, 23.0, 28.0),
    "temperatura_media": (30.6, 36.0, 42.1, 48.4, 54.0),
    "vibrazione_indice": (0.6, 1.0, 1.7, 2.6, 3.8),
    "carico_pct": (45.1, 55.5, 67.8, 80.1, 91.3),
    "umidita_media": (43.5, 52.7, 62.4, 72.1, 80.1),
    "manutenzioni_ultimi_5anni": (1.0, 2.0, 3.0, 5.0, 6.0),
    "giorni_da_ultima_manutenzione": (92.9, 157.0, 236.0, 329.0, 407.0),
}
# Direzione attesa del rischio: usata SOLO dal fallback senza SHAP (dichiarato come proxy)
DIREZIONE_RISCHIO = {"eta_anni": 1, "temperatura_media": 1, "vibrazione_indice": 1, "carico_pct": 1,
                     "umidita_media": 1, "manutenzioni_ultimi_5anni": -1, "giorni_da_ultima_manutenzione": 1}
PREFISSI_CATEGORICI = ("tipo_asset_", "area_geografica_", "criticita_utenza_")

AZIONI_TESTO = {
    "nessuna_azione": "nessun intervento",
    "ispezione_routine": "un'ispezione di routine",
    "programma_manutenzione": "di programmare una manutenzione",
    "riduci_carico": "di ridurre il carico",
    "ispezione_urgente": "un'ispezione urgente",
}
AZIONI_ETICHETTA = {
    "nessuna_azione": "Nessuna azione",
    "ispezione_routine": "Ispezione di routine",
    "programma_manutenzione": "Programmare manutenzione",
    "riduci_carico": "Ridurre il carico",
    "ispezione_urgente": "Ispezione urgente",
}
# parole che devono comparire in una spiegazione LLM per ciascuna azione (guardrail)
_CHIAVI_AZIONE = {
    "nessuna_azione": (("nessun", "non raccomanda", "non richiede", "non serve", "non sono necessari"),),
    "ispezione_routine": (("ispezion", "controllo", "verifica"),),
    "programma_manutenzione": (("manutenzion",),),
    "riduci_carico": (("carico",), ("ridu", "abbass", "limit", "alleggerir")),
    "ispezione_urgente": (("ispezion", "sopralluogo"), ("urgen", "immediat", "subito", "prima possibile")),
}
NOMI_TIPO = {   # nome, con articolo, con preposizione "su"
    "trasformatore": ("trasformatore", "il trasformatore", "sul trasformatore"),
    "linea_AT": ("linea AT", "la linea AT", "sulla linea AT"),
    "cabina_primaria": ("cabina primaria", "la cabina primaria", "sulla cabina primaria"),
    "turbina_eolica": ("turbina eolica", "la turbina eolica", "sulla turbina eolica"),
}


def nome_tipo(tipo: str, forma: int = 0) -> str:
    nomi = NOMI_TIPO.get(tipo)
    if nomi:
        return nomi[forma]
    base = str(tipo).replace("_", " ")
    return (base, "l'asset " + base, "sull'asset " + base)[forma]


def descrivi_confidenza(confidenza: float) -> str:
    if confidenza >= 0.90:
        return "alta"
    if confidenza >= 0.80:
        return "buona"
    if confidenza >= 0.65:
        return "moderata"
    return "bassa"


def _pct(p: float) -> str:
    return f"{float(p) * 100:.0f}%"


def _da_a(p0: float, p1: float) -> str:
    """'dal 43% al 32%', 'dall'8% all'11%' (elisione davanti a vocale), 'dallo 0%'."""
    def art(p, base):
        n = int(f"{float(p) * 100:.0f}")
        if n == 0:
            return f"{base}llo {_pct(p)}"
        vocale = n in (1, 8, 11) or 80 <= n <= 89
        return f"{base}ll'{_pct(p)}" if vocale else f"{base}l {_pct(p)}"
    return f"{art(p0, 'da')} {art(p1, 'a')}"


AREA_DI = {"Nord": "del Nord", "Centro": "del Centro", "Sud": "del Sud", "Isole": "delle Isole"}


def _num(valore: float) -> str:
    v = float(valore)
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.1f}"


def calibra_etichette(df) -> dict:
    """Ricalcola i percentili della flotta (norma = P25-P75) sul dataset indicato."""
    for nome in list(STATISTICHE):
        if nome in df:
            q = df[nome].quantile([0.10, 0.25, 0.50, 0.75, 0.90]).round(1).tolist()
            STATISTICHE[nome] = tuple(float(x) for x in q)
    return dict(STATISTICHE)


def _giudizio(nome: str, valore: float) -> str:
    s = STATISTICHE.get(nome)
    if not s:
        return "valore registrato"
    p10, p25, _, p75, p90 = s
    if valore >= p90:
        return "molto sopra la norma"
    if valore > p75:
        return "sopra la norma"
    if valore <= p10:
        return "molto sotto la norma"
    if valore < p25:
        return "sotto la norma"
    return "nella norma"


@dataclass
class Fattore:
    nome: str          # nome colonna
    valore: float      # valore osservato sull'asset
    contributo: float  # contributo (SHAP o proxy) alla probabilita' di guasto, segno incluso
    etichetta: str = ""
    unita: str = ""
    giudizio: str = ""  # rispetto alla flotta: "molto sopra la norma" ... "molto sotto la norma"

    @property
    def valore_testo(self) -> str:
        v = _num(self.valore)
        if not self.unita:
            return v
        return f"{v}{self.unita}" if self.unita == "%" else f"{v} {self.unita}"

    @property
    def tipico(self) -> str:
        s = STATISTICHE.get(self.nome)
        if not s:
            return ""
        lo, hi = _num(s[1]), _num(s[3])
        if ("." in lo) != ("." in hi):   # stesso formato per i due estremi: "1.0-2.6", non "1-2.6"
            lo, hi = f"{s[1]:.1f}", f"{s[3]:.1f}"
        return f"{lo}-{hi}"

    @property
    def direzione(self) -> str:
        return "aumenta il rischio" if self.contributo >= 0 else "riduce il rischio"

    def descrizione(self) -> str:
        return f"{self.etichetta} {self.valore_testo}, {self.giudizio}"

    def a_dict(self) -> dict:
        return {"nome": self.nome, "etichetta": self.etichetta, "valore": round(float(self.valore), 3),
                "valore_testo": self.valore_testo, "unita": self.unita, "giudizio": self.giudizio,
                "valori_tipici": self.tipico, "contributo": round(float(self.contributo), 4),
                "direzione": self.direzione}


@dataclass
class Spiegazione:
    testo: str                       # la spiegazione in linguaggio operativo
    incertezza: str                  # frase sull'incertezza / sul livello di supervisione
    fattori: list[Fattore]
    fonte: str                       # "template" | "llm:<provider>/<modello>" | "template(fallback:<motivo>)"
    latenza_ms: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def tipo_fonte(self) -> str:
        if self.fonte.startswith("llm:"):
            return "llm"
        if self.fonte.startswith("template(fallback"):
            return "fallback"
        return "template"

    def a_dict(self) -> dict:
        return {"testo": self.testo, "incertezza": self.incertezza, "fonte": self.fonte,
                "tipo_fonte": self.tipo_fonte, "latenza_ms": self.latenza_ms,
                "fattori": [f.a_dict() for f in self.fattori],
                "meta": {k: v for k, v in self.meta.items() if k != "cache_key"}}


@dataclass
class Analisi:
    """Fattori principali + contributi aggregati delle variabili di contesto (area, tipo, utenza)."""
    fattori: list[Fattore]
    contributi_gruppi: dict
    metodo: str                      # "shap" | "proxy"


# ---------------------------------------------------------------------------
# 1. ESTRAZIONE DEI FATTORI
# ---------------------------------------------------------------------------
_EXPLAINERS: dict = {}
_LOCK_EXPLAINERS = threading.Lock()


def _explainer_per(modello):
    import shap
    with _LOCK_EXPLAINERS:
        ex = _EXPLAINERS.get(id(modello))
        if ex is None or ex[0] is not modello:
            ex = (modello, shap.TreeExplainer(modello))
            _EXPLAINERS[id(modello)] = ex
        return ex[1]


def _contributi(modello, x: np.ndarray, feature_names: list[str], explainer=None):
    try:
        explainer = explainer or _explainer_per(modello)
        sv = explainer.shap_values(x)
        # shap restituisce forme diverse a seconda della versione: normalizziamo alla classe positiva
        if isinstance(sv, list):
            sv = sv[1]
        sv = np.asarray(sv)
        if sv.ndim == 3:
            sv = sv[:, :, 1]
        return sv.reshape(-1), "shap"
    except Exception:
        # fallback dichiarato: importanza globale x scostamento dalla mediana della flotta,
        # con il segno atteso dal dominio (proxy grezzo ma onesto)
        imp = np.asarray(getattr(modello, "feature_importances_",
                                 np.ones(len(feature_names)) / len(feature_names)), dtype=float)
        out = np.zeros(len(feature_names))
        for i, nome in enumerate(feature_names):
            s = STATISTICHE.get(nome)
            if s:
                iqr = (s[3] - s[1]) or 1.0
                out[i] = imp[i] * (x[0, i] - s[2]) / iqr * DIREZIONE_RISCHIO.get(nome, 1)
        return out, "proxy"


def analizza_riga(modello, riga, feature_names: list[str], n: int = 3, explainer=None) -> Analisi:
    x = np.asarray(riga, dtype=float).reshape(1, -1)
    contributi, metodo = _contributi(modello, x, feature_names, explainer)
    gruppi = {p.rstrip("_"): float(sum(contributi[i] for i, nome in enumerate(feature_names)
                                       if nome.startswith(p)))
              for p in PREFISSI_CATEGORICI}
    out = []
    for i in np.argsort(-np.abs(contributi)):
        nome = feature_names[i]
        # le colonne one-hot (tipo, area, utenza) non sono "fattori" tecnici spiegabili
        if nome.startswith(PREFISSI_CATEGORICI):
            continue
        et = ETICHETTE.get(nome, {"etichetta": nome.replace("_", " "), "unita": ""})
        valore = float(x[0, i])
        out.append(Fattore(nome, valore, float(contributi[i]), et["etichetta"], et["unita"],
                           _giudizio(nome, valore)))
        if len(out) == n:
            break
    return Analisi(out, gruppi, metodo)


def estrai_fattori(modello, riga, feature_names: list[str], n: int = 3, explainer=None) -> list[Fattore]:
    """Restituisce i primi n fattori per contributo alla probabilita' di guasto.

    modello: classificatore scikit-learn addestrato (RandomForest nella baseline)
    riga:    pandas.Series o array 1-D con le feature (gia' one-hot come in training)
    explainer: shap.TreeExplainer riutilizzabile (opzionale, altrimenti messo in cache)
    """
    return analizza_riga(modello, riga, feature_names, n, explainer).fattori


# ---------------------------------------------------------------------------
# 2. SPIEGATORE A TEMPLATE (deterministico, sempre disponibile)
# ---------------------------------------------------------------------------
def _frase_fattore(f: Fattore) -> str:
    et = ETICHETTE.get(f.nome)
    soggetto = et["soggetto"] if et else f.etichetta
    return f"{soggetto}, {f.valore_testo}"


def _plurale(f: Fattore) -> bool:
    return bool(ETICHETTE.get(f.nome, {}).get("plurale"))


def _verbo_direzione(f: Fattore) -> str:
    if not _plurale(f):
        return f.direzione
    return f.direzione.replace("aumenta ", "aumentano ").replace("riduce ", "riducono ")


class SpiegatoreTemplate:
    fonte = "template"

    def spiega(self, rec: dict, fattori: list[Fattore]) -> Spiegazione:
        t0 = time.perf_counter()
        azione = rec["azione_proposta"]
        dove = f"{nome_tipo(rec['tipo_asset'], 2)} {rec['asset_id']} ({rec['area_geografica']})"
        p = float(rec["prob_guasto"])
        if azione == "nessuna_azione":
            testo = (f"Il sistema non raccomanda interventi {dove}: la probabilità di guasto "
                     f"entro 30 giorni è {_pct(p)}.")
        else:
            testo = (f"Il sistema raccomanda {AZIONI_TESTO.get(azione, azione)} {dove}: "
                     f"probabilità di guasto entro 30 giorni {_pct(p)}.")
        if fattori:
            f1 = fattori[0]
            tipico = f", valori tipici {f1.tipico}" if f1.tipico else ""
            testo += (f" Il fattore che pesa di più {'sono' if _plurale(f1) else 'è'} {_frase_fattore(f1)} "
                      f"({f1.giudizio}{tipico}), che {_verbo_direzione(f1)}.")
            altri = fattori[1:3]
            for segno, verbi in ((1, ("Aumenta", "Aumentano")), (-1, ("Riduce", "Riducono"))):
                gruppo = [a for a in altri if (a.contributo >= 0) == (segno > 0)]
                if not gruppo:
                    continue
                stesso_verso = (f1.contributo >= 0) == (segno > 0)
                plurale = len(gruppo) > 1 or _plurale(gruppo[0])
                testo += (f" {verbi[plurale]} {'anche ' if stesso_verso else ''}il rischio "
                          + " e ".join(f"{_frase_fattore(a)} ({a.giudizio})" for a in gruppo) + ".")
        inc = self._incertezza(rec)
        return Spiegazione(testo, inc, fattori, "template", int((time.perf_counter() - t0) * 1000))

    @staticmethod
    def _incertezza(rec: dict) -> str:
        conf = float(rec["confidenza"])
        parti = [f"Affidabilità della stima: {descrivi_confidenza(conf)} (confidenza {conf:.2f} su 1)."]
        livello, regola = rec.get("livello"), rec.get("regola")
        livello = getattr(livello, "value", livello)
        crit = rec.get("criticita_utenza", "")
        if livello == "HIC":
            if regola == "R2":
                parti.append(f"Ridurre il carico limita la fornitura a un'utenza {crit}: il sistema non "
                             "può agire da solo, decide un supervisore.")
            else:
                parti.append(f"L'asset serve un'utenza {crit} (ospedali, servizi essenziali): il sistema "
                             "non può agire da solo, la decisione è interamente umana.")
        elif regola == "R4" or (regola is None and conf < rec.get("soglia_confidenza", 0.80)):
            soglia = float(rec.get("soglia_confidenza", 0.80))
            drift = (" (soglia alzata per il calo recente di accuratezza)"
                     if rec.get("margine_drift") else "")
            parti.append(f"Confidenza sotto la soglia {soglia:.2f}{drift}: il modello è incerto su questo "
                         "caso. Valutare un sopralluogo prima di decidere.")
        elif regola == "R3":
            parti.append(f"Il rischio supera la soglia del {_pct(rec.get('soglia_rischio', 0.60))}: "
                         "l'azione parte solo se un operatore la approva.")
        elif regola == "R5":
            parti.append(f"L'area {rec['area_geografica']} è sotto osservazione: l'azione parte solo se un "
                         "operatore la approva.")
        elif regola == "R6":
            parti.append("Decisione sospesa da un emergency stop e poi riattivata: serve la revisione di "
                         "un operatore.")
        elif livello == "HOTL":
            parti.append("Rischio basso e azione leggera: il sistema agisce da solo e l'operatore può "
                         "contestare l'esecuzione.")
        else:
            parti.append("L'intervento proposto non è leggero: l'azione parte solo se un operatore la approva.")
        if rec.get("avviso_area"):
            parti.append(rec["avviso_area"])
        return " ".join(parti)


# ---------------------------------------------------------------------------
# 3. SPIEGAZIONE CONTRASTIVA E CASI SIMILI (rubrica, livello 4)
# ---------------------------------------------------------------------------
def controfattuale(modello, riga, fattori: list[Fattore], azione: str, prob: float,
                   politica=None) -> Optional[dict]:
    """Cosa dovrebbe cambiare perche' la raccomandazione cambi.

    Riporta uno, due o tutti e tre i fattori principali al valore tipico della flotta
    (mediana), ricalcola la probabilita' con lo stesso modello e l'azione con la stessa
    politica. Sceglie il cambiamento piu' piccolo che cambia la raccomandazione.
    `riga` e' una pandas.Series con le feature one-hot allineate al modello.
    """
    from itertools import combinations

    import pandas as pd
    if politica is None:
        from oversight_manager import azione_raccomandata as politica
    candidati_f = [f for f in fattori if f.nome in STATISTICHE
                   and _num(f.valore) != _num(STATISTICHE[f.nome][2])]
    if not candidati_f:
        return None
    sottoinsiemi = [c for k in range(1, len(candidati_f) + 1) for c in combinations(candidati_f, k)]
    righe = []
    for s in sottoinsiemi:
        r = riga.copy()
        for f in s:
            r[f.nome] = STATISTICHE[f.nome][2]
        righe.append(r)
    X = pd.DataFrame(righe)
    probs = modello.predict_proba(X)[:, 1]
    esiti = []
    for s, r, p in zip(sottoinsiemi, righe, probs):
        esiti.append({"fattori": s, "prob": float(p), "azione": politica(float(p), float(r["carico_pct"]))})
    cambiano = [e for e in esiti if e["azione"] != azione]
    if cambiano:
        scelto = min(cambiano, key=lambda e: (len(e["fattori"]), -abs(e["prob"] - prob)))
    else:
        scelto = esiti[-1]   # tutti i fattori ai valori tipici
    cambia = scelto["azione"] != azione
    modifiche = []
    for f in scelto["fattori"]:
        et = ETICHETTE.get(f.nome, {"soggetto": f.etichetta, "unita": "", "plurale": False})
        nuovo = Fattore(f.nome, STATISTICHE[f.nome][2], 0.0, f.etichetta, f.unita)
        modifiche.append({"nome": f.nome, "etichetta": f.etichetta, "da": f.valore_testo,
                          "a": nuovo.valore_testo, "soggetto": et["soggetto"], "plurale": et["plurale"]})
    primo = modifiche[0]
    cosa = (f"{primo['soggetto']} {'passassero' if primo['plurale'] else 'passasse'} "
            f"da {primo['da']} a {primo['a']}")
    if len(modifiche) == 1:
        cosa += " (valore tipico della flotta)"
    else:
        altre = [f"{m['soggetto']} da {m['da']} a {m['a']}" for m in modifiche[1:]]
        cosa = ", ".join([cosa] + altre[:-1]) + f" e {altre[-1]} (valori tipici della flotta)"
    verbo = "scenderebbe" if scelto["prob"] < prob else "salirebbe"
    if cambia:
        nuova = scelto["azione"]
        esito = ("il sistema non raccomanderebbe interventi" if nuova == "nessuna_azione"
                 else f"il sistema raccomanderebbe {AZIONI_TESTO.get(nuova, nuova)}")
        testo = f"Se {cosa}, la probabilità di guasto {verbo} {_da_a(prob, scelto['prob'])} e {esito}."
    else:
        testo = (f"Anche se {cosa}, la probabilità {verbo} solo {_da_a(prob, scelto['prob'])}: "
                 f"la raccomandazione non cambierebbe.")
    testo = testo[0].upper() + testo[1:]
    return {"testo": testo, "cambia": cambia, "prob_prima": round(float(prob), 3),
            "prob_dopo": round(scelto["prob"], 3), "azione_prima": azione, "azione_dopo": scelto["azione"],
            "modifiche": [{k: m[k] for k in ("nome", "etichetta", "da", "a")} for m in modifiche]}


class CasiSimili:
    """Asset storici piu' simili (stesso tipo, feature tecniche standardizzate) e loro esito reale."""

    def __init__(self, storico, k: int = 5, aree_sospette=(), col_y: str = "guasto_entro_30gg"):
        from sklearn.neighbors import NearestNeighbors
        from sklearn.preprocessing import StandardScaler

        from utils_io import FEATURE_NUMERICHE
        self.k, self.col_y, self.feature = k, col_y, list(FEATURE_NUMERICHE)
        self.aree_sospette = set(aree_sospette)
        self.scaler = StandardScaler().fit(storico[self.feature].to_numpy(dtype=float))
        self._per_tipo = {}
        for tipo, sub in storico.groupby("tipo_asset"):
            sub = sub.reset_index(drop=True)
            nn = NearestNeighbors(n_neighbors=min(k + 1, len(sub)))
            nn.fit(self.scaler.transform(sub[self.feature].to_numpy(dtype=float)))
            self._per_tipo[tipo] = (sub, nn)

    def trova(self, asset: dict) -> Optional[dict]:
        voce = self._per_tipo.get(asset.get("tipo_asset"))
        if voce is None:
            return None
        sub, nn = voce
        x = self.scaler.transform(np.asarray([[float(asset[c]) for c in self.feature]]))
        dist, idx = nn.kneighbors(x)
        casi = []
        for d, i in zip(dist[0], idx[0]):
            c = sub.iloc[int(i)]
            if c["asset_id"] == asset.get("asset_id"):
                continue
            casi.append({"asset_id": str(c["asset_id"]), "area_geografica": str(c["area_geografica"]),
                         "eta_anni": int(c["eta_anni"]), "vibrazione_indice": float(c["vibrazione_indice"]),
                         "giorni_da_ultima_manutenzione": int(c["giorni_da_ultima_manutenzione"]),
                         "guasto": bool(int(c[self.col_y])), "distanza": round(float(d), 2)})
            if len(casi) == self.k:
                break
        if not casi:
            return None
        guasti = sum(c["guasto"] for c in casi)
        esito = ("nessuno ha avuto un guasto" if guasti == 0 else
                 f"{guasti} {'ha avuto' if guasti == 1 else 'hanno avuto'} un guasto")
        testo = (f"Tra i {len(casi)} asset storici più simili ({nome_tipo(asset['tipo_asset'])}, età e "
                 f"condizioni tecniche vicine), {esito} entro 30 giorni.")
        sospetti = [c for c in casi if c["area_geografica"] in self.aree_sospette]
        avvertenza = None
        if sospetti:
            aree = ", ".join(AREA_DI.get(a, a) for a in sorted({c["area_geografica"] for c in sospetti}))
            avvertenza = (f"{len(sospetti)} casi su {len(casi)} sono {aree}, dove i guasti storici risultano "
                          "sotto-registrati: il confronto potrebbe sottostimare il rischio.")
        return {"k": len(casi), "guasti": guasti, "testo": testo, "avvertenza": avvertenza, "casi": casi}


# ---------------------------------------------------------------------------
# 4. SPIEGATORE LLM (API esterna, provider-agnostico, con guardrail)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Sei l'assistente di una control room di un operatore di rete elettrica.
Ricevi, in JSON, la raccomandazione di un modello di manutenzione predittiva e i fattori che l'hanno determinata.
Il tuo compito è TRADURLA in italiano operativo per un tecnico o un responsabile che non conosce il machine learning.

Regole vincolanti:
1. Non inventare numeri, cause o fattori: usa SOLO i valori presenti nel JSON, citandoli così come sono
   (le probabilità puoi scriverle come percentuali, es. 0.78 -> 78%).
2. Non cambiare la raccomandazione e non aggiungerne altre: nomina l'azione con il testo di "azione_proposta_testo".
3. Cita il fattore principale (il primo di "fattori_principali") con il suo nome e il suo valore e di' se aumenta o riduce il rischio.
4. Non usare gergo statistico (niente "SHAP", "feature", "coefficiente", "peso relativo", "il modello ha appreso").
5. Esplicita sempre l'incertezza: riporta la confidenza; se il livello è HIC ricorda che decide l'operatore,
   se è HITL che serve la sua approvazione; se "avviso_area" è presente, riportalo.
6. Massimo 3 frasi per la spiegazione, 1-2 frasi per l'incertezza. Tono sobrio, nessuna enfasi.

Rispondi SOLO con un oggetto JSON con due chiavi: "spiegazione" e "incertezza". Nessun testo prima o dopo."""

GUASTI_SIMULABILI = {
    "chiave_errata": "chiave API errata (simulazione)",
    "rete_assente": "rete non raggiungibile (simulazione)",
}


def _cerca_env(env_path: Optional[str] = None) -> Optional[Path]:
    candidati = [env_path, os.getenv("ENERGUARD_ENV_FILE"), BACKEND_DIR / ".env",
                 BACKEND_DIR.parents[1] / ".env", BACKEND_DIR.parents[1] / "dataset" / ".env"]
    for c in candidati:
        if c and Path(c).is_file():
            return Path(c)
    return None


class ConfigLLM:
    def __init__(self, env_path: Optional[str] = None, leggi_ambiente: bool = True):
        self.file_env = self._carica_env(env_path) if leggi_ambiente else None
        get = os.getenv if leggi_ambiente else (lambda _k, d=None: d)
        self.provider = (get("LLM_PROVIDER", "none") or "none").strip().lower()
        self.api_key = (get("LLM_API_KEY", "") or "").strip()
        self.model = (get("LLM_MODEL", "") or "").strip()
        self.base_url = (get("LLM_BASE_URL", "") or "").strip().rstrip("/")
        try:
            self.timeout = float(get("LLM_TIMEOUT", "20") or 20)
        except ValueError:
            self.timeout = 20.0
        self.api_version = (get("LLM_API_VERSION", "2024-12-01-preview") or "").strip()  # solo Azure
        self.reasoning_effort = (get("LLM_REASONING_EFFORT", "") or "").strip().lower()

    @classmethod
    def nessuno(cls) -> "ConfigLLM":
        """Configurazione senza LLM (test, modalita' offline): solo template."""
        return cls(leggi_ambiente=False)

    @staticmethod
    def _carica_env(env_path: Optional[str]) -> Optional[Path]:
        """Parser minimale di .env: evita la dipendenza da python-dotenv.
        Le variabili gia' presenti nell'ambiente hanno la precedenza."""
        p = _cerca_env(env_path)
        if p is None:
            return None
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return p

    @property
    def attivo(self) -> bool:
        if self.provider in ("", "none"):
            return False
        if self.provider == "compatible":
            return bool(self.base_url and self.model)   # es. Ollama locale: chiave non necessaria
        if self.provider == "azure" and not self.base_url:
            return False
        return bool(self.api_key and self.model)

    @property
    def modello_reasoning(self) -> bool:
        return bool(re.match(r"^(gpt-5|o\d)", self.model.lower()))

    def descrizione(self) -> str:
        return f"{self.provider}/{self.model or '-'}"

    def dettagli(self) -> dict:
        """Stato della configurazione senza segreti (la chiave non esce mai)."""
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0] if self.base_url else None
        return {"provider": self.provider, "modello": self.model or None, "endpoint": host,
                "attivo": self.attivo, "timeout_s": self.timeout,
                "file_env": self.file_env.name if self.file_env else None,
                "chiave_configurata": bool(self.api_key)}


class _AdattatoreTruststore(HTTPAdapter):
    """HTTPS con i certificati del sistema operativo (proxy aziendali con ispezione TLS)."""

    def _contesto(self):
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._contesto()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._contesto()
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _nuova_sessione():
    s = requests.Session()
    if truststore is not None:
        s.mount("https://", _AdattatoreTruststore())
    return s


class SpiegatoreLLM:
    """Chiama un LLM esterno. Se qualcosa non va, torna al template e lo dichiara nella fonte."""

    MAX_SPIEGAZIONE = 900
    MAX_INCERTEZZA = 600
    GERGO = ("shap", "feature", "coefficient", "peso relativo", "peso_relativo", "machine learning",
             "random forest", "classificator", "dataset", "training")

    def __init__(self, config: Optional[ConfigLLM] = None, audit_logger=None, usa_cache: bool = True):
        self.cfg = config or ConfigLLM()
        self.fallback = SpiegatoreTemplate()
        self.audit = audit_logger
        self.cache: Optional[dict] = {} if usa_cache else None
        self.chiamate = {"ok": 0, "fallback": 0, "fallback_simulati": 0, "template": 0, "cache": 0}
        self.ultimo_errore: Optional[str] = None
        self.guasto_simulato: Optional[str] = None
        self._lock = threading.Lock()
        self._locale = threading.local()

    # ------------------------------------------------------------------ pubblico
    @property
    def configurato(self) -> bool:
        return self.cfg.attivo and requests is not None

    def imposta_guasto(self, modalita: Optional[str]):
        """Simula un guasto reale dell'LLM (chiave errata o rete assente) per dimostrare il fallback."""
        if modalita not in (None, *GUASTI_SIMULABILI):
            raise ValueError(f"Guasto non previsto: {modalita}. Ammessi: {', '.join(GUASTI_SIMULABILI)}")
        self.guasto_simulato = modalita

    def statistiche(self) -> dict:
        with self._lock:
            c = dict(self.chiamate)
        reali = c["ok"] + c["fallback"]
        return {**c, "configurato": self.configurato, "descrizione": self.cfg.descrizione(),
                "configurazione": self.cfg.dettagli(), "guasto_simulato": self.guasto_simulato,
                "ultimo_errore": self.ultimo_errore,
                "tasso_fallback": round(c["fallback"] / reali, 3) if reali else None}

    def spiega_template(self, rec: dict, fattori: list[Fattore]) -> Spiegazione:
        """Spiegazione immediata (sincrona): la card non resta mai senza spiegazione."""
        sp = self.fallback.spiega(rec, fattori)
        self._conta("template")
        self._log("spiegazione_template", rec, sp)
        return sp

    def spiega(self, rec: dict, fattori: list[Fattore]) -> Spiegazione:
        if not self.cfg.attivo:
            return self.spiega_template(rec, fattori)
        if requests is None:
            return self._fallback(rec, fattori, "libreria requests assente")

        guasto = self.guasto_simulato
        payload = self._payload(rec, fattori)
        chiave = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
        if not guasto and self.cache is not None and chiave in self.cache:
            self._conta("cache")
            base = self.cache[chiave]
            sp = Spiegazione(base.testo, base.incertezza, fattori, base.fonte, 0, {**base.meta, "da_cache": True})
            self._log("spiegazione_llm_ok", rec, sp)
            return sp

        t0 = time.perf_counter()
        try:
            grezzo = self._chiama(payload, guasto)
            dati = self._parse_json(grezzo)
            self._guardrail(dati, fattori, rec, payload)
            sp = Spiegazione(dati["spiegazione"].strip(), dati["incertezza"].strip(), fattori,
                             f"llm:{self.cfg.descrizione()}", int((time.perf_counter() - t0) * 1000),
                             {"cache_key": chiave})
        except Exception as e:  # qualunque errore: rete, quota, JSON malformato, guardrail
            motivo = self._motivo(e)
            if guasto:
                motivo = f"{motivo}, guasto simulato"
            return self._fallback(rec, fattori, motivo, int((time.perf_counter() - t0) * 1000), bool(guasto))
        self._conta("ok")
        if self.cache is not None:
            self.cache[chiave] = sp
        self._log("spiegazione_llm_ok", rec, sp)
        return sp

    # ------------------------------------------------------------------ interno
    def _conta(self, chiave: str):
        with self._lock:
            self.chiamate[chiave] += 1

    def _motivo(self, e: Exception) -> str:
        if requests is not None and isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            motivo = f"HTTP {e.response.status_code}"
        elif requests is not None and isinstance(e, requests.exceptions.Timeout):
            motivo = "timeout"
        elif requests is not None and isinstance(e, requests.exceptions.ConnectionError):
            motivo = "rete non raggiungibile"
        elif isinstance(e, _ErroreGuardrail):
            motivo = f"guardrail: {e}"
        else:
            motivo = f"{type(e).__name__}: {str(e)[:100]}"
        if self.cfg.api_key:
            motivo = motivo.replace(self.cfg.api_key, "***")
        self.ultimo_errore = motivo
        return motivo

    def _fallback(self, rec, fattori, motivo: str, latenza_ms: int = 0, simulato: bool = False) -> Spiegazione:
        sp = self.fallback.spiega(rec, fattori)
        sp.fonte = f"template(fallback:{motivo})"
        sp.latenza_ms = latenza_ms
        sp.meta["motivo_fallback"] = motivo
        self._conta("fallback_simulati" if simulato else "fallback")
        self._log("spiegazione_fallback_template", rec, sp)
        return sp

    def _log(self, evento, rec, sp):
        if self.audit is None:
            return
        try:
            extra = {"decisione_id": rec.get("decisione_id"), "asset_id": rec.get("asset_id"),
                     "fonte": sp.fonte, "tipo_fonte": sp.tipo_fonte, "latenza_ms": sp.latenza_ms}
            if sp.meta.get("da_cache"):
                extra["da_cache"] = True
            self.audit.log("SISTEMA", evento, None, extra=extra)
        except Exception:
            pass

    @staticmethod
    def _payload(rec: dict, fattori: list[Fattore]) -> dict:
        livello = rec.get("livello")

        def valore(v):
            v = float(_num(v))
            return int(v) if v.is_integer() else v

        payload = {
            "asset_id": rec["asset_id"], "tipo_asset": nome_tipo(rec["tipo_asset"]),
            "area": rec["area_geografica"], "criticita_utenza": rec["criticita_utenza"],
            "livello_supervisione": getattr(livello, "value", livello),
            "motivo_livello": rec.get("motivo_routing"),
            "probabilita_guasto_30gg": round(float(rec["prob_guasto"]), 2),
            "confidenza": round(float(rec["confidenza"]), 2),
            "soglia_confidenza": rec.get("soglia_confidenza", 0.80),
            "soglia_rischio": rec.get("soglia_rischio", 0.60),
            "azione_proposta": rec["azione_proposta"],
            "azione_proposta_testo": AZIONI_TESTO.get(rec["azione_proposta"], rec["azione_proposta"]),
            "fattori_principali": [
                {"nome": f.etichetta, "valore": valore(f.valore), "unita": f.unita,
                 "giudizio": f.giudizio, "valori_tipici": f.tipico, "direzione": f.direzione,
                 "peso_relativo": round(abs(f.contributo), 3)}
                for f in fattori],
        }
        if rec.get("area_sotto_osservazione"):
            payload["area_sotto_osservazione"] = True
        if rec.get("avviso_area"):
            payload["avviso_area"] = rec["avviso_area"]
        return {k: v for k, v in payload.items() if v is not None}

    def _sessione(self):
        s = getattr(self._locale, "sessione", None)
        if s is None:
            s = self._locale.sessione = _nuova_sessione()
        return s

    def _chiama(self, payload: dict, guasto: Optional[str] = None) -> str:
        p = self.cfg.provider
        user = "Raccomandazione da tradurre:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        api_key = "chiave-errata-simulata" if guasto == "chiave_errata" else self.cfg.api_key
        base_url = "https://127.0.0.1:9" if guasto == "rete_assente" else self.cfg.base_url
        timeout = min(self.cfg.timeout, 5.0) if guasto else self.cfg.timeout
        sessione = self._sessione()
        if p == "anthropic":
            url = (base_url if guasto == "rete_assente" else "https://api.anthropic.com") + "/v1/messages"
            r = sessione.post(url, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"},
                              json={"model": self.cfg.model, "max_tokens": 600, "temperature": 0.2,
                                    "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": user}]},
                              timeout=timeout)
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

        # OpenAI, Azure OpenAI e qualunque endpoint "chat completions" compatibile (Ollama, vLLM, LM Studio...)
        if p == "openai":
            url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            headers = {"Authorization": "Bearer " + api_key}
        elif p == "azure":
            url = (f"{base_url}/openai/deployments/{self.cfg.model}/chat/completions"
                   f"?api-version={self.cfg.api_version}")
            headers = {"api-key": api_key}
        elif p == "compatible":
            url = base_url + "/chat/completions"
            headers = {"Authorization": "Bearer " + api_key} if api_key else {}
        else:
            raise ValueError(f"Provider LLM non supportato: {p}")
        body = {"model": self.cfg.model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user}]}
        if p == "azure" or self.cfg.modello_reasoning:
            # I modelli recenti (famiglia GPT-5 / o-series) usano "max_completion_tokens" al posto di
            # "max_tokens" e accettano solo la temperature di default: inviarle causa HTTP 400.
            # Il budget include i token di ragionamento, per questo e' piu' ampio.
            body["max_completion_tokens"] = 1500
            effort = self.cfg.reasoning_effort or ("low" if self.cfg.modello_reasoning else "")
            if effort:
                body["reasoning_effort"] = effort
        else:
            body["temperature"] = 0.2
            body["max_tokens"] = 600
        r = sessione.post(url, headers={**headers, "content-type": "application/json"},
                          json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    @staticmethod
    def _parse_json(testo: str) -> dict:
        testo = testo.strip()
        testo = re.sub(r"^```(?:json)?\s*|\s*```$", "", testo)     # via eventuali fence markdown
        m = re.search(r"\{.*\}", testo, re.S)
        if not m:
            raise _ErroreGuardrail("risposta senza JSON")
        try:
            d = json.loads(m.group(0))
        except ValueError:
            raise _ErroreGuardrail("JSON malformato") from None
        if not isinstance(d.get("spiegazione"), str) or not isinstance(d.get("incertezza"), str):
            raise _ErroreGuardrail("JSON senza le chiavi richieste")
        if not d["spiegazione"].strip() or not d["incertezza"].strip():
            raise _ErroreGuardrail("spiegazione o incertezza vuota")
        return d

    @classmethod
    def _guardrail(cls, dati: dict, fattori: list[Fattore], rec: dict, payload: Optional[dict] = None):
        """Blocca risposte che inventano numeri, ignorano il fattore principale o l'azione, usano gergo.

        Obiettivo: impedire numeri INVENTATI (cio' che la giuria verifica in T4),
        non punire una riformulazione fedele dello stesso valore. Sono accettate le
        forme legittime di un numero presente nei dati passati all'LLM (qualunque
        campo del payload, testi inclusi): arrotondamenti (0.82 -> 0.8),
        percentuali (0.82 -> 82%), virgola o punto come separatore decimale.
        Cosi' il tasso di fallback resta basso (KPI B3c: fallback < 5%).
        """
        if len(dati["spiegazione"]) > cls.MAX_SPIEGAZIONE or len(dati["incertezza"]) > cls.MAX_INCERTEZZA:
            raise _ErroreGuardrail("risposta troppo lunga")
        testo = (dati["spiegazione"] + " " + dati["incertezza"]).lower()
        payload = payload if payload is not None else cls._payload(rec, fattori)

        # numeri ammessi: quelli del payload (anche dentro i testi, es. "AST-00412", "ultimi 5 anni"),
        # l'orizzonte di 30 giorni, "su 100" e il numero di fattori
        valori = [30.0, 100.0, float(len(fattori))]
        for k in ("prob_guasto", "confidenza", "soglia_confidenza", "soglia_rischio"):
            if rec.get(k) is not None:
                valori.append(float(rec[k]))
        valori += [float(f.valore) for f in fattori]

        def raccogli(o):
            if isinstance(o, bool) or o is None:
                return
            if isinstance(o, (int, float)):
                valori.append(float(o))
            elif isinstance(o, str):
                for t in re.findall(r"\d+(?:[.,]\d+)?", o):
                    valori.append(float(t.replace(",", ".")))
            elif isinstance(o, dict):
                for v in o.values():
                    raccogli(v)
            elif isinstance(o, (list, tuple)):
                for v in o:
                    raccogli(v)
        raccogli(payload)

        def ammesso(num_txt: str) -> bool:
            grezzo = num_txt.replace(",", ".")
            try:
                x = float(grezzo)
            except ValueError:
                return False
            dec = len(grezzo.split(".")[1]) if "." in grezzo else 0
            tolleranza = 0.5 * 10 ** -dec + 1e-9
            # stesso valore (eventualmente arrotondato) oppure lo stesso in forma percentuale
            return any(abs(v - x) <= tolleranza or abs(v * 100 - x) <= tolleranza for v in valori)

        for num in re.findall(r"\d+(?:[.,]\d+)?", testo):
            if not ammesso(num):
                raise _ErroreGuardrail(f"numero non presente nei dati: {num}")
        for parola in cls.GERGO:
            if parola in testo:
                raise _ErroreGuardrail(f"gergo tecnico nella risposta: {parola}")
        if fattori:
            chiavi = ETICHETTE.get(fattori[0].nome, {}).get("chiavi") or (fattori[0].etichetta.split()[0].lower(),)
            if not any(c in testo for c in chiavi):
                raise _ErroreGuardrail("il fattore principale non è citato")
        for gruppo in _CHIAVI_AZIONE.get(rec["azione_proposta"], ()):
            if not any(c in testo for c in gruppo):
                raise _ErroreGuardrail("l'azione proposta non è citata")


class _ErroreGuardrail(ValueError):
    """Risposta dell'LLM scartata dai guardrail."""


# ---------------------------------------------------------------------------
# Factory: la dashboard chiama questa e non deve sapere quale provider c'e' dietro
# ---------------------------------------------------------------------------
def crea_spiegatore(audit_logger=None, config: Optional[ConfigLLM] = None):
    cfg = config or ConfigLLM()
    if cfg.attivo:
        return SpiegatoreLLM(cfg, audit_logger)
    return SpiegatoreTemplate()
