import unittest
from dataclasses import replace

from reference.core import (
    Alias, Evidence, Incoming, Session, close_reason, decide,
    flatten_text, may_publish, pack_evidence, reciprocal_rank_fusion,
    select_export, user_id, utf16_slice,
)


def msg(mid, author, text, timestamp=1000, **kwargs):
    return dict(id=mid, type="message", from_id=author, text=text,
                date_unixtime=str(timestamp), **kwargs)


class ImportTests(unittest.TestCase):
    def select(self, rows, **kwargs):
        return select_export(rows, target_id=17, since=900, until=2000, **kwargs)

    def test_rich_text_not_stringified(self):
        self.assertEqual(flatten_text(["Привет, ", {"type": "bold", "text": "Макс"}]),
                         "Привет, Макс")

    def test_invalid_fragment_rejected(self):
        with self.assertRaises(ValueError):
            flatten_text([{"type": "bold"}])

    def test_unknown_and_channel_are_not_users(self):
        self.assertIsNone(user_id("channel17"))
        self.assertIsNone(user_id(None))
        self.assertEqual(user_id("user17"), 17)

    def test_self_reply_mention_separate(self):
        rows = [msg(1, "user17", "Обещаю выиграть"),
                msg(2, "user18", "Ну да", reply_to_message_id=1),
                msg(3, "user19", [{"type": "mention_name", "text": "Макс", "user_id": 17}]),
                msg(4, "user20", "Макс вообще молодец"),
                msg(5, "user17", "Чужая цитата", forwarded_from="Другой")]
        selected = self.select(rows)
        self.assertEqual([(r.message_id, r.relation) for r in selected],
                         [(1, "self"), (2, "reply"), (3, "mention")])
        self.assertEqual(selected[1].author_id, 18)

    def test_alias_requires_date_approval(self):
        text = [{"type": "mention", "text": "@MAXWELL17"}]
        rows = [msg(1, "user18", text, 1000), msg(2, "user18", text, 1500)]
        self.assertEqual(self.select(rows), [])
        selected = self.select(rows, approved_aliases=[Alias("maxwell17", 900, 1200)])
        self.assertEqual([r.message_id for r in selected], [1])

    def test_same_name_not_identity(self):
        self.assertEqual(self.select([msg(1, "user18", "Привет", **{"from": "Макс"})]), [])

    def test_neighbors_do_not_become_target_quotes(self):
        rows = [msg(1, "user18", "игра", 999), msg(2, "user17", "Я лучший", 1000),
                msg(3, "user18", "позже", 1400)]
        selected = self.select(rows, neighbors=1)
        self.assertEqual([(r.message_id, r.relation) for r in selected],
                         [(1, "context"), (2, "self")])

    def test_neighbors_stay_in_topic(self):
        rows = [msg(1, "user18", "чужая тема", message_thread_id=2),
                msg(2, "user17", "моя", message_thread_id=1)]
        self.assertEqual(len(self.select(rows, neighbors=1)), 1)

    def test_range_and_explicit_selection(self):
        rows = [msg(1, "user17", "старое", 899), msg(2, "user17", "/roast", 900),
                msg(3, "user17", "граница", 2000)]
        selected = self.select(rows, selected_ids={2, 3})
        self.assertEqual([r.message_id for r in selected], [2])
        self.assertEqual(selected[0].text, "/roast")  # data, no command side effect

    def test_missing_parent_does_not_invent_subject(self):
        self.assertEqual(self.select([msg(1, "user18", "ответ", reply_to_message_id=99)]), [])

    def test_duplicate_source_id_rejected(self):
        with self.assertRaises(ValueError):
            self.select([msg(1, "user17", "a"), msg(1, "user17", "b")])

    def test_unknown_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            self.select([msg(1, "user17", "a", "unknown")])


