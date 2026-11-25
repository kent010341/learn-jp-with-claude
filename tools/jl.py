#!/usr/bin/env python3
"""日文反向學習專案 CLI（僅標準庫）。所有進度讀寫都經由這支程式。

用法：python tools/jl.py <command> [...]；`python tools/jl.py schema` 看資料表、view 與列舉值。
環境變數：JL_NOW=<epoch 或 ISO 時間> 模擬時間（驗證用）；JL_DB=<路徑>、JL_SEED=<目錄> 指定資料庫與種子目錄（驗證用）。

資料模型（schema v6）：
- 原始時序表（append-only）：rounds、presentations、attempts、attempt_reports、attempt_tags、item_edits、checkpoints。
- 衍生 aggregate（可由 rebuild 重算）：unit_state（unit×skill 排程）、unit_stats（unit×skill 計數）。
- 具名 view：v_* 與 confusion_status，供 `jl.py sql` 查詢。
"""
import argparse
import difflib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("JL_DB") or ROOT / "data" / "learning.db")
SEED_DIR = Path(os.environ.get("JL_SEED") or ROOT / "curriculum" / "seed")
SCHEMA_VERSION = 6

DEFAULT_META = {
    "stage": "0",
    "target_retention": "0.9",     # 目標保留率
    "cons_min_days": "3",          # 鞏固：至少幾個不同學習日成功
    "cons_gap_days": "21",         # 鞏固：最近兩次成功間隔皆需 >= 此天數
    "cons_min_stability": "60",    # 鞏固：穩定度需 >= 此天數
    "learn_step_min": "10",        # 答錯後最快幾分鐘再出現
    "day_start_hour": "4",         # 學習日的日界（此時刻前算前一天：中間要經過睡眠才算隔天）
    "round_max": "20",             # 一輪題數上限（round start 一次排好整輪）
    "new_item_ratio": "0.35",      # 題庫有未作答過的題時，每輪至少這個比例是新題
}

# 排程模型常數：FSRS 風格的簡化版，初始值未經個人資料擬合
_FACTOR = 19 / 81
_DECAY = -0.5
_S0 = {0: 0.3, 1: 0.5, 2: 0.8, 3: 1.5, 4: 3.0, 5: 5.0}
QUALITY_PASS = 3
MAX_PRODUCE_ANSWERS = 5           # 產出題只放參考答案，可接受範圍寫在 judge

# 列舉值（同時寫進 SQL CHECK，`jl.py schema` 會列出）
OUTCOMES = ("correct", "incorrect", "skipped")
CONFIDENCES = ("sure", "unsure", "guess")
BASES = ("known", "intuition", "context", "assisted", "other")
NO_EVIDENCE_BASES = ("context", "assisted", "other")  # 這些依據下的答對不算提取證據
MODES = ("jp", "romaji", "choice", "unknown")
GRADE_RESULTS = ("exact", "tolerant", "mismatch", "judged", "unknown")
SKILLS = ("comprehend", "produce")
GRADINGS = ("auto", "auto_then_judge", "judge")
UNIT_KINDS = ("kana", "word", "phrase", "grammar", "other")
PHASES = ("with_answer", "after_feedback", "correction")   # correction＝改判或撤回
NONE = "none"                                   # 自述欄位寫 none＝撤回先前的值
# 答錯原因（使用者自述）：slip 粗心/漏看；probe 卡在題目裡的未知元素；unreadable 整題看不懂
MISSES = ("slip", "probe", "unreadable")
LIGHT_MISSES = ("slip", "probe")            # 輕度失誤：有量到一部分
ENCOUNTER_KINDS = ("word", "grammar", "expression")
ENCOUNTER_LOCATIONS = ("prompt", "choice")
TAGS = ("particle", "conjugation", "vocab", "kana_form", "long_vowel", "sokuon", "register",
        "word_order", "unnatural", "omission", "meaning", "other")
CONF_KINDS = ("particle", "request_form", "politeness", "false_friend", "similar_form", "other")
CONF_CAUSES = ("zh_transfer", "overgeneralize", "form_mixup", "sound_mixup", "unknown")
CONF_AXES = ("form", "meaning", "usage")
CONF_STATUSES = ("observed", "drilling", "resolved", "merged")

# 初始題型；之後用 `jl.py task add` 擴充（不必改 schema）
DEFAULT_TASKS = (
    ("choice", "comprehend", "auto", "選擇題：從選項選出正確的一項"),
    ("natural_pick", "comprehend", "auto", "哪句自然：從幾個說法選出在該情境最自然的"),
    ("reading", "comprehend", "auto", "讀音：看日文寫出假名讀音"),
    ("meaning", "comprehend", "auto_then_judge", "意思：看/聽日文說出中文意思"),
    ("respond", "produce", "auto_then_judge", "情境回應：在情境中用日文回應"),
    ("translate", "produce", "auto_then_judge", "情境翻譯：「你想說 X」用日文怎麼說"),
    ("reorder", "produce", "auto_then_judge", "排序構句：把片段排成自然的句子"),
    ("cloze", "produce", "auto_then_judge", "挖空：在句中填入缺的部分（助詞、活用等）"),
)


def _in(vals):
    return "(" + ",".join(f"'{v}'" for v in vals) + ")"


# --------------------------------------------------------------------------- 時間
def now_ts():
    v = os.environ.get("JL_NOW")
    if not v:
        return time.time()
    try:
        return float(v)
    except ValueError:
        return datetime.fromisoformat(v).timestamp()


def learn_day(ts, hour):
    """以 day_start_hour 為日界的日序：凌晨 4 點前算前一天。"""
    return (datetime.fromtimestamp(ts) - timedelta(hours=int(hour))).toordinal()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts is not None else "-"


# --------------------------------------------------------------------------- 資料庫
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS task_types(
  task TEXT PRIMARY KEY,
  skill TEXT NOT NULL CHECK(skill IN {_in(SKILLS)}),
  grading TEXT NOT NULL CHECK(grading IN {_in(GRADINGS)}),
  description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units(
  unit TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN {_in(UNIT_KINDS)}),
  label TEXT, jp TEXT, zh TEXT,
  stage INTEGER NOT NULL DEFAULT 0,
  gist TEXT CHECK(gist IS NULL OR length(gist) <= 80)      -- 一行構式重點
);
CREATE TABLE IF NOT EXISTS unit_skills(
  unit TEXT NOT NULL REFERENCES units(unit),
  skill TEXT NOT NULL CHECK(skill IN {_in(SKILLS)}),
  PRIMARY KEY(unit, skill)
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  unit TEXT NOT NULL REFERENCES units(unit),
  task TEXT NOT NULL REFERENCES task_types(task),
  prompt TEXT NOT NULL,
  choices TEXT,                -- JSON list of text（有值＝選擇式作答）
  choice_units TEXT,           -- JSON list，與 choices 對齊：該選項對應的 unit（可為 null）
  answers TEXT NOT NULL,       -- JSON list，可接受答案（核心寫法即可；產出題的變體交由判斷）
  jp TEXT, reading TEXT, zh TEXT, scenario TEXT, why_natural TEXT, explain TEXT,
  stage INTEGER NOT NULL DEFAULT 0,
  source TEXT,
  retired TEXT,                -- NULL＝使用中；否則為停用原因（parked: 開頭＝暫停，可 unpark）
  probe TEXT,                  -- 刻意放入題目的未知元素（每題最多一個）
  judge TEXT                   -- 判分準則：考點、什麼算對、可接受的考點外差異（AI 依此判斷）
);
CREATE INDEX IF NOT EXISTS idx_items_unit ON items(unit);
CREATE INDEX IF NOT EXISTS idx_items_task ON items(task);
CREATE TABLE IF NOT EXISTS item_edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id), ts REAL NOT NULL,
  field TEXT NOT NULL, old TEXT, new TEXT
);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, started_ts REAL, ended_ts REAL);
CREATE TABLE IF NOT EXISTS rounds(            -- 一輪＝round start 一次排好的題目；手動 checkpoint 關閉
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES sessions(id),
  started_ts REAL NOT NULL, closed_ts REAL,
  planned INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS presentations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  ts REAL NOT NULL,
  session_id INTEGER REFERENCES sessions(id),
  shown_choices TEXT,          -- 實際顯示順序（JSON list）
  round_id INTEGER REFERENCES rounds(id),
  seq INTEGER                  -- 在該輪的順序（1 起）
);
CREATE INDEX IF NOT EXISTS idx_presentations_item ON presentations(item_id, ts);
CREATE INDEX IF NOT EXISTS idx_presentations_round ON presentations(round_id, seq);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  unit TEXT NOT NULL REFERENCES units(unit),
  presentation_id INTEGER UNIQUE REFERENCES presentations(id),
  ts REAL NOT NULL,
  learn_day INTEGER NOT NULL,
  session_id INTEGER REFERENCES sessions(id),
  outcome TEXT NOT NULL CHECK(outcome IN {_in(OUTCOMES)}),
  answer_mode TEXT NOT NULL CHECK(answer_mode IN {_in(MODES)}),
  user_answer TEXT,            -- 原始作答
  picked_text TEXT,            -- 選擇題實際選到的選項
  picked_unit TEXT,            -- 該選項對應的 unit
  grade_result TEXT CHECK(grade_result IS NULL OR grade_result IN {_in(GRADE_RESULTS)}),
  note TEXT                    -- 批改備註（使用者自述放 attempt_reports）
);
CREATE INDEX IF NOT EXISTS idx_attempts_unit ON attempts(unit, ts);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON attempts(item_id);
CREATE INDEX IF NOT EXISTS idx_attempts_day ON attempts(learn_day);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_attempts_outcome ON attempts(outcome);
CREATE INDEX IF NOT EXISTS idx_attempts_picked ON attempts(picked_unit);
CREATE TABLE IF NOT EXISTS attempt_reports(   -- 自述與改判（append-only；各欄最新的非空值有效，none＝撤回）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  ts REAL NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN {_in(PHASES)}),
  confidence TEXT CHECK(confidence IS NULL OR confidence IN {_in(CONFIDENCES + (NONE,))}),
  basis TEXT CHECK(basis IS NULL OR basis IN {_in(BASES + (NONE,))}),
  note TEXT,
  miss TEXT CHECK(miss IS NULL OR miss IN {_in(MISSES + (NONE,))}),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN {_in(OUTCOMES)})   -- 改判（phase=correction）
);
CREATE INDEX IF NOT EXISTS idx_reports_attempt ON attempt_reports(attempt_id);
CREATE TABLE IF NOT EXISTS encounters(    -- 在題目（題幹/選項）中看到但不認識的元素；答不出考點不記這裡
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  term TEXT NOT NULL UNIQUE,
  reading TEXT, zh TEXT,
  kind TEXT NOT NULL CHECK(kind IN {_in(ENCOUNTER_KINDS)}),
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  seen_count INTEGER NOT NULL DEFAULT 1,
  unit TEXT REFERENCES units(unit),       -- 已升格或本來就對應的 unit
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_encounters_unit ON encounters(unit);
CREATE TABLE IF NOT EXISTS encounter_attempts(
  encounter_id INTEGER NOT NULL REFERENCES encounters(id),
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  location TEXT NOT NULL CHECK(location IN {_in(ENCOUNTER_LOCATIONS)}),
  PRIMARY KEY(encounter_id, attempt_id)
);
CREATE TABLE IF NOT EXISTS attempt_tags(
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  tag TEXT NOT NULL CHECK(tag IN {_in(TAGS)}),
  PRIMARY KEY(attempt_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON attempt_tags(tag);
CREATE TABLE IF NOT EXISTS unit_state(
  unit TEXT NOT NULL REFERENCES units(unit),
  skill TEXT NOT NULL CHECK(skill IN {_in(SKILLS)}),
  tier TEXT NOT NULL CHECK(tier IN ('learning','review','consolidated')),
  difficulty REAL, stability_days REAL, due_ts REAL,
  reps_success INTEGER, lapses INTEGER,
  distinct_success_days INTEGER, max_successful_gap_days INTEGER,
  success_days TEXT, last_ts REAL,
  last_fail_day INTEGER,       -- 最近一次答錯/跳過的學習日（同日之後的答對不算成功日）
  PRIMARY KEY(unit, skill)
);
CREATE INDEX IF NOT EXISTS idx_unit_state_due ON unit_state(due_ts);
CREATE TABLE IF NOT EXISTS unit_stats(
  unit TEXT NOT NULL, skill TEXT NOT NULL,
  n INTEGER, n_correct INTEGER, n_incorrect INTEGER, n_skipped INTEGER,
  n_guess INTEGER, n_unsure INTEGER, n_no_evidence INTEGER, n_after_feedback_reports INTEGER,
  n_slip INTEGER, n_probe_miss INTEGER, n_unreadable INTEGER,
  last_incorrect_ts REAL, last_ts REAL, top_wrong_pick TEXT,
  PRIMARY KEY(unit, skill)
);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL,
  kind TEXT NOT NULL DEFAULT 'manual' CHECK(kind IN ('manual','auto')),
  summary TEXT, next_focus TEXT
);
CREATE TABLE IF NOT EXISTS confusions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN {_in(CONF_KINDS)}),
  cause TEXT NOT NULL CHECK(cause IN {_in(CONF_CAUSES)}),
  axis TEXT NOT NULL CHECK(axis IN {_in(CONF_AXES)}),
  gist TEXT NOT NULL CHECK(length(gist) <= 80),
  pair_key TEXT NOT NULL UNIQUE,
  miss_count INTEGER NOT NULL DEFAULT 1,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  taught_at REAL, taught_session INTEGER,
  merged_into INTEGER REFERENCES confusions(id)
);
CREATE TABLE IF NOT EXISTS confusion_members(
  confusion_id INTEGER NOT NULL REFERENCES confusions(id),
  unit TEXT NOT NULL REFERENCES units(unit),
  PRIMARY KEY(confusion_id, unit)
);
CREATE TABLE IF NOT EXISTS confusion_attempts(
  confusion_id INTEGER NOT NULL REFERENCES confusions(id),
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  PRIMARY KEY(confusion_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_confusion_members_unit ON confusion_members(unit);
CREATE INDEX IF NOT EXISTS idx_confusions_kind ON confusions(kind);
CREATE INDEX IF NOT EXISTS idx_confusions_last_seen ON confusions(last_seen);
"""

# view 每次連線時重建（與程式同步）
VIEWS = {
    "v_attempt_effective": """
SELECT a.id, a.item_id, a.unit, a.presentation_id, a.ts, a.learn_day, a.session_id,
  COALESCE((SELECT r.outcome FROM attempt_reports r WHERE r.attempt_id=a.id AND r.outcome IS NOT NULL
     ORDER BY r.id DESC LIMIT 1), a.outcome) AS outcome,
  a.outcome AS recorded_outcome, a.answer_mode, a.user_answer, a.picked_text, a.picked_unit, a.grade_result, a.note,
  i.key AS item_key, i.task AS task, t.skill AS skill,
  NULLIF((SELECT r.confidence FROM attempt_reports r WHERE r.attempt_id=a.id AND r.confidence IS NOT NULL
     ORDER BY r.id DESC LIMIT 1), 'none') AS confidence,
  NULLIF((SELECT r.basis FROM attempt_reports r WHERE r.attempt_id=a.id AND r.basis IS NOT NULL
     ORDER BY r.id DESC LIMIT 1), 'none') AS basis,
  NULLIF((SELECT r.miss FROM attempt_reports r WHERE r.attempt_id=a.id AND r.miss IS NOT NULL
     ORDER BY r.id DESC LIMIT 1), 'none') AS miss,
  EXISTS(SELECT 1 FROM attempt_reports r WHERE r.attempt_id=a.id AND r.phase='after_feedback') AS has_after_feedback,
  EXISTS(SELECT 1 FROM attempt_reports r WHERE r.attempt_id=a.id AND r.outcome IS NOT NULL) AS regraded,
  (SELECT group_concat(g.tag, ',') FROM attempt_tags g WHERE g.attempt_id=a.id) AS tags,
  i.probe AS probe
FROM attempts a JOIN items i ON i.id=a.item_id JOIN task_types t ON t.task=i.task""",
    "v_unit_status": """
SELECT u.unit, u.kind, u.stage, u.gist,
  (SELECT group_concat(k.skill, ',') FROM unit_skills k WHERE k.unit=u.unit) AS skills,
  EXISTS(SELECT 1 FROM items i WHERE i.unit=u.unit AND i.retired IS NULL) AS active,
  CASE
    WHEN NOT EXISTS(SELECT 1 FROM unit_state s WHERE s.unit=u.unit) THEN 'new'
    WHEN NOT EXISTS(SELECT 1 FROM unit_skills k LEFT JOIN unit_state s ON s.unit=k.unit AND s.skill=k.skill
                    WHERE k.unit=u.unit AND COALESCE(s.tier,'new')!='consolidated') THEN 'consolidated'
    WHEN EXISTS(SELECT 1 FROM unit_state s WHERE s.unit=u.unit AND s.tier='learning') THEN 'learning'
    ELSE 'review'
  END AS status
FROM units u""",
    "v_schedule": """
SELECT s.unit, s.skill, s.tier, s.stability_days, s.difficulty, s.lapses, s.reps_success,
  s.distinct_success_days, s.max_successful_gap_days, s.due_ts,
  datetime(s.due_ts,'unixepoch','localtime') AS due_at, datetime(s.last_ts,'unixepoch','localtime') AS last_at
FROM unit_state s""",
    "v_item_progress": """
SELECT i.id AS item_id, i.key, i.unit, i.task, t.skill, i.retired, i.scenario,
  (SELECT COUNT(*) FROM presentations p WHERE p.item_id=i.id) AS n_shown,
  (SELECT COUNT(*) FROM attempts a WHERE a.item_id=i.id) AS n_attempts,
  (SELECT COUNT(*) FROM attempts a WHERE a.item_id=i.id AND a.outcome='correct') AS n_correct,
  (SELECT COUNT(DISTINCT a.learn_day) FROM attempts a WHERE a.item_id=i.id AND a.outcome='correct') AS n_correct_days,
  (SELECT MAX(a.ts) FROM attempts a WHERE a.item_id=i.id) AS last_ts
FROM items i JOIN task_types t ON t.task=i.task""",
    "v_recent_errors": """
SELECT e.id AS attempt_id, datetime(e.ts,'unixepoch','localtime') AS at, e.learn_day, e.item_key, e.unit, e.task,
  e.skill, i.jp, i.zh, e.user_answer, e.picked_text, e.picked_unit, e.outcome, e.confidence, e.basis, e.tags, e.note
FROM v_attempt_effective e JOIN items i ON i.id=e.item_id WHERE e.outcome!='correct'""",
    "v_distractor_picks": """
SELECT unit, picked_unit, COUNT(*) AS n, MAX(ts) AS last_ts
FROM attempts WHERE picked_unit IS NOT NULL AND picked_unit!=unit GROUP BY unit, picked_unit""",
    "v_unseen_items": """
SELECT i.id AS item_id, i.key, i.unit, i.task, t.skill, i.source, i.stage
FROM items i JOIN task_types t ON t.task=i.task
WHERE i.retired IS NULL AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.item_id=i.id)""",
    "v_rounds": """
SELECT r.id, r.session_id, r.started_ts, r.closed_ts, r.planned,
  (SELECT COUNT(*) FROM presentations p JOIN attempts a ON a.presentation_id=p.id WHERE p.round_id=r.id) AS recorded,
  (SELECT MIN(p.seq) FROM presentations p WHERE p.round_id=r.id
     AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.presentation_id=p.id)) AS next_seq
