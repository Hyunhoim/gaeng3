"""공개·블라인드 문제 세트 실행기 — 질문 파일만 받아 공식 GET /answer 를 순차 호출하고 응답·소요시간을 저장한다.

사용법:
  python3 run_open_set.py --base-url http://<서버>:<포트> --questions blind_set_A.questions.json --output responses_A.json
옵션:
  --timeout 300   (문항당 최대 대기 초. 초과하면 answer 비우고 elapsed_seconds 만 기록)
  --retries 2     (연결 실패·5xx 시 재시도 횟수. 평가 서버와 같은 규칙)
  --resume        (output 파일이 있으면 이미 답한 question_id 는 건너뛴다)
정답은 다루지 않는다. 표준 라이브러리만 사용한다.
"""
import argparse, json, pathlib, sys, time, urllib.error, urllib.parse, urllib.request

def call(base_url, question_id, question, timeout):
    url = f"{base_url.rstrip('/')}/answer?" + urllib.parse.urlencode({"question_id": question_id, "question": question})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True); ap.add_argument("--questions", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path); ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--retries", type=int, default=2); ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    items = json.load(open(a.questions, encoding="utf-8"))
    done = {}
    if a.resume and a.output.exists():
        done = {r["question_id"]: r for r in json.load(open(a.output, encoding="utf-8"))}
    results = []
    for i, q in enumerate(items, 1):
        qid, text = q["question_id"], q["question"]
        if qid in done:
            results.append(done[qid]); print(f"[{i}/{len(items)}] {qid} (이미 있음)"); continue
        rec = {"question_id": qid, "question": text, "retrieved_context": "", "think_trace": "", "answer": "", "elapsed_seconds": None, "http_status": None, "error": None, "attempts": 0}
        t0 = time.time()
        for attempt in range(a.retries + 1):
            rec["attempts"] = attempt + 1
            try:
                status, body = call(a.base_url, qid, text, a.timeout)
                rec["http_status"] = status
                for k in ("retrieved_context", "think_trace", "answer"):
                    rec[k] = body.get(k, "")
                rec["error"] = None; break
            except urllib.error.HTTPError as e:
                rec["http_status"] = e.code; rec["error"] = f"HTTP {e.code}"
                if e.code < 500: break
            except Exception as e:  # timeout, connection
                rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(2)
        rec["elapsed_seconds"] = round(time.time() - t0, 2)
        results.append(rec)
        flag = "" if rec["error"] is None else f"  !! {rec['error']}"
        print(f"[{i}/{len(items)}] {qid} {rec['elapsed_seconds']}s HTTP {rec['http_status']} answer={len(rec['answer'])}자{flag}", flush=True)
        a.output.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    el = [r["elapsed_seconds"] for r in results if r["elapsed_seconds"] is not None]; el.sort()
    p = lambda f: el[min(len(el) - 1, int(len(el) * f))] if el else None
    print(f"끝. {len(results)}문항, 오류 {sum(1 for r in results if r['error'])}건, 시간 p50 {p(0.5)}s p95 {p(0.95)}s 최대 {el[-1] if el else None}s, 60초 초과 {sum(1 for x in el if x > 60)}건")
    print(f"저장: {a.output}  (채점은 프롬프트 3 + 정답 파일로 사용자 PC에서)")

if __name__ == "__main__":
    main()
