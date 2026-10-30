"""EnerGuard - modello con probabilita' calibrate (KPI C2).

Il RandomForest con class_weight="balanced" sovrastima la probabilita' di guasto. Questo wrapper
applica sopra le sue probabilita' una calibrazione sigmoide (Platt), addestrata su predizioni
out-of-fold del solo training set: il test set non viene mai usato per calibrare.

Il wrapper espone la stessa interfaccia del classificatore (predict_proba, feature_names_in_,
feature_importances_). Gli explainer ad alberi (SHAP) lavorano sul modello di base in `base`.

Confidenza = META-MODELLO DI AFFIDABILITA': un secondo Random Forest, addestrato sul training set
a prevedere se il modello, valutato out-of-fold alla soglia decisionale, ha dato la risposta giusta.
La confidenza e' la sua stima di P(previsione corretta). Usa solo le feature dell'asset, non p:
con p tra gli input impara che "p basso = quasi sempre giusto" e lascia in autonomia proprio i
guasti mancati. Cosi' due asset con la stessa probabilita' possono avere confidenze diverse.
"""
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

DESCRIZIONE_CONFIDENZA = ("probabilita' che la previsione sia corretta, stimata da un secondo Random Forest "
                          "addestrato sugli errori passati del modello")


class MetaAffidabilita:
    def __init__(self, X_tr, corretti):
        corretti = np.asarray(corretti, dtype=int)
        self.costante = None if len(np.unique(corretti)) > 1 else float(corretti[0])
        self.rf = None
        if self.costante is None:
            self.rf = RandomForestClassifier(n_estimators=300, min_samples_leaf=10, random_state=0)
            self.rf.fit(X_tr, corretti)

    def __call__(self, X) -> np.ndarray:
        if self.rf is None:
            return np.full(len(X), self.costante)
        return self.rf.predict_proba(X)[:, list(self.rf.classes_).index(1)]

    @property
    def feature_importances_(self):
        return None if self.rf is None else self.rf.feature_importances_


class ModelloCalibrato(BaseEstimator, ClassifierMixin):
    def __init__(self, base, calibratore: LogisticRegression, affidabilita: MetaAffidabilita = None):
        self.base = base
        self.calibratore = calibratore
        self.affidabilita = affidabilita
        self.classes_ = base.classes_

    @classmethod
    def da_predizioni_oof(cls, base, p_oof, y, X_tr=None, soglia: float = 0.5):
        """`base` e' gia' addestrato su tutto il training; `p_oof` sono le sue probabilita' out-of-fold.
        Con `X_tr` addestra anche il meta-modello di affidabilita' sugli esiti out-of-fold alla `soglia`."""
        cal = LogisticRegression(C=1e6, max_iter=1000)
        cal.fit(_logit(p_oof).reshape(-1, 1), y)
        aff = None
        if X_tr is not None:
            p_cal = cal.predict_proba(_logit(p_oof).reshape(-1, 1))[:, 1]
            aff = MetaAffidabilita(X_tr, (p_cal >= soglia).astype(int) == np.asarray(y))
        return cls(base, cal, aff)

    def confidenza(self, X) -> np.ndarray:
        """P(previsione corretta) dal meta-modello (0-1); senza meta-modello ripiega su max(p, 1-p)."""
        if self.affidabilita is not None:
            return self.affidabilita(X)
        p = self.predict_proba(X)[:, 1]
        return np.maximum(p, 1 - p)

    @property
    def feature_names_in_(self):
        return self.base.feature_names_in_

    @property
    def feature_importances_(self):
        return self.base.feature_importances_

    def predict_proba(self, X):
        p = self.calibratore.predict_proba(_logit(self.base.predict_proba(X)[:, 1]).reshape(-1, 1))[:, 1]
        return np.column_stack([1 - p, p])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))
