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
    "items": [
        {"key": "a_free", "unit": "u_a", "kind": "phrase", "qtype": "free", "format": "produce",
         "prompt": "A?", "answers": ["お会計お願いします", "おかいけいおねがいします"], "jp": "お会計お願いします",
         "zh": "結帳", "scenario": "s", "stage": 0},
        {"key": "b_free", "unit": "u_b", "kind": "grammar", "qtype": "free", "format": "recall",
         "prompt": "B?", "answers": ["これをください", "これをくださいね"], "jp": "これをください",
         "zh": "給我這個", "scenario": "s", "stage": 0},
        {"key": "c_choice", "unit": "u_c", "kind": "phrase", "qtype": "choice", "format": "recognize",
         "prompt": "C?", "choices": ["で", "に", "が"], "answers": ["で"], "jp": "で", "zh": "在",
         "scenario": "s", "stage": 0},
        {"key": "d_free", "unit": "u_d", "kind": "word", "qtype": "free", "format": "recall",
         "prompt": "D?", "answers": ["找零"], "jp": "お釣り", "zh": "找零", "scenario": "s", "stage": 0},
        {"key": "e_long", "unit": "u_e", "kind": "word", "qtype": "free", "format": "recall",
         "prompt": "E?", "answers": ["おおきい"], "jp": "大きい", "zh": "大", "scenario": "s", "stage": 0},
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
        self.jl("init")  # 建立資料庫並匯入測試種子

    def tearDown(self):
        self.tmp.cleanup()

    def jl(self, *args, now=None, ok=True):
        env = dict(os.environ, JL_DB=str(self.db), JL_SEED=str(self.seed),
                   JL_NOW=now or self.now, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-X", "utf8", str(JL), *map(str, args)],
                           capture_output=True, text=True, encoding="utf-8", env=env)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r

    def sql(self, q, *p):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(q, p).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def item_id(self, key):
        return self.sql("SELECT id FROM items WHERE key=?", key)[0]["id"]

    def record(self, key, outcome, **kw):
        a = ["record", self.item_id(key), outcome, "--mode", "jp", "--answer", "x"]
        for k, v in kw.items():
            if k == "now":
                continue
            a += [f"--{k}", v]
        return self.jl(*a, now=kw.get("now"))


class TestGrading(Base):
    def grade(self, key, text):
        out = self.jl("grade", self.item_id(key), text).stdout
        return out.splitlines()[0].split(": ")[1]

    def test_exact_and_romaji(self):
        self.assertEqual(self.grade("a_free", "お会計お願いします"), "correct")
        self.assertEqual(self.grade("a_free", "okaikei onegaishimasu"), "correct")

    def test_particle_typed_as_wa_o_is_correct(self):
        self.assertEqual(self.grade("b_free", "これおください"), "correct")
        self.assertEqual(self.grade("a_free", "おかいけいおねがいします"), "correct")

    def test_particle_tolerance_does_not_hide_other_errors(self):
        self.assertEqual(self.grade("b_free", "これをくだらい"), "incorrect")

    def test_long_vowel_still_counts_when_kana(self):
        # 假名輸入時長音有無本身是考點，不得被抹平
        self.assertEqual(self.grade("e_long", "おおきい"), "correct")
        self.assertEqual(self.grade("e_long", "おきい"), "incorrect")

    def test_unknown(self):
        self.assertEqual(self.grade("a_free", "不知道"), "unknown")
        self.assertEqual(self.grade("a_free", "idk"), "unknown")

    def test_chinese_keyword(self):
        self.assertEqual(self.grade("d_free", "就是找零的意思"), "correct")

    def test_choice_by_letter_follows_shown_order(self):
        self.jl("next", "-n", 1)  # 不保證抽到 choice 題；直接寫入顯示順序
        iid = self.item_id("c_choice")
        self.sql("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", f"shown:{iid}",
                 json.dumps(["に", "で", "が"], ensure_ascii=False))
        self.assertEqual(self.grade("c_choice", "B"), "correct")
        self.assertEqual(self.grade("c_choice", "A"), "incorrect")


class TestScheduling(Base):
    def test_record_creates_state(self):
        self.record("a_free", "correct")
        r = self.sql("SELECT tier, lapses FROM item_state WHERE unit='u_a'")[0]
        self.assertEqual(r["tier"], "learning")
        self.assertEqual(r["lapses"], 0)

    def test_guess_correct_is_no_evidence(self):
        self.record("a_free", "correct", confidence="guess")
        r = self.sql("SELECT stability_days, reps_success FROM item_state WHERE unit='u_a'")[0]
        self.assertEqual(r["reps_success"], 0)
        self.assertAlmostEqual(r["stability_days"], 0.3)

    def test_context_basis_is_no_evidence(self):
        self.record("a_free", "correct", basis="context")
        self.assertEqual(self.sql("SELECT reps_success FROM item_state WHERE unit='u_a'")[0][0], 0)

    def test_sure_correct_counts(self):
        self.record("a_free", "correct", confidence="sure")
        self.assertEqual(self.sql("SELECT reps_success FROM item_state WHERE unit='u_a'")[0][0], 1)

    def test_lapse_counts_only_after_success(self):
        self.record("a_free", "incorrect")
        self.assertEqual(self.sql("SELECT lapses FROM item_state WHERE unit='u_a'")[0][0], 0)
        self.record("a_free", "correct", now="2026-01-02T10:00")
        self.record("a_free", "incorrect", now="2026-01-03T10:00")
        self.assertEqual(self.sql("SELECT lapses FROM item_state WHERE unit='u_a'")[0][0], 1)

    def test_rebuild_reproduces_state(self):
        self.record("a_free", "correct", confidence="sure")
        self.record("a_free", "incorrect", now="2026-01-03T10:00")
        before = [dict(r) for r in self.sql("SELECT * FROM item_state ORDER BY unit")]
        self.jl("rebuild")
        after = [dict(r) for r in self.sql("SELECT * FROM item_state ORDER BY unit")]
        self.assertEqual(before, after)

    def test_attempts_have_no_latency_column(self):
        cols = {r["name"] for r in self.sql("PRAGMA table_info(attempts)")}
        self.assertFalse({"latency", "latency_s", "quality"} & cols)

    def test_check_constraints_reject_bad_enum(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO attempts(item_id,unit,ts,outcome) VALUES(1,'u_a',0,'maybe')")


class TestNext(Base):
    def test_new_item_then_none_when_all_seen(self):
        seen = set()
        for _ in range(5):
            out = self.jl("next").stdout
            self.assertIn("item_id", out)
            key = out.split("key: ")[1].split()[0]
            seen.add(key)
            self.record(key, "correct", confidence="sure")
        self.assertEqual(len(seen), 5)
        self.assertIn("NO_ITEMS", self.jl("next").stdout)

    def test_choices_not_in_answer_output(self):
        out = self.jl("next", "-n", 5).stdout
        self.assertNotIn("answers", out)

    def test_retired_item_not_served(self):
        for k in ("a_free", "b_free", "c_choice", "d_free"):
            self.jl("retire", k, "--reason", "t")
        out = self.jl("next").stdout
        self.assertIn("e_long", out)

    def test_stage_gate(self):
        con = sqlite3.connect(self.db)
        self.jl("stats")
        con.execute("UPDATE items SET stage=2 WHERE key='e_long'")
        con.commit()
        con.close()
        keys = []
        for _ in range(4):
            out = self.jl("next").stdout
            keys.append(out.split("key: ")[1].split()[0])
            self.record(keys[-1], "correct", confidence="sure")
        self.assertNotIn("e_long", keys)


class TestImport(Base):
    def test_import_is_idempotent(self):
        self.jl("stats")
        n1 = self.sql("SELECT COUNT(*) FROM items")[0][0]
        f = Path(self.tmp.name) / "r.json"
        f.write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
        out = self.jl("import", f).stdout
        self.assertIn("skipped 5", out)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM items")[0][0], n1)


class TestMigration(Base):
    def test_fresh_db_is_v3_with_confusion_tables(self):
        self.jl("stats")
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='schema_version'")[0][0], "3")
        names = {r["name"] for r in self.sql("SELECT name FROM sqlite_master")}
        for n in ("confusions", "confusion_members", "confusion_attempts", "confusion_status"):
            self.assertIn(n, names)

    def test_v2_db_upgrades_and_keeps_data(self):
        self.jl("stats")
        self.record("a_free", "correct", confidence="sure")
        self.sql("DROP VIEW confusion_status")
        for t in ("confusion_attempts", "confusion_members", "confusions"):
            self.sql(f"DROP TABLE {t}")
        self.sql("UPDATE meta SET value='2' WHERE key='schema_version'")
        self.jl("stats")
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key='schema_version'")[0][0], "3")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM attempts")[0][0], 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 0)

    def test_v1_attempts_migrate_to_outcome(self):
        self.db.unlink()  # 以手工建立的 v1 資料庫取代
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, unit TEXT NOT NULL,
              kind TEXT NOT NULL, qtype TEXT NOT NULL, format TEXT NOT NULL, prompt TEXT NOT NULL, choices TEXT,
              answers TEXT NOT NULL, jp TEXT, reading TEXT, zh TEXT, scenario TEXT, stage INTEGER NOT NULL DEFAULT 0,
              explain TEXT, source TEXT);
            CREATE TABLE attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, unit TEXT NOT NULL,
              ts REAL NOT NULL, session_id INTEGER, quality INTEGER NOT NULL, latency_s REAL, answer_mode TEXT,
              user_answer TEXT);
            INSERT INTO items(key,unit,kind,qtype,format,prompt,answers) VALUES('k','u','word','free','recall','p','["x"]');
            INSERT INTO attempts(item_id,unit,ts,quality,answer_mode,user_answer) VALUES(1,'u',1000,4,'jp','x');
            INSERT INTO attempts(item_id,unit,ts,quality,answer_mode,user_answer) VALUES(1,'u',2000,0,'unknown','');
            INSERT INTO attempts(item_id,unit,ts,quality,answer_mode,user_answer) VALUES(1,'u',3000,1,'jp','y');
        """)
        con.commit()
        con.close()
        self.jl("stats")
        got = [r["outcome"] for r in self.sql("SELECT outcome FROM attempts ORDER BY id")]
        self.assertEqual(got, ["correct", "skipped", "incorrect"])


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
        out = self.add(members=("u_b", "u_a")).stdout  # 順序不同仍視為同一組
        self.assertIn("updated confusion 1", out)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 1)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions")[0][0], 2)

    def test_member_unit_must_exist(self):
        r = self.add(members=("u_a", "nope"), ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 0)

    def test_needs_two_distinct_members(self):
        self.assertNotEqual(self.add(members=("u_a", "u_a"), ok=False).returncode, 0)

    def test_gist_length_capped_in_cli_and_db(self):
        self.assertNotEqual(self.add(gist="あ" * 81, ok=False).returncode, 0)
        self.add()
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE confusions SET gist=? WHERE id=1", "あ" * 81)

    def test_enum_checks(self):
        self.add()
        for col, bad in (("kind", "x"), ("cause", "x"), ("axis", "x")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.sql(f"UPDATE confusions SET {col}=? WHERE id=1", bad)

    def test_unknown_attempt_rejected_without_side_effects(self):
        r = self.add("--attempt", "999", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM confusions")[0][0], 0)

    def test_attempt_link(self):
        self.record("a_free", "incorrect")
        aid = self.sql("SELECT id FROM attempts")[0][0]
        self.add("--attempt", aid)
        self.assertEqual(self.sql("SELECT attempt_id FROM confusion_attempts")[0][0], aid)

    def test_status_derivation(self):
        self.add()
        self.assertEqual(self.status(), "observed")
        self.jl("confusion", "taught", 1)
        self.assertEqual(self.status(), "drilling")  # 成員尚無 item_state
        for u in ("u_a", "u_b"):
            self.sql("INSERT OR REPLACE INTO item_state(unit,tier,stability_days,due_ts,last_ts) VALUES(?,?,?,?,?)",
                     u, "consolidated", 90.0, 1e12, 0.0)
        self.assertEqual(self.status(), "resolved")
        self.sql("UPDATE item_state SET tier='review' WHERE unit='u_b'")
        self.assertEqual(self.status(), "drilling")  # 舊的鞏固失效會重新開啟

    def test_untaught_filter_and_unit_filter(self):
        self.add()
        self.add(members=("u_c", "u_d"), label="C vs D")
        self.jl("confusion", "taught", 2)
        out = self.jl("confusion", "list", "--untaught").stdout
        self.assertIn("A vs B", out)
        self.assertNotIn("C vs D", out)
        out = self.jl("confusion", "list", "--unit", "u_d").stdout
        self.assertIn("C vs D", out)
        self.assertNotIn("A vs B", out)

    def test_merge(self):
        self.add()
        self.add(members=("u_c", "u_d"), label="C vs D")
        self.jl("confusion", "merge", 2, 1)
        self.assertEqual(self.status(2), "merged")
        self.assertNotIn("C vs D", self.jl("confusion", "list").stdout)
        self.assertIn("C vs D", self.jl("confusion", "list", "--status", "merged").stdout)
        self.assertEqual(self.sql("SELECT miss_count FROM confusions WHERE id=1")[0][0], 2)
        units = {r[0] for r in self.sql("SELECT unit FROM confusion_members WHERE confusion_id=1")}
        self.assertEqual(units, {"u_a", "u_b", "u_c", "u_d"})
        # 再回報已被併入的那組，計入目標
        self.add(members=("u_c", "u_d"))
        self.assertEqual(self.sql("SELECT miss_count FROM confusions WHERE id=1")[0][0], 3)

    def test_merge_rejects_self_and_missing(self):
        self.add()
        self.assertNotEqual(self.jl("confusion", "merge", 1, 1, ok=False).returncode, 0)
        self.assertNotEqual(self.jl("confusion", "merge", 1, 9, ok=False).returncode, 0)

    def test_taught_records_session_and_is_idempotent_safe(self):
        self.add()
        self.jl("confusion", "taught", 1)
        self.assertIsNotNone(self.sql("SELECT taught_at FROM confusions")[0][0])
        self.assertIn("no active", self.jl("confusion", "taught", 77).stdout)

    def test_resume_shows_active_confusions(self):
        self.add()
        self.assertIn("進行中的混淆", self.jl("resume").stdout)

    def test_next_prefers_due_units_in_drilling_confusion(self):
        for k in ("a_free", "d_free"):
            self.record(k, "incorrect")
        self.add(members=("u_a", "u_b"))
        self.jl("confusion", "taught", 1)
        self.sql("UPDATE item_state SET due_ts=0 WHERE unit IN ('u_a','u_d')")
        self.sql("UPDATE item_state SET due_ts=-5 WHERE unit='u_d'")  # u_d 本來更早到期
        out = self.jl("next").stdout
        self.assertIn("key: a_free", out)

    def test_attempts_untouched_by_confusion_ops(self):
        self.record("a_free", "incorrect")
        before = [tuple(r) for r in self.sql("SELECT * FROM attempts")]
        self.add()
        self.jl("confusion", "taught", 1)
        self.jl("confusion", "merge", 1, 1, ok=False)
        self.assertEqual(before, [tuple(r) for r in self.sql("SELECT * FROM attempts")])


if __name__ == "__main__":
    unittest.main()
