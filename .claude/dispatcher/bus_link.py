"""Клиент шины для диспетчера (TK-045): long-poll очереди dispatcher, сигнал «проснуться сейчас», ack после тика.
Очередь `ceo` диспетчер НЕ читает (В-192): её читает и подтверждает CEO командой `tickets.py inbox`.
Шина лежит — диспетчер работает по таймеру как раньше (обёртка не бросает), CEO получает одну строку за период."""
import os
import sys
import threading
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bus"))
import busclient  # noqa: E402

WAIT_S = 25
SNAPSHOT_EVERY_S = float(os.environ.get("RPV_BUS_SNAPSHOT_S") or os.environ.get("ALPHA_BUS_SNAPSHOT_S", "300"))


class Listener(threading.Thread):
    """Читает очередь `recipient`; новые события кладёт в pending и вызывает on_events(events)."""

    def __init__(self, recipient, on_events, on_state=None, tick=None):
        super().__init__(daemon=True, name=f"bus-{recipient}")
        self.recipient, self.on_events, self.on_state, self.tick = recipient, on_events, on_state, tick
        self.after, self.seen, self.up = 0, set(), None
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            try:
                # связь не подтверждена (старт/после падения) — короткий запрос: «шина снова доступна» без ожидания long-poll
                wait = WAIT_S if self.up else 0
                r = busclient.request(f"/q/{self.recipient}?after={self.after}&wait={wait}", timeout=WAIT_S + 10)
                self._state(True)
                new = [e for e in r["events"] if e["seq"] not in self.seen]
                for e in new:
                    self.seen.add(e["seq"])
                    self.after = max(self.after, e["seq"])
                if new:
                    self.on_events(new)
                if self.tick:
                    self.tick()
            except Exception as e:
                self._state(False, f"{type(e).__name__}: {e}")
                self.stop_flag.wait(5)

    def _state(self, up, why=""):
        if up != self.up:
            self.up = up
            if self.on_state:
                self.on_state(up, why)


def ack(recipient, seqs) -> bool:
    """True — подтверждено (или нечего). False — не дошло: вызывающий обязан вернуть seqs и повторить
    (курсор Listener.after уже ушёл вперёд, шина сама их не выдаст — потерянный ack = «не_обработано», TK-055)."""
    if not seqs:
        return True
    try:
        busclient.request("/ack", {"recipient": recipient, "seqs": sorted(seqs)}, timeout=5)
        return True
    except Exception:
        return False


def blockers_snapshot(tickets) -> dict:
    """{TK: причина} для тикетов, чьи события держим: blocked/needs_owner или waiting на незакрытый ticket:<ID>."""
    done = {t.id for t in tickets if t.status == "done"}
    out = {}
    for t in tickets:
        if t.status in ("blocked", "needs_owner"):
            out[t.id] = t.status
        elif t.status == "waiting" and (t.header.get("wait_for") or "").startswith("ticket:"):
            dep = t.header["wait_for"].split(":", 1)[1].strip()
            if dep and dep not in done:
                out[t.id] = f"depends:{dep}"
    return out


class Link:
    """Состояние связи для цикла диспетчера: wake.wait(...) вместо sleep; ack — после законченного тика."""

    def __init__(self, ceo_line, on_event=None):
        self.on_event = on_event  # callable(event) — wait_for по событию (TK-055), вызывается из потока слушателя
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.to_ack = set()
        self.ceo_line = ceo_line  # callable(kind, note) — сигнал CEO (шина, при её падении — запасной файл)
        self.down_since = None
        self.last_snapshot = 0.0
        self.disp = Listener("dispatcher", self._on_disp, self._on_state)

    def start(self):
        self.disp.start()

    def _on_disp(self, events):
        if self.on_event:
            for e in events:
                try:
                    self.on_event(e)
                except Exception:
                    pass  # разбор события не должен терять ack/пробуждение
        with self.lock:
            self.to_ack.update(e["seq"] for e in events)
        self.wake.set()

    def _on_state(self, up, why):
        if not up and self.down_since is None:
            self.down_since = time.time()
            self.ceo_line("bus-down", f"шина недоступна ({why}); диспетчер работает по таймеру и wait_for host:…")
        elif up and self.down_since is not None:
            self.ceo_line("bus-up", f"шина снова доступна (простой {int(time.time() - self.down_since)} с)")
            self.down_since = None
            self.last_snapshot = 0.0  # снимок блокеров сразу после возврата
            self.wake.set()

    def take_ack(self):
        with self.lock:
            s, self.to_ack = self.to_ack, set()
        return s

    def give_back(self, seqs):
        with self.lock:
            self.to_ack.update(seqs)

    def maybe_snapshot(self, tickets):
        if time.time() - self.last_snapshot < SNAPSHOT_EVERY_S or self.disp.up is False:
            return
        try:
            busclient.request("/event", {"addr": "служба.блокеры.снимок", "payload": {"blocked": blockers_snapshot(tickets)},
                                         "id": f"snap-{int(time.time())}"}, timeout=3)
            self.last_snapshot = time.time()
        except Exception:
            pass  # устаревший снимок в spool не кладём
