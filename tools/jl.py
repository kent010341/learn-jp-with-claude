#!/usr/bin/env python3
"""日文反向學習專案 CLI（僅標準庫）。所有進度讀寫都經由這支程式。

用法：python tools/jl.py <command> [...]
指令：resume | next | grade | record | due | weak | stats | checkpoint | import | stage | rebuild | retire | init
環境變數：JL_NOW=<epoch 或 ISO 時間> 可模擬時間（驗證用）；JL_DB=<路徑>、JL_SEED=<目錄> 可指定資料庫與種子目錄（驗證用）。
"""
import argparse
import difflib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("JL_DB") or ROOT / "data" / "learning.db")
SEED_DIR = Path(os.environ.get("JL_SEED") or ROOT / "curriculum" / "seed")
SCHEMA_VERSION = "2"

DEFAULT_META = {
    "schema_version": SCHEMA_VERSION,
    "stage": "0",
    "target_retention": "0.9",     # 目標保留率
    "cons_min_days": "3",          # 鞏固：至少幾個不同日成功
    "cons_gap_days": "21",         # 鞏固：最近兩次成功間隔皆需 >= 此天數
    "cons_min_stability": "60",    # 鞏固：穩定度需 >= 此天數
    "learn_step_min": "10",        # 答錯後最快幾分鐘再出現
}

# 排程模型常數：FSRS 風格的簡化版，初始值未經個人資料擬合（累積足夠 attempts 後可調整）
_FACTOR = 19 / 81
_DECAY = -0.5
_S0 = {0: 0.3, 1: 0.5, 2: 0.8, 3: 1.5, 4: 3.0, 5: 5.0}

QUALITY_PASS = 3
OUTCOMES = ("correct", "incorrect", "skipped")
CONFIDENCES = ("sure", "unsure", "guess")
BASES = ("known", "intuition", "context", "assisted", "other")
# 這些依據下的「答對」不算提取證據（結果只排程重測，不加穩定度也不算失誤）
NO_EVIDENCE_BASES = ("context", "assisted", "other")
FORMAT_RANK = {"recognize": 0, "recall": 1, "produce": 2}


# --------------------------------------------------------------------------- 時間
def now_ts():
    v = os.environ.get("JL_NOW")
    if not v:
        return time.time()
    try:
        return float(v)
    except ValueError:
        return datetime.fromisoformat(v).timestamp()


