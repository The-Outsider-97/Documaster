"""Agent integration with injected stand-ins: what Documaster does with an agent's answer - and what it does when there is none.

These tests do not claim anything about the real SLAI agents; they check the rules around them (gate, budget, circuit breaker, no
invented answers). The real agents are exercised separately by ``tools/probe_agents.py`` where they can be loaded.
"""
import time
import types
import unittest

from support import APP_ROOT  # noqa: F401  (puts the app on sys.path)

from slai.analysis import agent_calls as ac
from slai.bridge.agents import AgentGateway
from slai.config import Settings
from slai.errors import AgentUnavailable, TimedOut


def svc(mode="auto", timeout=2.0):
    s = Settings()
    s.agents = mode
    return types.SimpleNamespace(settings=s, agents=AgentGateway(mode=mode, timeout_s=timeout))


class FakePrivacy:
    def __init__(self, decision="allow", boom=False):
        self.decision, self.boom, self.seen = decision, boom, []

    def evaluate_privacy(self, payload, **kw):
        if self.boom:
            raise RuntimeError("privacy exploded")
        self.seen.append((payload, kw))
        return {"decision": self.decision, "summary": "s", "stages": {"identification": {"sensitivity_score": 0.8, "entity_count": 3}}}

    def register_consent(self, **kw):
        return {"id": "c1"}

    def bind_purpose(self, **kw):
        return {"id": "p1"}


class FakeLanguage:
    def __init__(self, **answers):
        self.answers = answers

    def classify_text(self, text, labels):
        return self.answers["classify"]

    def rerank_texts(self, query, candidates, top_k):
        return self.answers["rerank"]

    def summarize_text(self, text, max_length):
        return self.answers["summary"]