class RetrievalTests(unittest.TestCase):
    def test_utf16_emoji_before_mention(self):
        self.assertEqual(utf16_slice("😀 @maxwell17", 3, 10), "@maxwell17")

    def test_utf16_split_surrogate_rejected(self):
        with self.assertRaises(UnicodeDecodeError):
            utf16_slice("😀", 0, 1)

    def test_rrf_overlap_wins_and_duplicate_not_boosted(self):
        scores = reciprocal_rank_fusion([(1, ["a", "a", "b"]), (1, ["b", "c"])])
        self.assertEqual(scores[0][0], "b")
        self.assertAlmostEqual(dict(scores)["a"], 1 / 61)

    def test_packer_scope_duplicates_budget_and_diversity(self):
        rows = [Evidence("foreign-chat", 2, 17, "Секрет", "x"),
                Evidence("foreign-target", 1, 18, "Чужое", "x"),
                Evidence("a", 1, 17, "Ёж", "game"),
                Evidence("duplicate", 1, 17, "еж", "game"),
                Evidence("b", 1, 17, "Лучший", "game"),
                Evidence("c", 1, 17, "Третий", "game"),
                Evidence("large", 1, 17, "long", "reddit"),
                Evidence("d", 1, 17, "Reddit", "reddit")]
        packed = pack_evidence(rows, chat_id=1, target_id=17, budget=30,
                               token_cost=lambda row: 40 if row.id == "large" else 10)
        self.assertEqual([row.id for row in packed], ["a", "b", "d"])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = Session("s", 1, 0, 17, 18, 3, 4, 1000, 1010, 1)

    def publish(self, session=None, **overrides):
        args = dict(job_session_id="s", job_revision=3, job_epoch=4,
                    job_turn=2, deadline=1060, now=1030)
        args.update(overrides)
        return may_publish(session or self.session, **args)

    def test_target_direct_reply_only(self):
        self.assertEqual(decide(self.session, Incoming(1, 0, 17, True), 1030), "continue")
        self.assertEqual(decide(self.session, Incoming(1, 0, 17, False), 1030), "ignore")
        self.assertEqual(decide(self.session, Incoming(1, 0, 19, True), 1030), "ignore")

    def test_stop_does_not_wait_for_rate_limit(self):
        self.assertEqual(decide(self.session, Incoming(1, 0, 17, True, stop=True), 1011),
                         "close:explicit")

    def test_fast_reply_is_deferred_not_lost(self):
        self.assertEqual(decide(self.session, Incoming(1, 0, 17, True), 1011), "defer")

    def test_other_chat_and_unauthorized_stop_ignored(self):
        self.assertEqual(decide(self.session, Incoming(2, 0, 17, True, stop=True), 1030), "ignore")
        self.assertEqual(decide(self.session, Incoming(1, 0, 19, True, stop=True), 1030), "ignore")

    def test_finish_by_initiator(self):
        self.assertEqual(decide(self.session, Incoming(1, 0, 18, True, finish=True), 1030),
                         "close:explicit")

    def test_silence_is_closure_not_new_punch(self):
        self.assertEqual(close_reason(self.session, 1100), "silence")
        self.assertFalse(self.publish(now=1100, deadline=1200))

    def test_limits(self):
        self.assertEqual(close_reason(replace(self.session, sent_turns=4), 1030), "turn_limit")
        self.assertEqual(close_reason(replace(self.session, last_sent_at=1290), 1300), "ttl")

    def test_stale_results_never_publish(self):
        self.assertTrue(self.publish())
        for overrides in ({"job_revision": 2}, {"job_epoch": 3}, {"job_turn": 1},
                          {"job_session_id": "another"}, {"now": 1060}):
            with self.subTest(overrides=overrides):
                self.assertFalse(self.publish(**overrides))
        self.assertFalse(self.publish(replace(self.session, active=False)))
        self.assertFalse(self.publish(replace(self.session, consenting=False)))


if __name__ == "__main__":
    unittest.main()