def day_ord(ts):
    return datetime.fromtimestamp(ts).toordinal()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- 資料庫
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  unit TEXT NOT NULL,
  kind TEXT NOT NULL,          -- kana | word | phrase | grammar
  qtype TEXT NOT NULL,         -- choice | free
  format TEXT NOT NULL,        -- recognize | recall | produce
  prompt TEXT NOT NULL,
  choices TEXT,                -- JSON list（choice 題）
  answers TEXT NOT NULL,       -- JSON list，可接受答案（日文假名/漢字、中文關鍵詞）
  jp TEXT, reading TEXT, zh TEXT,
  scenario TEXT, stage INTEGER NOT NULL DEFAULT 0,
  explain TEXT,                -- 答錯時的差異點提示
  source TEXT,
  retired TEXT                 -- NULL＝使用中；否則為停用原因（不再出題，紀錄保留）
);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL, unit TEXT NOT NULL, ts REAL NOT NULL,
  session_id INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('correct','incorrect','skipped')),
  confidence TEXT CHECK(confidence IS NULL OR confidence IN ('sure','unsure','guess')),
  basis TEXT CHECK(basis IS NULL OR basis IN ('known','intuition','context','assisted','other')),
  answer_mode TEXT,            -- jp | romaji | choice | unknown（輸入形式）
  user_answer TEXT,            -- 僅存原始作答
  note TEXT                    -- 自由文字（細節、自述原因）
);
CREATE TABLE IF NOT EXISTS item_state(
  unit TEXT PRIMARY KEY,
  tier TEXT NOT NULL, difficulty REAL, stability_days REAL, due_ts REAL,
  reps_success INTEGER, lapses INTEGER,
  distinct_success_days INTEGER, max_successful_gap_days INTEGER,
  formats_passed TEXT, success_days TEXT, last_ts REAL
);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, started_ts REAL, ended_ts REAL);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL, summary TEXT, next_focus TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_unit ON attempts(unit, ts);
CREATE INDEX IF NOT EXISTS idx_items_unit ON items(unit);
"""


def migrate_v1_attempts(con):
    """v1 attempts(quality, latency_s) → v2 attempts(outcome, confidence, basis, note)。舊欄位捨棄。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(attempts)")}
    if "quality" not in cols:
        return False
    con.execute("DROP INDEX IF EXISTS idx_attempts_unit")
    con.execute("ALTER TABLE attempts RENAME TO attempts_v1")
    con.executescript(SCHEMA)
    con.execute("""INSERT INTO attempts(id,item_id,unit,ts,session_id,outcome,answer_mode,user_answer)
                   SELECT id,item_id,unit,ts,session_id,
                          CASE WHEN quality>=3 THEN 'correct' WHEN quality=0 THEN 'skipped' ELSE 'incorrect' END,
                          answer_mode,user_answer FROM attempts_v1""")
    con.execute("DROP TABLE attempts_v1")
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','2')")
    return True


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    migrated = False
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone():
        migrated = migrate_v1_attempts(con)
    con.executescript(SCHEMA)
    if "retired" not in {r[1] for r in con.execute("PRAGMA table_info(items)")}:
        con.execute("ALTER TABLE items ADD COLUMN retired TEXT")
    for k, v in DEFAULT_META.items():
        con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (k, v))
    con.commit()
    if migrated:
        cmd_rebuild(con, None)
        print("[migrate] schema v1 -> v2 完成，已重算排程狀態", file=sys.stderr)
    return con