class GateTests(unittest.TestCase):
    def test_nothing_is_forwarded_without_consent(self):
        s, c = svc(), ac.Consent()
        s.agents.inject("privacy", FakePrivacy())
        g = ac.privacy_gate(s, c, "d1", "hello", 0)
        self.assertFalse(g["forward"])
        self.assertEqual(g["decision"], "no-consent")
        self.assertEqual(s.agents.slots["privacy"].calls, 0, "the Privacy agent must not even see text before consent")

    def test_consent_is_recorded_with_the_privacy_agent(self):
        s, c = svc(), ac.Consent()
        s.agents.inject("privacy", FakePrivacy())
        note = c.grant(s, "d1")
        self.assertEqual(note.status, "used")
        self.assertTrue(c.is_granted("d1"))
        c.revoke(s, "d1")
        self.assertFalse(c.is_granted("d1"))

    def test_agents_are_allowed_by_default_and_consent_is_recorded_on_first_use(self):
        s, c = svc(), ac.Consent(default=True)
        priv = FakePrivacy()
        registered = []
        priv.register_consent = lambda **kw: registered.append(kw) or {"id": "c"}
        s.agents.inject("privacy", priv)
        self.assertTrue(c.is_granted("d1"))
        g = ac.privacy_gate(s, c, "d1", "plain text", 0)
        self.assertTrue(g["forward"])
        self.assertEqual(len(registered), 1, "the permission is recorded with the Privacy agent before it evaluates")
        ac.privacy_gate(s, c, "d1", "plain text", 0)
        self.assertEqual(len(registered), 1, "and only once per document")

    def test_a_person_can_still_turn_agents_off_for_one_document(self):
        s, c = svc(), ac.Consent(default=True)
        s.agents.inject("privacy", FakePrivacy())
        c.revoke(s, "d1")
        g = ac.privacy_gate(s, c, "d1", "plain text", 0)
        self.assertEqual(g["decision"], "no-consent")
        self.assertIn("turned off", g["reason"])
        self.assertTrue(c.is_granted("d2"), "other documents keep the default")

    def test_self_test_reports_what_each_agent_really_returned(self):
        s = svc()

        class Discriminating(FakePrivacy):
            def evaluate_privacy(self, payload, **kw):
                self.decision = "modify" if "@" in payload["content"] else "allow"
                return super().evaluate_privacy(payload, **kw)

        s.agents.inject("privacy", Discriminating())
        s.agents.inject("safety", types.SimpleNamespace(perform_task=lambda text, ctx: {"decision": "allow"}))
        s.agents.inject("language", types.SimpleNamespace(
            classify_text=lambda text, labels: {"label": "summarize", "score": 0.9},
            rerank_texts=lambda q, c, top_k: {"ranked": [{"index": 1}, {"index": 0}]},
            summarize_text=lambda text, max_length: "The committee met to review the annual budget and approved the hiring plan."))
        s.agents.inject("quality", types.SimpleNamespace(perform_task=lambda d: {"status": "ok"}))
        res = ac.self_test(s)
        self.assertEqual({k: v["ok"] for k, v in res.items()}, {"privacy": True, "safety": True, "language": True, "quality": True})
        self.assertEqual(s.agents.slots["language"].view()["test"]["ok"], True)

    def test_warm_up_and_a_first_request_build_each_agent_once(self):
        import threading, time
        gw = AgentGateway(mode="auto", timeout_s=2.0)
        created = []

        class Factory:
            def create(self, role, **kw):
                created.append(role)
                time.sleep(0.2)
                return types.SimpleNamespace(role=role)

        gw.factory, gw._initialised = Factory(), True
        warm = gw.warm_up(["language"], log=lambda m: None)
        t = threading.Thread(target=lambda: gw.ensure("language"))
        t.start()
        warm.join(5)
        t.join(5)
        self.assertEqual(created, ["language"])
        self.assertEqual(gw.slots["language"].state, "ready")

    def test_a_privacy_agent_that_allows_personal_data_does_not_pass_the_self_test(self):
        s = svc()
        s.agents.inject("privacy", FakePrivacy("allow"))
        res = ac.self_test(s, ["privacy"])
        self.assertFalse(res["privacy"]["ok"])
        self.assertIn("expected", res["privacy"]["detail"])

    def test_self_test_shows_a_failing_agent_as_failed_not_as_a_pass(self):
        s = svc()
        s.agents.slots["safety"].state, s.agents.slots["safety"].error = "unavailable", "no torch"
        res = ac.self_test(s, ["safety"])
        self.assertFalse(res["safety"]["ok"])
        self.assertIn("no torch", res["safety"]["detail"])

    def test_the_privacy_agent_decides_and_its_reason_is_shown(self):
        s, c = svc(), ac.Consent()
        p = FakePrivacy("deny")
        s.agents.inject("privacy", p)
        c.grant(s, "d1")
        g = ac.privacy_gate(s, c, "d1", "my secret text", 0)
        self.assertFalse(g["forward"])
        self.assertIn("deny", g["reason"])
        self.assertEqual(p.seen[0][0], {"content": "my secret text"})
        s2 = svc(); s2.agents.inject("privacy", FakePrivacy("allow")); c.grant(s2, "d2")
        self.assertTrue(ac.privacy_gate(s2, c, "d2", "fine", 0)["forward"])

    def test_a_modify_decision_means_the_text_is_not_forwarded(self):
        """The real Privacy agent answers 'modify' for text with personal data and tokenises the whole content."""
        s, c = svc(), ac.Consent()
        s.agents.inject("privacy", FakePrivacy("modify"))
        c.grant(s, "d1")
        g = ac.privacy_gate(s, c, "d1", "mail me at a@b.co", 1)
        self.assertFalse(g["forward"])
        self.assertEqual(g["decision"], "modify")
        self.assertIn("sanitised", g["reason"])

    def test_without_a_privacy_agent_personal_data_stays_local(self):
        s, c = svc(), ac.Consent()
        s.agents.slots["privacy"].state, s.agents.slots["privacy"].error = "unavailable", "ImportError: torch"
        c.grant(s, "d1")
        blocked = ac.privacy_gate(s, c, "d1", "mail me at a@b.co", 1)
        self.assertFalse(blocked["forward"])
        self.assertEqual(blocked["decision"], "blocked-locally")
        self.assertIn("torch", blocked["reason"])
        clean = ac.privacy_gate(s, c, "d1", "plain text", 0)
        self.assertTrue(clean["forward"])
        self.assertEqual(clean["note"].status, "unavailable")      # still reported as unavailable, not as "used"

    def test_a_crashing_privacy_agent_is_treated_as_unavailable(self):
        s, c = svc(), ac.Consent()
        s.agents.inject("privacy", FakePrivacy(boom=True))
        c.grant(s, "d1")
        g = ac.privacy_gate(s, c, "d1", "x@y.zz", 1)
        self.assertFalse(g["forward"])