FROM rounds r""",
    "v_basis_accuracy": """
SELECT task, skill, COALESCE(confidence,'unreported') AS confidence, COALESCE(basis,'unreported') AS basis,
  COUNT(*) AS n, SUM(outcome='correct') AS n_correct
FROM v_attempt_effective GROUP BY 1,2,3,4""",
    "v_tag_counts": """
SELECT g.tag, e.unit, e.outcome, COUNT(*) AS n, MAX(e.ts) AS last_ts
FROM attempt_tags g JOIN v_attempt_effective e ON e.id=g.attempt_id GROUP BY g.tag, e.unit, e.outcome""",
    "v_comprehend_vs_produce": """
SELECT u.unit, c.tier AS comp_tier, c.stability_days AS comp_S, p.tier AS prod_tier, p.stability_days AS prod_S,
  sc.n AS comp_n, sc.n_correct AS comp_ok, sp.n AS prod_n, sp.n_correct AS prod_ok
FROM units u
JOIN unit_skills k1 ON k1.unit=u.unit AND k1.skill='comprehend'
JOIN unit_skills k2 ON k2.unit=u.unit AND k2.skill='produce'
LEFT JOIN unit_state c ON c.unit=u.unit AND c.skill='comprehend'
LEFT JOIN unit_state p ON p.unit=u.unit AND p.skill='produce'
LEFT JOIN unit_stats sc ON sc.unit=u.unit AND sc.skill='comprehend'
LEFT JOIN unit_stats sp ON sp.unit=u.unit AND sp.skill='produce'""",
    "v_variants_needed": """
SELECT p.unit, p.skill, COUNT(*) AS n_items, group_concat(p.key, ',') AS keys,
  group_concat(p.scenario, ' / ') AS scenarios
FROM v_item_progress p LEFT JOIN unit_state s ON s.unit=p.unit AND s.skill=p.skill
WHERE p.retired IS NULL AND COALESCE(s.tier,'new')!='consolidated'
GROUP BY p.unit, p.skill HAVING MIN(p.n_correct_days) >= 2""",
    "v_skill_gaps": """
SELECT k.unit, k.skill FROM unit_skills k
WHERE NOT EXISTS(SELECT 1 FROM items i JOIN task_types t ON t.task=i.task
                 WHERE i.unit=k.unit AND t.skill=k.skill AND i.retired IS NULL)
  AND EXISTS(SELECT 1 FROM items i WHERE i.unit=k.unit AND i.retired IS NULL)""",
    "v_encounters": """
SELECT e.id, e.term, e.reading, e.zh, e.kind, e.seen_count, e.first_seen, e.last_seen, e.unit, e.note,
  CASE WHEN e.unit IS NULL THEN 'open' ELSE 'linked' END AS status,
  v.status AS unit_status,
  (SELECT COUNT(*) FROM encounter_attempts x JOIN attempts a ON a.id=x.attempt_id JOIN items i ON i.id=a.item_id
     WHERE x.encounter_id=e.id AND i.probe=e.term) AS n_as_probe,
  (SELECT group_concat(x.attempt_id || ':' || x.location, ',') FROM encounter_attempts x
     WHERE x.encounter_id=e.id) AS attempts
FROM encounters e LEFT JOIN v_unit_status v ON v.unit=e.unit""",
    "v_parked": """
SELECT i.id AS item_id, i.key, i.unit, i.task, substr(i.retired, 8) AS reason,
  (SELECT group_concat(e.term || '→' || COALESCE(e.unit, '-') || '(' || COALESCE(v.status, 'open') || ')', ', ')
     FROM encounters e JOIN encounter_attempts x ON x.encounter_id=e.id JOIN attempts a ON a.id=x.attempt_id
     LEFT JOIN v_unit_status v ON v.unit=e.unit WHERE a.item_id=i.id) AS encounters
FROM items i WHERE i.retired LIKE 'parked:%'""",
    "v_miss_reasons": """
SELECT unit, skill, task, miss, COUNT(*) AS n, MAX(ts) AS last_ts
FROM v_attempt_effective WHERE miss IS NOT NULL AND outcome!='correct' GROUP BY unit, skill, task, miss""",
    "confusion_status": """
SELECT c.id AS id,
  CASE
    WHEN c.merged_into IS NOT NULL THEN 'merged'
    WHEN c.taught_at IS NULL THEN 'observed'
    WHEN EXISTS(SELECT 1 FROM confusion_members m LEFT JOIN v_unit_status v ON v.unit=m.unit
                WHERE m.confusion_id=c.id AND COALESCE(v.status,'new')!='consolidated') THEN 'drilling'
    WHEN NOT EXISTS(SELECT 1 FROM confusion_members m WHERE m.confusion_id=c.id) THEN 'drilling'
    ELSE 'resolved'
  END AS status