def meta(con, key):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULT_META.get(key)


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def import_file(con, path, quiet=False):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    src = data.get("round_id") or Path(path).stem
    stage = int(data.get("stage", 0))
    added = skipped = 0
    for it in data["items"]:
        key = it["key"]
        if con.execute("SELECT 1 FROM items WHERE key=?", (key,)).fetchone():
            skipped += 1
            continue
        qtype = it.get("qtype", "free")
        choices = it.get("choices")
        answers = it["answers"]
        if qtype == "choice":
            if not choices or not any(a in choices for a in answers):
                raise SystemExit(f"[import] {key}: choice 題的 answers 必須出現在 choices 中")
        con.execute(
            """INSERT INTO items(key,unit,kind,qtype,format,prompt,choices,answers,jp,reading,zh,
               scenario,stage,explain,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, it.get("unit", key), it["kind"], qtype, it.get("format", "recall"), it["prompt"],
             json.dumps(choices, ensure_ascii=False) if choices else None,
             json.dumps(answers, ensure_ascii=False), it.get("jp"), it.get("reading"), it.get("zh"),
             it.get("scenario"), int(it.get("stage", stage)), it.get("explain"), src),
        )
        added += 1
    con.commit()
    if not quiet:
        print(f"imported {src}: +{added}, skipped {skipped}")
    return added


def ensure_seeds(con):
    """資料庫為空或有新增 seed 時自動匯入（以 key 去重）。"""
    if SEED_DIR.is_dir():
        for p in sorted(SEED_DIR.glob("*.json")):
            import_file(con, p, quiet=True)


# --------------------------------------------------------------------------- 羅馬字轉假名
_V = "aiueo"
_ROM = {}


def _fill():
    rows = {
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
        "je": "じぇ", "she": "しぇ", "che": "ちぇ", "ti_": "てぃ",
        "xtu": "っ", "ltu": "っ", "xtsu": "っ", "ltsu": "っ",
        "xya": "ゃ", "xyu": "ゅ", "xyo": "ょ", "lya": "ゃ", "lyu": "ゅ", "lyo": "ょ",
    }
    rows.pop("ti_")
    _ROM.update(rows)
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
            if nx and (nx in _V or nx == "y"):
                pass  # 落到下面的音節比對（na/ni/nya…）
            else:
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
            prev = out[-1]
            for grp, v in vow.items():
                if prev in grp:
                    out.append(v)
                    break
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def norm(s, loose):
    """loose=True：羅馬字輸入模式（拒絕區分的差異一律抹平）。"""
    s = unicodedata.normalize("NFKC", s).strip().lower()
    s = _PUNCT.sub("", s)
    if loose:
        s = romaji_to_kana(s)
        s = kata_to_hira(s)
        s = _expand_choon(s)
        s = s.translate(str.maketrans({"は": "わ", "へ": "え", "を": "お", "ぢ": "じ", "づ": "ず"}))
        s = re.sub("([おこごそぞとどのほぼぽもよょろ])う", lambda m: m.group(1) + "お", s)  # ou ≡ oo（羅馬字無法區分）
        s = re.sub("([けげせぜてでねべぺめれえ])い", lambda m: m.group(1) + "え", s)  # ei ≡ ee
    return s


# --------------------------------------------------------------------------- 判分
UNKNOWN = {"不知道", "不知", "不會", "不会", "idk", "dk", "?", "不曉得", "わからない", "わかりません",
           "wakaranai", "wakarimasen", "shiranai", "pass", "skip", "不確定"}
_KANA = re.compile(r"[ぁ-ゟ゠-ヿ]")
_ASCII = re.compile(r"[a-zA-Z]")
_CJK = re.compile(r"[一-鿿]")


def is_chinese_answer(a):
    return not _KANA.search(a) and not _ASCII.search(a)


def grade_answer(item, raw, choices=None):
    raw = (raw or "").strip()
    answers = json.loads(item["answers"])
    if choices is None:
        choices = json.loads(item["choices"]) if item["choices"] else None
    low = unicodedata.normalize("NFKC", raw).strip().lower()
    if low in UNKNOWN or low.replace("？", "?") in UNKNOWN or low == "":
        return {"result": "unknown", "quality": 0, "mode": "unknown", "matched": None}
    # 選擇題：字母 / 數字 / 文字
    if item["qtype"] == "choice" and choices:
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
            return {"result": "ambiguous", "quality": None, "mode": "choice", "matched": None}
        ok = picked in answers
        return {"result": "correct" if ok else "incorrect", "quality": QUALITY_PASS if ok else 1,
                "mode": "choice", "matched": picked}
    has_jp = bool(_KANA.search(raw) or _CJK.search(raw))
    mode = "jp" if has_jp else "romaji"
    loose = not has_jp
    if loose:
        u = norm(raw, True)
        for a in answers:
            if norm(a, True) == u:
                return {"result": "correct", "quality": 4, "mode": mode, "matched": a}
    else:
        u = norm(raw, False)
        for a in answers:
            if norm(a, False) == u:
                return {"result": "correct", "quality": 4, "mode": mode, "matched": a}
        # 輸入法差異：助詞 は/を 以 わ/お 輸入，視為完全正確（長音等其他差異仍不抹平）
        _pt = str.maketrans({"は": "わ", "を": "お"})
        if item["kind"] != "kana":
            for a in answers:
                if norm(a, False).translate(_pt) == u.translate(_pt):
                    return {"result": "correct", "quality": 4, "mode": mode, "matched": a}
        # 片假名/平假名混用：非 kana 題視為正確但品質 3
        if item["kind"] != "kana":
            for a in answers:
                if norm(a, True) == norm(raw, True) and _KANA.search(a):
                    return {"result": "correct", "quality": 3, "mode": mode, "matched": a}
    # 中文關鍵詞（意思類）
    for a in answers:
        if is_chinese_answer(a) and len(a) >= 2 and _CJK.search(raw):
            r = unicodedata.normalize("NFKC", raw)
            if a in r or (len(r) >= 2 and r in a):
                return {"result": "correct", "quality": 4, "mode": mode, "matched": a}
    if all(is_chinese_answer(a) for a in answers):
        return {"result": "ambiguous", "quality": None, "mode": mode, "matched": None}
    best = max((difflib.SequenceMatcher(None, norm(raw, loose), norm(a, loose)).ratio() for a in answers),
               default=0)
    return {"result": "incorrect", "quality": 2 if best >= 0.7 else 1, "mode": mode, "matched": None}


# --------------------------------------------------------------------------- 排程
def retr(t_days, S):
    return (1 + _FACTOR * max(t_days, 0) / max(S, 1e-6)) ** _DECAY


def interval_days(S, r):
    return S / _FACTOR * (r ** (1 / _DECAY) - 1)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def required_formats(con, unit):
    rows = con.execute("SELECT DISTINCT format FROM items WHERE unit=?", (unit,)).fetchall()
    return {r["format"] for r in rows} & {"recall", "produce"}


def empty_state():
    return {"tier": "new", "difficulty": None, "stability_days": None, "due_ts": None,
            "reps_success": 0, "lapses": 0, "distinct_success_days": 0,
            "max_successful_gap_days": 0, "formats_passed": [], "success_days": [], "last_ts": None}


def is_consolidated(st, req, cfg, elapsed):
    # 最近一次作答本身必須是「隔了夠久仍提取成功」，避免失敗後短時間內答對就恢復鞏固
    if elapsed < int(cfg["cons_gap_days"]):
        return False
    days = st["success_days"]
    if len(days) < int(cfg["cons_min_days"]):
        return False
    gaps = [b - a for a, b in zip(days, days[1:])]
    if len(gaps) < 2 or min(gaps[-2:]) < int(cfg["cons_gap_days"]):
        return False
    if st["stability_days"] < float(cfg["cons_min_stability"]):
        return False
    passed = set(st["formats_passed"])
    if not req or not req <= passed:
        return False
    return True


def evidence(outcome, confidence, basis, mode):
    """(結果, 信心, 依據, 輸入形式) → 排程證據碼：0 略過／1 答錯／'N' 無證據的答對／'W' 弱證據／3、4 一般答對。
    答對但是猜的、或依據為前後文／被提示／其他 → 無證據；不確定 → 弱證據；其餘為一般證據。
    不使用作答速度。"""
    if outcome == "skipped":
        return 0
    if outcome == "incorrect":
        return 1
    if basis in NO_EVIDENCE_BASES or confidence == "guess":
        return "N"
    if confidence == "unsure":
        return "W"
    return 4 if mode in ("jp", "romaji") else 3


def apply_attempt(st, ev, ts, fmt, req, cfg):
    st = dict(st)
    if ev == "N":  # 無證據：不改穩定度、不算失誤，只確保約一天內會再測
        st["formats_passed"] = list(st["formats_passed"])
        st["success_days"] = list(st["success_days"])
        if st["last_ts"] is None:
            st.update(stability_days=0.3, difficulty=7.0, last_ts=ts, tier="learning", due_ts=ts + 86400)
        else:
            st["due_ts"] = min(st["due_ts"], ts + 86400)
        return st
    weak = ev == "W"
    q = 3 if weak else ev
    elapsed = (ts - st["last_ts"]) / 86400 if st["last_ts"] is not None else 0
    st["formats_passed"] = list(st["formats_passed"])
    st["success_days"] = list(st["success_days"])
    retention = float(cfg["target_retention"])
    if st["last_ts"] is None:
        st["stability_days"] = _S0[2 if weak else q]
        st["difficulty"] = clamp(9 - 1.5 * q, 1, 10)
    else:
        S, D = st["stability_days"], st["difficulty"]
        R = retr((ts - st["last_ts"]) / 86400, S)
        if q >= QUALITY_PASS:
            g = math.exp(0.9) * (11 - D) * S ** -0.15 * (math.exp(1.0 - R) - 1)
            S = min(S * (1 + g * {3: 0.8, 4: 1.0, 5: 1.3}[q] * (0.4 if weak else 1.0)), 1825)
            D = clamp(D + {3: 0.2, 4: -0.1, 5: -0.3}[q], 1, 10)
        else:
            S = max(0.3, S * (0.2 + 0.05 * q))   # 下調但不歸零：遺忘的是提取而非全部消失
            D = clamp(D + 0.8, 1, 10)
            if st["reps_success"] > 0:
                st["lapses"] += 1
        st["stability_days"], st["difficulty"] = S, D
    if q >= QUALITY_PASS and not weak:  # 弱證據不計入鞏固所需的成功日與格式
        st["reps_success"] += 1
        if fmt not in st["formats_passed"]:
            st["formats_passed"].append(fmt)
        d = day_ord(ts)
        if d not in st["success_days"]:
            st["success_days"].append(d)
            st["success_days"].sort()
    st["distinct_success_days"] = len(st["success_days"])
    sd = st["success_days"]
    st["max_successful_gap_days"] = max([b - a for a, b in zip(sd, sd[1:])], default=0)
    st["last_ts"] = ts
    S = st["stability_days"]
    if q < QUALITY_PASS:
        st["due_ts"] = ts + float(cfg["learn_step_min"]) * 60
        st["tier"] = "learning" if S < 7 else "review"
    else:
        st["due_ts"] = ts + max(interval_days(S, retention), float(cfg["learn_step_min"]) / 1440) * 86400
        if is_consolidated(st, req, cfg, elapsed):
            st["tier"] = "consolidated"
        elif st["reps_success"] >= 2 and S >= 7:
            st["tier"] = "review"
        else:
            st["tier"] = "learning"
    return st


def load_state(con, unit):
    r = con.execute("SELECT * FROM item_state WHERE unit=?", (unit,)).fetchone()
    if not r:
        return empty_state()
    d = dict(r)
    d["formats_passed"] = [x for x in (d["formats_passed"] or "").split(",") if x]
    d["success_days"] = json.loads(d["success_days"] or "[]")
    return d


def save_state(con, unit, st):
    con.execute(
        """INSERT OR REPLACE INTO item_state(unit,tier,difficulty,stability_days,due_ts,reps_success,lapses,
           distinct_success_days,max_successful_gap_days,formats_passed,success_days,last_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (unit, st["tier"], st["difficulty"], st["stability_days"], st["due_ts"], st["reps_success"], st["lapses"],
         st["distinct_success_days"], st["max_successful_gap_days"], ",".join(st["formats_passed"]),
         json.dumps(st["success_days"]), st["last_ts"]),
    )


def cfg_of(con):
    return {k: meta(con, k) for k in DEFAULT_META}


# --------------------------------------------------------------------------- 指令
def current_session(con, create=False):
    sid = meta(con, "current_session")
    if sid and con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
        return int(sid)
    if not create:
        return None
    cur = con.execute("INSERT INTO sessions(started_ts) VALUES(?)", (now_ts(),))
    set_meta(con, "current_session", cur.lastrowid)
    con.commit()
    return cur.lastrowid


def pick_item_for_unit(con, unit):
    rows = con.execute("SELECT * FROM items WHERE unit=? AND retired IS NULL", (unit,)).fetchall()
    if not rows:
        return None
    st = load_state(con, unit)
    passed = set(st["formats_passed"])
    last = {r["item_id"]: r["m"] for r in con.execute(
        "SELECT item_id, MAX(ts) m FROM attempts WHERE unit=? GROUP BY item_id", (unit,))}
    # 優先：尚未通過的格式、由易到難；其次：最久沒問的題
    rows.sort(key=lambda r: (r["format"] in passed, FORMAT_RANK.get(r["format"], 1), last.get(r["id"], 0)))
    return rows[0]


def cmd_next(con, args):
    ts = now_ts()
    stage = int(meta(con, "stage"))
    out = []
    due = con.execute("""SELECT s.unit, s.stability_days, s.last_ts FROM item_state s
                         WHERE s.due_ts<=? ORDER BY s.due_ts""", (ts,)).fetchall()
    due = sorted(due, key=lambda r: retr((ts - r["last_ts"]) / 86400, r["stability_days"]))
    seen = {r["unit"] for r in con.execute("SELECT unit FROM item_state")}
    new_units = [r["unit"] for r in con.execute(
        "SELECT unit, MIN(id) i FROM items WHERE stage<=? AND retired IS NULL GROUP BY unit ORDER BY i", (stage,)) if r["unit"] not in seen]
    last = con.execute("""SELECT i.kind FROM attempts a JOIN items i ON i.id=a.item_id
                          ORDER BY a.id DESC LIMIT 1""").fetchone()
    last_kind = last["kind"] if last else None
    cands = [u for u in [r["unit"] for r in due] + new_units
             if con.execute("SELECT 1 FROM items WHERE unit=? AND retired IS NULL", (u,)).fetchone()]
    taken = []
    for _ in range(args.n):
        pool = [u for u in cands if u not in taken]
        if not pool:
            break
        head = pool[:6]
        pick = head[0]
        if last_kind:  # 交錯：盡量與上一題不同類型
            for u in head:
                if pick_item_for_unit(con, u)["kind"] != last_kind:
                    pick = u
                    break
        it = pick_item_for_unit(con, pick)
        last_kind = it["kind"]
        taken.append(pick)
        out.append(it)
    if not out:
        print("NO_ITEMS: 目前沒有到期或新的題目。請依 rules/subagent-policy.md 讓 generator 產出下一輪，或 `jl.py stage <n>` 前進階段。")
        return
    for it in out:
        print(f"--- item_id: {it['id']}")
        print(f"key: {it['key']}  kind: {it['kind']}  qtype: {it['qtype']}  format: {it['format']}  scenario: {it['scenario']}")
        print(f"prompt: {it['prompt']}")
        if it["choices"]:
            order = json.loads(it["choices"])
            random.shuffle(order)  # 避免正解固定在同一位置；顯示順序存起來供 grade 對照
            set_meta(con, f"shown:{it['id']}", json.dumps(order, ensure_ascii=False))
            for i, c in enumerate(order):
                print(f"  {chr(65 + i)}. {c}")
        st = load_state(con, it["unit"])
        print(f"state: {st['tier']}")
    con.commit()


def cmd_grade(con, args):
    it = con.execute("SELECT * FROM items WHERE id=?", (args.item_id,)).fetchone()
    if not it:
        raise SystemExit("no such item")
    raw = " ".join(args.answer)
    shown = meta(con, f"shown:{it['id']}")
    g = grade_answer(it, raw, json.loads(shown) if shown else None)
    answers = json.loads(it["answers"])
    print(f"result: {g['result']}")
    oc = {"correct": "correct", "incorrect": "incorrect", "unknown": "skipped"}.get(g["result"], "<outcome>")
    print(f"suggested_outcome: {oc if oc != '<outcome>' else '(請自行判斷 correct/incorrect)'}")
    if g["result"] == "incorrect" and g["quality"] == 2:
        print("near_miss: yes（接近正解，可在 note 註明）")
    print(f"mode: {g['mode']}")
    print("--- 回饋素材（僅作答後使用）")
    print(f"正解: {answers[0]}   全部可接受: {' / '.join(answers)}")
    print(f"jp: {it['jp']}  reading: {it['reading']}  zh: {it['zh']}")
    print(f"explain: {it['explain']}")
    safe = raw.replace('"', "'")
    print(f'record_cmd: python tools/jl.py record {it["id"]} {oc} --mode {g["mode"]} --answer "{safe}"'
          '  [可加 --confidence sure|unsure|guess --basis known|intuition|context|assisted|other --note "..."]')


def cmd_record(con, args):
    it = con.execute("SELECT * FROM items WHERE id=?", (args.item_id,)).fetchone()
    if not it:
        raise SystemExit("no such item")
    ts = now_ts()
    sid = current_session(con, create=True)
    con.execute("""INSERT INTO attempts(item_id,unit,ts,session_id,outcome,confidence,basis,answer_mode,user_answer,note)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (it["id"], it["unit"], ts, sid, args.outcome, args.confidence, args.basis, args.mode,
                 args.answer, args.note))
    cfg = cfg_of(con)
    ev = evidence(args.outcome, args.confidence, args.basis, args.mode)
    st = apply_attempt(load_state(con, it["unit"]), ev, ts, it["format"],
                       required_formats(con, it["unit"]), cfg)
    save_state(con, it["unit"], st)
    con.commit()
    print(f"recorded. unit={it['unit']} tier={st['tier']} stability={st['stability_days']:.2f}d "
          f"next_due={fmt_ts(st['due_ts'])}")


def cmd_rebuild(con, args):
    cfg = cfg_of(con)
    con.execute("DELETE FROM item_state")
    units = [r["unit"] for r in con.execute("SELECT DISTINCT unit FROM attempts")]
    for u in units:
        st = empty_state()
        req = required_formats(con, u)
        for a in con.execute("""SELECT a.ts, a.outcome, a.confidence, a.basis, a.answer_mode, i.format
                                FROM attempts a JOIN items i ON i.id=a.item_id
                                WHERE a.unit=? ORDER BY a.ts, a.id""", (u,)):
            ev = evidence(a["outcome"], a["confidence"], a["basis"], a["answer_mode"])
            st = apply_attempt(st, ev, a["ts"], a["format"], req, cfg)
        save_state(con, u, st)
    con.commit()
    print(f"rebuilt {len(units)} units")


def cmd_due(con, args):
    ts = now_ts()
    rows = con.execute("""SELECT * FROM item_state s WHERE EXISTS
                          (SELECT 1 FROM items WHERE unit=s.unit AND retired IS NULL) ORDER BY due_ts""").fetchall()
    due = [r for r in rows if r["due_ts"] <= ts]
    print(f"due now: {len(due)} / tracked: {len(rows)}")
    for r in due[:20]:
        R = retr((ts - r["last_ts"]) / 86400, r["stability_days"])
        print(f"  {r['unit']}  tier={r['tier']}  S={r['stability_days']:.1f}d  R≈{R:.2f}")
    upcoming = [r for r in rows if r["due_ts"] > ts][:5]
    for r in upcoming:
        print(f"  upcoming {fmt_ts(r['due_ts'])}  {r['unit']}")


def cmd_weak(con, args):
    print("== 最弱單元（lapses 多 / 穩定度低）")
    for r in con.execute("""SELECT s.unit, s.tier, s.stability_days S, s.lapses,
                            (SELECT jp FROM items WHERE unit=s.unit LIMIT 1) jp,
                            (SELECT zh FROM items WHERE unit=s.unit LIMIT 1) zh
                            FROM item_state s ORDER BY s.lapses DESC, s.stability_days ASC LIMIT ?""", (args.limit,)):
        print(f"  {r['unit']} ({r['jp']} / {r['zh']}) tier={r['tier']} S={r['S']:.1f}d lapses={r['lapses']}")
    print("== 最近 50 筆作答：各類型答對率（含猜對、不確定；細分見 stats）")
    for r in con.execute("""SELECT i.kind k, i.format f, COUNT(*) n, SUM(a.outcome='correct') ok FROM
                            (SELECT * FROM attempts ORDER BY id DESC LIMIT 50) a JOIN items i ON i.id=a.item_id
                            GROUP BY i.kind, i.format"""):
        print(f"  {r['k']}/{r['f']}: {r['ok']}/{r['n']}")
    if args.detail:
        print("== 最近錯誤（作答 vs 正解）")
        for r in con.execute("""SELECT a.user_answer ua, a.outcome o, a.confidence c, a.basis b, a.note n,
                                i.jp, i.zh, i.kind FROM attempts a
                                JOIN items i ON i.id=a.item_id WHERE a.outcome!='correct'
                                ORDER BY a.id DESC LIMIT 15"""):
            print(f"  [{r['kind']}] {r['jp']} ({r['zh']}) ← 你答: {r['ua']}  {r['o']}/{r['c']}/{r['b']}  {r['n'] or ''}")


def cmd_stats(con, args):
    print("tier counts:", {r["tier"]: r["n"] for r in con.execute("SELECT tier, COUNT(*) n FROM item_state GROUP BY tier")})
    r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM attempts").fetchone()
    print(f"attempts total: {r['n']}  correct: {r['ok'] or 0}")
    print("correct by confidence:", {(x["c"] or "unreported"): x["n"] for x in con.execute(
        "SELECT confidence c, COUNT(*) n FROM attempts WHERE outcome='correct' GROUP BY confidence")})
    print("correct by basis:", {(x["b"] or "unreported"): x["n"] for x in con.execute(
        "SELECT basis b, COUNT(*) n FROM attempts WHERE outcome='correct' GROUP BY basis")})
    sid = current_session(con)
    if sid:
        r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM attempts WHERE session_id=?", (sid,)).fetchone()
        print(f"session {sid}: {r['n']} attempts, {r['ok'] or 0} correct")
    print("items total:", con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
          " stage:", meta(con, "stage"))


def cmd_resume(con, args):
    ts = now_ts()
    # 每次 resume 視為新對話/新片段：開新 session
    cur = con.execute("INSERT INTO sessions(started_ts) VALUES(?)", (ts,))
    set_meta(con, "current_session", cur.lastrowid)
    con.commit()
    print(f"session: {cur.lastrowid}")
    print(f"stage: {meta(con, 'stage')}   target_retention: {meta(con, 'target_retention')}")
    cp = con.execute("SELECT * FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
    if cp:
        print(f"last checkpoint ({fmt_ts(cp['ts'])}): {cp['summary']}")
        print(f"next_focus: {cp['next_focus']}")
    else:
        print("last checkpoint: (none) — 這是全新專案狀態，尚未測驗過。")
    cmd_stats(con, args)
    cmd_due(con, args)
    n = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    if n:
        args.limit, args.detail = 5, False
        cmd_weak(con, args)


def cmd_checkpoint(con, args):
    sid = current_session(con, create=True)
    last = con.execute("SELECT ts FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
    since = last["ts"] if last else 0
    r = con.execute("SELECT COUNT(*) n, SUM(outcome='correct') ok FROM attempts WHERE ts>?", (since,)).fetchone()
    con.execute("INSERT INTO checkpoints(session_id,ts,summary,next_focus) VALUES(?,?,?,?)",
                (sid, now_ts(), args.summary, args.next))
    con.execute("UPDATE sessions SET ended_ts=? WHERE id=?", (now_ts(), sid))
    con.commit()
    print(f"checkpoint saved (session {sid}). 自上次 checkpoint：{r['n']} 題，答對 {r['ok'] or 0}。")


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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="jl.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("resume")
    p = sub.add_parser("next"); p.add_argument("-n", type=int, default=1)
    p = sub.add_parser("grade"); p.add_argument("item_id", type=int); p.add_argument("answer", nargs="*")
    p = sub.add_parser("record"); p.add_argument("item_id", type=int); p.add_argument("outcome", choices=OUTCOMES)
    p.add_argument("--mode", default="jp"); p.add_argument("--answer", default="")
    p.add_argument("--confidence", choices=CONFIDENCES); p.add_argument("--basis", choices=BASES)
    p.add_argument("--note")
    sub.add_parser("due")
    p = sub.add_parser("weak"); p.add_argument("--limit", type=int, default=10); p.add_argument("--detail", action="store_true")
    sub.add_parser("stats")
    p = sub.add_parser("checkpoint"); p.add_argument("--summary", required=True); p.add_argument("--next", default="")
    p = sub.add_parser("import"); p.add_argument("file")
    p = sub.add_parser("stage"); p.add_argument("n", type=int, nargs="?")
    sub.add_parser("rebuild")
    p = sub.add_parser("retire"); p.add_argument("key"); p.add_argument("--reason", required=True)
    args = ap.parse_args()
    con = connect()
    ensure_seeds(con)
    {"init": lambda c, a: print(f"ok. db={DB_PATH}"), "resume": cmd_resume, "next": cmd_next, "grade": cmd_grade,
     "record": cmd_record, "due": cmd_due, "weak": cmd_weak, "stats": cmd_stats, "checkpoint": cmd_checkpoint,
     "import": cmd_import, "stage": cmd_stage, "rebuild": cmd_rebuild, "retire": cmd_retire}[args.cmd](con, args)


if __name__ == "__main__":
    main()