class AnswerHandlingTests(unittest.TestCase):
    def lang(self, **answers):
        s = svc()
        s.agents.inject("language", FakeLanguage(**answers))
        return s

    def test_classification_accepts_the_documented_shapes(self):
        labels = ["summarize", "convert"]
        for out in ({"label": "convert", "score": 0.9}, {"scores": {"summarize": 0.1, "convert": 0.8}}, {"labels": [{"label": "summarize", "score": 0.7}]}, "convert"):
            n = ac.classify_intent(self.lang(classify=out), "x", labels)
            self.assertEqual(n.status, "used", out)
            self.assertIn(n.output["label"], labels)

    def test_a_label_the_caller_did_not_offer_is_not_accepted(self):
        n = ac.classify_intent(self.lang(classify={"label": "format_disk", "score": 0.99}), "x", ["summarize"])
        self.assertEqual(n.status, "failed")
        self.assertIsNone(n.output)

    def test_rerank_order_is_validated(self):
        s = self.lang(rerank={"ranked": [{"index": 2}, {"index": 0}]})
        note, order = ac.rerank(s, "q", ["a", "b", "c"], 2)
        self.assertEqual(order, [2, 0])
        bad, none = ac.rerank(self.lang(rerank={"ranked": [{"index": 9}]}), "q", ["a"], 1)
        self.assertEqual(bad.status, "failed")
        self.assertIsNone(none)

    def test_rerank_answers_in_other_shapes_are_understood(self):
        cands = ["alpha", "beta", "gamma"]
        shapes = [({"results": [{"candidate_index": 2, "score": 0.9}, {"candidate_index": 0, "score": 0.1}]}, [2, 0]),
                  ({"ranked": [{"text": "gamma"}, {"text": "alpha"}]}, [2, 0]),
                  ({"scores": [0.1, 0.2, 0.9]}, [2, 1, 0]),
                  ({"query": "q", "results": [[1, 0.8], [0, 0.2]]}, [1, 0])]
        for answer, expected in shapes:
            note, order = ac.rerank(self.lang(rerank=answer), "q", cands, 2)
            self.assertEqual(order, expected, answer)
        note, order = ac.rerank(self.lang(rerank={"weird": "yes"}), "q", cands, 2)
        self.assertIsNone(order)
        self.assertIn("weird", note.reason, "the raw answer is shown so a mismatch can be diagnosed")

    def test_quality_gets_a_dataset_id(self):
        seen = {}
        s = svc()
        s.agents.inject("quality", types.SimpleNamespace(perform_task=lambda d: seen.update(d) or {"status": "ok"}))
        note = ac.quality_review(s, [{"page": 1}], dataset_id="documaster:x")
        self.assertEqual(note.status, "used")
        self.assertEqual(seen["dataset_id"], "documaster:x")
        self.assertTrue(seen["batch_id"])

    def test_the_privacy_gate_does_not_declare_the_text_sensitive_itself(self):
        s, c = svc(), ac.Consent(default=True)
        priv = FakePrivacy()
        s.agents.inject("privacy", priv)
        ac.privacy_gate(s, c, "d1", "plain", 0)
        self.assertNotIn("sensitive_fields", priv.seen[0][1])

    def test_an_abstract_summary_must_be_shorter_than_the_text_and_is_labelled_by_the_caller(self):
        text = "a short but real summary of the text here " * 20
        ok = ac.abstract_summary(self.lang(summary="a short but real summary of the text here"), text, 30)
        self.assertEqual(ok.status, "used")
        self.assertEqual(ok.output["text"], "a short but real summary of the text here")
        longer = ac.abstract_summary(self.lang(summary="short " * 400), text, 30)
        self.assertEqual(longer.status, "failed")
        empty = ac.abstract_summary(self.lang(summary=""), text, 30)
        self.assertEqual(empty.status, "failed")

    def test_an_abstract_summary_whose_words_are_not_in_the_document_is_discarded(self):
        text = "the committee reviewed the annual budget and approved the hiring plan " * 10
        bad = ac.abstract_summary(self.lang(summary="purple elephants dance quietly beside rivers tonight"), text, 30)
        self.assertEqual(bad.status, "failed")
        self.assertIn("cannot be trusted", bad.reason)
        good = ac.abstract_summary(self.lang(summary="the committee approved the annual budget and hiring plan"), text, 30)
        self.assertEqual(good.status, "used")
        self.assertGreaterEqual(good.output["grounded"], 0.6)

    def test_safety_decision_blocks(self):
        for out, blocks in (({"decision": "block"}, True), ({"blockers": ["injection"]}, True), ({"decision": "allow"}, False)):
            s = svc()
            s.agents.inject("safety", types.SimpleNamespace(perform_task=lambda text, ctx, o=out: o))
            self.assertEqual(ac.safety_blocks(ac.safety_review(s, "t")), blocks, out)

    def test_no_safety_agent_never_counts_as_a_pass(self):
        s = svc()
        s.agents.slots["safety"].state, s.agents.slots["safety"].error = "unavailable", "no model"
        n = ac.safety_review(s, "t")
        self.assertEqual(n.status, "unavailable")
        self.assertFalse(ac.safety_blocks(n))
        self.assertIsNone(n.output)


