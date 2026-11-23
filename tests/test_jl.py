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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JL = ROOT / "tools" / "jl.py"

SEED = {
    "round_id": "t", "stage": 0,
    "units": [{"unit": "u_c", "kind": "grammar", "gist": "動作地點→で；存在/抵達→に"}],
    "items": [
        {"key": "a_free", "unit": "u_a", "kind": "phrase", "task": "translate",
         "prompt": "A?", "answers": ["お会計お願いします"], "jp": "お会計お願いします",
         "reading": "おかいけいおねがいします",
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
         "prompt": "公園（ ）遊ぶ", "answers": ["で"], "jp": "公園で遊ぶ", "scenario": "s2"},
    ],
}


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

    def present(self, key, now=None):
        out = self.jl("present", key, now=now).stdout
        return int(out.split("presentation: ")[1].split()[0])

    def answer(self, key, text, *extra, now=None, ok=True):
        pid = self.present(key, now=now)
        return self.jl("answer", pid, text, *extra, now=now, ok=ok)

    def letter_for(self, pid, text):
        shown = json.loads(self.sql("SELECT shown_choices FROM presentations WHERE id=?", pid)[0][0])
        return chr(65 + shown.index(text))

    def state(self, unit, skill="comprehend"):
        r = self.sql("SELECT * FROM unit_state WHERE unit=? AND skill=?", unit, skill)
        return r[0] if r else None


def first_line(r):
    return r.stdout.splitlines()[0]