FROM confusions c""",
}

VIEW_DOCS = {
    "v_attempt_effective": "attempts＋題型/skill＋最新自述(confidence/basis/miss，none＝撤回)＋改判後的 outcome＋tags",
    "v_unit_status": "每個 unit 的狀態 new/learning/review/consolidated（全部 skill 都鞏固才算）",
    "v_schedule": "unit×skill 排程（due_at、穩定度、lapses）",
    "v_item_progress": "每題被出幾次、答幾次、答對幾次",
    "v_recent_errors": "所有答錯/跳過（含選到的誘答項、tags、自述）",
    "v_distractor_picks": "誘答項被選次數（題目 unit → 被選的 unit）",
    "v_basis_accuracy": "依題型/信心/依據分組的答對數",
    "v_tag_counts": "tag × unit × outcome 次數（outcome=correct 的 tag＝考點以外的錯）",
    "v_unseen_items": "還沒作答過的有效題（判斷要不要出新題）",
    "v_rounds": "每輪：排了幾題、已記錄幾題、下一個未記錄的 seq",
    "v_comprehend_vs_produce": "同時有理解與產出的 unit：兩邊的狀態與答對數（找會認不會說）",
    "v_variants_needed": "該 skill 的每題都已在 2 個以上學習日答對、但未鞏固 → 需要新情境變體（避免只記住那一題）",
    "v_skill_gaps": "宣告了 skill 卻沒有該 skill 的有效題",
    "v_encounters": "題目中看到但不認識的元素：open（未升格）/linked、看到次數、是否為刻意放入的 probe",
    "v_parked": "暫停出題（整題看不懂等）的題目，與其 encounter 的 unit 進度（判斷何時 unpark）",
    "v_miss_reasons": "答錯原因自述（slip/probe/unreadable）× unit 次數",
    "confusion_status": "混淆對狀態 observed/drilling/resolved/merged",
}


def _peek_version(path):
    con = sqlite3.connect(path)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        if "meta" not in names:
            return None if "items" not in names else 0
        r = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(r[0]) if r else 0
    finally:
        con.close()


def _create(con):
    con.executescript(SCHEMA)
    for name in VIEWS:
        con.execute(f"DROP VIEW IF EXISTS {name}")
    for name, body in VIEWS.items():
        con.execute(f"CREATE VIEW {name} AS {body}")
    con.executemany("INSERT OR IGNORE INTO task_types(task,skill,grading,description) VALUES(?,?,?,?)", DEFAULT_TASKS)
    for k, v in DEFAULT_META.items():
        con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (k, v))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))


DERIVED_TABLES = ("unit_state", "unit_stats")   # 遷移時不搬，遷移後 rebuild 重算


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    migrated = None
    if DB_PATH.exists():
        v = _peek_version(DB_PATH)
        if v is not None and v < SCHEMA_VERSION:
            if v < 5:
                raise SystemExit(f"[migrate] 只支援從 schema v5 起遷移（目前 v{v}）；更舊的備份請用舊版 jl.py 先升到 v5")
            bak = DB_PATH.with_name(f"{DB_PATH.stem}.v{v}.bak.db")
            if not bak.exists():
                shutil.copy2(DB_PATH, bak)
            migrate_copy(DB_PATH, v)
            migrated = (v, bak.name)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    _create(con)
    con.commit()
    if migrated:
        rebuild_all(con)
        con.commit()
        print(f"[migrate] schema v{migrated[0]} -> v{SCHEMA_VERSION} 完成（備份：{migrated[1]}），已重算排程",
              file=sys.stderr)
    return con


def migrate_copy(path, old_version):
    """通用遷移：以目前的 SCHEMA 建新檔，逐表搬移兩邊都有的欄位，再替換原檔（原檔已備份）。
    開發期允許破壞性改寫（見 docs/development.md）：新欄位取預設值，衍生表不搬、之後 rebuild。"""
    tmp = path.with_name(path.name + ".migrating")
    if tmp.exists():
        tmp.unlink()
    new = sqlite3.connect(tmp)
    new.execute(f"ATTACH DATABASE ? AS old", (str(path),))
    try:
        new.executescript(SCHEMA)
        tables = [r[0] for r in new.execute("SELECT name FROM main.sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%'")]
        old_tables = {r[0] for r in new.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
        for t in tables:
            if t in DERIVED_TABLES or t not in old_tables:
                continue
            new_cols = [r[1] for r in new.execute(f"PRAGMA main.table_info({t})")]
            old_cols = {r[1] for r in new.execute(f"PRAGMA old.table_info({t})")}
            cols = ",".join(c for c in new_cols if c in old_cols)
            q = f"INSERT INTO main.{t}({cols}) SELECT {cols} FROM old.{t}"
            if t == "meta":
                q += " WHERE key NOT IN ('schema_version','round_closed')"
            new.execute(q)
        _post_migrate(new, old_version)
        new.commit()
        new.execute("DETACH DATABASE old")
    finally:
        new.close()
    os.replace(tmp, path)


def _post_migrate(con, old_version):
    """各版遷移後的值調整（AI 決定，只要下一輪測驗仍有意義）。"""
    if old_version < 6:
        # v5 沒有 rounds：舊的出題不屬於任何一輪；舊的 round_closed 旗標改由 rounds 表表示（無進行中的一輪）
        pass


def meta(con, key):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULT_META.get(key)


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def cfg_of(con):
    return {k: meta(con, k) for k in DEFAULT_META}


def task_map(con):
    return {r["task"]: dict(r) for r in con.execute("SELECT * FROM task_types")}


# --------------------------------------------------------------------------- 匯入
def _split_choices(raw):
    if not raw:
        return None, None
    texts, units = [], []
    for c in raw:
        if isinstance(c, dict):
            texts.append(c["text"])
            units.append(c.get("unit"))
        else:
            texts.append(c)
            units.append(None)
    return texts, (units if any(units) else None)


def upsert_unit(con, u, default_stage):
    con.execute("""INSERT INTO units(unit,kind,label,jp,zh,stage,gist) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(unit) DO UPDATE SET gist=COALESCE(excluded.gist,gist), label=COALESCE(excluded.label,label)""",
                (u["unit"], u.get("kind", "other"), u.get("label"), u.get("jp"), u.get("zh"),
                 int(u.get("stage", default_stage)), u.get("gist")))
    for s in u.get("skills", []):
        con.execute("INSERT OR IGNORE INTO unit_skills(unit,skill) VALUES(?,?)", (u["unit"], s))
    if u.get("from_encounter"):  # 由題目中遇到的未知元素升格而來
        n = con.execute("UPDATE encounters SET unit=? WHERE term=?", (u["unit"], u["from_encounter"])).rowcount
        if not n:
            raise SystemExit(f"[import] unit {u['unit']}: from_encounter 找不到 {u['from_encounter']!r}（`jl.py seen list`）")


def import_file(con, path, quiet=False):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    src = data.get("round_id") or Path(path).stem
    stage = int(data.get("stage", 0))
    tasks = task_map(con)
    new_keys = [it["key"] for it in data["items"]
                if not con.execute("SELECT 1 FROM items WHERE key=?", (it["key"],)).fetchone()]
    if new_keys and con.execute("SELECT 1 FROM items WHERE source=?", (src,)).fetchone():
        raise SystemExit(f"[import] round_id {src!r} 已被先前匯入的題目使用；請改用新的 round_id（建議 {next_round_id(con, src)}）")
    for u in data.get("units", []):
        upsert_unit(con, u, stage)
    added = skipped = 0
    for it in data["items"]:
        key = it["key"]
        if con.execute("SELECT 1 FROM items WHERE key=?", (key,)).fetchone():
            skipped += 1
            continue
        task = it.get("task")
        if task not in tasks:
            raise SystemExit(f"[import] {key}: 未知題型 task={task!r}（可用：{', '.join(tasks)}；新增題型用 `jl.py task add`）")
        unit = it["unit"]
        if not con.execute("SELECT 1 FROM units WHERE unit=?", (unit,)).fetchone():
            if not it.get("kind"):
                raise SystemExit(f"[import] {key}: unit {unit} 不存在，需在 units 段宣告或在題目填 kind")
            upsert_unit(con, {"unit": unit, "kind": it["kind"], "label": it.get("jp"), "jp": it.get("jp"),
                              "zh": it.get("zh"), "stage": it.get("stage", stage)}, stage)
        texts, cunits = _split_choices(it.get("choices"))
        answers = it.get("answers") or []
        if texts and not any(a in texts for a in answers):
            raise SystemExit(f"[import] {key}: 選擇式題目的 answers 必須出現在 choices 中")
        if not answers and tasks[task]["grading"] != "judge":
            raise SystemExit(f"[import] {key}: 缺少 answers")
        if tasks[task]["skill"] == "produce" and len(answers) > MAX_PRODUCE_ANSWERS:
            raise SystemExit(f"[import] {key}: 產出題的 answers 只放 1–{MAX_PRODUCE_ANSWERS} 個參考答案，"
                             "可接受的範圍寫在 judge（由 AI 判斷），不要窮舉")
        con.execute(
            """INSERT INTO items(key,unit,task,prompt,choices,choice_units,answers,jp,reading,zh,scenario,why_natural,
               explain,stage,source,probe,judge) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, unit, task, it["prompt"],
             json.dumps(texts, ensure_ascii=False) if texts else None,
             json.dumps(cunits, ensure_ascii=False) if cunits else None,
             json.dumps(answers, ensure_ascii=False), it.get("jp"), it.get("reading"), it.get("zh"),
             it.get("scenario"), it.get("why_natural"), it.get("explain"), int(it.get("stage", stage)), src,
             it.get("probe"), it.get("judge")))
        con.execute("INSERT OR IGNORE INTO unit_skills(unit,skill) VALUES(?,?)", (unit, tasks[task]["skill"]))
        added += 1
    if added and not quiet:
        set_meta(con, "last_import", json.dumps({"source": src, "n": added, "ts": now_ts()}, ensure_ascii=False))
    con.commit()
    if not quiet:
        print(f"imported {src}: +{added}, skipped {skipped}")
    return added