class GatewayTests(unittest.TestCase):
    def test_agents_none_switches_everything_off(self):
        s = svc("none")
        n = ac.safety_review(s, "t")
        self.assertEqual(n.status, "skipped")
        self.assertIn("none", n.reason)

    def test_strict_mode_turns_a_missing_agent_into_an_error(self):
        s = svc("strict")
        s.agents.slots["language"].state, s.agents.slots["language"].error = "unavailable", "gone"
        with self.assertRaises(AgentUnavailable):
            ac.classify_intent(s, "x", ["a"])

    def test_a_slow_agent_is_abandoned_not_waited_for(self):
        s = svc()
        s.agents.inject("language", types.SimpleNamespace(classify_text=lambda text, labels: time.sleep(1.5)))
        t0 = time.perf_counter()
        n = ac._run(s, "language", lambda a: a.classify_text("x", ["a"]), 0.2)       # the budget of the call
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(n.status, "failed")
        self.assertIn("did not answer", n.reason)
        time.sleep(1.5)                                  # let the abandoned call finish so the lock is free

    def test_the_circuit_breaker_pauses_a_failing_agent_and_says_so(self):
        s = svc()
        def boom(text, labels):
            raise ValueError("nope")
        s.agents.inject("language", types.SimpleNamespace(classify_text=boom))
        for _ in range(3):
            self.assertEqual(ac.classify_intent(s, "x", ["a"]).status, "failed")
        n = ac.classify_intent(s, "x", ["a"])
        self.assertIn(n.status, ("unavailable",))
        self.assertIn("paused", n.reason)
        self.assertEqual(s.agents.slots["language"].calls, 3)

    def test_failures_do_not_leak_tracebacks_into_the_notes(self):
        s = svc()
        s.agents.inject("language", types.SimpleNamespace(classify_text=lambda t, l: (_ for _ in ()).throw(ValueError("x" * 1000))))
        n = ac.classify_intent(s, "x", ["a"])
        self.assertLess(len(n.reason), 400)
        self.assertNotIn("Traceback", n.reason)

    def test_notes_name_the_agent_that_answered(self):
        s = svc()
        s.agents.inject("language", FakeLanguage(classify={"label": "a", "score": 1}))
        n = ac.classify_intent(s, "x", ["a"])
        self.assertEqual((n.role, n.agent), ("language", "FakeLanguage"))

    def test_observability_is_fire_and_forget(self):
        s = svc()
        got = []
        s.agents.inject("observability", types.SimpleNamespace(perform_task=lambda payload: got.append(payload)))
        t0 = time.perf_counter()
        ac.observe(s, "convert", 12.5, True, "done")
        self.assertLess(time.perf_counter() - t0, 0.2)
        for _ in range(50):
            if got:
                break
            time.sleep(0.05)
        self.assertEqual(got[0]["operation_name"], "convert")

    def test_failure_normalisation_is_none_when_there_is_no_handler(self):
        s = svc()
        s.agents.slots["handler"].state, s.agents.slots["handler"].error = "unavailable", "x"
        self.assertIsNone(ac.normalise_failure(s, RuntimeError("boom"), "convert"))

    def test_failure_normalisation_keeps_only_known_fields(self):
        s = svc()
        s.agents.inject("handler", types.SimpleNamespace(failure_normalization=lambda error, context: {"category": "io", "severity": "low", "retryable": True, "secret": "x"}))
        out = ac.normalise_failure(s, OSError("disk"), "save")
        self.assertEqual(out, {"category": "io", "severity": "low", "retryable": True})

    def test_the_status_report_lists_every_role_honestly(self):
        s = svc("none")
        st = s.agents.status()
        self.assertEqual(set(st["agents"]), {"language", "reader", "privacy", "safety", "quality", "observability", "handler", "planning"})
        self.assertTrue(all(v["state"] == "disabled" for v in st["agents"].values()))


if __name__ == "__main__":
    unittest.main()
