"""
EnerGuard Starter Kit - AuditLogger
====================================
Log append-only in JSONL con concatenazione di hash: ogni riga contiene
l'hash della precedente, quindi qualunque manomissione a posteriori rompe
la catena ed e' rilevabile. Risponde ai requisiti di tracciabilita'
(Art. 12 e Art. 14 AI Act).

Cosa garantisce (e cosa no):
  - ogni transizione di stato e' registrata con attore, timestamp con fuso
    orario, decisione coinvolta e motivazione (KPI D1);
  - `verifica()` ricalcola la catena e indica il primo record non valido
    (KPI D2); in piu' confronta la coda del file con l'ultimo hash scritto
    dal processo in esecuzione ("ancora"), cosi' rileva anche troncamenti e
    catene ricalcolate da zero mentre il sistema e' attivo;
  - `ricostruisci()` risponde in pochi secondi a "chi, cosa, quando, perche'"
    per un asset (KPI D3 / test T6), `cerca()` filtra per asset, operatore,
    evento e periodo, `esporta()` produce il file per un audit esterno.
  - Limite dichiarato: l'hash non e' firmato (nessuna chiave segreta); dopo un
    riavvio l'ancora viene riletta dal file stesso. Per un uso reale servirebbe
    un'ancora esterna (es. hash periodico depositato su un sistema terzo).
"""

import copy
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

GENESI = "GENESI"


def _json_default(obj):
    """Converte scalari numpy & co. in tipi nativi (stesso risultato in scrittura e in verifica)."""
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "value"):     # Enum
        return obj.value
    return str(obj)


def _parse_ts(valore) -> Optional[datetime]:
    """ISO 8601 -> datetime con fuso orario (i valori senza fuso sono intesi come ora locale)."""
    if valore is None or valore == "":
        return None
    dt = valore if isinstance(valore, datetime) else datetime.fromisoformat(str(valore))
    return dt if dt.tzinfo else dt.astimezone()