def next_round_id(con, src):
    m = re.match(r"(.*_r)(\d+)$", src)
    prefix = m.group(1) if m else src + "_r"
    nums = [int(x[0][len(prefix):]) for x in con.execute("SELECT DISTINCT source FROM items WHERE source LIKE ?",
                                                         (prefix + "%",)) if x[0][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def ensure_seeds(con):
    """有新增 seed 時自動匯入（以 key 去重）。"""
    if SEED_DIR.is_dir():
        for p in sorted(SEED_DIR.glob("*.json")):
            import_file(con, p, quiet=True)


# --------------------------------------------------------------------------- 羅馬字轉假名
_V = "aiueo"
_ROM = {}


def _fill():
    _ROM.update({
        "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お",
        "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
        "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
        "sa": "さ", "shi": "し", "si": "し", "su": "す", "se": "せ", "so": "そ",
        "za": "ざ", "ji": "じ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
        "ta": "た", "chi": "ち", "ti": "ち", "tsu": "つ", "tu": "つ", "te": "て", "to": "と",
        "da": "だ", "di": "ぢ", "du": "づ", "de": "で", "do": "ど",
        "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
        "ha": "は", "hi": "ひ", "fu": "ふ", "hu": "ふ", "he": "へ", "ho": "ほ",
        "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
        "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
        "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
        "ya": "や", "yu": "ゆ", "yo": "よ",
        "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
        "wa": "わ", "wo": "を", "wi": "うぃ", "we": "うぇ",
        "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
        "je": "じぇ", "she": "しぇ", "che": "ちぇ",
        "xtu": "っ", "ltu": "っ", "xtsu": "っ", "ltsu": "っ",
        "xya": "ゃ", "xyu": "ゅ", "xyo": "ょ", "lya": "ゃ", "lyu": "ゅ", "lyo": "ょ",
    })
    youon = {"ky": "き", "gy": "ぎ", "sh": "し", "sy": "し", "j": "じ", "zy": "じ", "jy": "じ",
             "ch": "ち", "ty": "ち", "ny": "に", "hy": "ひ", "by": "び", "py": "ぴ", "my": "み",
             "ry": "り", "dy": "ぢ"}
    for pre, k in youon.items():
        for v, s in (("a", "ゃ"), ("u", "ゅ"), ("o", "ょ")):
            _ROM[pre + v] = k + s


_fill()
_MACRON = {"ā": "aa", "ī": "ii", "ū": "uu", "ē": "ee", "ō": "ou",
           "â": "aa", "î": "ii", "û": "uu", "ê": "ee", "ô": "ou"}


def romaji_to_kana(s):
    s = s.lower()
    for k, v in _MACRON.items():
        s = s.replace(k, v)
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if not ("a" <= c <= "z" or c == "'"):
            out.append(c)
            i += 1
            continue
        if c == "'":
            i += 1
            continue
        if c == "n":
            nx = s[i + 1] if i + 1 < n else ""
            if nx == "n" and (i + 2 >= n or s[i + 2] not in _V + "y"):
                out.append("ん")
                i += 2
                continue
            if not (nx and (nx in _V or nx == "y")):
                out.append("ん")
                i += 1
                continue
        if i + 1 < n and c not in _V and c != "n" and (s[i + 1] == c or (c == "t" and s[i + 1] == "c")):
            out.append("っ")
            i += 1
            continue
        for ln in (4, 3, 2, 1):
            seg = s[i:i + ln]
            if seg in _ROM:
                out.append(_ROM[seg])
                i += ln
                break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def kata_to_hira(s):
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)


_PUNCT = re.compile(r"[\s。、！？!?.,，．・「」『』〜～~'\"“”‘’()（）\[\]\-—…]+")


def _expand_choon(s):
    out = []
    vow = {"あかがさざただなはばぱまやゃらわ": "あ", "いきぎしじちぢにひびぴみり": "い",
           "うくぐすずつづぬふぶぷむゆゅる": "う", "えけげせぜてでねへべぺめれ": "え", "おこごそぞとどのほぼぽもよょろを": "お"}
    for ch in s:
        if ch == "ー" and out:
            for grp, v in vow.items():
                if out[-1] in grp:
                    out.append(v)
                    break
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def norm(s, loose):
    """loose=True：羅馬字輸入模式（羅馬字無法區分的差異一律抹平）。"""
    s = unicodedata.normalize("NFKC", s).strip().lower()
    s = _PUNCT.sub("", s)
    if loose:
        s = romaji_to_kana(s)
        s = kata_to_hira(s)
        s = _expand_choon(s)
        s = s.translate(str.maketrans({"は": "わ", "へ": "え", "を": "お", "ぢ": "じ", "づ": "ず"}))
        s = re.sub("([おこごそぞとどのほぼぽもよょろ])う", lambda m: m.group(1) + "お", s)  # ou ≡ oo
        s = re.sub("([けげせぜてでねべぺめれえ])い", lambda m: m.group(1) + "え", s)  # ei ≡ ee
    return s


# --------------------------------------------------------------------------- 判分
UNKNOWN = {"不知道", "不知", "不會", "不会", "idk", "dk", "?", "不曉得", "わからない", "わかりません",
           "wakaranai", "wakarimasen", "shiranai", "pass", "skip", "不確定", "忘了", "不記得"}
_KANA = re.compile(r"[ぁ-ゟ゠-ヿ]")
_ASCII = re.compile(r"[a-zA-Z]")
_CJK = re.compile(r"[一-鿿]")
_PARTICLE_IME = str.maketrans({"は": "わ", "を": "お"})


def is_chinese_answer(a):
    return not _KANA.search(a) and not _ASCII.search(a)


def grade_answer(item, raw, choices=None, extra=()):
    """回傳 {result: correct|incorrect|unknown|ambiguous, how: exact|tolerant|None, mode, matched, near}。
    ambiguous＝程式無法確定（交給判斷）。extra：額外可接受的寫法（例如題目的假名讀音）。"""
    raw = (raw or "").strip()
    answers = json.loads(item["answers"])
    answers += [a for a in extra if a and a not in answers]
    if choices is None:
        choices = json.loads(item["choices"]) if item["choices"] else None
    low = unicodedata.normalize("NFKC", raw).strip().lower()
    base = {"how": None, "matched": None, "near": False}
    if low in UNKNOWN or low.replace("？", "?") in UNKNOWN or low == "":
        return {**base, "result": "unknown", "mode": "unknown"}
    if choices:  # 選擇式：字母 / 數字 / 文字
        picked = None
        if len(low) == 1 and "a" <= low <= "z" and ord(low) - 97 < len(choices):
            picked = choices[ord(low) - 97]
        elif low.isdigit() and 1 <= int(low) <= len(choices):
            picked = choices[int(low) - 1]
        else:
            for c in choices:
                if norm(c, False) == norm(raw, False) or norm(c, True) == norm(raw, True):
                    picked = c
                    break
        if picked is None:
            return {**base, "result": "ambiguous", "mode": "choice"}
        ok = picked in answers
        return {**base, "result": "correct" if ok else "incorrect", "how": "exact", "mode": "choice", "matched": picked}
    has_jp = bool(_KANA.search(raw) or _CJK.search(raw))
    loose = not has_jp
    mode = "jp" if has_jp else "romaji"
    u = norm(raw, loose)
    for a in answers:
        if norm(a, loose) == u:
            return {**base, "result": "correct", "how": "exact", "mode": mode, "matched": a}
    if has_jp:
        kana_item = item["unit_kind"] == "kana" if "unit_kind" in item.keys() else False
        if not kana_item:
            # 輸入法差異：助詞 は/を 以 わ/お 輸入，視為完全正確（長音等其他差異不抹平）
            for a in answers:
                if norm(a, False).translate(_PARTICLE_IME) == u.translate(_PARTICLE_IME):
                    return {**base, "result": "correct", "how": "exact", "mode": mode, "matched": a}
            # 片假名/平假名混用：視為正確但標 tolerant
            for a in answers:
                if _KANA.search(a) and norm(a, True) == norm(raw, True):
                    return {**base, "result": "correct", "how": "tolerant", "mode": mode, "matched": a}
    zh_answers = [a for a in answers if is_chinese_answer(a)]
    if zh_answers and _CJK.search(raw) and not _KANA.search(raw):
        # 中文意思：只接受與關鍵詞完全相同（去標點）；其餘交給判斷（避免「不是出口」被判對）
        r = _PUNCT.sub("", unicodedata.normalize("NFKC", raw))
        for a in zh_answers:
            if _PUNCT.sub("", unicodedata.normalize("NFKC", a)) == r:
                return {**base, "result": "correct", "how": "exact", "mode": mode, "matched": a}
        return {**base, "result": "ambiguous", "mode": mode}
    if answers and all(is_chinese_answer(a) for a in answers):
        return {**base, "result": "ambiguous", "mode": mode}
    best = max((difflib.SequenceMatcher(None, u, norm(a, loose)).ratio() for a in answers), default=0)
    return {**base, "result": "incorrect", "mode": mode, "near": best >= 0.7}


# --------------------------------------------------------------------------- 排程
def retr(t_days, S):
    return (1 + _FACTOR * max(t_days, 0) / max(S, 1e-6)) ** _DECAY


def interval_days(S, r):
    return S / _FACTOR * (r ** (1 / _DECAY) - 1)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def empty_state():
    return {"tier": "new", "difficulty": None, "stability_days": None, "due_ts": None,
            "reps_success": 0, "lapses": 0, "distinct_success_days": 0,
            "max_successful_gap_days": 0, "success_days": [], "last_ts": None, "last_fail_day": None}


def is_consolidated(st, cfg, elapsed):
    # 最近一次作答本身必須是「隔了夠久仍提取成功」，避免失敗後短時間內答對就恢復鞏固
    if elapsed < float(cfg["cons_gap_days"]):
        return False
    days = st["success_days"]
    if len(days) < int(cfg["cons_min_days"]):
        return False
    gaps = [b - a for a, b in zip(days, days[1:])]
    if len(gaps) < 2 or min(gaps[-2:]) < int(cfg["cons_gap_days"]):
        return False
    return st["stability_days"] >= float(cfg["cons_min_stability"])


def evidence(outcome, confidence, basis, mode, miss=None):
    """(結果, 信心, 依據, 輸入形式, 答錯原因) → 0 略過／1 答錯／'L' 輕度失誤／'N' 無證據／'W' 弱證據／3、4 一般答對。
    答錯原因只由使用者自述：slip、probe → 輕度失誤；unreadable（整題看不懂，沒量到考點）→ 無證據。不使用作答速度。"""
    if outcome != "correct" and miss == "unreadable":
        return "N"
    if outcome == "skipped":
        return 0
    if outcome == "incorrect":
        return "L" if miss in LIGHT_MISSES else 1
    if basis in NO_EVIDENCE_BASES or confidence == "guess":
        return "N"
    if confidence == "unsure":
        return "W"
    return 4 if mode in ("jp", "romaji") else 3


def apply_attempt(st, ev, ts, cfg):
    st = dict(st, success_days=list(st["success_days"]))
    if ev == "N":  # 無證據：不改穩定度、不算失誤，只確保約一天內會再測
        if st["last_ts"] is None:
            st.update(stability_days=0.3, difficulty=7.0, last_ts=ts, tier="learning", due_ts=ts + 86400)
        else:
            st["due_ts"] = min(st["due_ts"], ts + 86400)
        return st
    if ev == "L":  # 輕度失誤（粗心、卡在題目裡的未知元素）：穩定度小降、不計 lapse、不動成功日，約一天內重測
        if st["last_ts"] is None:
            st.update(stability_days=_S0[2], difficulty=7.0)
        else:
            st["stability_days"] = max(0.3, st["stability_days"] * 0.85)
            st["difficulty"] = clamp(st["difficulty"] + 0.2, 1, 10)
        S = st["stability_days"]
        st["last_ts"] = ts
        st["due_ts"] = ts + max(min(1.0, interval_days(S, float(cfg["target_retention"]))),
                                float(cfg["learn_step_min"]) / 1440) * 86400
        if st["tier"] != "consolidated":
            st["tier"] = "learning" if S < 7 else "review"
        return st
    weak = ev == "W"
    q = 3 if weak else ev
    elapsed = (ts - st["last_ts"]) / 86400 if st["last_ts"] is not None else 0
    retention = float(cfg["target_retention"])
    if st["last_ts"] is None:
        st["stability_days"] = _S0[2 if weak else q]
        st["difficulty"] = clamp(9 - 1.5 * q, 1, 10)
    else:
        S, D = st["stability_days"], st["difficulty"]
        R = retr(elapsed, S)
        if q >= QUALITY_PASS:
            g = math.exp(0.9) * (11 - D) * S ** -0.15 * (math.exp(1.0 - R) - 1)
            S = min(S * (1 + g * {3: 0.8, 4: 1.0, 5: 1.3}[q] * (0.4 if weak else 1.0)), 1825)
            D = clamp(D + {3: 0.2, 4: -0.1, 5: -0.3}[q], 1, 10)
        else:
            S = max(0.3, S * (0.2 + 0.05 * q))   # 下調但不歸零
            D = clamp(D + 0.8, 1, 10)
            if st["reps_success"] > 0:
                st["lapses"] += 1
        st["stability_days"], st["difficulty"] = S, D
    if q >= QUALITY_PASS and not weak:  # 弱證據不計入鞏固所需的成功日
        st["reps_success"] += 1
        d = learn_day(ts, cfg["day_start_hour"])
        if d not in st["success_days"]:
            st["success_days"].append(d)
            st["success_days"].sort()
    sd = st["success_days"]
    st["distinct_success_days"] = len(sd)
    st["max_successful_gap_days"] = max([b - a for a, b in zip(sd, sd[1:])], default=0)
    st["last_ts"] = ts
    S = st["stability_days"]
    if q < QUALITY_PASS:
        st["due_ts"] = ts + float(cfg["learn_step_min"]) * 60
        st["tier"] = "learning" if S < 7 else "review"
    else:
        st["due_ts"] = ts + max(interval_days(S, retention), float(cfg["learn_step_min"]) / 1440) * 86400
        if is_consolidated(st, cfg, elapsed):
            st["tier"] = "consolidated"
        elif st["reps_success"] >= 2 and S >= 7:
            st["tier"] = "review"
        else:
            st["tier"] = "learning"
    return st


def save_state(con, unit, skill, st):
    con.execute(
        """INSERT OR REPLACE INTO unit_state(unit,skill,tier,difficulty,stability_days,due_ts,reps_success,lapses,
           distinct_success_days,max_successful_gap_days,success_days,last_ts,last_fail_day)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (unit, skill, st["tier"], st["difficulty"], st["stability_days"], st["due_ts"], st["reps_success"],
         st["lapses"], st["distinct_success_days"], st["max_successful_gap_days"], json.dumps(st["success_days"]),
         st["last_ts"], st["last_fail_day"]))


def refresh_unit(con, unit, cfg=None):
    """由 attempts（＋最新自述）重算該 unit 的 unit_state 與 unit_stats。"""
    cfg = cfg or cfg_of(con)
    con.execute("DELETE FROM unit_state WHERE unit=?", (unit,))
    states = {}
    for a in con.execute("""SELECT ts, learn_day, outcome, confidence, basis, answer_mode, miss, skill
                            FROM v_attempt_effective WHERE unit=? ORDER BY ts, id""", (unit,)):
        st = states.get(a["skill"]) or empty_state()
        ev = evidence(a["outcome"], a["confidence"], a["basis"], a["answer_mode"], a["miss"])
        if ev in (3, 4) and st["last_fail_day"] == a["learn_day"]:
            ev = "W"  # 同一學習日已答錯（看過正解）後的答對只是短期記憶：弱證據，不算成功日
        st = apply_attempt(st, ev, a["ts"], cfg)
        if a["outcome"] != "correct":
            st["last_fail_day"] = a["learn_day"]
        states[a["skill"]] = st
    for skill, st in states.items():
        save_state(con, unit, skill, st)
    con.execute("DELETE FROM unit_stats WHERE unit=?", (unit,))
    con.execute(f"""
        INSERT INTO unit_stats(unit,skill,n,n_correct,n_incorrect,n_skipped,n_guess,n_unsure,n_no_evidence,
          n_after_feedback_reports,n_slip,n_probe_miss,n_unreadable,last_incorrect_ts,last_ts,top_wrong_pick)
        SELECT e.unit, e.skill, COUNT(*),
          SUM(e.outcome='correct'), SUM(e.outcome='incorrect'), SUM(e.outcome='skipped'),
          SUM(CASE WHEN e.confidence='guess' THEN 1 ELSE 0 END),
          SUM(CASE WHEN e.confidence='unsure' THEN 1 ELSE 0 END),
          SUM(CASE WHEN e.outcome='correct' AND (e.confidence='guess' OR e.basis IN {_in(NO_EVIDENCE_BASES)})
              THEN 1 ELSE 0 END),
          SUM(e.has_after_feedback),
          SUM(CASE WHEN e.outcome!='correct' AND e.miss='slip' THEN 1 ELSE 0 END),
          SUM(CASE WHEN e.outcome!='correct' AND e.miss='probe' THEN 1 ELSE 0 END),
          SUM(CASE WHEN e.outcome!='correct' AND e.miss='unreadable' THEN 1 ELSE 0 END),
          MAX(CASE WHEN e.outcome='incorrect' THEN e.ts END), MAX(e.ts),
          (SELECT x.picked_unit FROM v_attempt_effective x WHERE x.unit=e.unit AND x.skill=e.skill
             AND x.picked_unit IS NOT NULL AND x.picked_unit!=x.unit GROUP BY x.picked_unit
             ORDER BY COUNT(*) DESC LIMIT 1)
        FROM v_attempt_effective e WHERE e.unit=? GROUP BY e.unit, e.skill""", (unit,))


def rebuild_all(con):
    cfg = cfg_of(con)
    con.execute("DELETE FROM unit_state")
    con.execute("DELETE FROM unit_stats")
    units = [r["unit"] for r in con.execute("SELECT DISTINCT unit FROM attempts")]
    for u in units:
        refresh_unit(con, u, cfg)
    return len(units)


# --------------------------------------------------------------------------- 出題
def current_session(con, create=False):
    sid = meta(con, "current_session")
    if sid and con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        return int(sid)
    if not create:
        return None
    return new_session(con)


def new_session(con):
    cur = con.execute("INSERT INTO sessions(started_ts) VALUES(?)", (now_ts(),))
    set_meta(con, "current_session", cur.lastrowid)
    con.commit()
    return cur.lastrowid


def active_items(con, unit, skill):
    return con.execute("""SELECT i.*, u.kind AS unit_kind FROM items i JOIN task_types t ON t.task=i.task
                          JOIN units u ON u.unit=i.unit
                          WHERE i.unit=? AND t.skill=? AND i.retired IS NULL""", (unit, skill)).fetchall()


def pick_item(con, unit, skill, last_task=None, exclude=()):
    rows = [r for r in active_items(con, unit, skill) if r["id"] not in exclude]
    if not rows:
        return None
    ok = {r[0] for r in con.execute(
        "SELECT DISTINCT item_id FROM v_attempt_effective WHERE unit=? AND outcome='correct'", (unit,))}
    last = {}
    for r in con.execute("SELECT item_id, ts, outcome FROM v_attempt_effective WHERE unit=? ORDER BY ts, id", (unit,)):
        last[r["item_id"]] = (r["ts"], r["outcome"])
    # 優先：上次不是答錯的題（答錯後換同 unit 的另一題重測）→ 尚未答對過 → 最久沒作答 → 與上一題不同題型
    rows = sorted(rows, key=lambda r: (last.get(r["id"], (0, None))[1] not in (None, "correct"),
                                       r["id"] in ok, last.get(r["id"], (0, None))[0],
                                       r["task"] == last_task, r["id"]))
    return rows[0]


def active_confusion_units(con):
    return {r["unit"] for r in con.execute(
        """SELECT m.unit FROM confusion_members m JOIN confusion_status v ON v.id=m.confusion_id
           WHERE v.status='drilling'""")}


def due_pairs(con, ts):
    """到期的 unit×skill：進行中混淆的 unit 優先，其次最易忘。"""
    due = con.execute("""SELECT s.unit, s.skill, s.stability_days, s.last_ts FROM unit_state s
                         WHERE s.due_ts<=? AND EXISTS(SELECT 1 FROM items i JOIN task_types t ON t.task=i.task
                           WHERE i.unit=s.unit AND t.skill=s.skill AND i.retired IS NULL)""", (ts,)).fetchall()
    due = sorted(due, key=lambda r: retr((ts - r["last_ts"]) / 86400, r["stability_days"]))
    act = active_confusion_units(con)
    due = sorted(due, key=lambda r: r["unit"] not in act)
    return [(r["unit"], r["skill"]) for r in due]


def unseen_items(con):
    """還沒作答過、可出題（stage 內）的題，依匯入順序。"""
    return con.execute("""SELECT i.*, u.kind AS unit_kind, v.skill FROM v_unseen_items v JOIN items i ON i.id=v.item_id
                          JOIN units u ON u.unit=i.unit WHERE i.stage<=? ORDER BY i.id""",
                       (int(meta(con, "stage")),)).fetchall()


def unseen_summary(con):
    rows = con.execute("""SELECT source, COUNT(*) n FROM v_unseen_items WHERE stage<=? GROUP BY source
                          ORDER BY MIN(item_id)""", (int(meta(con, "stage")),)).fetchall()
    total = sum(r["n"] for r in rows)
    return total, "、".join(f"{r['source']}×{r['n']}" for r in rows)


def plan_round(con, n, ts):
    """排一整輪：到期的 unit 與未作答過的新題各佔名額；同一輪每個 unit 只出一題，最後交錯類型與題型。"""
    kinds = {r["unit"]: r["kind"] for r in con.execute("SELECT unit, kind FROM units")}
    started = {(r["unit"], r["skill"]) for r in con.execute("SELECT unit, skill FROM unit_state")}
    comp_units = {r[0] for r in con.execute(
        "SELECT DISTINCT i.unit FROM items i JOIN task_types t ON t.task=i.task "
        "WHERE t.skill='comprehend' AND i.retired IS NULL")}
    fresh = unseen_items(con)
    fresh_ids = {r["id"] for r in fresh}
    reserve = min(math.ceil(n * float(meta(con, "new_item_ratio"))), len({r["unit"] for r in fresh}))
    plan, taken = [], set()

    def n_new():
        return sum(1 for it in plan if it["id"] in fresh_ids)

    def take_due(limit_fn):
        for u, sk in due:
            if len(plan) >= limit_fn():
                break
            if u in taken:
                continue
            it = pick_item(con, u, sk)
            if it:
                plan.append(it)
                taken.add(u)

    due = due_pairs(con, ts)
    take_due(lambda: n - max(reserve - n_new(), 0))      # 1. 到期的，保留新題名額
    for it in fresh:                                       # 2. 新題（產出排在理解之後）
        if len(plan) >= n:
            break
        if it["unit"] in taken:
            continue
        if it["skill"] == "produce" and it["unit"] in comp_units and (it["unit"], "comprehend") not in started:
            continue
        plan.append(it)
        taken.add(it["unit"])
    take_due(lambda: n)                                    # 3. 名額還有剩就補到期的
    today = learn_day(ts, meta(con, "day_start_hour"))
    early = con.execute("""SELECT unit, skill, stability_days, last_ts FROM unit_state
                           WHERE due_ts>? AND last_ts IS NOT NULL""", (ts,)).fetchall()
    early = [r for r in early if learn_day(r["last_ts"], meta(con, "day_start_hour")) != today]
    due = [(r["unit"], r["skill"]) for r in sorted(early, key=lambda r: retr((ts - r["last_ts"]) / 86400,
                                                                             r["stability_days"]))]
    take_due(lambda: n)                                    # 4. 還有剩：提早複習最易忘的（今天已作答過的不出）
    ordered, last_kind, last_task = [], None, None
    pool = plan[:]
    while pool:  # 交錯：盡量與上一題不同類型、不同題型
        it = (next((x for x in pool if kinds[x["unit"]] != last_kind and x["task"] != last_task), None)
              or next((x for x in pool if kinds[x["unit"]] != last_kind), None)
              or next((x for x in pool if x["task"] != last_task), None) or pool[0])
        pool.remove(it)
        ordered.append(it)
        last_kind, last_task = kinds[it["unit"]], it["task"]
    return ordered, fresh_ids


def open_round(con):
    return con.execute("SELECT * FROM v_rounds WHERE closed_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def _present(con, item, sid, rid, seq, ts):
    shown = None
    if item["choices"]:
        shown = json.loads(item["choices"])
        random.shuffle(shown)
    con.execute("INSERT INTO presentations(item_id,ts,session_id,shown_choices,round_id,seq) VALUES(?,?,?,?,?,?)",
                (item["id"], ts, sid, json.dumps(shown, ensure_ascii=False) if shown else None, rid, seq))


def print_cards(con, rid, pending_only=True):
    q = """SELECT p.seq, p.shown_choices, i.*, u.kind AS unit_kind, u.gist, t.skill, s.tier,
             NOT EXISTS(SELECT 1 FROM attempts x WHERE x.item_id=i.id AND x.presentation_id!=p.id) AS is_new
           FROM presentations p JOIN items i ON i.id=p.item_id JOIN units u ON u.unit=i.unit
           JOIN task_types t ON t.task=i.task LEFT JOIN unit_state s ON s.unit=i.unit AND s.skill=t.skill
           WHERE p.round_id=?""" + (" AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.presentation_id=p.id)"
                                    if pending_only else "") + " ORDER BY p.seq"
    for r in con.execute(q, (rid,)):
        print(f"[{r['seq']}] {r['key']}  unit={r['unit']}({r['unit_kind']})  task={r['task']}({r['skill']})  "
              f"state={r['tier'] or 'new'}" + ("  新題" if r["is_new"] else ""))
        print(f"  題幹: {r['prompt']}")
        answers = json.loads(r["answers"])
        if r["shown_choices"]:
            shown = json.loads(r["shown_choices"])
            print("  選項: " + "  ".join(f"{chr(65 + i)}. {c}" for i, c in enumerate(shown)))
            print("  正解: " + " / ".join(f"{chr(65 + shown.index(a))}. {a}" for a in answers if a in shown))
        elif answers:
            print(f"  {'參考答案' if r['skill'] == 'produce' else '答案'}: {' / '.join(answers)}")
        if r["judge"]:
            print(f"  判分: {r['judge']}")
        print(f"  jp: {r['jp'] or '-'}  reading: {r['reading'] or '-'}  zh: {r['zh'] or '-'}")
        if r["explain"]:
            print(f"  explain: {r['explain']}")
        if r["gist"]:
            print(f"  gist: {r['gist']}")
        if r["probe"]:
            print(f"  probe: {r['probe']}（刻意放入的未知元素，不要提示使用者）")


ROUND_FLOW = ("流程：一次只出一題（只給題幹與選項）→ 依答案與判分準則判斷 → 回饋並出下一題 → "
              "回覆的最後呼叫 `record` 寫入剛答完的那題，寫完不再加文字。")


def cmd_round(con, args):
    ts = now_ts()
    r = open_round(con)
    if args.action == "status":
        if r:
            print(f"round {r['id']}: 已記錄 {r['recorded']}/{r['planned']}" +
                  (f"，下一題 seq {r['next_seq']}" if r["next_seq"] else "（全部記錄完，待手動 checkpoint）"))
        else:
            print("沒有進行中的一輪")
        return
    if args.action == "add":
        if not r:  # 沒有進行中的一輪：與 round start 相同的閘門，開一個空的一輪再加題
            if con.execute("SELECT 1 FROM rounds").fetchone() and not args.new_round:
                raise SystemExit("沒有進行中的一輪；使用者明確說要開始後才加 --new-round")
            con.execute("INSERT INTO rounds(session_id,started_ts,planned) VALUES(?,?,0)",
                        (current_session(con, create=True), ts))
            r = open_round(con)
        it = con.execute("SELECT * FROM items WHERE key=?", (args.key,)).fetchone()
        if not it:
            raise SystemExit(f"no such item: {args.key}")
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM presentations WHERE round_id=?", (r["id"],)).fetchone()[0]
        _present(con, it, current_session(con, create=True), r["id"], seq, ts)
        con.execute("UPDATE rounds SET planned=planned+1 WHERE id=?", (r["id"],))
        con.commit()
        print(f"added seq {seq}")
        print_cards(con, r["id"])
        return
    # start
    if r:
        if r["next_seq"]:
            print(f"ROUND_OPEN: 第 {r['id']} 輪進行中，已記錄 {r['recorded']}/{r['planned']}，從 seq {r['next_seq']} 接續"
                  "（已作答但沒寫入的題，能從對話還原就先 record 補寫）。剩下的題：")
            print_cards(con, r["id"])
            print(ROUND_FLOW)
        else:
            print(f"ROUND_COMPLETE: 第 {r['id']} 輪已全部記錄。做手動 checkpoint 結束本輪、回報，等使用者指示。")
        return
    last = con.execute("SELECT closed_ts FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
    if last and not args.new_round:
        print("ROUND_CLOSED: 上一輪已在 checkpoint 結束。不要出題：先向使用者回報並等待指示。"
              "使用者明確說要開始／繼續後，才執行 `round start --new-round`。"
              "若未作答的題不夠一輪且 next_focus 需要新題，先走 generator → reviewer → import。")
        return
    cap = int(meta(con, "round_max"))
    n = min(args.n or cap, cap)
    plan, fresh_ids = plan_round(con, n, ts)
    if not plan:
        print("NO_ITEMS: 目前沒有到期或未作答的題目。請依 rules/subagent-policy.md 讓 generator 產出下一輪"
              "（可參考 `jl.py variants`），或 `jl.py stage <n>` 前進階段。")
        return
    sid = current_session(con, create=True)
    rid = con.execute("INSERT INTO rounds(session_id,started_ts,planned) VALUES(?,?,?)", (sid, ts, len(plan))).lastrowid
    for seq, it in enumerate(plan, 1):
        _present(con, it, sid, rid, seq, ts)
    con.commit()
    n_new = sum(1 for it in plan if it["id"] in fresh_ids)
    print(f"ROUND {rid} 開始：{len(plan)} 題（新題 {n_new}）。以下是給 AI 的題目資料，含答案、判分準則與 probe，"
          "不要讓使用者看到。")
    print_cards(con, rid)
    total, _ = unseen_summary(con)
    print(f"本輪之後仍未作答過的題：{total - n_new}")
    print(ROUND_FLOW)


def _check_enum(val, allowed, label, seq):
    if val is not None and val not in allowed:
        raise SystemExit(f"[record] seq {seq}: {label}={val!r} 不在 {allowed}")


def _validate_entry(e):
    seq = e.get("seq")
    _check_enum(e.get("outcome"), OUTCOMES, "outcome", seq)
    _check_enum(e.get("confidence"), CONFIDENCES, "confidence", seq)
    _check_enum(e.get("basis"), BASES, "basis", seq)
    _check_enum(e.get("miss"), MISSES, "miss", seq)
    for t in e.get("tags") or []:
        _check_enum(t, TAGS, "tag", seq)
    aft = e.get("after") or {}
    _check_enum(aft.get("confidence"), CONFIDENCES + (NONE,), "after.confidence", seq)
    _check_enum(aft.get("basis"), BASES + (NONE,), "after.basis", seq)
    _check_enum(aft.get("miss"), MISSES + (NONE,), "after.miss", seq)
    for s in e.get("seen") or []:
        if not s.get("term"):
            raise SystemExit(f"[record] seq {seq}: seen 需要 term")
        _check_enum(s.get("kind") or "word", ENCOUNTER_KINDS, "seen.kind", seq)
        _check_enum(s.get("where") or "prompt", ENCOUNTER_LOCATIONS, "seen.where", seq)


def cmd_record(con, args):
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"[record] JSON 解析失敗：{e}")
    entries = [data] if isinstance(data, dict) else data
    r = open_round(con)
    if not r:
        raise SystemExit("[record] 沒有進行中的一輪（round start）")
    pres, seen_seq = [], set()
    for e in entries:
        seq = e.get("seq")
        if not isinstance(seq, int) or seq in seen_seq:
            raise SystemExit(f"[record] seq 缺少、不是整數或重複：{seq!r}")
        seen_seq.add(seq)
        if not e.get("outcome"):
            raise SystemExit(f"[record] seq {seq}: 需要 outcome（correct|incorrect|skipped，由你判斷）")
        _validate_entry(e)
        p = con.execute("SELECT * FROM presentations WHERE round_id=? AND seq=?", (r["id"], seq)).fetchone()
        if not p:
            raise SystemExit(f"[record] 本輪沒有 seq {seq}")
        done = con.execute("SELECT id FROM attempts WHERE presentation_id=?", (p["id"],)).fetchone()
        if done:
            raise SystemExit(f"[record] seq {seq} 已記錄（attempt {done['id']}）；更正請用 `jl.py report --attempt {done['id']}`")
        pres.append((e, p))
    cfg = cfg_of(con)
    sid = current_session(con, create=True)
    ts = now_ts()
    out, extra_lines, units = [], [], {}
    for e, p in pres:
        it = con.execute("SELECT i.*, u.kind AS unit_kind FROM items i JOIN units u ON u.unit=i.unit WHERE i.id=?",
                         (p["item_id"],)).fetchone()
        tt = con.execute("SELECT * FROM task_types WHERE task=?", (it["task"],)).fetchone()
        ans = str(e.get("answer") or "")
        shown = json.loads(p["shown_choices"]) if p["shown_choices"] else None
        extra = [it["reading"]] if it["reading"] and (it["task"] == "reading" or tt["skill"] == "produce") else []
        g = grade_answer(it, ans, shown, extra)
        outcome = e["outcome"]
        if tt["grading"] == "auto":  # 答案有限的題型：程式判分當稽核
            gres = {"correct": g["how"] or "exact", "incorrect": "mismatch", "unknown": "unknown"}.get(g["result"], "judged")
            audit = g["result"] in ("correct", "incorrect") and (g["result"] == "correct") != (outcome == "correct")
        else:
            gres, audit = ("unknown" if g["result"] == "unknown" else "judged"), False
        picked = e.get("pick") or (g["matched"] if shown else None)
        picked_unit = None
        if picked and it["choice_units"] and it["choices"]:
            picked_unit = dict(zip(json.loads(it["choices"]), json.loads(it["choice_units"]))).get(picked)
        aid = con.execute("""INSERT INTO attempts(item_id,unit,presentation_id,ts,learn_day,session_id,outcome,answer_mode,
                             user_answer,picked_text,picked_unit,grade_result,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (it["id"], it["unit"], p["id"], ts, learn_day(ts, cfg["day_start_hour"]), sid, outcome,
                           g["mode"], ans, picked, picked_unit, gres, e.get("note"))).lastrowid
        if audit:
            extra_lines.append(f"AUDIT seq {p['seq']}: 程式判 {g['result']}（作答 {g['matched'] or ans}；"
                               f"正解 {' / '.join(json.loads(it['answers']))}），你判 {outcome}。"
                               f"確認後若要改判：`jl.py report --attempt {aid} --outcome ...`")
        if e.get("confidence") or e.get("basis") or e.get("miss"):
            con.execute("INSERT INTO attempt_reports(attempt_id,ts,phase,confidence,basis,miss) VALUES(?,?,?,?,?,?)",
                        (aid, ts, "with_answer", e.get("confidence"), e.get("basis"), e.get("miss")))
        aft = e.get("after") or {}
        if any(aft.get(k) for k in ("confidence", "basis", "miss", "note")):
            con.execute("""INSERT INTO attempt_reports(attempt_id,ts,phase,confidence,basis,miss,note)
                           VALUES(?,?,?,?,?,?,?)""",
                        (aid, ts, "after_feedback", aft.get("confidence"), aft.get("basis"), aft.get("miss"),
                         aft.get("note")))
        con.executemany("INSERT OR IGNORE INTO attempt_tags(attempt_id,tag) VALUES(?,?)",
                        [(aid, t) for t in e.get("tags") or []])
        for s in e.get("seen") or []:
            eid, verb = add_encounter(con, s["term"], s.get("kind") or "word", aid, s.get("where") or "prompt",
                                      s.get("reading"), s.get("zh"), s.get("unit"), s.get("note"))
            extra_lines.append(f"seen: {s['term']}（{verb} encounter {eid}）")
        if picked_unit and picked_unit != it["unit"] and outcome == "incorrect":
            pair = "|".join(sorted({it["unit"], picked_unit}))
            n = con.execute("""SELECT COUNT(*) FROM attempts WHERE (unit=? AND picked_unit=?) OR (unit=? AND picked_unit=?)""",
                            (it["unit"], picked_unit, picked_unit, it["unit"])).fetchone()[0]
            c = con.execute("SELECT id FROM confusions WHERE pair_key=?", (pair,)).fetchone()
            extra_lines.append(f"CONFUSION_CANDIDATE: {pair}（累計 {n} 次，attempt {aid}）"
                               + (f" 已有 confusion {c['id']}" if c else ""))
        units[it["unit"]] = True
        out.append((p["seq"], aid, it["key"], it["unit"], tt["skill"], outcome, gres))
    for u in units:
        refresh_unit(con, u, cfg)
    con.commit()
    for seq, aid, key, unit, skill, outcome, gres in out:
        st = con.execute("SELECT tier, stability_days, due_ts FROM unit_state WHERE unit=? AND skill=?",
                         (unit, skill)).fetchone()
        print(f"[{seq}] attempt {aid} {key} → {outcome}（{gres}）  {unit}/{skill} "
              f"tier={st['tier']} S={st['stability_days']:.2f}d due={fmt_ts(st['due_ts'])}")
    for line in extra_lines:
        print(line)
    r = open_round(con)
    if r["next_seq"]:
        print(f"本輪：已記錄 {r['recorded']}/{r['planned']}，下一題 seq {r['next_seq']}")
    else:
        print(f"ROUND_COMPLETE: 本輪 {r['planned']} 題已全部記錄。做手動 checkpoint 結束本輪、回報，等使用者指示。")


def cmd_report(con, args):
    if not (args.confidence or args.basis or args.note or args.miss or args.outcome):
        raise SystemExit("至少要給 --confidence、--basis、--miss、--outcome 或 --note 其中之一")
    phase = "correction" if args.outcome else args.phase
    if args.attempt:
        a = con.execute("SELECT id, unit FROM attempts WHERE id=?", (args.attempt,)).fetchone()
    else:
        sid = current_session(con)
        a = (con.execute("SELECT id, unit FROM attempts WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
             if sid else None) or con.execute("SELECT id, unit FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
    if not a:
        raise SystemExit("找不到作答紀錄")
    con.execute("""INSERT INTO attempt_reports(attempt_id,ts,phase,confidence,basis,note,miss,outcome)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (a["id"], now_ts(), phase, args.confidence, args.basis, args.note, args.miss, args.outcome))
    refresh_unit(con, a["unit"])
    con.commit()
    e = con.execute("SELECT confidence, basis, miss, outcome FROM v_attempt_effective WHERE id=?", (a["id"],)).fetchone()
    print(f"reported attempt {a['id']} ({phase}): outcome={e['outcome']} confidence={e['confidence']} "
          f"basis={e['basis']} miss={e['miss']}；已重算 {a['unit']}")
    if e["miss"] and e["outcome"] == "correct":
        print("注意：miss 只對答錯/跳過有作用，這筆是答對，排程不受影響")


# --------------------------------------------------------------------------- 查詢與報告
def cmd_due(con, args):
    ts = now_ts()
    rows = con.execute("""SELECT * FROM unit_state s WHERE EXISTS
                          (SELECT 1 FROM items WHERE unit=s.unit AND retired IS NULL) ORDER BY due_ts""").fetchall()
    due = [r for r in rows if r["due_ts"] <= ts]
    print(f"due now: {len(due)} / tracked: {len(rows)} (unit×skill)")
    for r in due[:20]:
        R = retr((ts - r["last_ts"]) / 86400, r["stability_days"])
        print(f"  {r['unit']}/{r['skill']}  tier={r['tier']}  S={r['stability_days']:.1f}d  R≈{R:.2f}")
    for r in [r for r in rows if r["due_ts"] > ts][:5]:
        print(f"  upcoming {fmt_ts(r['due_ts'])}  {r['unit']}/{r['skill']}")


def cmd_weak(con, args):
    print("== 最弱單元（lapses 多 / 穩定度低）")
    for r in con.execute("""SELECT s.unit, s.skill, s.tier, s.stability_days S, s.lapses, u.jp, u.zh,
                            st.n, st.n_correct FROM unit_state s JOIN units u ON u.unit=s.unit
                            LEFT JOIN unit_stats st ON st.unit=s.unit AND st.skill=s.skill
                            ORDER BY s.lapses DESC, s.stability_days ASC LIMIT ?""", (args.limit,)):
        print(f"  {r['unit']}/{r['skill']} ({r['jp']} / {r['zh']}) tier={r['tier']} S={r['S']:.1f}d "
              f"lapses={r['lapses']} ok={r['n_correct']}/{r['n']}")
    print("== 最近 50 筆作答：各題型答對率")
    for r in con.execute("""SELECT task, COUNT(*) n, SUM(outcome='correct') ok FROM
                            (SELECT * FROM v_attempt_effective ORDER BY id DESC LIMIT 50) GROUP BY task"""):
        print(f"  {r['task']}: {r['ok']}/{r['n']}")
    tags = con.execute("""SELECT tag, SUM(n) n FROM v_tag_counts WHERE outcome!='correct'
                          GROUP BY tag ORDER BY n DESC LIMIT 5""").fetchall()
    if tags:
        print("== 錯誤 tag：" + "  ".join(f"{r['tag']}×{r['n']}" for r in tags))
    off = con.execute("""SELECT tag, SUM(n) n FROM v_tag_counts WHERE outcome='correct'
                         GROUP BY tag ORDER BY n DESC LIMIT 5""").fetchall()
    if off:
        print("== 考點以外的錯（答對題上的 tag）：" + "  ".join(f"{r['tag']}×{r['n']}" for r in off))
    if args.detail:
        print("== 最近錯誤（作答 vs 正解）")
        for r in con.execute("SELECT * FROM v_recent_errors ORDER BY attempt_id DESC LIMIT 15"):
            pk = f" 選了:{r['picked_text']}" if r["picked_text"] else ""
            print(f"  [{r['task']}] {r['jp']} ({r['zh']}) ← 作答: {r['user_answer']}{pk}  "
                  f"{r['outcome']}/{r['confidence']}/{r['basis']} tags={r['tags']}  {r['note'] or ''}")


def cmd_stats(con, args):
    print("unit status:", {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM v_unit_status WHERE active GROUP BY status")})
    print("tier (unit×skill):", {f"{r['skill']}/{r['tier']}": r["n"] for r in con.execute(
        "SELECT skill, tier, COUNT(*) n FROM unit_state GROUP BY skill, tier")})
    r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM v_attempt_effective").fetchone()
    print(f"attempts total: {r['n']}  correct: {r['ok'] or 0}")
    print("correct by confidence:", {(x["c"] or "unreported"): x["n"] for x in con.execute(
        "SELECT confidence c, COUNT(*) n FROM v_attempt_effective WHERE outcome='correct' GROUP BY confidence")})
    print("correct by basis:", {(x["b"] or "unreported"): x["n"] for x in con.execute(
        "SELECT basis b, COUNT(*) n FROM v_attempt_effective WHERE outcome='correct' GROUP BY basis")})
    sid = current_session(con)
    if sid:
        r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM v_attempt_effective WHERE session_id=?",
                        (sid,)).fetchone()
        print(f"session {sid}: {r['n']} attempts, {r['ok'] or 0} correct")
    print("items active:", con.execute("SELECT COUNT(*) FROM items WHERE retired IS NULL").fetchone()[0],
          " stage:", meta(con, "stage"))


def _read_hook_source():
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        data = sys.stdin.read()
        return json.loads(data).get("source") if data.strip() else None
    except (ValueError, OSError):
        return None


def cmd_resume(con, args):
    source = args.source or (_read_hook_source() if args.hook else None) or "startup"
    sid = current_session(con) if source in ("compact", "resume") else None
    sid = sid or new_session(con)
    if args.hook:
        set_meta(con, "last_hook_ts", now_ts())
        set_meta(con, "last_hook_source", source)
        con.commit()
        print(f"[jl resume：由 SessionStart hook 自動執行（source={source}），學習狀態如下]")
    else:
        h = meta(con, "last_hook_ts")
        last = f"{fmt_ts(float(h))}，{meta(con, 'last_hook_source')}" if h else "從未執行"
        print(f"（手動執行 resume；上次 SessionStart hook：{last}）")
    print(f"session: {sid}   stage: {meta(con, 'stage')}")
    man = con.execute("SELECT * FROM checkpoints WHERE kind='manual' ORDER BY id DESC LIMIT 1").fetchone()
    auto = con.execute("SELECT * FROM checkpoints WHERE kind='auto' ORDER BY id DESC LIMIT 1").fetchone()
    if man:
        print(f"last checkpoint ({fmt_ts(man['ts'])}): {man['summary']}")
        print(f"next_focus: {man['next_focus']}")
    else:
        print("last checkpoint: (none)")
    if auto and (not man or auto["ts"] > man["ts"]):
        print(f"auto checkpoint ({fmt_ts(auto['ts'])}): {auto['summary']}")
    li = meta(con, "last_import")
    if li:
        li = json.loads(li)
        if not man or li["ts"] > man["ts"]:
            print(f"注意：上次 checkpoint 之後已匯入 {li['source']}（{li['n']} 題）。next_focus 若寫需要新題，"
                  "那批題已經備好，不要重新出題。")
    total, by_src = unseen_summary(con)
    print(f"未作答過的題：{total}" + (f"（{by_src}）" if total else "") +
          f"；一輪 {meta(con, 'round_max')} 題。未作答的題夠一輪時不出新題。")
    cmd_stats(con, args)
    cmd_due(con, args)
    if con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
        args.limit, args.detail = 5, False
        cmd_weak(con, args)
    if con.execute("SELECT COUNT(*) FROM confusion_status WHERE status IN ('observed','drilling')").fetchone()[0]:
        print("== 進行中的混淆（observed＝尚未講解，drilling＝已講解待鞏固）")
        args.status, args.kind, args.unit, args.untaught, args.limit = None, None, None, False, 5
        confusion_list(con, args, active_only=True)
    nv = con.execute("SELECT COUNT(*) FROM v_variants_needed").fetchone()[0]
    ng = con.execute("SELECT COUNT(*) FROM v_skill_gaps").fetchone()[0]
    if nv or ng:
        print(f"題庫：需要新情境變體 {nv} 組（jl.py variants）、缺題的 skill {ng} 組")
    enc = con.execute("SELECT term FROM v_encounters WHERE status='open' ORDER BY seen_count DESC, last_seen DESC").fetchall()
    if enc:
        print(f"題目中遇到、尚未升格的未知元素 {len(enc)} 個：" + "、".join(r["term"] for r in enc[:8])
              + "（jl.py seen list --open）")
    npk = con.execute("SELECT COUNT(*) FROM v_parked").fetchone()[0]
    if npk:
        print(f"暫停出題的題目 {npk} 題（jl.py sql \"SELECT * FROM v_parked\"）")
    r = open_round(con)
    if r:
        nxt = (f"，下一題 seq {r['next_seq']}（`round start` 會列出剩下的題）" if r["next_seq"]
               else "，全部記錄完，待手動 checkpoint")
        print(f"本輪：第 {r['id']} 輪進行中，已記錄 {r['recorded']}/{r['planned']}{nxt}")
    else:
        print("本輪：沒有進行中的一輪（使用者說開始／繼續後才 `round start --new-round`）")
    print("流程：round start 排好整輪 → 一次出一題、AI 判斷並回饋 → 回覆最後 `record` 寫入剛答完的題 → "
          "全部記錄完做手動 checkpoint，回報後等使用者指示。")


def auto_summary(con, since):
    r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM v_attempt_effective WHERE ts>?", (since,)).fetchone()
    if not r["n"]:
        return None
    parts = [f"自上次 checkpoint {r['n']} 題，答對 {r['ok'] or 0}"]
    miss = [x["unit"] for x in con.execute("""SELECT unit, COUNT(*) n FROM v_attempt_effective WHERE ts>? AND outcome!='correct'
                                              GROUP BY unit ORDER BY n DESC LIMIT 6""", (since,))]
    if miss:
        parts.append("錯/跳過：" + ",".join(miss))
    tags = [f"{x['tag']}×{x['n']}" for x in con.execute(
        """SELECT g.tag, COUNT(*) n FROM attempt_tags g JOIN attempts a ON a.id=g.attempt_id WHERE a.ts>?
           GROUP BY g.tag ORDER BY n DESC LIMIT 4""", (since,))]
    if tags:
        parts.append("tags：" + " ".join(tags))
    picks = [f"{x['unit']}→{x['picked_unit']}" for x in con.execute(
        """SELECT unit, picked_unit FROM attempts WHERE ts>? AND picked_unit IS NOT NULL AND picked_unit!=unit
           GROUP BY unit, picked_unit""", (since,))]
    if picks:
        parts.append("誘答：" + ",".join(picks))
    af = con.execute("""SELECT COUNT(*) FROM attempt_reports WHERE ts>? AND phase='after_feedback'""", (since,)).fetchone()[0]
    if af:
        parts.append(f"事後自述 {af} 筆")
    due = con.execute("SELECT COUNT(*) FROM unit_state WHERE due_ts<=?", (now_ts(),)).fetchone()[0]
    parts.append(f"到期 {due}")
    return "[auto] " + "；".join(parts)


def cmd_checkpoint(con, args):
    sid = current_session(con, create=True)
    last = con.execute("SELECT ts FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
    since = last["ts"] if last else 0
    if args.auto:
        summary = auto_summary(con, since)
        if not summary:
            print("checkpoint --auto: 自上次 checkpoint 沒有新作答，不寫入")
            return
        prev = con.execute("SELECT next_focus FROM checkpoints WHERE kind='manual' ORDER BY id DESC LIMIT 1").fetchone()
        nxt, kind = (prev["next_focus"] if prev else ""), "auto"
    else:
        if not args.summary:
            raise SystemExit("需要 --summary（或用 --auto）")
        summary, nxt, kind = args.summary, args.next, "manual"
    r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM v_attempt_effective WHERE ts>?", (since,)).fetchone()
    con.execute("INSERT INTO checkpoints(session_id,ts,kind,summary,next_focus) VALUES(?,?,?,?,?)",
                (sid, now_ts(), kind, summary, nxt))
    con.execute("UPDATE sessions SET ended_ts=? WHERE id=?", (now_ts(), sid))
    dropped = 0
    if kind == "manual":
        rr = open_round(con)
        if rr:
            dropped = rr["planned"] - rr["recorded"]
            con.execute("UPDATE rounds SET closed_ts=? WHERE id=?", (now_ts(), rr["id"]))
    con.commit()
    print(f"checkpoint ({kind}) saved (session {sid}). 自上次 checkpoint：{r['n']} 題，答對 {r['ok'] or 0}。")
    if kind == "manual":
        print("ROUND_CLOSED: 本輪已結束。停止出題，回報答對率、錯最多的類型與下一輪建議，等使用者明確說繼續。"
              + (f"（本輪 {dropped} 題未記錄，已捨棄）" if dropped else ""))


def cmd_variants(con, args):
    total, by_src = unseen_summary(con)
    print(f"未作答過的題：{total}" + (f"（{by_src}）" if total else "") + "。出新題前先看這些題涵蓋了哪些 unit，避免重複：")
    for r in con.execute("""SELECT v.key, v.unit, v.task, i.scenario FROM v_unseen_items v JOIN items i ON i.id=v.item_id
                            ORDER BY v.item_id"""):
        print(f"  {r['key']} [{r['unit']}/{r['task']}] {r['scenario'] or ''}")
    rows = con.execute("""SELECT v.*, u.gist, u.jp, u.zh FROM v_variants_needed v JOIN units u ON u.unit=v.unit
                          ORDER BY v.unit""").fetchall()
    print(f"需要新情境變體：{len(rows)} 組（該 skill 的每題都已在 2 個學習日答對，但尚未鞏固）")
    for r in rows:
        print(f"  {r['unit']}/{r['skill']} ({r['jp']} / {r['zh']}) gist={r['gist']}")
        print(f"      已用情境: {r['scenarios']}")
    gaps = con.execute("SELECT * FROM v_skill_gaps").fetchall()
    if gaps:
        print(f"缺題的 skill：{len(gaps)} 組")
        for r in gaps:
            print(f"  {r['unit']}/{r['skill']}")
    enc = con.execute("SELECT * FROM v_encounters WHERE status='open' ORDER BY seen_count DESC").fetchall()
    if enc:
        print(f"題目中遇到、尚未升格的未知元素：{len(enc)} 個（可出成新 unit，units 段帶 from_encounter）")
        for r in enc:
            print(f"  {r['term']}（{r['reading'] or '-'}／{r['zh'] or '-'}）kind={r['kind']} seen={r['seen_count']}")
    pk = con.execute("SELECT * FROM v_parked").fetchall()
    if pk:
        print(f"暫停出題的題目：{len(pk)} 題")
        for r in pk:
            print(f"  {r['key']} [{r['unit']}/{r['task']}] {r['reason']}  encounters: {r['encounters'] or '-'}")


def cmd_sql(con, args):
    q = args.query.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        raise SystemExit("sql 只允許單一 SELECT / WITH 查詢")
    con.execute("PRAGMA query_only=ON")
    try:
        cur = con.execute(q)
    except sqlite3.Error as e:
        raise SystemExit(f"sql error: {e}")
    cols = [d[0] for d in cur.description or []]
    print("\t".join(cols))
    n = 0
    for row in cur:
        if n >= args.limit:
            print(f"... (只顯示前 {args.limit} 列)")
            break
        print("\t".join("" if v is None else str(v).replace("\t", " ").replace("\n", " ") for v in row))
        n += 1


def cmd_schema(con, args):
    print("== 資料表")
    for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        cols = [c["name"] for c in con.execute(f"PRAGMA table_info({r['name']})")]
        print(f"  {r['name']}({', '.join(cols)})")
    print("== view")
    for name, doc in VIEW_DOCS.items():
        cols = [c["name"] for c in con.execute(f"PRAGMA table_info({name})")]
        print(f"  {name}: {doc}\n      ({', '.join(cols)})")
    print("== 列舉值")
    for label, vals in (("outcome", OUTCOMES), ("confidence", CONFIDENCES), ("basis", BASES),
                        ("answer_mode", MODES), ("grade_result", GRADE_RESULTS), ("skill", SKILLS),
                        ("unit kind", UNIT_KINDS), ("report phase", PHASES), ("tag", TAGS),
                        ("confusion kind", CONF_KINDS), ("confusion cause", CONF_CAUSES),
                        ("confusion axis", CONF_AXES), ("confusion status", CONF_STATUSES),
                        ("miss（答錯原因）", MISSES), ("encounter kind", ENCOUNTER_KINDS),
                        ("encounter location", ENCOUNTER_LOCATIONS)):
        print(f"  {label}: {' | '.join(vals)}")
    print("== 題型 task_types（task / skill / grading）")
    for r in con.execute("SELECT * FROM task_types ORDER BY skill, task"):
        print(f"  {r['task']} / {r['skill']} / {r['grading']}：{r['description']}")
    print("時間欄位為 epoch 秒；learn_day 以 day_start_hour（預設 4:00）為日界。")


def cmd_task(con, args):
    if args.action == "add":
        con.execute("INSERT INTO task_types(task,skill,grading,description) VALUES(?,?,?,?)",
                    (args.name, args.skill, args.grading, args.desc))
        con.commit()
        print(f"task added: {args.name}")
    else:
        for r in con.execute("SELECT * FROM task_types ORDER BY skill, task"):
            print(f"{r['task']}\t{r['skill']}\t{r['grading']}\t{r['description']}")


def cmd_unit(con, args):
    u = con.execute("SELECT * FROM v_unit_status WHERE unit=?", (args.unit,)).fetchone()
    if not u:
        raise SystemExit(f"no such unit: {args.unit}")
    if args.action == "set":
        if args.gist is not None:
            if len(args.gist) > 80:
                raise SystemExit("gist 超過 80 字")
            con.execute("UPDATE units SET gist=? WHERE unit=?", (args.gist, args.unit))
        if args.label is not None:
            con.execute("UPDATE units SET label=? WHERE unit=?", (args.label, args.unit))
        for s in args.add_skill or []:
            con.execute("INSERT OR IGNORE INTO unit_skills(unit,skill) VALUES(?,?)", (args.unit, s))
        con.commit()
        u = con.execute("SELECT * FROM v_unit_status WHERE unit=?", (args.unit,)).fetchone()
    print(f"{u['unit']} kind={u['kind']} status={u['status']} skills={u['skills']} gist={u['gist']}")
    for r in con.execute("SELECT * FROM v_schedule WHERE unit=?", (args.unit,)):
        print(f"  {r['skill']}: tier={r['tier']} S={r['stability_days']:.1f}d lapses={r['lapses']} due={r['due_at']}")
    for r in con.execute("SELECT * FROM unit_stats WHERE unit=?", (args.unit,)):
        print(f"  {r['skill']} stats: {r['n_correct']}/{r['n']} skip={r['n_skipped']} guess={r['n_guess']} "
              f"unsure={r['n_unsure']} no_evidence={r['n_no_evidence']} top_wrong_pick={r['top_wrong_pick']}")
    for r in con.execute("SELECT * FROM v_item_progress WHERE unit=?", (args.unit,)):
        print(f"  item {r['key']} [{r['task']}] shown={r['n_shown']} ok={r['n_correct']}/{r['n_attempts']}"
              + (" retired" if r["retired"] else ""))


def cmd_item(con, args):
    it = con.execute("SELECT * FROM items WHERE key=?", (args.key,)).fetchone()
    if not it:
        raise SystemExit(f"no such item: {args.key}")
    ts = now_ts()
    if args.action == "park":  # 暫停出題（例如整題看不懂）；可 unpark，與永久 retire 區分
        if it["retired"] and not it["retired"].startswith("parked:"):
            raise SystemExit(f"{args.key} 已退役（{it['retired']}），不能 park")
        new = f"parked:{args.reason}"
        con.execute("INSERT INTO item_edits(item_id,ts,field,old,new) VALUES(?,?,?,?,?)",
                    (it["id"], ts, "retired", it["retired"], new))
        con.execute("UPDATE items SET retired=? WHERE id=?", (new, it["id"]))
        con.commit()
        print(f"parked {args.key}: {args.reason}")
        return
    if args.action == "unpark":
        if not (it["retired"] or "").startswith("parked:"):
            raise SystemExit(f"{args.key} 不是暫停狀態")
        con.execute("INSERT INTO item_edits(item_id,ts,field,old,new) VALUES(?,?,?,?,?)",
                    (it["id"], ts, "retired", it["retired"], None))
        con.execute("UPDATE items SET retired=NULL WHERE id=?", (it["id"],))
        con.commit()
        print(f"unparked {args.key}")
        return
    changes = {}
    if args.set_answers or args.add_answer or args.remove_answer:
        ans = json.loads(args.set_answers) if args.set_answers else json.loads(it["answers"])
        if not isinstance(ans, list) or not all(isinstance(a, str) for a in ans):
            raise SystemExit("--set-answers 必須是字串的 JSON list")
        ans += [a for a in args.add_answer or [] if a not in ans]
        ans = [a for a in ans if a not in (args.remove_answer or [])]
        if it["choices"] and not any(a in json.loads(it["choices"]) for a in ans):
            raise SystemExit("選擇式題目的 answers 必須出現在 choices 中")
        changes["answers"] = json.dumps(ans, ensure_ascii=False)
    for f in ("explain", "zh", "reading", "prompt", "judge", "probe"):
        v = getattr(args, f)
        if v is not None:
            changes[f] = v
    if "answers" in changes and not it["choices"]:
        skill = con.execute("SELECT skill FROM task_types WHERE task=?", (it["task"],)).fetchone()["skill"]
        if skill == "produce" and len(json.loads(changes["answers"])) > MAX_PRODUCE_ANSWERS:
            raise SystemExit(f"產出題的 answers 最多 {MAX_PRODUCE_ANSWERS} 個參考答案；可接受範圍寫在 --judge")
    if not changes:
        raise SystemExit("沒有要修改的欄位")
    for f, v in changes.items():
        con.execute("INSERT INTO item_edits(item_id,ts,field,old,new) VALUES(?,?,?,?,?)", (it["id"], ts, f, it[f], v))
        con.execute(f"UPDATE items SET {f}=? WHERE id=?", (v, it["id"]))
    con.commit()
    print(f"edited {args.key}: {', '.join(changes)}")


def add_encounter(con, term, kind, attempt_id, where="prompt", reading=None, zh=None, unit=None, note=None):
    """記一筆題目中看到但不認識的元素；同 term 再加會累計並補連結。回傳 (id, created|updated)。"""
    if not con.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
        raise SystemExit(f"attempt 不存在：{attempt_id}")
    if unit and not con.execute("SELECT 1 FROM units WHERE unit=?", (unit,)).fetchone():
        raise SystemExit(f"unit 不存在：{unit}")
    ts = now_ts()
    row = con.execute("SELECT * FROM encounters WHERE term=?", (term,)).fetchone()
    if row:
        eid = row["id"]
        new_link = not con.execute("SELECT 1 FROM encounter_attempts WHERE encounter_id=? AND attempt_id=?",
                                   (eid, attempt_id)).fetchone()
        con.execute("""UPDATE encounters SET seen_count=seen_count+?, last_seen=?, reading=COALESCE(reading,?),
                       zh=COALESCE(zh,?), unit=COALESCE(unit,?), note=COALESCE(?,note) WHERE id=?""",
                    (1 if new_link else 0, ts, reading, zh, unit, note, eid))
        verb = "updated"
    else:
        eid = con.execute("""INSERT INTO encounters(term,reading,zh,kind,first_seen,last_seen,unit,note)
                             VALUES(?,?,?,?,?,?,?,?)""", (term, reading, zh, kind, ts, ts, unit, note)).lastrowid
        verb = "created"
    con.execute("INSERT OR IGNORE INTO encounter_attempts(encounter_id,attempt_id,location) VALUES(?,?,?)",
                (eid, attempt_id, where))
    return eid, verb


def cmd_seen(con, args):
    """題目（題幹/選項）中看到但不認識的元素。答不出考點本身不記這裡。"""
    if args.action == "add":
        eid, verb = add_encounter(con, args.term, args.kind, args.attempt, args.where, args.reading, args.zh,
                                  args.unit, args.note)
        con.commit()
        r = con.execute("SELECT * FROM v_encounters WHERE id=?", (eid,)).fetchone()
        print(f"{verb} encounter {eid}: {r['term']} seen={r['seen_count']} status={r['status']}")
    elif args.action == "link":
        if not con.execute("SELECT 1 FROM units WHERE unit=?", (args.unit,)).fetchone():
            raise SystemExit(f"unit 不存在：{args.unit}")
        key = args.ref
        n = con.execute("UPDATE encounters SET unit=? WHERE term=? OR CAST(id AS TEXT)=?", (args.unit, key, key)).rowcount
        con.commit()
        print(f"linked {key} -> {args.unit}" if n else f"no encounter {key}")
    else:
        q = "SELECT * FROM v_encounters" + (" WHERE status='open'" if args.open else "") + " ORDER BY seen_count DESC, last_seen DESC"
        rows = con.execute(q).fetchall()
        print(f"encounters: {len(rows)}")
        for r in rows:
            print(f"  [{r['id']}] {r['term']}（{r['reading'] or '-'}／{r['zh'] or '-'}）kind={r['kind']} "
                  f"seen={r['seen_count']} status={r['status']} unit={r['unit'] or '-'} as_probe={r['n_as_probe']} "
                  f"attempts={r['attempts']}")


def cmd_retire(con, args):
    n = con.execute("UPDATE items SET retired=? WHERE key=? AND retired IS NULL", (args.reason, args.key)).rowcount
    con.commit()
    print(f"retired {args.key}" if n else f"no active item with key {args.key}")


def cmd_stage(con, args):
    if args.n is not None:
        set_meta(con, "stage", args.n)
        con.commit()
    print("stage:", meta(con, "stage"))


def cmd_import(con, args):
    import_file(con, args.file)


def cmd_rebuild(con, args):
    n = rebuild_all(con)
    con.commit()
    print(f"rebuilt {n} units")


# --------------------------------------------------------------------------- 混淆表
def confusion_list(con, args, active_only=False):
    q = "SELECT c.*, v.status FROM confusions c JOIN confusion_status v ON v.id=c.id WHERE 1=1"
    params = []
    if active_only:
        q += " AND v.status IN ('observed','drilling')"
    elif args.status:
        q += " AND v.status=?"
        params.append(args.status)
    else:
        q += " AND v.status!='merged'"
    if args.kind:
        q += " AND c.kind=?"
        params.append(args.kind)
    if args.unit:
        q += " AND EXISTS(SELECT 1 FROM confusion_members m WHERE m.confusion_id=c.id AND m.unit=?)"
        params.append(args.unit)
    if args.untaught:
        q += " AND c.taught_at IS NULL"
    q += " ORDER BY c.miss_count DESC, c.last_seen DESC LIMIT ?"
    params.append(args.limit)
    rows = con.execute(q, params).fetchall()
    print(f"confusions: {len(rows)}")
    for r in rows:
        mem = ",".join(x["unit"] for x in con.execute(
            "SELECT unit FROM confusion_members WHERE confusion_id=? ORDER BY unit", (r["id"],)))
        print(f"  [{r['id']}] {r['label']}  status={r['status']} kind={r['kind']} cause={r['cause']} "
              f"axis={r['axis']} miss={r['miss_count']}")
        print(f"      gist: {r['gist']}   units: {mem}")


def cmd_confusion(con, args):
    act = args.action
    if act == "add":
        units = sorted(set(args.members))
        if len(units) < 2:
            raise SystemExit("需要至少兩個不同的 unit 作為成員")
        for u in units:
            if not con.execute("SELECT 1 FROM units WHERE unit=?", (u,)).fetchone():
                raise SystemExit(f"unit 不存在：{u}（成員必須對應既有 unit）")
        if len(args.gist) > 80:
            raise SystemExit("gist 超過 80 字：只存一行重點，不存講解全文")
        for aid in args.attempt or []:
            if not con.execute("SELECT 1 FROM attempts WHERE id=?", (aid,)).fetchone():
                raise SystemExit(f"attempt 不存在：{aid}")
        key = "|".join(units)
        ts = now_ts()
        row = con.execute("SELECT id, merged_into FROM confusions WHERE pair_key=?", (key,)).fetchone()
        if row:
            cid = row["merged_into"] or row["id"]
            con.execute("UPDATE confusions SET miss_count=miss_count+1, last_seen=? WHERE id=?", (ts, cid))
            verb = "updated"
        else:
            cid = con.execute("""INSERT INTO confusions(label,kind,cause,axis,gist,pair_key,first_seen,last_seen)
                                 VALUES(?,?,?,?,?,?,?,?)""",
                              (args.label, args.kind, args.cause, args.axis, args.gist, key, ts, ts)).lastrowid
            con.executemany("INSERT INTO confusion_members(confusion_id,unit) VALUES(?,?)", [(cid, u) for u in units])
            verb = "created"
        for aid in args.attempt or []:
            con.execute("INSERT OR IGNORE INTO confusion_attempts(confusion_id,attempt_id) VALUES(?,?)", (cid, aid))
        con.commit()
        print(f"{verb} confusion {cid}: {args.label}")
    elif act == "taught":
        n = con.execute("UPDATE confusions SET taught_at=?, taught_session=? WHERE id=? AND merged_into IS NULL",
                        (now_ts(), current_session(con, create=True), args.id)).rowcount
        con.commit()
        print(f"taught {args.id}" if n else f"no active confusion {args.id}")
    elif act == "merge":
        if args.id == args.into:
            raise SystemExit("不能併入自己")
        a = con.execute("SELECT * FROM confusions WHERE id=? AND merged_into IS NULL", (args.id,)).fetchone()
        b = con.execute("SELECT * FROM confusions WHERE id=? AND merged_into IS NULL", (args.into,)).fetchone()
        if not a or not b:
            raise SystemExit("來源或目標不存在或已被併入")
        con.execute("UPDATE confusions SET merged_into=? WHERE id=?", (args.into, args.id))
        con.execute("UPDATE confusions SET miss_count=miss_count+?, last_seen=MAX(last_seen,?) WHERE id=?",
                    (a["miss_count"], a["last_seen"], args.into))
        con.execute("INSERT OR IGNORE INTO confusion_attempts SELECT ?, attempt_id FROM confusion_attempts "
                    "WHERE confusion_id=?", (args.into, args.id))
        con.execute("INSERT OR IGNORE INTO confusion_members SELECT ?, unit FROM confusion_members "
                    "WHERE confusion_id=?", (args.into, args.id))
        con.commit()
        print(f"merged {args.id} -> {args.into}")
    elif act == "list":
        confusion_list(con, args)


# --------------------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(prog="jl.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("resume")
    p.add_argument("--hook", action="store_true", help="由 SessionStart hook 呼叫（從 stdin 讀 source）")
    p.add_argument("--source", choices=("startup", "resume", "clear", "compact"))
    p = sub.add_parser("round"); rs = p.add_subparsers(dest="action", required=True)
    q = rs.add_parser("start", help="排好整輪並列出題目資料（給 AI）")
    q.add_argument("-n", type=int); q.add_argument("--new-round", action="store_true",
                                                   help="上一輪已 checkpoint 結束、使用者明確說開始／繼續後才加")
    rs.add_parser("status")
    q = rs.add_parser("add", help="把指定題目加到進行中的一輪（例如使用者要求重考）"); q.add_argument("key")
    q.add_argument("--new-round", action="store_true")
    p = sub.add_parser("record", help="從 stdin（或 --file）讀 JSON 寫入作答；見 tools/CLAUDE.md")
    p.add_argument("--file")
    p = sub.add_parser("report"); p.add_argument("--attempt", type=int)
    p.add_argument("--confidence", choices=CONFIDENCES + (NONE,)); p.add_argument("--basis", choices=BASES + (NONE,))
    p.add_argument("--miss", choices=MISSES + (NONE,), help="答錯原因自述；none＝撤回")
    p.add_argument("--outcome", choices=OUTCOMES, help="改判（寫成 phase=correction）")
    p.add_argument("--note"); p.add_argument("--phase", choices=("with_answer", "after_feedback"), default="after_feedback")
    sub.add_parser("due")
    p = sub.add_parser("weak"); p.add_argument("--limit", type=int, default=10); p.add_argument("--detail", action="store_true")
    sub.add_parser("stats")
    p = sub.add_parser("checkpoint"); p.add_argument("--summary"); p.add_argument("--next", default="")
    p.add_argument("--auto", action="store_true")
    p = sub.add_parser("import"); p.add_argument("file")
    p = sub.add_parser("stage"); p.add_argument("n", type=int, nargs="?")
    sub.add_parser("rebuild")
    p = sub.add_parser("retire"); p.add_argument("key"); p.add_argument("--reason", required=True)
    sub.add_parser("variants")
    p = sub.add_parser("sql"); p.add_argument("query"); p.add_argument("--limit", type=int, default=200)
    sub.add_parser("schema")
    p = sub.add_parser("task"); ts = p.add_subparsers(dest="action", required=True)
    q = ts.add_parser("add"); q.add_argument("name"); q.add_argument("--skill", required=True, choices=SKILLS)
    q.add_argument("--grading", required=True, choices=GRADINGS); q.add_argument("--desc", required=True)
    ts.add_parser("list")
    p = sub.add_parser("unit"); us = p.add_subparsers(dest="action", required=True)
    q = us.add_parser("show"); q.add_argument("unit")
    q = us.add_parser("set"); q.add_argument("unit"); q.add_argument("--gist"); q.add_argument("--label")
    q.add_argument("--add-skill", choices=SKILLS, action="append")
    p = sub.add_parser("item"); its = p.add_subparsers(dest="action", required=True)
    q = its.add_parser("edit"); q.add_argument("key")
    q.add_argument("--set-answers", help="整個替換 answers（JSON list）")
    q.add_argument("--add-answer", action="append"); q.add_argument("--remove-answer", action="append")
    for f in ("explain", "zh", "reading", "prompt", "judge", "probe"):
        q.add_argument(f"--{f}")
    q = its.add_parser("park"); q.add_argument("key"); q.add_argument("--reason", required=True)
    q = its.add_parser("unpark"); q.add_argument("key")
    p = sub.add_parser("seen"); ss = p.add_subparsers(dest="action", required=True)
    q = ss.add_parser("add"); q.add_argument("--term", required=True)
    q.add_argument("--kind", required=True, choices=ENCOUNTER_KINDS)
    q.add_argument("--attempt", type=int, required=True)
    q.add_argument("--where", choices=ENCOUNTER_LOCATIONS, default="prompt")
    q.add_argument("--reading"); q.add_argument("--zh"); q.add_argument("--unit"); q.add_argument("--note")
    q = ss.add_parser("link"); q.add_argument("ref"); q.add_argument("unit")
    q = ss.add_parser("list"); q.add_argument("--open", action="store_true")
    p = sub.add_parser("confusion")
    cs = p.add_subparsers(dest="action", required=True)
    q = cs.add_parser("add"); q.add_argument("--label", required=True)
    q.add_argument("--kind", required=True, choices=CONF_KINDS)
    q.add_argument("--cause", default="unknown", choices=CONF_CAUSES)
    q.add_argument("--axis", required=True, choices=CONF_AXES); q.add_argument("--gist", required=True)
    q.add_argument("--members", nargs="+", required=True); q.add_argument("--attempt", type=int, nargs="*")
    q = cs.add_parser("taught"); q.add_argument("id", type=int)
    q = cs.add_parser("merge"); q.add_argument("id", type=int); q.add_argument("into", type=int)
    q = cs.add_parser("list"); q.add_argument("--status", choices=CONF_STATUSES)
    q.add_argument("--kind", choices=CONF_KINDS); q.add_argument("--unit")
    q.add_argument("--untaught", action="store_true"); q.add_argument("--limit", type=int, default=20)
    return ap


COMMANDS = {
    "init": lambda c, a: print(f"ok. db={DB_PATH}"), "resume": cmd_resume, "round": cmd_round, "record": cmd_record,
    "report": cmd_report, "due": cmd_due, "weak": cmd_weak, "stats": cmd_stats,
    "checkpoint": cmd_checkpoint, "import": cmd_import, "stage": cmd_stage, "rebuild": cmd_rebuild,
    "retire": cmd_retire, "variants": cmd_variants, "sql": cmd_sql, "schema": cmd_schema, "task": cmd_task,
    "unit": lambda c, a: cmd_unit(c, a), "item": cmd_item, "seen": cmd_seen, "confusion": cmd_confusion,
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    con = connect()
    ensure_seeds(con)
    COMMANDS[args.cmd](con, args)


if __name__ == "__main__":
    main()
