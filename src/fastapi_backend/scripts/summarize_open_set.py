"""공개 문제 세트 결과 요약 — run_open_set.py 출력(응답 JSON)을 유형별 표로 정리한다.

사용법:
  python3 summarize_open_set.py --questions open90.questions.json --responses A.json [--responses B.json ...] [--labels new old]
질문 파일 필드: question_id, question, 그리고 있으면 set, difficulty, answerable, expected_answer.
응답 파일: run_open_set.py 출력(question_id, retrieved_context, think_trace, answer, elapsed_seconds, http_status, error).
상태는 think_trace 안의 status 값을 읽는다(success / not_found / clarification / cannot_verify / unsupported / error).
"""
import argparse, json, re, statistics, collections

STATUS_RE = re.compile(r'"?(?:status|outcome|backend_status|final_status)"?\s*[:=]\s*"?([A-Za-z_]+)')

def status_of(r):
    if r.get("http_status") != 200 or r.get("error"):
        return "error"
    m = STATUS_RE.search(r.get("think_trace", "") or "")
    return (m.group(1).lower() if m else "unknown")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True); ap.add_argument("--responses", nargs="+", required=True)
    ap.add_argument("--labels", nargs="*"); ap.add_argument("--show", action="store_true", help="문항별 답변 앞부분 출력")
    a = ap.parse_args()
    qs = {q["question_id"]: q for q in json.load(open(a.questions, encoding="utf-8"))}
    labels = a.labels or [f"R{i+1}" for i in range(len(a.responses))]
    runs = {}
    for lab, path in zip(labels, a.responses):
        runs[lab] = {r["question_id"]: r for r in json.load(open(path, encoding="utf-8"))}
    refusal = {"not_found", "clarification", "cannot_verify", "unsupported", "clarify"}
    for lab, rs in runs.items():
        print(f"\n===== {lab} ({len(rs)}문항) =====")
        groups = collections.defaultdict(list)
        for qid, q in qs.items():
            r = rs.get(qid)
            if not r: continue
            key = (str(q.get("set", "-")), q.get("difficulty", "-"), "가능" if q.get("answerable", True) else "불가")
            groups[key].append((q, r, status_of(r)))
        print(f"{'세트':4} {'난이도':4} {'유형':4} {'n':>3} {'success':>7} {'거부/되묻기':>9} {'error':>5} {'p50s':>6} {'max s':>6}  기대충족")
        for key in sorted(groups):
            items = groups[key]; st = [s for _, _, s in items]
            times = [r["elapsed_seconds"] for _, r, _ in items if r.get("elapsed_seconds")]
            ok = sum(1 for q, r, s in items if (s == "success") if q.get("answerable", True)) + \
                 sum(1 for q, r, s in items if (s in refusal) if not q.get("answerable", True))
            print(f"{key[0]:4} {key[1]:4} {key[2]:4} {len(items):>3} {st.count('success'):>7} {sum(s in refusal for s in st):>9} {st.count('error'):>5} "
                  f"{(statistics.median(times) if times else 0):>6.1f} {(max(times) if times else 0):>6.1f}  {ok}/{len(items)}")
        allt = [r["elapsed_seconds"] for r in rs.values() if r.get("elapsed_seconds")]
        print(f"전체: p50 {statistics.median(allt):.1f}s, p95 {sorted(allt)[int(len(allt)*0.95)-1]:.1f}s, 60초 초과 {sum(t>60 for t in allt)}건, 오류 {sum(status_of(r)=='error' for r in rs.values())}건")
    if len(runs) >= 2:
        l1, l2 = labels[0], labels[1]
        print(f"\n===== 상태가 달라진 문항 ({l1} → {l2}) =====")
        for qid, q in qs.items():
            r1, r2 = runs[l1].get(qid), runs[l2].get(qid)
            if r1 and r2 and status_of(r1) != status_of(r2):
                print(f"{qid:8} {q.get('difficulty','-')}/{'가능' if q.get('answerable', True) else '불가'}  {status_of(r1):14} → {status_of(r2):14}  {q['question'][:50]}")
    if a.show:
        for lab, rs in runs.items():
            print(f"\n===== {lab} 문항별 =====")
            for qid, q in qs.items():
                r = rs.get(qid)
                if r: print(f"[{qid}] {status_of(r):13} {r.get('elapsed_seconds',0):5.1f}s | {q['question'][:40]} | {r.get('answer','')[:80].replace(chr(10),' ')}"
                            + (f"\n     기대: {str(q.get('expected_answer'))[:80]}" if q.get("expected_answer") else ""))

if __name__ == "__main__":
    main()