class TestGrading(Base):
    def test_exact_and_romaji(self):
        self.assertIn("outcome=correct", first_line(self.answer("a_free", "お会計お願いします")))
        self.assertIn("outcome=correct", first_line(self.answer("a_free", "okaikei onegaishimasu")))

    def test_particle_typed_as_wa_o_is_correct(self):
        self.assertIn("outcome=correct", first_line(self.answer("b_free", "これおください")))
        self.assertIn("outcome=correct", first_line(self.answer("b_free", "kore wo kudasai")))
        self.assertIn("outcome=correct", first_line(self.answer("b_free", "kore o kudasai")))

    def test_particle_tolerance_does_not_hide_other_errors(self):
        self.assertIn("outcome=incorrect", first_line(self.answer("b_free", "これをくだらい")))

    def test_long_vowel_still_counts_when_kana(self):
        self.assertIn("outcome=correct", first_line(self.answer("e_long", "おおきい")))
        self.assertIn("outcome=incorrect", first_line(self.answer("e_long", "おきい")))

    def test_unknown_is_skipped(self):
        self.assertIn("outcome=skipped", first_line(self.answer("a_free", "不知道")))
        self.assertIn("outcome=skipped", first_line(self.answer("a_free", "idk")))

    def test_produce_mismatch_needs_judgment_and_writes_nothing(self):
        pid = self.present("a_free")
        r = self.jl("answer", pid, "お会計をお願いします")
        self.assertTrue(r.stdout.startswith("NEEDS_JUDGMENT"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM attempts")[0][0], 0)
        r = self.jl("answer", pid, "お会計をお願いします", "--outcome", "correct")
        self.assertIn("(judged)", first_line(r))
        r = self.jl("answer", pid, "x", "--outcome", "correct", ok=False)  # 同一次出題不能重複作答
        self.assertNotEqual(r.returncode, 0)

    def test_judged_incorrect_with_tags(self):
        pid = self.present("a_free")
        self.jl("answer", pid, "お会計おください", "--outcome", "incorrect", "--tags", "register,unnatural")
        tags = {r[0] for r in self.sql("SELECT tag FROM attempt_tags")}
        self.assertEqual(tags, {"register", "unnatural"})
        self.assertNotEqual(self.jl("answer", self.present("a_free"), "x", "--outcome", "incorrect",
                                    "--tags", "nope", ok=False).returncode, 0)

    def test_chinese_meaning_exact_only(self):
        self.assertIn("outcome=correct", first_line(self.answer("d_free", "找零")))
        # 子字串不再自動判對：「不是找零」交給判斷
        self.assertTrue(self.answer("d_free", "不是找零").stdout.startswith("NEEDS_JUDGMENT"))

    def test_choice_by_letter_follows_shown_order_and_stores_pick(self):
        pid = self.present("c_choice")
        r = self.jl("answer", pid, self.letter_for(pid, "に"))
        self.assertIn("outcome=incorrect", first_line(r))
        self.assertIn("CONFUSION_CANDIDATE: u_c|u_ni", r.stdout)
        a = self.sql("SELECT picked_text, picked_unit, answer_mode, user_answer FROM attempts")[0]
        self.assertEqual((a["picked_text"], a["picked_unit"], a["answer_mode"]), ("に", "u_ni", "choice"))
        pid = self.present("c_choice")
        self.assertIn("outcome=correct", first_line(self.jl("answer", pid, self.letter_for(pid, "で"))))
        self.assertEqual(self.sql("SELECT n FROM v_distractor_picks WHERE unit='u_c'")[0][0], 1)

    def test_choice_by_text(self):
        self.assertIn("outcome=correct", first_line(self.answer("c_choice", "で")))


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
    def test_answer_creates_state_and_stats(self):
        self.answer("a_free", "お会計お願いします")
        st = self.state("u_a", "produce")
        self.assertEqual(st["tier"], "learning")
        s = self.sql("SELECT n, n_correct FROM unit_stats WHERE unit='u_a'")[0]
        self.assertEqual((s["n"], s["n_correct"]), (1, 1))

    def test_guess_correct_is_no_evidence(self):
        self.answer("e_long", "おおきい", "--confidence", "guess")
        st = self.state("u_e")
        self.assertEqual(st["reps_success"], 0)
        self.assertAlmostEqual(st["stability_days"], 0.3)

    def test_context_basis_is_no_evidence(self):
        self.answer("e_long", "おおきい", "--basis", "context")
        self.assertEqual(self.state("u_e")["reps_success"], 0)

    def test_after_feedback_report_changes_schedule_not_attempts(self):
        self.answer("e_long", "おおきい")
        self.assertEqual(self.state("u_e")["reps_success"], 1)
        before = [tuple(r) for r in self.sql("SELECT * FROM attempts")]
        self.jl("report", "--confidence", "guess", "--note", "其實是猜的")
        self.assertEqual(before, [tuple(r) for r in self.sql("SELECT * FROM attempts")])
        self.assertEqual(self.state("u_e")["reps_success"], 0)
        e = self.sql("SELECT confidence, has_after_feedback FROM v_attempt_effective")[0]
        self.assertEqual((e["confidence"], e["has_after_feedback"]), ("guess", 1))
        self.assertEqual(self.sql("SELECT n_after_feedback_reports FROM unit_stats WHERE unit='u_e'")[0][0], 1)

    def test_report_merges_fields(self):
        self.answer("e_long", "おおきい", "--basis", "intuition")
        self.jl("report", "--confidence", "unsure")
        e = self.sql("SELECT confidence, basis FROM v_attempt_effective")[0]
        self.assertEqual((e["confidence"], e["basis"]), ("unsure", "intuition"))

    def test_lapse_counts_only_after_success(self):
        self.answer("e_long", "おきい")
        self.assertEqual(self.state("u_e")["lapses"], 0)
        self.answer("e_long", "おおきい", now="2026-01-02T10:00")
        self.answer("e_long", "おきい", now="2026-01-03T10:00")
        self.assertEqual(self.state("u_e")["lapses"], 1)

    def test_skills_scheduled_separately(self):
        self.answer("c_choice", "で")
        self.assertIsNotNone(self.state("u_c", "comprehend"))
        self.assertIsNone(self.state("u_c", "produce"))
        self.assertTrue(self.answer("c_cloze", "に").stdout.startswith("NEEDS_JUDGMENT"))
        self.answer("c_cloze", "に", "--outcome", "incorrect", "--tags", "particle")
        self.assertEqual(self.state("u_c", "produce")["reps_success"], 0)
        self.assertEqual(self.state("u_c", "comprehend")["reps_success"], 1)

    def test_comprehend_only_unit_can_consolidate(self):
        for k, v in (("cons_gap_days", "1"), ("cons_min_stability", "0.1"), ("cons_min_days", "3")):
            self.sql("UPDATE meta SET value=? WHERE key=?", v, k)
        for d in ("2026-01-01T10:00", "2026-01-02T10:00", "2026-01-03T10:00"):
            self.answer("k_kana", "nu", now=d)
        self.assertEqual(self.state("u_k")["tier"], "consolidated")
        self.assertEqual(self.sql("SELECT status FROM v_unit_status WHERE unit='u_k'")[0][0], "consolidated")

    def test_unit_needs_all_skills_to_consolidate(self):
        for k, v in (("cons_gap_days", "1"), ("cons_min_stability", "0.1"), ("cons_min_days", "3")):
            self.sql("UPDATE meta SET value=? WHERE key=?", v, k)
        for d in ("2026-01-01T10:00", "2026-01-02T10:00", "2026-01-03T10:00"):
            self.answer("c_choice", "で", now=d)
        self.assertEqual(self.state("u_c")["tier"], "consolidated")
        self.assertNotEqual(self.sql("SELECT status FROM v_unit_status WHERE unit='u_c'")[0][0], "consolidated")

    def test_rebuild_reproduces_state(self):
        self.answer("e_long", "おおきい")
        self.answer("e_long", "おきい", now="2026-01-03T10:00")
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


class TestNext(Base):
    def keys_served(self, n=1, now=None):
        out = self.jl("next", "-n", n, now=now).stdout
        return [line.split("item: ")[1].split()[0] for line in out.splitlines() if line.startswith("item: ")], out

    def test_serves_then_none_when_all_seen(self):
        seen = set()
        for _ in range(10):
            keys, out = self.keys_served()
            if not keys:
                break
            pid = int(out.split("presentation: ")[1].split()[0])
            self.jl("answer", pid, "x", "--outcome", "correct")
            seen.add(keys[0])
        self.assertNotIn("ni_choice", seen)  # stage 5 > 0
        self.assertIn("NO_ITEMS", self.jl("next").stdout)

    def test_produce_waits_for_comprehend(self):
        served = []
        for _ in range(8):
            keys, out = self.keys_served()
            if not keys:
                break
            served.append(keys[0])
            pid = int(out.split("presentation: ")[1].split()[0])
            self.jl("answer", pid, "x", "--outcome", "correct")
        self.assertLess(served.index("c_choice"), served.index("c_cloze"))

    def test_answers_not_in_output(self):
        _, out = self.keys_served(5)
        self.assertNotIn("answers", out)
        self.assertNotIn("找零", out)

    def test_retired_item_not_served(self):
        for k in ("a_free", "b_free", "c_choice", "d_free", "k_kana", "c_cloze"):
            self.jl("retire", k, "--reason", "t")
        keys, _ = self.keys_served()
        self.assertEqual(keys, ["e_long"])

    def test_prefers_items_not_yet_correct(self):
        self.sql("UPDATE meta SET value='9' WHERE key='stage'")
        extra = {"round_id": "x", "stage": 0, "items": [
            {"key": "e_long2", "unit": "u_e", "task": "reading", "prompt": "E2?", "answers": ["おおきい"],
             "scenario": "s9"}]}
        f = Path(self.tmp.name) / "x.json"
        f.write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
        self.jl("import", f)
        self.answer("e_long", "おおきい")
        self.sql("UPDATE unit_state SET due_ts=0")
        for k in ("a_free", "b_free", "c_choice", "d_free", "k_kana", "c_cloze", "ni_choice"):
            self.jl("retire", k, "--reason", "t")
        keys, _ = self.keys_served()
        self.assertEqual(keys, ["e_long2"])


class TestImportAndTasks(Base):
    def write(self, data, name="r.json"):
        f = Path(self.tmp.name) / name
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return f

    def test_import_is_idempotent(self):
        n1 = self.sql("SELECT COUNT(*) FROM items")[0][0]
        self.assertIn(f"skipped {len(SEED['items'])}", self.jl("import", self.write(SEED)).stdout)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM items")[0][0], n1)

    def test_unknown_task_rejected_then_task_add(self):
        data = {"round_id": "r", "stage": 0, "items": [
            {"key": "z", "unit": "u_z", "kind": "phrase", "task": "shadowing", "prompt": "Z", "answers": ["x"]}]}
        f = self.write(data)
        self.assertNotEqual(self.jl("import", f, ok=False).returncode, 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM items WHERE key='z'")[0][0], 0)
        self.jl("task", "add", "shadowing", "--skill", "produce", "--grading", "judge", "--desc", "跟讀")
        self.jl("import", f)
        self.assertEqual(self.sql("SELECT skill FROM unit_skills WHERE unit='u_z'")[0][0], "produce")
        r = self.answer("z", "x")
        self.assertTrue(r.stdout.startswith("NEEDS_JUDGMENT"))  # judge 題型一律判斷

    def test_choice_answers_must_be_in_choices(self):
        data = {"round_id": "r", "items": [{"key": "y", "unit": "u_y", "kind": "word", "task": "choice",
                                            "prompt": "Y", "choices": ["a", "b"], "answers": ["c"]}]}
        self.assertNotEqual(self.jl("import", self.write(data), ok=False).returncode, 0)

    def test_units_section_and_skills(self):
        self.assertEqual(self.sql("SELECT gist FROM units WHERE unit='u_c'")[0][0], "動作地點→で；存在/抵達→に")
        skills = {r[0] for r in self.sql("SELECT skill FROM unit_skills WHERE unit='u_c'")}
        self.assertEqual(skills, {"comprehend", "produce"})
        self.assertEqual({r[0] for r in self.sql("SELECT skill FROM unit_skills WHERE unit='u_k'")}, {"comprehend"})

    def test_item_edit_keeps_history(self):
        self.jl("item", "edit", "a_free", "--add-answer", "お会計をお願いします")
        self.assertIn("outcome=correct", first_line(self.answer("a_free", "お会計をお願いします")))
        h = self.sql("SELECT field, old FROM item_edits")[0]
        self.assertEqual(h["field"], "answers")
        self.assertNotIn("お会計をお願いします", h["old"])


class TestQueries(Base):
    def test_sql_select_and_view(self):
        self.answer("e_long", "おきい")
        out = self.jl("sql", "SELECT unit, outcome FROM v_recent_errors").stdout
        self.assertIn("u_e\tincorrect", out)

    def test_sql_rejects_writes(self):
        for q in ("DELETE FROM attempts", "WITH x AS (SELECT 1) DELETE FROM attempts",
                  "SELECT 1; DELETE FROM attempts", "UPDATE meta SET value='9'"):
            self.assertNotEqual(self.jl("sql", q, ok=False).returncode, 0, q)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='stage'")[0][0], "0")

    def test_schema_lists_views_and_tasks(self):
        out = self.jl("schema").stdout
        for s in ("v_attempt_effective", "v_variants_needed", "translate / produce", "long_vowel"):
            self.assertIn(s, out)

    def test_variants_needed(self):
        self.answer("e_long", "おおきい")
        self.answer("e_long", "おおきい", now="2026-01-02T02:00")  # 凌晨 4 點前＝同一學習日
        self.assertNotIn("u_e/comprehend", self.jl("variants").stdout)
        self.answer("e_long", "おおきい", now="2026-01-02T10:00")
        self.assertIn("u_e/comprehend", self.jl("variants").stdout)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM v_variants_needed WHERE unit='u_c'")[0][0], 0)

    def test_unit_show(self):
        self.answer("c_choice", "で")
        out = self.jl("unit", "show", "u_c").stdout
        self.assertIn("skills=", out)
        self.assertIn("c_choice", out)


class TestSessionAndCheckpoint(Base):
    def test_resume_startup_opens_session_compact_keeps(self):
        self.jl("resume")
        s1 = self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0]
        out = self.jl("resume", "--hook", stdin=json.dumps({"source": "compact"})).stdout
        self.assertIn("source=compact", out)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0], s1)
        self.jl("resume", "--hook", stdin=json.dumps({"source": "startup"}))
        self.assertNotEqual(self.sql("SELECT value FROM meta WHERE key='current_session'")[0][0], s1)

    def test_auto_checkpoint(self):
        self.assertIn("不寫入", self.jl("checkpoint", "--auto").stdout)
        self.jl("checkpoint", "--summary", "s", "--next", "focus X")
        self.answer("e_long", "おきい", now="2026-01-01T11:00")
        self.jl("checkpoint", "--auto", now="2026-01-01T12:00")
        cp = self.sql("SELECT kind, summary, next_focus FROM checkpoints ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(cp["kind"], "auto")
        self.assertIn("u_e", cp["summary"])
        self.assertEqual(cp["next_focus"], "focus X")
        out = self.jl("resume", now="2026-01-01T13:00").stdout
        self.assertIn("next_focus: focus X", out)
        self.assertIn("auto checkpoint", out)


class TestMigrationV3(Base):
    V3 = """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, unit TEXT NOT NULL,
      kind TEXT NOT NULL, qtype TEXT NOT NULL, format TEXT NOT NULL, prompt TEXT NOT NULL, choices TEXT,
      answers TEXT NOT NULL, jp TEXT, reading TEXT, zh TEXT, scenario TEXT, stage INTEGER NOT NULL DEFAULT 0,
      explain TEXT, source TEXT, retired TEXT);
    CREATE TABLE attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, unit TEXT NOT NULL,
      ts REAL NOT NULL, session_id INTEGER, outcome TEXT NOT NULL, confidence TEXT, basis TEXT,
      answer_mode TEXT, user_answer TEXT, note TEXT);
    CREATE TABLE item_state(unit TEXT PRIMARY KEY, tier TEXT NOT NULL);
    CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, started_ts REAL, ended_ts REAL);
    CREATE TABLE checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL, summary TEXT, next_focus TEXT);
    CREATE TABLE confusions(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, kind TEXT NOT NULL,
      cause TEXT NOT NULL, axis TEXT NOT NULL, gist TEXT NOT NULL, pair_key TEXT NOT NULL UNIQUE,
      miss_count INTEGER NOT NULL DEFAULT 1, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
      taught_at REAL, taught_session INTEGER, merged_into INTEGER);
    CREATE TABLE confusion_members(confusion_id INTEGER NOT NULL, unit TEXT NOT NULL, PRIMARY KEY(confusion_id, unit));
    CREATE TABLE confusion_attempts(confusion_id INTEGER NOT NULL, attempt_id INTEGER NOT NULL,
      PRIMARY KEY(confusion_id, attempt_id));
    INSERT INTO meta VALUES('schema_version','3'),('stage','1'),('shown:1','["x"]'),('current_session','1');
    INSERT INTO items(key,unit,kind,qtype,format,prompt,choices,answers,jp,stage) VALUES
      ('k1','u1','grammar','choice','recognize','p','["で","に"]','["で"]','で',0),
      ('k2','u2','word','free','recall','讀音?',NULL,'["えき","eki"]','駅',0),
      ('k3','u3','word','free','recall','意思?（用中文回答）',NULL,'["找零","おつり"]','お釣り',0),
      ('k4','u1','grammar','free','produce','你想說 X，日文怎麼說？',NULL,'["で"]','で',1);
    INSERT INTO sessions(started_ts) VALUES(1767225600);
    INSERT INTO attempts(item_id,unit,ts,session_id,outcome,confidence,basis,answer_mode,user_answer,note) VALUES
      (1,'u1',1767250000,1,'correct','unsure','intuition','choice','B',NULL),
      (2,'u2',1767250100,1,'incorrect',NULL,NULL,'jp','えぎ','n'),
      (3,'u3',1767250200,1,'skipped',NULL,NULL,'unknown','不知道',NULL);
    INSERT INTO checkpoints(session_id,ts,summary,next_focus) VALUES(1,1767250300,'sum','nxt');
    INSERT INTO confusions(label,kind,cause,axis,gist,pair_key,first_seen,last_seen)
      VALUES('で vs に','particle','unknown','usage','g','u1|u2',1,1);
    INSERT INTO confusion_members VALUES(1,'u1'),(1,'u2');
    INSERT INTO confusion_attempts VALUES(1,2);
    """

    def test_v3_migrates(self):
        self.db.unlink()
        con = sqlite3.connect(self.db)
        con.executescript(self.V3)
        con.commit()
        con.close()
        r = self.jl("stats")
        self.assertIn("v3 -> v4", r.stderr)
        self.assertTrue((self.db.parent / "learning.v3.bak.db").exists())
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='schema_version'")[0][0], "4")
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='stage'")[0][0], "1")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM meta WHERE key LIKE 'shown:%'")[0][0], 0)
        tasks = {r["key"]: r["task"] for r in self.sql("SELECT key, task FROM items")}
        self.assertEqual({k: tasks[k] for k in ("k1", "k2", "k3", "k4")},
                         {"k1": "choice", "k2": "reading", "k3": "meaning", "k4": "translate"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM attempts")[0][0], 3)
        e = self.sql("SELECT confidence, basis FROM v_attempt_effective WHERE id=1")[0]
        self.assertEqual((e["confidence"], e["basis"]), ("unsure", "intuition"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusion_members")[0][0], 2)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusion_attempts")[0][0], 1)
        self.assertEqual(self.sql("SELECT kind FROM checkpoints")[0][0], "manual")
        self.assertEqual({r[0] for r in self.sql("SELECT skill FROM unit_skills WHERE unit='u1'")},
                         {"comprehend", "produce"})
        self.assertFalse(self.sql("SELECT 1 FROM sqlite_master WHERE name='item_state'"))
        q = "SELECT * FROM unit_state ORDER BY unit, skill"
        before = [tuple(r) for r in self.sql(q)]
        self.assertEqual(len(before), 3)
        self.jl("rebuild")
        self.assertEqual(before, [tuple(r) for r in self.sql(q)])


class TestConfusion(Base):
    def add(self, *extra, ok=True, members=("u_a", "u_b"), label="A vs B", gist="東西→B；請對方→A"):
        return self.jl("confusion", "add", "--label", label, "--kind", "request_form", "--cause", "overgeneralize",
                       "--axis", "usage", "--gist", gist, "--members", *members, *extra, ok=ok)

    def status(self, cid=1):
        return self.sql("SELECT status FROM confusion_status WHERE id=?", cid)[0][0]

    def test_create_and_list(self):
        self.assertIn("created confusion 1", self.add().stdout)
        out = self.jl("confusion", "list").stdout
        self.assertIn("status=observed", out)
        self.assertIn("u_a,u_b", out)

    def test_pair_key_dedupes_and_counts(self):
        self.add()
        self.assertIn("updated confusion 1", self.add(members=("u_b", "u_a")).stdout)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 2)

    def test_member_unit_must_exist(self):
        self.assertNotEqual(self.add(members=("u_a", "nope"), ok=False).returncode, 0)
        self.assertNotEqual(self.add(members=("u_a", "u_a"), ok=False).returncode, 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 0)

    def test_gist_and_enum_checks(self):
        self.assertNotEqual(self.add(gist="あ" * 81, ok=False).returncode, 0)
        self.add()
        for col, bad in (("gist", "あ" * 81), ("kind", "x"), ("cause", "x"), ("axis", "x")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.sql(f"UPDATE confusions SET {col}=? WHERE id=1", bad)

    def test_attempt_link_and_validation(self):
        self.assertNotEqual(self.add("--attempt", "999", ok=False).returncode, 0)
        self.answer("e_long", "おきい")
        aid = self.sql("SELECT id FROM attempts")[0][0]
        self.add("--attempt", aid)
        self.assertEqual(self.sql("SELECT attempt_id FROM confusion_attempts")[0][0], aid)

    def test_status_derivation(self):
        self.add()
        self.assertEqual(self.status(), "observed")
        self.jl("confusion", "taught", 1)
        self.assertEqual(self.status(), "drilling")
        for u, sk in (("u_a", "produce"), ("u_b", "comprehend")):
            self.sql("INSERT OR REPLACE INTO unit_state(unit,skill,tier,stability_days,due_ts,last_ts) VALUES(?,?,?,?,?,?)",
                     u, sk, "consolidated", 90.0, 1e12, 0.0)
        self.assertEqual(self.status(), "resolved")
        self.sql("UPDATE unit_state SET tier='review' WHERE unit='u_b'")
        self.assertEqual(self.status(), "drilling")

    def test_merge(self):
        self.add()
        self.add(members=("u_c", "u_d"), label="C vs D")
        self.jl("confusion", "merge", 2, 1)
        self.assertEqual(self.status(2), "merged")
        self.assertNotIn("C vs D", self.jl("confusion", "list").stdout)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions WHERE id=1")[0][0], 2)
        self.add(members=("u_c", "u_d"))
        self.assertEqual(self.sql("SELECT miss_count FROM confusions WHERE id=1")[0][0], 3)
        self.assertNotEqual(self.jl("confusion", "merge", 1, 1, ok=False).returncode, 0)

    def test_resume_shows_active_confusions(self):
        self.add()
        self.assertIn("進行中的混淆", self.jl("resume").stdout)

    def test_next_prefers_due_units_in_drilling_confusion(self):
        self.answer("b_free", "x")
        self.answer("e_long", "おきい")  # 上一題為 word，b_free(grammar) 不受交錯影響
        self.add(members=("u_b", "u_a"))
        self.jl("confusion", "taught", 1)
        self.sql("UPDATE unit_state SET due_ts=0 WHERE unit='u_b'")
        self.sql("UPDATE unit_state SET due_ts=-5 WHERE unit='u_e'")
        out = self.jl("next").stdout
        self.assertIn("item: b_free", out)


if __name__ == "__main__":
    unittest.main()