class AuditLogger:
    def __init__(self, percorso="audit_trail.jsonl", orologio=None):
        self.path = Path(percorso)
        self._ora = orologio or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._cache_verifica = None
        self._ultimo_hash = self._recupera_ultimo_hash()

    # ------------------------------------------------------------------ scrittura
    def log(self, attore: str, evento: str, raccomandazione=None, extra: dict = None):
        with self._lock:
            record = {
                "timestamp": self._ora().isoformat(timespec="milliseconds"),
                "attore": attore,                  # "SISTEMA" oppure id operatore
                "evento": evento,
                "hash_precedente": self._ultimo_hash,
            }
            if raccomandazione is not None:
                record["decisione"] = self._decisione(raccomandazione)
            if extra:
                record["extra"] = extra
            # normalizzazione: cio' che viene firmato e' esattamente cio' che verra' riletto
            record = json.loads(json.dumps(record, ensure_ascii=False, default=_json_default))
            record["hash"] = self._hash(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._ultimo_hash = record["hash"]
            self._cache_verifica = None
            return record

    @staticmethod
    def _decisione(r) -> dict:
        def g(nome, default=None):
            v = getattr(r, nome, default)
            return getattr(v, "value", v)
        return {
            "id": r.id,
            "asset_id": r.asset_id,
            "tipo_asset": g("tipo_asset"),
            "area_geografica": g("area_geografica"),
            "criticita_utenza": g("criticita_utenza"),
            "livello": g("livello"),
            "regola": g("regola"),
            "stato": g("stato"),
            "azione": r.azione_proposta,
            "azione_originale": g("azione_originale", r.azione_proposta),
            "prob_guasto": float(r.prob_guasto),
            "confidenza": float(r.confidenza),
            "revisore": g("revisore"),
            "motivazione": g("motivazione"),
            "eseguita": bool(g("eseguita", False)),
        }

    # ------------------------------------------------------------------ verifica
    def verifica_catena(self) -> tuple[bool, int]:
        """Ritorna (integra?, n_record). Da mostrare nella dashboard."""
        esito = self.verifica()
        return esito["integra"], esito["n_record"]

    def verifica(self) -> dict:
        """Verifica completa: catena di hash + ancora del processo in esecuzione."""
        with self._lock:
            chiave = self._firma_file()
            if self._cache_verifica and self._cache_verifica[0] == chiave:
                return dict(self._cache_verifica[1])
            esito = self._verifica_righe(self._righe())
            if esito["integra"] and esito["ultimo_hash"] != self._ultimo_hash:
                esito.update(integra=False, record_non_valido=esito["n_record"] + 1,
                             motivo="il file non termina con l'ultimo record scritto dal sistema "
                                    "(record rimossi, aggiunti o catena ricalcolata dall'esterno)")
            esito["verificato_il"] = self._ora().isoformat(timespec="seconds")
            esito["file"] = self.path.name
            self._cache_verifica = (chiave, esito)
            return dict(esito)

    def _verifica_righe(self, righe: list[str]) -> dict:
        precedente, n = GENESI, 0
        for riga in righe:
            try:
                rec = json.loads(riga)
                atteso = rec.pop("hash")
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._esito_ko(n, "riga illeggibile o senza hash")
            if rec.get("hash_precedente") != precedente:
                return self._esito_ko(n, "catena interrotta: hash_precedente non corrisponde al record precedente")
            if self._hash(rec) != atteso:
                return self._esito_ko(n, "contenuto alterato: l'hash del record non corrisponde")
            precedente = atteso
            n += 1
        return {"integra": True, "n_record": n, "ultimo_hash": precedente,
                "record_non_valido": None, "motivo": None}

    @staticmethod
    def _esito_ko(n_validi: int, motivo: str) -> dict:
        return {"integra": False, "n_record": n_validi, "ultimo_hash": None,
                "record_non_valido": n_validi + 1, "motivo": motivo}

    def simula_manomissione(self) -> dict:
        """Altera un record su una COPIA in memoria e mostra che la verifica lo rileva.
        Il file reale non viene toccato (la dimostrazione non deve distruggere il log)."""
        with self._lock:
            righe = self._righe()
        if not righe:
            raise ValueError("Log vuoto: nessun record da alterare")
        copia = [json.loads(r) for r in righe]
        indice = next((i for i in range(len(copia) - 1, -1, -1)
                       if (copia[i].get("decisione") or {}).get("motivazione")), len(copia) // 2)
        bersaglio = copy.deepcopy(copia[indice])
        if (bersaglio.get("decisione") or {}).get("motivazione"):
            campo, originale = "decisione.motivazione", bersaglio["decisione"]["motivazione"]
            copia[indice]["decisione"]["motivazione"] = "(modificata a posteriori)"
        else:
            campo, originale = "attore", bersaglio.get("attore")
            copia[indice]["attore"] = "OP-FALSO"
        esito = self._verifica_righe([json.dumps(r, ensure_ascii=False) for r in copia])
        return {"record_alterato": indice + 1, "campo": campo, "valore_originale": originale,
                "valore_alterato": "(modificata a posteriori)" if campo != "attore" else "OP-FALSO",
                "esito_sulla_copia": esito,
                "nota": "Simulazione su una copia in memoria: il file di log reale non e' stato modificato."}

    # ------------------------------------------------------------------ lettura
    def record(self) -> list[dict]:
        """Tutti i record leggibili (le righe corrotte sono saltate: le segnala verifica())."""
        out = []
        for i, riga in enumerate(self._righe(), start=1):
            try:
                rec = json.loads(riga)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rec["n"] = i
                out.append(rec)
        return out

    def cerca(self, testo: str = None, asset_id: str = None, attore: str = None,
              evento: str = None, decisione_id: str = None, da=None, a=None,
              limite: int = 300) -> dict:
        """Filtro del log per asset, operatore, evento, periodo e testo libero (piu' recenti prima)."""
        t_da, t_a = _parse_ts(da), _parse_ts(a)
        testo = (testo or "").strip().lower()
        out = []
        for rec in reversed(self.record()):
            dec, ex = rec.get("decisione") or {}, rec.get("extra") or {}
            if asset_id and asset_id.strip().lower() not in {
                    str(dec.get("asset_id", "")).lower(), str(ex.get("asset_id", "")).lower()}:
                continue
            if attore and attore.strip().lower() not in str(rec.get("attore", "")).lower():
                continue
            if evento and evento.strip().lower() not in str(rec.get("evento", "")).lower():
                continue
            if decisione_id and decisione_id not in (dec.get("id"), ex.get("decisione_id")):
                continue
            if t_da or t_a:
                ts = _parse_ts(rec.get("timestamp"))
                if ts is None or (t_da and ts < t_da) or (t_a and ts > t_a):
                    continue
            if testo and testo not in json.dumps(rec, ensure_ascii=False).lower():
                continue
            out.append(rec)
        return {"totale": len(out), "record": out[:limite]}

    def ricostruisci(self, asset_id: str) -> dict:
        """Chi ha deciso, cosa, quando e perche', per ogni decisione su un asset (test T6)."""
        chiave = asset_id.strip().lower()
        gruppi: dict[str, list] = {}
        for rec in self.record():
            dec, ex = rec.get("decisione") or {}, rec.get("extra") or {}
            if chiave not in (str(dec.get("asset_id", "")).lower(), str(ex.get("asset_id", "")).lower()):
                continue
            did = dec.get("id") or ex.get("decisione_id") or "-"
            gruppi.setdefault(did, []).append(rec)
        decisioni = [self._sintesi(did, recs) for did, recs in gruppi.items()]
        decisioni.sort(key=lambda d: d["creata_il"] or "", reverse=True)
        return {"asset_id": asset_id.strip(), "decisioni": decisioni,
                "n_record": sum(len(v) for v in gruppi.values()), "integrita": self.verifica()}

    @staticmethod
    def _sintesi(did: str, recs: list[dict]) -> dict:
        decs = [r["decisione"] for r in recs if r.get("decisione")]
        ultima = decs[-1] if decs else {}
        prima = decs[0] if decs else {}
        revisioni = [r for r in recs if r["evento"] in
                     ("revisione_APPROVATA", "revisione_MODIFICATA", "revisione_RIFIUTATA")]
        eseguita = [r for r in recs if r["evento"] == "esecuzione_azione"]
        escalation = [r for r in recs if r["evento"] in ("revisione_ESCALATION", "escalation_SLA")]
        contestazioni = [r for r in recs if r["evento"] == "contestazione_HOTL"]
        respinti = [r for r in recs if r["evento"] == "rubber_stamping_bloccato"]
        spiegazioni = [{"fonte": (r.get("extra") or {}).get("fonte"), "timestamp": r["timestamp"]}
                       for r in recs if r["evento"].startswith("spiegazione_")]
        azione_ai = ultima.get("azione_originale") or prima.get("azione")
        stato = ultima.get("stato")
        if revisioni:
            rv = revisioni[-1]
            d = rv["decisione"]
            esito = {"revisione_APPROVATA": "approvata senza modifiche",
                     "revisione_MODIFICATA": f"corretta dall'operatore ({azione_ai} -> {d.get('azione')})",
                     "revisione_RIFIUTATA": "rifiutata dall'operatore"}[rv["evento"]]
            chi, quando, perche = rv["attore"], rv["timestamp"], d.get("motivazione")
        elif stato == "AUTO_ESEGUITA":
            esito, chi = "eseguita automaticamente (HOTL, monitorata ex post)", "SISTEMA"
            auto = [r for r in recs if r["evento"] == "auto_esecuzione_HOTL"]
            quando = eseguita[-1]["timestamp"] if eseguita else recs[-1]["timestamp"]
            perche = ((auto[-1].get("extra") or {}).get("motivo_routing") if auto else None) or ultima.get("regola")
        else:
            esito = {"IN_ATTESA": "in attesa di revisione umana", "ESCALATION": "in escalation (supervisore)",
                     "BLOCCATA_STOP": "bloccata da emergency stop"}.get(stato, stato or "-")
            # ultimo cambio di stato: ne' le spiegazioni ne' i tentativi respinti lo modificano
            rif = next((r for r in reversed(recs)
                        if r.get("decisione") and r["evento"] != "rubber_stamping_bloccato"), recs[-1])
            ex = rif.get("extra") or {}
            chi, quando, perche = rif["attore"], rif["timestamp"], ex.get("motivazione") or ex.get("motivo_routing")
        if contestazioni:
            c = contestazioni[-1]
            esito += f"; contestata ex post da {c['attore']}"
        return {
            "decisione_id": did, "asset_id": ultima.get("asset_id"),
            "tipo_asset": ultima.get("tipo_asset"), "area_geografica": ultima.get("area_geografica"),
            "criticita_utenza": ultima.get("criticita_utenza"),
            "livello": ultima.get("livello"), "regola": ultima.get("regola"), "stato": stato,
            "prob_guasto": ultima.get("prob_guasto"), "confidenza": ultima.get("confidenza"),
            "azione_ai": azione_ai, "azione_finale": ultima.get("azione"),
            "esito": esito, "chi": chi, "quando": quando, "perche": perche,
            "eseguita": bool(eseguita), "eseguita_il": eseguita[-1]["timestamp"] if eseguita else None,
            "escalation": [{"attore": e["attore"], "timestamp": e["timestamp"], "evento": e["evento"],
                            "motivazione": (e.get("extra") or {}).get("motivazione")} for e in escalation],
            "contestazioni": [{"attore": c["attore"], "timestamp": c["timestamp"],
                               "motivazione": (c.get("extra") or {}).get("motivazione")} for c in contestazioni],
            "revisioni_respinte": [{"attore": x["attore"], "timestamp": x["timestamp"],
                                    "motivo": (x.get("extra") or {}).get("motivo"),
                                    "esito_tentato": (x.get("extra") or {}).get("esito_tentato"),
                                    "motivazione": (x.get("extra") or {}).get("motivazione")} for x in respinti],
            "spiegazioni": spiegazioni,
            "creata_il": recs[0]["timestamp"],
            "eventi": [{"n": r.get("n"), "timestamp": r["timestamp"], "attore": r["attore"], "evento": r["evento"],
                        "stato": (r.get("decisione") or {}).get("stato"),
                        "motivazione": (r.get("decisione") or {}).get("motivazione") if r["evento"].startswith("revisione_")
                        and r["evento"] != "revisione_ESCALATION" else (r.get("extra") or {}).get("motivazione"),
                        "hash": r.get("hash")} for r in recs],
        }

    def esporta(self) -> str:
        """Contenuto JSONL integrale, verificabile da terzi ricalcolando gli hash."""
        with self._lock:
            return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def archivia(self, attore: str, motivo: str) -> Optional[dict]:
        """Chiude il log corrente rinominandolo con un timestamp: nulla viene cancellato.
        Il nuovo log riparte da GENESI e il chiamante registra il riferimento all'archivio."""
        with self._lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return None
            self.log(attore, "archiviazione_log", extra={"motivazione": motivo})
            esito = self.verifica()
            base = self._ora().strftime("%Y%m%d-%H%M%S")
            dest, i = self.path.with_name(f"{self.path.stem}_{base}{self.path.suffix}"), 1
            while dest.exists():
                dest, i = self.path.with_name(f"{self.path.stem}_{base}_{i}{self.path.suffix}"), i + 1
            hash_finale = self._ultimo_hash
            self.path.replace(dest)
            self._ultimo_hash = GENESI
            self._cache_verifica = None
            return {"file": dest.name, "hash_finale": hash_finale,
                    "n_record": esito["n_record"], "integra": esito["integra"]}

    # ------------------------------------------------------------------ interno
    def _righe(self) -> list[str]:
        if not self.path.exists():
            return []
        return [r for r in self.path.read_text(encoding="utf-8").splitlines() if r.strip()]

    def _leggi(self):
        return self.record()

    def _firma_file(self):
        try:
            st = self.path.stat()
            return (st.st_size, st.st_mtime_ns, self._ultimo_hash)
        except FileNotFoundError:
            return (0, 0, self._ultimo_hash)

    def _recupera_ultimo_hash(self) -> str:
        for riga in reversed(self._righe()):
            try:
                return json.loads(riga)["hash"]
            except (ValueError, KeyError, TypeError):
                continue
        return GENESI

    @staticmethod
    def _hash(record: dict) -> str:
        return hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
