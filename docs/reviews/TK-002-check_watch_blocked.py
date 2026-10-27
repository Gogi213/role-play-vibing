import sys, unittest
sys.path.insert(0, r"C:/visual projects/role-play-vibing/.claude/dispatcher")
import test_tk002 as X
D, T, W = X.D, X.T, X.W
class C(X.Sandbox):
    def test_watch_wakes_ceo_on_executor_blocked(self):
        p = self.ticket(status="in_progress")
        T.append_log(p, "engineer", "[итог: blocked] — вопрос по методике", now=X.T0)
        T.write_header_updates(p, {"status": "blocked", "next": "judge"}, now=X.T0)
        f = W.collect_findings(X.T0)
        print("findings:", [(x.kind, x.key) for x in f])
        W.notify_findings(f, {}, X.T0)
        print("ceo-wake.log:", self.wake_lines())
unittest.main(argv=["x"], verbosity=0)
