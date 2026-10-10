"""Клиент шины для диспетчера (TK-045): long-poll очереди dispatcher, сигнал «проснуться сейчас», ack после тика.
Очередь `ceo` диспетчер только слушает ради будильника (строка в ceo-wake.log, без ack): подтверждает её CEO командой `tickets.py inbox` (В-192).
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
# Простой шины короче порога — переподключение, CEO не будим. 120 с — в разрыве между двумя группами простоев журнала
# ceo-inbox (04–08.10, 35 случаев: 18 — до 63 с, 17 — от 122 с); решение Судьи TK-100 10.10.
DOWN_NOTICE_S = float(os.environ.get("RPV_BUS_DOWN_NOTICE_S", "120"))


class Listener(threading.Thread):
    """Читает очередь `recipient`; новые события кладёт в pending и вызывает on_events(events)."""

    def __init__(self, recipient, on_events, on_state=None, tick=None):
        super().__init__(daemon=True, name=f"bus-{recipient}")
        self.recipient, self.on_events, self.on_state, self.tick = recipient, on_events, on_state, tick
        self.after, self.seen, self.up = 0, set(), None
        self.polls = 0  # число успешных ответов шины; 0 — идущий ответ первый (накопленное до старта)
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
                self.polls += 1
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

    def __init__(self, ceo_line, on_event=None, ceo_wake=None):
        self.on_event = on_event  # callable(event) — wait_for по событию (TK-055), вызывается из потока слушателя
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.to_ack = set()
        self.ceo_line = ceo_line  # callable(kind, note) — сигнал CEO (шина, при её падении — запасной файл)
        self.down_since = None
        self.down_why = ""
        self.down_noted = False  # строка bus-down записана — только тогда bus-up не молчит
        self.last_snapshot = 0.0
        self.disp = Listener("dispatcher", self._on_disp, self._on_state)
        self.ceo_wake = ceo_wake  # callable(addr, seq) — строка будильника CEO; None — очередь ceo не слушаем
        self.ceo = Listener("ceo", self._on_ceo) if ceo_wake else None

    def start(self):
        self.disp.start()
        if self.ceo:
            self.ceo.start()

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

    def _on_ceo(self, events):
        """Будильник на любое событие очереди ceo, кроме `.к_ceo` (его будит append_ceo_inbox при отправке). Без ack.
        Первая пачка после старта диспетчера — одна строка о накопленном, а не по строке на событие."""
        try:
            if self.ceo.polls == 0:  # первый ответ шины после старта — накопленное, одна строка; позже — по строке на событие
                if len(events) > 1:
                    self.ceo_wake(f"в очереди ceo {len(events)} событий", events[-1]["seq"])
                    return
            for e in events:
                if not e["addr"].endswith(".к_ceo"):
                    self.ceo_wake(e["addr"], e["seq"])
        except Exception as ex:
            print(f"[bus] будильник очереди ceo не записан: {type(ex).__name__}: {ex}", file=sys.stderr)

    def _on_state(self, up, why):
        with self.lock:  # состояние простоя трогают поток слушателя и цикл диспетчера (maybe_snapshot)
            if not up and self.down_since is None:
                self.down_since = time.time()
                self.down_why = why
                return
            if not up or self.down_since is None:
                return
            noted, down_for = self.down_noted, int(time.time() - self.down_since)
            self.down_since, self.down_noted = None, False
        if noted:
            self.ceo_line("bus-up", f"шина снова доступна (простой {down_for} с)")
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
        with self.lock:  # шина лежит дольше порога — одна строка CEO (поток слушателя только запоминает)
            since, why = self.down_since, self.down_why
            notice = since is not None and not self.down_noted and time.time() - since >= DOWN_NOTICE_S
            self.down_noted = self.down_noted or notice
        if notice:
            self.ceo_line("bus-down", f"шина недоступна {int(time.time() - since)} с ({why}); "
                                      "диспетчер работает по таймеру и wait_for host:…")
        if time.time() - self.last_snapshot < SNAPSHOT_EVERY_S or self.disp.up is False:
            return
        try:
            busclient.request("/event", {"addr": "служба.блокеры.снимок", "payload": {"blocked": blockers_snapshot(tickets)},
                                         "id": f"snap-{int(time.time())}"}, timeout=3)
            self.last_snapshot = time.time()
        except Exception:
            pass  # устаревший снимок в spool не кладём
