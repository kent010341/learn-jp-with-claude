"""假練習 e2e（開發用，手動跑，會花 API 費用）：用 headless 的 `claude -p` 跑一輪短練習，檢查每題回覆的格式。

做法：複製真實資料庫到暫存資料夾（JL_DB 指向副本，claude 的 Bash 工具與 hook 都會繼承），
關閉進行中的一輪、round_max 設 5，送「開始測驗」，之後由本腳本扮演學習者（奇數題答對、偶數題答錯）。
每回合檢查：回饋與下一題寫在 record 之前、record 之後只回「（已記錄）」、沒有被要求補輸出、attempts 剛好多一筆。
涵蓋不到：桌面 app 的 ccd_turn 工具（update_plan／wrap_up）、回覆還沒結束就送出下一則（搶答）。見 docs/development.md。

用法：python -X utf8 tools/e2e_practice.py [--n 5] [--model sonnet] [--keep]
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "learning.db"
ALLOWED = ["Bash(python*)", "Bash(cd*)", "Bash(cat*)", "Bash(sed*)", "Bash(ls*)", "Read", "Glob", "Grep"]
NO_OUTPUT_NUDGE = "no visible output"


def db(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def prepare(work, n):
    copy = work / "learning.db"
    shutil.copy2(REAL_DB, copy)
    con = db(copy)
    con.execute("UPDATE rounds SET closed_ts=? WHERE closed_ts IS NULL", (time.time(),))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('round_max',?)", (str(n),))
    con.commit()
    con.close()
    return copy


def claude(msg, sid, env, model, log):
    exe = shutil.which("claude") or "claude"
    # auto：和桌面 app 練習時一樣由分類器放行；-p 的預設模式會擋下 record 的 heredoc（JSON 的大括號＋引號）
    cmd = [exe, "-p", msg, "--model", model, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--allowedTools", *ALLOWED]
    if sid:
        cmd[2:2] = ["--resume", sid]
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
    log.write(f"\n===== USER: {msg}\n{r.stdout}\n{r.stderr}\n")
    events = []
    for line in r.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    for e in events:  # 認證失敗等環境問題：不是練習本身的錯，直接停
        if e.get("type") == "result" and e.get("is_error"):
            sys.exit(f"claude -p 失敗（環境問題，不是練習格式）：{e.get('result')}")
    return events, r.stdout + r.stderr


def flatten(events):
    """回傳 (session_id, 依序的 [(kind, text)])：kind = text｜tool｜result｜user。"""
    sid, seq = None, []
    for e in events:
        sid = e.get("session_id") or sid
        msg = e.get("message") or {}
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if e.get("type") == "user":
                seq.append(("user", content))
            continue
        for b in content or []:
            t = b.get("type")
            if t == "text" and e.get("type") == "assistant":
                seq.append(("text", b["text"]))
            elif t == "text" and e.get("type") == "user":
                seq.append(("user", b["text"]))
            elif t == "tool_use":
                inp = b.get("input") or {}
                seq.append(("tool", f"{b.get('name')}: {inp.get('command') or inp.get('file_path') or ''}"))
            elif t == "tool_result":
                c = b.get("content")
                seq.append(("result", c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)))
    return sid, seq


def current_question(copy):
    con = db(copy)
    r = con.execute("SELECT * FROM rounds WHERE closed_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    p = con.execute("""SELECT p.seq, p.shown_choices, i.key, i.answers, i.task FROM presentations p
                       JOIN items i ON i.id=p.item_id
                       WHERE p.round_id=? AND p.id NOT IN (SELECT presentation_id FROM attempts
                                                           WHERE presentation_id IS NOT NULL)
                       ORDER BY p.seq LIMIT 1""", (r["id"],)).fetchone()
    con.close()
    return dict(p) if p else None


def learner_answer(q):
    answers = json.loads(q["answers"])
    shown = json.loads(q["shown_choices"]) if q["shown_choices"] else None
    correct = q["seq"] % 2 == 1
    if shown:
        target = answers[0] if correct else next(c for c in shown if c not in answers)
        return chr(65 + shown.index(target)), correct
    return (answers[0] if correct else "えっと、ちょっとわからないけど…これ？"), correct


def count(copy, q):
    con = db(copy)
    v = con.execute(q).fetchone()[0]
    con.close()
    return v


def check_answer_turn(seq_events, raw, answered_seq, next_seq):
    problems, texts_before, texts_after = [], [], []
    rec = next((i for i, (k, s) in enumerate(seq_events) if k == "tool" and "jl.py record" in s), None)
    if rec is None:
        return ["沒有呼叫 record"], "", ""
    for i, (k, s) in enumerate(seq_events):
        if k == "text":
            (texts_before if i < rec else texts_after).append(s)
    before, after = "\n".join(texts_before), "\n".join(texts_after).strip()
    if next_seq and not any(f in before for f in (f"第 {next_seq} 題", f"第{next_seq}題")):
        problems.append(f"record 之前的文字沒有第 {next_seq} 題")
    if not next_seq and before.strip():  # 最後一題：先 record、checkpoint，回饋和報告寫在最後
        problems.append(f"最後一題在 record 之前就寫了文字：{before[:60]!r}")
    if next_seq:
        if "已記錄" not in after or len(after) > 20:
            problems.append(f"record 之後不是只有「（已記錄）」：{after[:80]!r}")
        if NO_OUTPUT_NUDGE in raw:
            problems.append("出現了 no visible output 的補輸出要求")
    tools = [s for k, s in seq_events if k == "tool"]
    if sum("jl.py record" in s for s in tools) != 1:
        problems.append(f"record 呼叫 {sum('jl.py record' in s for s in tools)} 次")
    return problems, before, after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--keep", action="store_true", help="保留暫存資料夾")
    args = ap.parse_args()
    real_attempts = count(REAL_DB, "SELECT COUNT(*) FROM attempts")
    work = Path(tempfile.mkdtemp(prefix="jl_e2e_"))
    copy = prepare(work, args.n)
    env = dict(os.environ, JL_DB=str(copy), PYTHONIOENCODING="utf-8")
    log_path = work / "stream.log"
    report, failed = [], False
    with open(log_path, "w", encoding="utf-8") as log:
        events, raw = claude("開始測驗", None, env, args.model, log)
        sid, seq = flatten(events)
        q = current_question(copy)
        ok = q is not None and q["seq"] == 1 and any(("第 1 題" in s or "第1題" in s)
                                                      for k, s in seq if k == "text")
        report.append(f"[start] {'OK' if ok else 'FAIL'}  tools={[s[:50] for k, s in seq if k == 'tool']}")
        failed |= not ok
        while q and not failed:
            ans, correct = learner_answer(q)
            n_before = count(copy, "SELECT COUNT(*) FROM attempts")
            events, raw = claude(ans, sid, env, args.model, log)
            _, seq = flatten(events)
            nq = current_question(copy)
            next_seq = nq["seq"] if nq else None
            problems, before, after = check_answer_turn(seq, raw, q["seq"], next_seq)
            if count(copy, "SELECT COUNT(*) FROM attempts") != n_before + 1:
                problems.append("attempts 沒有剛好多一筆")
            if not nq:
                closed = count(copy, "SELECT COUNT(*) FROM rounds WHERE closed_ts IS NULL") == 0
                if not closed:
                    problems.append("最後一題後沒有 checkpoint 關閉本輪")
            order = " ".join("T" if k == "text" else "R" if k == "tool" and "jl.py record" in s
                             else "t" if k == "tool" else "" for k, s in seq).split()
            report.append(f"[seq {q['seq']}] {'OK' if not problems else 'FAIL'}  answer={ans!r}"
                          f"({'對' if correct else '錯'})  order={''.join(order)}  before={len(before)}ch"
                          f"  after={after[:30]!r}" + ("".join(f"\n    - {p}" for p in problems)))
            failed |= bool(problems)
            q = nq
    print("\n".join(report))
    print("RESULT:", "FAIL" if failed else "PASS")
    print("log:", log_path)
    if count(REAL_DB, "SELECT COUNT(*) FROM attempts") != real_attempts:
        print("WARNING: 真實資料庫的 attempts 筆數變了！")
    if not args.keep and not failed:
        shutil.rmtree(work, ignore_errors=True)
        print("(暫存資料夾已刪除；要保留請加 --keep)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
