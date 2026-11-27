"""jl.py 測試（標準庫 unittest）。每個測試使用獨立的暫存資料庫與種子目錄，不碰真實進度。

執行：python -X utf8 -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JL = ROOT / "tools" / "jl.py"
sys.path.insert(0, str(JL.parent))
import jl  # noqa: E402  判分函式直接測

SEED = {
    "round_id": "t", "stage": 0,
    "units": [{"unit": "u_c", "kind": "grammar", "gist": "動作地點→で；存在/抵達→に"}],
    "items": [
        {"key": "a_free", "unit": "u_a", "kind": "phrase", "task": "translate",
         "prompt": "A?", "answers": ["お会計お願いします"], "jp": "お会計お願いします",
         "reading": "おかいけいおねがいします", "judge": "考點：結帳的請求",
         "zh": "結帳", "scenario": "s"},
        {"key": "b_free", "unit": "u_b", "kind": "grammar", "task": "reading",
         "prompt": "B?", "answers": ["これをください"], "jp": "これをください", "zh": "給我這個", "scenario": "s"},
        {"key": "c_choice", "unit": "u_c", "task": "choice",
         "prompt": "C?", "choices": ["で", {"text": "に", "unit": "u_ni"}, "が"], "answers": ["で"], "jp": "で",
         "zh": "在", "scenario": "s1"},
        {"key": "d_free", "unit": "u_d", "kind": "word", "task": "meaning",
         "prompt": "D?", "answers": ["找零", "おつり"], "jp": "お釣り", "zh": "找零", "scenario": "s"},
        {"key": "e_long", "unit": "u_e", "kind": "word", "task": "reading",
         "prompt": "E?", "answers": ["おおきい"], "jp": "大きい", "zh": "大", "scenario": "s"},
        {"key": "k_kana", "unit": "u_k", "kind": "kana", "task": "choice",
         "prompt": "K?", "choices": ["nu", "me"], "answers": ["nu"], "jp": "ぬ", "scenario": "s"},
        {"key": "ni_choice", "unit": "u_ni", "kind": "grammar", "task": "choice",
         "prompt": "N?", "choices": ["に", {"text": "で", "unit": "u_c"}], "answers": ["に"], "jp": "に",
         "scenario": "s", "stage": 5},
        {"key": "c_cloze", "unit": "u_c", "task": "cloze",
         "prompt": "公園（ ）遊ぶ", "answers": ["で"], "jp": "公園で遊ぶ", "scenario": "s2", "probe": "遊ぶ"},
    ],
}


def ts_of(s):
    return datetime.fromisoformat(s).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.seed = t / "seed"
        self.seed.mkdir()
        (self.seed / "t.json").write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
        self.db = t / "learning.db"
        self.now = "2026-01-01T10:00"
        self.jl("init")

    def tearDown(self):
        self.tmp.cleanup()

    def jl(self, *args, now=None, ok=True, stdin=None):
        env = dict(os.environ, JL_DB=str(self.db), JL_SEED=str(self.seed),
                   JL_NOW=now or self.now, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-X", "utf8", str(JL), *map(str, args)], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=env)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r

    def sql(self, q, *p):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            rows = con.execute(q, p).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def write(self, data, name="r.json"):
        f = Path(self.tmp.name) / name
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return f

    def add(self, key, now=None):
        out = self.jl("round", "add", key, "--new-round", now=now).stdout
        return int(out.split("added seq ")[1].split()[0])

    def record(self, entries, now=None, ok=True):
        return self.jl("record", stdin=json.dumps(entries, ensure_ascii=False), now=now, ok=ok)

    def answer(self, key, text, outcome="correct", now=None, ok=True, **fields):
        seq = self.add(key, now=now)
        return self.record([dict(seq=seq, answer=text, outcome=outcome, **fields)], now=now, ok=ok)

    def letter_for(self, seq, text):
        shown = json.loads(self.sql("""SELECT shown_choices FROM presentations WHERE seq=?
                                       ORDER BY id DESC LIMIT 1""", seq)[0][0])
        return chr(65 + shown.index(text))

    def state(self, unit, skill="comprehend"):
        r = self.sql("SELECT * FROM unit_state WHERE unit=? AND skill=?", unit, skill)
        return r[0] if r else None

    def last_attempt(self):
        return self.sql("SELECT * FROM v_attempt_effective ORDER BY id DESC LIMIT 1")[0]

    def close_round(self, now=None):
        self.jl("checkpoint", "--summary", "s", now=now)


def item(key):
    it = dict(next(i for i in SEED["items"] if i["key"] == key))
    it["answers"] = json.dumps(it["answers"], ensure_ascii=False)
    choices = it.get("choices")
    it["choices"] = json.dumps([c["text"] if isinstance(c, dict) else c for c in choices],
                               ensure_ascii=False) if choices else None
    it["unit_kind"] = "kana" if key == "k_kana" else "word"
    return it


class TestGradingFunction(unittest.TestCase):
    """程式判分只當稽核（答案有限的題型），但規則仍要正確。"""

    def g(self, key, raw, extra=()):
        return jl.grade_answer(item(key), raw, None, extra)

    def test_exact_and_romaji(self):
        self.assertEqual(self.g("a_free", "お会計お願いします")["result"], "correct")
        self.assertEqual(self.g("a_free", "okaikei onegaishimasu", ["おかいけいおねがいします"])["result"], "correct")

    def test_particle_typed_as_wa_o_is_correct(self):
        for raw in ("これおください", "kore wo kudasai", "kore o kudasai"):
            self.assertEqual(self.g("b_free", raw)["result"], "correct", raw)
        self.assertEqual(self.g("b_free", "これをくだらい")["result"], "incorrect")

    def test_long_vowel_still_counts_when_kana(self):
        self.assertEqual(self.g("e_long", "おおきい")["result"], "correct")
        self.assertEqual(self.g("e_long", "おきい")["result"], "incorrect")

    def test_unknown(self):
        self.assertEqual(self.g("a_free", "不知道")["result"], "unknown")
        self.assertEqual(self.g("a_free", "idk")["result"], "unknown")

    def test_chinese_meaning_exact_only(self):
        self.assertEqual(self.g("d_free", "找零")["result"], "correct")
        self.assertEqual(self.g("d_free", "不是找零")["result"], "ambiguous")

    def test_choice_by_letter_and_text(self):
        it = item("c_choice")
        self.assertEqual(jl.grade_answer(it, "b", ["が", "に", "で"])["matched"], "に")
        self.assertEqual(jl.grade_answer(it, "で", ["が", "に", "で"])["result"], "correct")


class TestRecord(Base):
    def test_outcome_is_ai_judgment_and_audit_for_auto_tasks(self):
        r = self.answer("e_long", "おきい", outcome="correct")
        self.assertIn("AUDIT seq", r.stdout)
        a = self.last_attempt()
        self.assertEqual((a["outcome"], a["grade_result"]), ("correct", "mismatch"))
        r = self.answer("a_free", "お会計をお願いします", outcome="correct")  # 產出題：不稽核
        self.assertNotIn("AUDIT", r.stdout)
        self.assertEqual(self.last_attempt()["grade_result"], "judged")

    def test_choice_letter_maps_to_shown_order_and_confusion_candidate(self):
        seq = self.add("c_choice")
        r = self.record([{"seq": seq, "answer": self.letter_for(seq, "に"), "outcome": "incorrect"}])
        self.assertIn("CONFUSION_CANDIDATE: u_c|u_ni", r.stdout)
        a = self.sql("SELECT picked_text, picked_unit, answer_mode FROM attempts")[0]
        self.assertEqual(tuple(a), ("に", "u_ni", "choice"))
        self.assertEqual(self.sql("SELECT n FROM v_distractor_picks WHERE unit='u_c'")[0][0], 1)

    def test_reports_tags_and_seen_in_one_call(self):
        seq = self.add("a_free")
        self.record([{"seq": seq, "answer": "お会計おください", "outcome": "incorrect", "tags": ["register"],
                      "confidence": "unsure", "after": {"miss": "slip", "note": "漏看"},
                      "seen": [{"term": "お会計", "kind": "word", "where": "prompt"}]}])
        a = self.last_attempt()
        self.assertEqual((a["confidence"], a["miss"], a["tags"], a["has_after_feedback"]),
                         ("unsure", "slip", "register", 1))
        self.assertEqual(self.sql("SELECT attempts FROM v_encounters")[0][0], f"{a['id']}:prompt")

    def test_batch_is_validated_before_writing(self):
        s1, s2 = self.add("e_long"), self.add("b_free")
        bad = [{"seq": s1, "answer": "x", "outcome": "correct"},
               {"seq": s2, "answer": "x", "outcome": "correct", "tags": ["nope"]}]
        self.assertNotEqual(self.record(bad, ok=False).returncode, 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM attempts")[0][0], 0)
        self.assertNotEqual(self.record([{"seq": s1, "answer": "x"}], ok=False).returncode, 0)  # 需要 outcome
        self.record(bad[:1])
        self.assertNotEqual(self.record(bad[:1], ok=False).returncode, 0)  # 同一題不能記兩次

    def test_round_complete_message(self):
        s1 = self.add("e_long")
        out = self.record([{"seq": s1, "answer": "おおきい", "outcome": "correct"}]).stdout
        self.assertIn("ROUND_COMPLETE", out)
        self.assertIn("答對 1", out)  # 本輪答對數由 v_attempt_effective 的 round_id 直接算
        self.assertNotIn("END_OF_REPLY", out)  # 一輪結束時還要 checkpoint 與回報
        r = self.sql("SELECT round_id, seq FROM v_attempt_effective")[0]
        self.assertEqual((r["round_id"], r["seq"]), (1, s1))

    def test_mid_round_record_ends_reply(self):
        s1 = self.add("e_long")
        self.jl("round", "add", "b_free")
        out = self.record([{"seq": s1, "answer": "おおきい", "outcome": "correct"}]).stdout
        last = out.strip().splitlines()[-1]
        self.assertEqual(last.split("：")[0], "END_OF_REPLY")
        self.assertIn("wrap_up", last)  # app 的收尾工具會讓先寫的回饋與下一題消失
        self.assertIn("（已記錄）", last)  # 工具後完全沒文字時 harness 會要求補輸出，模型就重印題目
        self.assertIn("LAST_QUESTION", out)  # 下一題是最後一題：那則回覆先 record、最後才寫回饋＋報告

    def test_no_last_question_hint_earlier(self):
        s1 = self.add("e_long")
        self.jl("round", "add", "b_free")
        self.jl("round", "add", "d_free")
        out = self.record([{"seq": s1, "answer": "おおきい", "outcome": "correct"}]).stdout
        self.assertNotIn("LAST_QUESTION", out)


class TestLearnDay(Base):
    def day(self, when):
        self.answer("e_long", "おおきい", now=when)
        return self.sql("SELECT learn_day FROM attempts ORDER BY id DESC LIMIT 1")[0][0]

    def test_4am_boundary(self):
        self.assertNotEqual(self.day("2026-01-02T03:50"), self.day("2026-01-02T04:10"))
        self.assertEqual(self.day("2026-01-02T23:50"), self.day("2026-01-03T00:10"))

    def test_success_days_use_learn_day(self):
        self.answer("e_long", "おおきい", now="2026-01-02T23:50")
        self.answer("e_long", "おおきい", now="2026-01-03T01:00")
        self.assertEqual(self.state("u_e")["distinct_success_days"], 1)
        self.answer("e_long", "おおきい", now="2026-01-03T09:00")
        self.assertEqual(self.state("u_e")["distinct_success_days"], 2)


class TestScheduling(Base):
    def test_record_creates_state_and_stats(self):
        self.answer("a_free", "お会計お願いします")
        self.assertEqual(self.state("u_a", "produce")["tier"], "learning")
        s = self.sql("SELECT n, n_correct FROM unit_stats WHERE unit='u_a'")[0]
        self.assertEqual((s["n"], s["n_correct"]), (1, 1))

    def test_guess_and_context_are_no_evidence(self):
        self.answer("e_long", "おおきい", confidence="guess")
        self.assertEqual(self.state("u_e")["reps_success"], 0)
        self.assertAlmostEqual(self.state("u_e")["stability_days"], 0.3)
        self.answer("b_free", "これをください", basis="context")
        self.assertEqual(self.state("u_b")["reps_success"], 0)

    def test_after_feedback_report_changes_schedule_not_attempts(self):
        self.answer("e_long", "おおきい")
        self.assertEqual(self.state("u_e")["reps_success"], 1)
        before = [tuple(r) for r in self.sql("SELECT * FROM attempts")]
        self.jl("report", "--confidence", "guess", "--note", "其實是猜的")
        self.assertEqual(before, [tuple(r) for r in self.sql("SELECT * FROM attempts")])
        self.assertEqual(self.state("u_e")["reps_success"], 0)
        self.assertEqual(self.last_attempt()["has_after_feedback"], 1)

    def test_report_merges_and_retracts_fields(self):
        self.answer("e_long", "おおきい", basis="context")
        self.assertEqual(self.state("u_e")["reps_success"], 0)
        self.jl("report", "--confidence", "unsure")
        self.assertEqual((self.last_attempt()["confidence"], self.last_attempt()["basis"]), ("unsure", "context"))
        self.jl("report", "--basis", "none")
        self.assertIsNone(self.last_attempt()["basis"])
        self.assertEqual(self.state("u_e")["reps_success"], 0)  # unsure 仍是弱證據
        self.jl("report", "--confidence", "none")
        self.assertEqual(self.state("u_e")["reps_success"], 1)

    def test_report_outcome_regrades_without_touching_attempts(self):
        self.answer("a_free", "あした7じ", outcome="incorrect", tags=["particle"])
        before = [tuple(r) for r in self.sql("SELECT * FROM attempts")]
        out = self.jl("report", "--outcome", "correct", "--note", "考點以外的錯").stdout
        self.assertIn("(correction)", out)
        self.assertEqual(before, [tuple(r) for r in self.sql("SELECT * FROM attempts")])
        a = self.last_attempt()
        self.assertEqual((a["outcome"], a["recorded_outcome"], a["regraded"]), ("correct", "incorrect", 1))
        self.assertEqual(self.state("u_a", "produce")["reps_success"], 1)
        self.assertEqual(self.sql("SELECT outcome FROM v_tag_counts WHERE tag='particle'")[0][0], "correct")

    def test_same_day_correct_after_failure_is_not_a_success_day(self):
        self.answer("e_long", "おおきい", now="2026-01-01T10:00")
        self.answer("e_long", "おきい", outcome="incorrect", now="2026-01-02T10:00")
        self.answer("e_long", "おおきい", now="2026-01-02T10:20")
        st = self.state("u_e")
        self.assertEqual(json.loads(st["success_days"]), [jl.learn_day(ts_of("2026-01-01T10:00"), 4)])
        self.assertEqual(st["last_fail_day"], jl.learn_day(ts_of("2026-01-02T10:00"), 4))
        self.answer("e_long", "おおきい", now="2026-01-03T10:00")  # 隔天（睡過）才算
        self.assertEqual(self.state("u_e")["distinct_success_days"], 2)

    def test_lapse_counts_only_after_success(self):
        self.answer("e_long", "おきい", outcome="incorrect")
        self.assertEqual(self.state("u_e")["lapses"], 0)
        self.answer("e_long", "おおきい", now="2026-01-02T10:00")
        self.answer("e_long", "おきい", outcome="incorrect", now="2026-01-03T10:00")
        self.assertEqual(self.state("u_e")["lapses"], 1)

    def test_skills_scheduled_separately(self):
        self.answer("c_choice", "で")
        self.assertIsNone(self.state("u_c", "produce"))
        self.answer("c_cloze", "に", outcome="incorrect", tags=["particle"])
        self.assertEqual(self.state("u_c", "produce")["reps_success"], 0)
        self.assertEqual(self.state("u_c", "comprehend")["reps_success"], 1)

    def consolidate_cfg(self):
        for k, v in (("cons_gap_days", "1"), ("cons_min_stability", "0.1"), ("cons_min_days", "3")):
            self.sql("UPDATE meta SET value=? WHERE key=?", v, k)

    def test_comprehend_only_unit_can_consolidate(self):
        self.consolidate_cfg()
        for d in ("2026-01-01T10:00", "2026-01-02T10:00", "2026-01-03T10:00"):
            self.answer("k_kana", "nu", now=d)
        self.assertEqual(self.state("u_k")["tier"], "consolidated")
        self.assertEqual(self.sql("SELECT status FROM v_unit_status WHERE unit='u_k'")[0][0], "consolidated")

    def test_unit_needs_all_skills_to_consolidate(self):
        self.consolidate_cfg()
        for d in ("2026-01-01T10:00", "2026-01-02T10:00", "2026-01-03T10:00"):
            self.answer("c_choice", "で", now=d)
        self.assertEqual(self.state("u_c")["tier"], "consolidated")
        self.assertNotEqual(self.sql("SELECT status FROM v_unit_status WHERE unit='u_c'")[0][0], "consolidated")

    def test_rebuild_reproduces_state(self):
        self.answer("e_long", "おおきい")
        self.answer("e_long", "おきい", outcome="incorrect", now="2026-01-03T10:00")
        q = "SELECT * FROM unit_state ORDER BY unit, skill"
        before = [tuple(r) for r in self.sql(q)]
        self.jl("rebuild")
        self.assertEqual(before, [tuple(r) for r in self.sql(q)])

    def test_no_speed_columns(self):
        cols = {r["name"] for r in self.sql("PRAGMA table_info(attempts)")}
        self.assertFalse({"latency", "latency_s", "quality", "duration"} & cols)

    def test_check_constraints_reject_bad_enum(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO attempts(item_id,unit,ts,learn_day,outcome,answer_mode) VALUES(1,'u_a',0,0,'maybe','jp')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO task_types VALUES('x','speak','auto','d')")


class TestRoundStart(Base):
    def start(self, *a, now=None):
        out = self.jl("round", "start", *a, now=now).stdout
        keys = [line.split("] ")[1].split()[0] for line in out.splitlines() if line.startswith("[")]
        return keys, out

    def test_plans_whole_round_unique_units_and_stage(self):
        keys, out = self.start()
        self.assertTrue(out.startswith("ROUND 1 開始"))
        self.assertNotIn("ni_choice", keys)  # stage 5 > 0
        units = [k for k in keys]
        self.assertEqual(len(units), len(set(units)))
        self.assertFalse({"c_choice", "c_cloze"} <= set(keys))  # 同一 unit 一輪只出一題
        self.assertIn("c_choice", keys)                          # 產出排在理解之後
        self.assertIn("答案", out)
        self.assertIn("判分: 考點：結帳的請求", out)
        self.assertEqual(self.sql("SELECT planned FROM rounds")[0][0], len(keys))

    def test_open_round_lists_pending_instead_of_replanning(self):
        keys, _ = self.start("-n", 3)
        self.record([{"seq": 1, "answer": "x", "outcome": "correct"}])
        _, out = self.start()
        self.assertTrue(out.startswith("ROUND_OPEN"))
        self.assertNotIn("[1]", out)
        self.assertIn("[2]", out)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM rounds")[0][0], 1)
        self.assertIn("已記錄 1/3", self.jl("round", "status").stdout)

    def test_checkpoint_closes_round_and_gate(self):
        self.start("-n", 2)
        self.record([{"seq": 1, "answer": "x", "outcome": "correct"}])
        out = self.jl("checkpoint", "--summary", "s", "--next", "n").stdout
        self.assertIn("ROUND_CLOSED", out)
        self.assertIn("1 題未記錄", out)
        _, out = self.start()
        self.assertTrue(out.startswith("ROUND_CLOSED"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM rounds")[0][0], 1)
        _, out = self.start("--new-round")
        self.assertTrue(out.startswith("ROUND 2"))
        self.assertIn("本輪：第 2 輪進行中", self.jl("resume").stdout)

    def test_auto_checkpoint_does_not_close_round(self):
        self.start("-n", 2)
        self.record([{"seq": 1, "answer": "x", "outcome": "correct"}])
        self.jl("checkpoint", "--auto", now="2026-01-01T11:00")
        self.assertTrue(self.start(now="2026-01-01T11:05")[1].startswith("ROUND_OPEN"))

    def test_round_max_caps_n(self):
        self.sql("UPDATE meta SET value='2' WHERE key='round_max'")
        keys, _ = self.start("-n", 5)
        self.assertEqual(len(keys), 2)

    def test_new_item_quota(self):
        for k in ("a_free", "b_free", "c_choice", "d_free"):
            self.answer(k, "x")
        self.close_round()
        self.sql("UPDATE unit_state SET due_ts=0")
        self.sql("UPDATE meta SET value='0.5' WHERE key='new_item_ratio'")
        keys, out = self.start("-n", 2, "--new-round", now="2026-01-02T10:00")
        self.assertEqual(len([k for k in keys if k in ("e_long", "k_kana", "c_cloze")]), 1)
        self.assertIn("新題 1", out)

    def test_retired_and_parked_not_served(self):
        for k in ("a_free", "b_free", "c_choice", "d_free", "k_kana", "c_cloze"):
            self.jl("retire", k, "--reason", "t")
        keys, _ = self.start()
        self.assertEqual(keys, ["e_long"])
        self.close_round()
        self.jl("item", "park", "e_long", "--reason", "too_hard")
        self.assertIn("NO_ITEMS", self.start("--new-round")[1])
        self.jl("item", "unpark", "e_long")
        self.assertEqual(self.start("--new-round")[0], ["e_long"])

    def test_retest_after_miss_uses_another_item(self):
        self.sql("UPDATE meta SET value='9' WHERE key='stage'")
        extra = {"round_id": "x", "stage": 0, "items": [
            {"key": "e_long2", "unit": "u_e", "task": "reading", "prompt": "E2?", "answers": ["おおきい"],
             "scenario": "s9"}]}
        self.jl("import", self.write(extra))
        self.answer("e_long2", "おおきい")
        self.answer("e_long", "おきい", outcome="incorrect", now="2026-01-02T10:00")
        self.close_round(now="2026-01-02T10:01")
        for k in ("a_free", "b_free", "c_choice", "d_free", "k_kana", "c_cloze", "ni_choice"):
            self.jl("retire", k, "--reason", "t")
        self.sql("UPDATE unit_state SET due_ts=0")
        keys, _ = self.start("--new-round", now="2026-01-03T10:00")
        self.assertEqual(keys, ["e_long2"])

    def test_prefers_due_units_in_drilling_confusion(self):
        self.answer("b_free", "x", outcome="incorrect")
        self.answer("e_long", "x", outcome="incorrect")
        self.close_round()
        self.jl("confusion", "add", "--label", "A vs B", "--kind", "request_form", "--axis", "usage",
                "--gist", "g", "--members", "u_b", "u_a")
        self.jl("confusion", "taught", 1)
        self.sql("UPDATE unit_state SET due_ts=0 WHERE unit='u_b'")
        self.sql("UPDATE unit_state SET due_ts=-5 WHERE unit='u_e'")
        self.sql("UPDATE meta SET value='0' WHERE key='new_item_ratio'")
        keys, _ = self.start("-n", 1, "--new-round")
        self.assertEqual(keys, ["b_free"])

    def test_probe_shown_to_ai(self):
        self.answer("c_choice", "で")
        self.close_round()
        self.sql("UPDATE unit_state SET due_ts=1e12")
        out = self.jl("round", "add", "c_cloze", "--new-round").stdout
        self.assertIn("probe: 遊ぶ", out)


class TestImportAndTasks(Base):
    def test_import_is_idempotent(self):
        n1 = self.sql("SELECT COUNT(*) FROM items")[0][0]
        self.assertIn(f"skipped {len(SEED['items'])}", self.jl("import", self.write(SEED)).stdout)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM items")[0][0], n1)

    def test_reused_round_id_rejected_with_suggestion(self):
        data = {"round_id": "t", "items": [{"key": "new1", "unit": "u_e", "task": "reading", "prompt": "?",
                                            "answers": ["x"]}]}
        r = self.jl("import", self.write(data), ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("t_r1", r.stdout + r.stderr)

    def test_produce_answers_capped(self):
        data = {"round_id": "cap", "items": [{"key": "p1", "unit": "u_a", "task": "translate", "prompt": "?",
                                              "answers": [f"a{i}" for i in range(6)]}]}
        self.assertNotEqual(self.jl("import", self.write(data), ok=False).returncode, 0)
        self.assertNotEqual(self.jl("item", "edit", "a_free", "--set-answers",
                                    json.dumps([f"a{i}" for i in range(6)]), ok=False).returncode, 0)

    def test_import_sets_last_import_and_resume_warns(self):
        self.jl("checkpoint", "--summary", "s", "--next", "需要新題", now="2026-01-01T09:00")
        data = {"round_id": "r9", "items": [{"key": "z1", "unit": "u_e", "task": "reading", "prompt": "?",
                                             "answers": ["x"]}]}
        self.jl("import", self.write(data))
        out = self.jl("resume").stdout
        self.assertIn("已匯入 r9（1 題）", out)
        self.assertIn("未作答過的題：", out)

    def test_unknown_task_rejected_then_task_add(self):
        data = {"round_id": "r", "stage": 0, "items": [
            {"key": "z", "unit": "u_z", "kind": "phrase", "task": "shadowing", "prompt": "Z", "answers": ["x"]}]}
        f = self.write(data)
        self.assertNotEqual(self.jl("import", f, ok=False).returncode, 0)
        self.jl("task", "add", "shadowing", "--skill", "produce", "--grading", "judge", "--desc", "跟讀")
        self.jl("import", f)
        self.assertEqual(self.sql("SELECT skill FROM unit_skills WHERE unit='u_z'")[0][0], "produce")

    def test_choice_answers_must_be_in_choices(self):
        data = {"round_id": "r", "items": [{"key": "y", "unit": "u_y", "kind": "word", "task": "choice",
                                            "prompt": "Y", "choices": ["a", "b"], "answers": ["c"]}]}
        self.assertNotEqual(self.jl("import", self.write(data), ok=False).returncode, 0)

    def test_units_section_and_skills(self):
        self.assertEqual(self.sql("SELECT gist FROM units WHERE unit='u_c'")[0][0], "動作地點→で；存在/抵達→に")
        skills = {r[0] for r in self.sql("SELECT skill FROM unit_skills WHERE unit='u_c'")}
        self.assertEqual(skills, {"comprehend", "produce"})

    def test_item_edit_keeps_history(self):
        self.jl("item", "edit", "a_free", "--set-answers", json.dumps(["お会計お願いします"]),
                "--judge", "考點：お願いします")
        self.jl("item", "edit", "a_free", "--add-answer", "お会計をお願いします")
        it = self.sql("SELECT answers, judge FROM items WHERE key='a_free'")[0]
        self.assertIn("お会計をお願いします", it["answers"])
        self.assertEqual(it["judge"], "考點：お願いします")
        fields = [r[0] for r in self.sql("SELECT field FROM item_edits ORDER BY id")]
        self.assertEqual(fields, ["answers", "judge", "answers"])

    def test_item_edit_set_choices(self):
        self.jl("item", "edit", "c_choice", "--set-choices",
                json.dumps(["で", {"text": "を", "unit": "u_ni"}, "へ"], ensure_ascii=False))
        it = self.sql("SELECT choices, choice_units FROM items WHERE key='c_choice'")[0]
        self.assertEqual(json.loads(it["choices"]), ["で", "を", "へ"])
        self.assertEqual(json.loads(it["choice_units"]), [None, "u_ni", None])
        # 答案不在新選項裡、不存在的 unit、非選擇題 → 拒絕
        for key, ch in (("c_choice", ["を", "へ"]), ("c_choice", ["で", {"text": "を", "unit": "nope"}]),
                        ("a_free", ["で", "に"])):
            self.assertNotEqual(self.jl("item", "edit", key, "--set-choices",
                                        json.dumps(ch, ensure_ascii=False), ok=False).returncode, 0)


class TestQueries(Base):
    def test_sql_select_and_view(self):
        self.answer("e_long", "おきい", outcome="incorrect")
        self.assertIn("u_e\tincorrect", self.jl("sql", "SELECT unit, outcome FROM v_recent_errors").stdout)

    def test_sql_rejects_writes(self):
        for q in ("DELETE FROM attempts", "WITH x AS (SELECT 1) DELETE FROM attempts",
                  "SELECT 1; DELETE FROM attempts", "UPDATE meta SET value='9'"):
            self.assertNotEqual(self.jl("sql", q, ok=False).returncode, 0, q)

    def test_schema_lists_views_and_tasks(self):
        out = self.jl("schema").stdout
        for s in ("v_attempt_effective", "v_unseen_items", "v_rounds", "translate / produce", "correction"):
            self.assertIn(s, out)

    def test_variants_needed_and_unseen(self):
        self.answer("e_long", "おおきい")
        self.answer("e_long", "おおきい", now="2026-01-02T02:00")  # 凌晨 4 點前＝同一學習日
        self.assertNotIn("u_e/comprehend", self.jl("variants").stdout)
        self.answer("e_long", "おおきい", now="2026-01-02T10:00")
        out = self.jl("variants").stdout
        self.assertIn("u_e/comprehend", out)
        self.assertIn("未作答過的題：6", out)  # 8 題－e_long－ni_choice(stage 5)

    def test_unit_show(self):
        self.answer("c_choice", "で")
        self.assertIn("c_choice", self.jl("unit", "show", "u_c").stdout)


class TestSessionAndCheckpoint(Base):
    def test_resume_startup_opens_session_compact_keeps_and_hook_is_logged(self):
        self.assertIn("從未執行", self.jl("resume").stdout)
        s1 = self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0]
        out = self.jl("resume", "--hook", stdin=json.dumps({"source": "compact"})).stdout
        self.assertIn("source=compact", out)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0], s1)
        self.jl("resume", "--hook", stdin=json.dumps({"source": "startup"}))
        self.assertNotEqual(self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0], s1)
        self.assertIn("startup", self.jl("resume").stdout.splitlines()[0])

    def test_auto_checkpoint(self):
        self.assertIn("不寫入", self.jl("checkpoint", "--auto").stdout)
        self.jl("checkpoint", "--summary", "s", "--next", "focus X")
        self.answer("e_long", "おきい", outcome="incorrect", now="2026-01-01T11:00")
        self.jl("checkpoint", "--auto", now="2026-01-01T12:00")
        cp = self.sql("SELECT kind, summary, next_focus FROM checkpoints ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((cp["kind"], cp["next_focus"]), ("auto", "focus X"))
        self.assertIn("u_e", cp["summary"])


class TestMigrationV7(Base):
    def test_v7_migrates_and_next_round_works(self):
        """依 docs/development.md：只驗證遷移後有備份、下一輪仍出得了題。"""
        r = subprocess.run(["git", "show", "6a58c3f:tools/jl.py"], cwd=ROOT, capture_output=True)
        if r.returncode:
            self.skipTest("git 歷史中找不到 v7 版 jl.py")
        old = Path(self.tmp.name) / "jl_v7.py"
        old.write_bytes(r.stdout)
        self.db.unlink()
        env = dict(os.environ, JL_DB=str(self.db), JL_SEED=str(self.seed), JL_NOW=self.now, PYTHONIOENCODING="utf-8")

        def old_jl(*a, stdin=None):
            p = subprocess.run([sys.executable, "-X", "utf8", str(old), *map(str, a)], capture_output=True,
                               text=True, encoding="utf-8", env=env, input=stdin)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout
        out = old_jl("round", "add", "e_long")
        seq = int(out.split("added seq ")[1].split()[0])
        old_jl("record", stdin=json.dumps([{"seq": seq, "answer": "おきい", "outcome": "incorrect"}]))
        old_jl("checkpoint", "--summary", "s")
        r = self.jl("round", "start", "--new-round", now="2026-01-02T10:00")
        self.assertIn("v7 -> v8", r.stderr)
        self.assertTrue((self.db.parent / "learning.v7.bak.db").exists())
        self.assertIn("e_long", r.stdout)  # 舊的錯題仍會再出現


class TestEncountersAndPark(Base):
    def test_seen_add_dedupes_and_links(self):
        self.answer("e_long", "おおきい")
        self.answer("b_free", "これをください")
        a1, a2 = [r[0] for r in self.sql("SELECT id FROM attempts ORDER BY id")]
        self.assertIn("created", self.jl("seen", "add", "--term", "お召し上がり", "--kind", "expression",
                                         "--attempt", a1).stdout)
        self.jl("seen", "add", "--term", "お召し上がり", "--kind", "expression", "--attempt", a1)
        self.jl("seen", "add", "--term", "お召し上がり", "--kind", "expression", "--attempt", a2, "--where", "choice")
        r = self.sql("SELECT * FROM v_encounters")[0]
        self.assertEqual((r["seen_count"], r["status"]), (2, "open"))
        self.jl("seen", "link", "お召し上がり", "u_a")
        self.assertEqual(self.sql("SELECT status FROM v_encounters")[0][0], "linked")
        self.assertNotEqual(self.jl("seen", "add", "--term", "x", "--kind", "word", "--attempt", 999,
                                    ok=False).returncode, 0)

    def test_seen_where_answer(self):
        """作答時想用但不會的詞（例如以 placeholder 作答）記為 where=answer。"""
        self.answer("a_free", "<席>お願いします", outcome="incorrect",
                    seen=[{"term": "席", "kind": "word", "where": "answer", "reading": "せき", "zh": "位子"}])
        r = self.sql("SELECT n_wanted, attempts FROM v_encounters WHERE term='席'")[0]
        self.assertEqual(r["n_wanted"], 1)
        self.assertTrue(r["attempts"].endswith(":answer"))
        self.assertIn("想用但不會=1", self.jl("variants").stdout)

    def test_from_encounter_import(self):
        self.answer("e_long", "おおきい")
        aid = self.sql("SELECT id FROM attempts")[0][0]
        self.jl("seen", "add", "--term", "お召し上がり", "--kind", "expression", "--attempt", aid)
        data = {"round_id": "p", "stage": 0,
                "units": [{"unit": "u_meshi", "kind": "phrase", "gist": "g", "from_encounter": "お召し上がり"}],
                "items": [{"key": "p1", "unit": "u_meshi", "task": "choice", "prompt": "P?",
                           "choices": ["吃", "買"], "answers": ["吃"], "probe": "店内"}]}
        self.jl("import", self.write(data))
        self.assertEqual(self.sql("SELECT unit FROM encounters")[0][0], "u_meshi")

    def test_miss_reasons(self):
        self.answer("e_long", "おおきい", now="2026-01-01T10:00")
        s1 = self.state("u_e")["stability_days"]
        self.answer("e_long", "おきい", outcome="incorrect", miss="slip", now="2026-01-02T10:00")
        st = self.state("u_e")
        self.assertEqual(st["lapses"], 0)
        self.assertAlmostEqual(st["stability_days"], s1 * 0.85)
        self.answer("e_long", "おきい", outcome="incorrect", miss="unreadable", now="2026-01-03T10:00")
        self.assertAlmostEqual(self.state("u_e")["stability_days"], s1 * 0.85)
        self.jl("report", "--miss", "none", now="2026-01-03T10:01")
        self.assertEqual(self.state("u_e")["lapses"], 1)


class TestConfusion(Base):
    def add_c(self, *extra, ok=True, members=("u_a", "u_b"), label="A vs B", gist="東西→B；請對方→A"):
        return self.jl("confusion", "add", "--label", label, "--kind", "request_form", "--cause", "overgeneralize",
                       "--axis", "usage", "--gist", gist, "--members", *members, *extra, ok=ok)

    def status(self, cid=1):
        return self.sql("SELECT status FROM confusion_status WHERE id=?", cid)[0][0]

    def test_create_dedupe_and_validation(self):
        self.assertIn("created confusion 1", self.add_c().stdout)
        self.assertIn("updated confusion 1", self.add_c(members=("u_b", "u_a")).stdout)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 2)
        self.assertNotEqual(self.add_c(members=("u_a", "nope"), ok=False).returncode, 0)
        self.assertNotEqual(self.add_c(gist="あ" * 81, ok=False).returncode, 0)
        self.assertNotEqual(self.add_c("--attempt", "999", ok=False).returncode, 0)

    def test_status_derivation_and_merge(self):
        self.add_c()
        self.assertEqual(self.status(), "observed")
        self.jl("confusion", "taught", 1)
        self.assertEqual(self.status(), "drilling")
        self.assertIn("進行中的混淆", self.jl("resume").stdout)
        for u, sk in (("u_a", "produce"), ("u_b", "comprehend")):
            self.sql("INSERT OR REPLACE INTO unit_state(unit,skill,tier,stability_days,due_ts,last_ts) VALUES(?,?,?,?,?,?)",
                     u, sk, "consolidated", 90.0, 2e9, 0.0)
        self.assertEqual(self.status(), "resolved")
        self.add_c(members=("u_c", "u_d"), label="C vs D")
        self.jl("confusion", "merge", 2, 1)
        self.assertEqual(self.status(2), "merged")

    def test_drop_removes_from_active(self):
        self.add_c()
        self.jl("confusion", "taught", 1)
        self.assertIn("dropped 1", self.jl("confusion", "drop", 1, "--reason", "事後自述 miss=probe").stdout)
        self.assertEqual(self.status(), "dropped")
        self.assertNotIn("進行中的混淆", self.jl("resume").stdout)
        self.assertIn("no active confusion 1", self.jl("confusion", "drop", 1, "--reason", "x").stdout)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 1)  # 紀錄保留

    def test_add_on_dropped_revives(self):
        self.add_c()
        self.jl("confusion", "taught", 1)
        self.jl("confusion", "drop", 1, "--reason", "x")
        self.assertIn("revived confusion 1", self.add_c().stdout)
        self.assertEqual(self.status(), "drilling")
        self.assertEqual(self.sql("SELECT drop_reason FROM confusions")[0][0], None)

    def test_miss_count_follows_linked_attempts(self):
        self.answer("c_choice", "x", outcome="incorrect")
        self.answer("ni_choice", "x", outcome="incorrect")
        aid1, aid2 = [r[0] for r in self.sql("SELECT id FROM attempts ORDER BY id")]
        self.add_c("--attempt", aid1)
        self.add_c("--attempt", aid1)  # 同一筆作答重複 add 不重複計
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 1)
        self.add_c("--attempt", aid2)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 2)
        self.jl("report", "--attempt", aid2, "--miss", "probe")  # 卡在看不懂的詞：rebuild 後不算
        self.jl("rebuild")
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 1)


class TestUnitSplit(Base):
    def test_split_moves_items_and_attempts(self):
        self.answer("c_choice", "A", outcome="correct")
        self.answer("c_cloze", "に", outcome="incorrect", now="2026-01-01T11:00")
        r = self.jl("unit", "split", "u_c", "--new", "u_c_means", "--items", "c_cloze",
                    "--gist", "交通手段→で")
        self.assertIn("1 題、1 筆作答", r.stdout)
        self.assertEqual(self.sql("SELECT unit FROM items WHERE key='c_cloze'")[0][0], "u_c_means")
        self.assertEqual(self.sql("SELECT unit FROM attempts WHERE outcome='incorrect'")[0][0], "u_c_means")
        stats = lambda u, sk: self.sql("SELECT n, n_incorrect FROM unit_stats WHERE unit=? AND skill=?", u, sk)
        self.assertEqual(stats("u_c", "produce"), [])            # 原 unit 不再背這次答錯
        self.assertEqual(tuple(stats("u_c_means", "produce")[0]), (1, 1))
        self.assertIsNotNone(self.state("u_c_means", "produce"))
        self.assertEqual(self.sql("SELECT old, new FROM item_edits WHERE field='unit'")[0][:], ("u_c", "u_c_means"))
        self.assertEqual([r[0] for r in self.sql("SELECT skill FROM unit_skills WHERE unit='u_c'")], ["comprehend"])
        self.assertNotEqual(self.jl("unit", "split", "u_c", "--new", "u_x", "--items", "a_free", "--gist", "g",
                                    ok=False).returncode, 0)
        self.assertNotEqual(self.jl("unit", "split", "u_c", "--new", "u_c_means", "--items", "c_choice",
                                    "--gist", "g", ok=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
