"""Детектор «диспетчер слеп» (TK-217, В-229 п.1): недоступность цели (ssh к машине с живыми wait_for) дольше порога —
тревога по КАЖДОЙ цели отдельно, каналом, не зависящим от этих машин (файл + звук на ПК + строка ceo-wake.log).
Решение — чистое (`Tracker.evaluate`), действия (файл/звук/лог) — в dispatch. Порог — RPV_BLIND_ALERT_S (по умолчанию 300 с,
вердикт Судьи TK-217 12.10); повтор тревоги — RPV_BLIND_REPEAT_S (1800 с)."""
import threading

ALERT_S = 300.0
REPEAT_S = 1800.0


class Tracker:
    def __init__(self, alert_s: float = ALERT_S, repeat_s: float = REPEAT_S):
        self.alert_s, self.repeat_s = alert_s, repeat_s
        self.down = {}  # цель -> {"since": ts, "alerted": ts|None}
        self.lock = threading.Lock()

    def record(self, target: str, ok: bool, now: float) -> None:
        with self.lock:
            if ok:
                ent = self.down.get(target)
                if ent is not None:
                    ent["back"] = ent.get("back") or now  # вернулась; evaluate выдаст "recovered" и забудет
            elif target not in self.down:
                self.down[target] = {"since": now, "alerted": None}
            else:
                self.down[target].pop("back", None)

    def any_down(self) -> bool:
        with self.lock:
            return any("back" not in e for e in self.down.values())

    def evaluate(self, now: float, active) -> list:
        """[(действие, цель, секунд_простоя)]: alert — впервые за порогом; repeat — раз в repeat_s; recovered — цель снова
        отвечает (после тревоги). Цель вне `active` (нет живых ожиданий) не тревожит и забывается."""
        out = []
        with self.lock:
            for tgt in list(self.down):
                ent = self.down[tgt]
                if tgt not in active:
                    del self.down[tgt]
                    continue
                if "back" in ent:
                    if ent["alerted"] is not None:
                        out.append(("recovered", tgt, int(ent["back"] - ent["since"])))
                    del self.down[tgt]
                    continue
                gone = now - ent["since"]
                if gone < self.alert_s:
                    continue
                if ent["alerted"] is None:
                    out.append(("alert", tgt, int(gone)))
                    ent["alerted"] = now
                elif now - ent["alerted"] >= self.repeat_s:
                    out.append(("repeat", tgt, int(gone)))
                    ent["alerted"] = now
        return out

    def alerted_targets(self) -> dict:
        with self.lock:
            return {t: e["since"] for t, e in self.down.items() if e["alerted"] is not None and "back" not in e}
