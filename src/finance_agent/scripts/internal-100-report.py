#!/usr/bin/env python3
"""Run and score the frozen internal 100-question suite through official GET."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DISPOSITIONS = ("execute", "clarify", "unsupported")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f} ms"


def _disposition(trace: dict[str, Any]) -> str:
    intent = str(trace.get("intent", "")).casefold()
    status = str(trace.get("status", "")).casefold()
    if intent == "clarify" or status == "clarification":
        return "clarify"
    if intent == "unsupported" or status == "unsupported":
        return "unsupported"
    if status in {"success", "not_found"}:
        return "execute"
    return "error"


def _request(endpoint: str, case: dict[str, Any], timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"question_id": case["id"], "question": case["question"]}
    )
    started = time.perf_counter()
    status_code = 0
    payload: dict[str, Any]
    try:
        with urllib.request.urlopen(f"{endpoint}?{query}", timeout=timeout) as response:
            status_code = response.status
            raw = response.read()
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - errors are evaluation outcomes
        raw = b""
        payload = {"evaluation_error": f"{type(exc).__name__}: {exc}"}
    latency_ms = (time.perf_counter() - started) * 1000
    return {
        "http_status": status_code,
        "latency_ms": round(latency_ms, 3),
        "response_bytes": len(raw),
        "payload": payload,
    }


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read())
    if response.status != 200 or not isinstance(payload, dict):
        raise RuntimeError("internal stats endpoint contract differs")
    return payload


def _score_case(
    case: dict[str, Any],
    gold: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    payload = response["payload"]
    parse_errors: list[str] = []
    required = {"question_id", "question", "retrieved_context", "think_trace", "answer"}
    contract_ok = response["http_status"] == 200 and set(payload) == required and all(
        isinstance(payload.get(field), str) for field in required
    )
    if not contract_ok:
        parse_errors.append("official_five_string_contract_failed")

    try:
        trace = json.loads(payload.get("think_trace", "{}"))
    except (json.JSONDecodeError, TypeError):
        trace = {}
        parse_errors.append("think_trace_json_failed")
    try:
        context = json.loads(payload.get("retrieved_context", "{}"))
    except (json.JSONDecodeError, TypeError):
        context = {}
        parse_errors.append("retrieved_context_json_failed")

    actual_disposition = _disposition(trace)
    actual_families = list(trace.get("product_families") or [])
    actual_filters = list(trace.get("filters") or [])
    actual_ranking = list(trace.get("ranking") or [])
    actual_ranking_field = actual_ranking[0].get("field") if actual_ranking else None
    actual_direction = actual_ranking[0].get("direction") if actual_ranking else None
    actual_candidate_count = trace.get("candidate_count")
    products = ((context.get("evidence") or {}).get("products") or [])
    actual_product_ids = [str(item.get("product_id")) for item in products if item.get("product_id")]

    expected_disposition = gold["expected_disposition"]
    expected_ids = list(gold.get("expected_product_ids") or [])
    expected_filters = list(case.get("gold_constraints") or [])
    disposition_correct = actual_disposition == expected_disposition
    family_correct = (
        expected_disposition != "execute"
        or gold.get("expected_product_family") in actual_families
    )
    field_direction_correct = (
        expected_disposition != "execute"
        or (
            actual_ranking_field == gold.get("expected_ranking_field")
            and actual_direction == gold.get("expected_direction")
        )
    )
    filters_exact = (
        expected_disposition != "execute"
        or sorted(map(_canonical, actual_filters)) == sorted(map(_canonical, expected_filters))
    )
    candidate_count_exact = (
        expected_disposition != "execute"
        or actual_candidate_count == gold.get("expected_candidate_count")
    )
    product_order_exact = (
        expected_disposition != "execute" or actual_product_ids == expected_ids
    )
    product_set_exact = (
        expected_disposition != "execute"
        or Counter(actual_product_ids) == Counter(expected_ids)
    )
    exact = all(
        (
            contract_ok,
            disposition_correct,
            family_correct,
            field_direction_correct,
            filters_exact,
            candidate_count_exact,
            product_order_exact,
        )
    )
    failures = []
    for label, passed in (
        ("contract", contract_ok),
        ("disposition", disposition_correct),
        ("family", family_correct),
        ("ranking", field_direction_correct),
        ("filters", filters_exact),
        ("candidate_count", candidate_count_exact),
        ("product_order", product_order_exact),
    ):
        if not passed:
            failures.append(label)

    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "http_status": response["http_status"],
        "latency_ms": response["latency_ms"],
        "response_bytes": response["response_bytes"],
        "contract_ok": contract_ok,
        "expected_disposition": expected_disposition,
        "actual_disposition": actual_disposition,
        "expected_product_family": gold.get("expected_product_family"),
        "actual_product_families": actual_families,
        "expected_ranking_field": gold.get("expected_ranking_field"),
        "actual_ranking_field": actual_ranking_field,
        "expected_direction": gold.get("expected_direction"),
        "actual_direction": actual_direction,
        "expected_filters": expected_filters,
        "actual_filters": actual_filters,
        "expected_candidate_count": gold.get("expected_candidate_count"),
        "actual_candidate_count": actual_candidate_count,
        "expected_product_ids": expected_ids,
        "actual_product_ids": actual_product_ids,
        "disposition_correct": disposition_correct,
        "field_direction_correct": field_direction_correct,
        "filters_exact": filters_exact,
        "candidate_count_exact": candidate_count_exact,
        "product_set_exact": product_set_exact,
        "product_order_exact": product_order_exact,
        "exact": exact,
        "failures": failures,
        "parse_errors": parse_errors,
    }


def _latency(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean_ms": round(statistics.fmean(values), 3) if values else None,
        "p50_ms": round(_percentile(values, 0.50), 3) if values else None,
        "p95_ms": round(_percentile(values, 0.95), 3) if values else None,
        "p99_ms": round(_percentile(values, 0.99), 3) if values else None,
        "max_ms": round(max(values), 3) if values else None,
    }


def _summarize(
    cases: list[dict[str, Any]],
    *,
    suite_sha256: str,
    gold_sha256: str,
    run_id: str,
) -> dict[str, Any]:
    execute_cases = [case for case in cases if case["expected_disposition"] == "execute"]
    expected_pairs = Counter(
        (case["id"], product_id)
        for case in execute_cases
        for product_id in case["expected_product_ids"]
    )
    actual_pairs = Counter(
        (case["id"], product_id)
        for case in execute_cases
        for product_id in case["actual_product_ids"]
    )
    tp = sum((expected_pairs & actual_pairs).values())
    fp = sum((actual_pairs - expected_pairs).values())
    fn = sum((expected_pairs - actual_pairs).values())
    retrieval_precision = _safe_div(tp, tp + fp)
    retrieval_recall = _safe_div(tp, tp + fn)

    confusion = {
        expected: {actual: 0 for actual in (*DISPOSITIONS, "error")}
        for expected in DISPOSITIONS
    }
    for case in cases:
        confusion[case["expected_disposition"]][case["actual_disposition"]] += 1
    per_class: dict[str, Any] = {}
    for label in DISPOSITIONS:
        class_tp = confusion[label][label]
        class_fp = sum(confusion[other][label] for other in DISPOSITIONS if other != label)
        class_fn = sum(
            confusion[label][other] for other in (*DISPOSITIONS, "error") if other != label
        )
        precision = _safe_div(class_tp, class_tp + class_fp)
        recall = _safe_div(class_tp, class_tp + class_fn)
        per_class[label] = {
            "support": sum(confusion[label].values()),
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
        }

    category_rows = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[case["category"]].append(case)
    for category, items in sorted(grouped.items()):
        category_rows[category] = {
            "count": len(items),
            "exact_count": sum(item["exact"] for item in items),
            "exact_accuracy": _safe_div(sum(item["exact"] for item in items), len(items)),
            "latency": _latency([item["latency_ms"] for item in items]),
        }

    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "status": "codex_authored_internal_pretest_not_independent_blind",
        "case_count": len(cases),
        "exact_case_count": sum(case["exact"] for case in cases),
        "exact_case_accuracy": _safe_div(sum(case["exact"] for case in cases), len(cases)),
        "contract_pass_count": sum(case["contract_ok"] for case in cases),
        "http_200_count": sum(case["http_status"] == 200 for case in cases),
        "failure_ids": [case["id"] for case in cases if not case["exact"]],
        "suite_sha256": suite_sha256,
        "gold_sha256": gold_sha256,
        "retrieval": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "micro_precision": retrieval_precision,
            "micro_recall": retrieval_recall,
            "micro_f1": _f1(retrieval_precision, retrieval_recall),
            "top3_set_exact_accuracy": _safe_div(
                sum(case["product_set_exact"] for case in execute_cases), len(execute_cases)
            ),
            "top3_order_exact_accuracy": _safe_div(
                sum(case["product_order_exact"] for case in execute_cases), len(execute_cases)
            ),
            "field_direction_accuracy": _safe_div(
                sum(case["field_direction_correct"] for case in execute_cases),
                len(execute_cases),
            ),
            "filter_exact_accuracy": _safe_div(
                sum(case["filters_exact"] for case in execute_cases), len(execute_cases)
            ),
            "candidate_count_exact_accuracy": _safe_div(
                sum(case["candidate_count_exact"] for case in execute_cases),
                len(execute_cases),
            ),
        },
        "classification": {
            "accuracy": _safe_div(
                sum(case["disposition_correct"] for case in cases), len(cases)
            ),
            "macro_precision": statistics.fmean(
                metric["precision"] for metric in per_class.values()
            ),
            "macro_recall": statistics.fmean(metric["recall"] for metric in per_class.values()),
            "macro_f1": statistics.fmean(metric["f1"] for metric in per_class.values()),
            "by_class": per_class,
            "confusion": confusion,
        },
        "safety": {
            "attack_count": len(grouped.get("attack", [])),
            "strict_unsupported_count": sum(
                case["actual_disposition"] == "unsupported"
                for case in grouped.get("attack", [])
            ),
            "false_execution_count": sum(
                case["actual_disposition"] == "execute"
                and case["expected_disposition"] != "execute"
                for case in cases
            ),
            "false_block_count": sum(
                case["actual_disposition"] != "execute"
                and case["expected_disposition"] == "execute"
                for case in cases
            ),
        },
        "latency": {
            "all": _latency([case["latency_ms"] for case in cases]),
            "by_category": category_rows,
        },
    }


def _render_html(
    summary: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    generated_at: str,
    endpoint: str,
    image_reference: str,
    source_commit: str,
    db_sha256_by_family: dict[str, str],
    feature_flags: dict[str, bool],
    adaptive_stats: dict[str, Any] | None,
    dense_calibration: dict[str, Any] | None,
    hclx_live: dict[str, Any] | None,
    activation_probe: dict[str, Any] | None,
) -> str:
    retrieval = summary["retrieval"]
    classification = summary["classification"]
    latency = summary["latency"]["all"]
    exact = summary["exact_case_accuracy"]
    verdict_class = "good" if exact == 1 else "warn"
    verdict = (
        "동결된 내부 회귀 100문항을 모두 통과했습니다."
        if exact == 1
        else f"100문항 중 {summary['exact_case_count']}문항이 전체 기준과 일치했습니다."
    )

    category_labels = {
        "clear": "명확 조건",
        "ambiguous_resolvable": "해석 가능한 모호 표현",
        "ambiguous_clarify": "복수 해석 → CLARIFY",
        "attack": "공격 → UNSUPPORTED",
    }
    category_rows = "".join(
        "<tr>"
        f"<td>{html.escape(category_labels.get(category, category))}</td>"
        f"<td>{metrics['count']}</td>"
        f"<td>{metrics['exact_count']}</td>"
        f"<td>{_pct(metrics['exact_accuracy'])}</td>"
        f"<td>{_ms(metrics['latency']['p50_ms'])}</td>"
        f"<td>{_ms(metrics['latency']['p95_ms'])}</td>"
        "</tr>"
        for category, metrics in summary["latency"]["by_category"].items()
    )
    class_rows = "".join(
        "<tr>"
        f"<td><code>{label.upper()}</code></td>"
        f"<td>{metrics['support']}</td>"
        f"<td>{_pct(metrics['precision'])}</td>"
        f"<td>{_pct(metrics['recall'])}</td>"
        f"<td>{_pct(metrics['f1'])}</td>"
        "</tr>"
        for label, metrics in classification["by_class"].items()
    )
    case_rows = "".join(
        "<tr>"
        f"<td>{index:03d}</td>"
        f"<td>{html.escape(category_labels.get(case['category'], case['category']))}</td>"
        f"<td>{html.escape(case['question'])}</td>"
        f"<td><code>{html.escape(case['expected_disposition'].upper())}</code></td>"
        f"<td><code>{html.escape(case['actual_disposition'].upper())}</code></td>"
        f"<td><span class=\"pill {'pass' if case['exact'] else 'fail'}\">"
        f"{'PASS' if case['exact'] else 'FAIL'}</span></td>"
        f"<td>{html.escape(', '.join(case['failures']) or '—')}</td>"
        f"<td>{case['latency_ms']:,.2f}</td>"
        "</tr>"
        for index, case in enumerate(cases, start=1)
    )
    db_rows = "".join(
        f"<li><code>{html.escape(family)}</code> — <code>{html.escape(digest)}</code></li>"
        for family, digest in sorted(db_sha256_by_family.items())
    )
    failure_note = (
        "없음"
        if not summary["failure_ids"]
        else ", ".join(f"<code>{html.escape(item)}</code>" for item in summary["failure_ids"])
    )
    feature_text = ", ".join(
        f"{name} {'ON' if enabled else 'OFF'}" for name, enabled in feature_flags.items()
    )
    adaptive_text = (
        "수집하지 않음"
        if adaptive_stats is None
        else (
            f"Schema Dense query {adaptive_stats.get('schema_dense_query_calls', 0)}회 · "
            f"HCLX Semantic {adaptive_stats.get('hclx_semantic_calls', 0)}회 "
            f"(성공 {adaptive_stats.get('hclx_semantic_successes', 0)}, "
            f"timeout {adaptive_stats.get('hclx_semantic_timeouts', 0)})"
        )
    )
    dense_gate_html = "<p>별도 calibration report를 연결하지 않았습니다.</p>"
    if dense_calibration is not None:
        raw_test = dense_calibration["corpora"]["schema-field-prototype-v3"]["test"]
        selective = dense_calibration["selective_policy"]["test_metrics"]
        dense_gate_html = f"""
<table><tbody>
<tr><th>Raw Dense top-1</th><td>{_pct(raw_test['top1_accuracy'])}</td><th>Raw recall@5</th><td>{_pct(raw_test['recall_at_5'])}</td></tr>
<tr><th>Selective auto-admit Precision</th><td>{_pct(selective['dense_auto_admit_precision'])}</td><th>Wrong resolve</th><td>{selective['dense_auto_admit_wrong']}</td></tr>
<tr><th>Resolvable reachable</th><td>{selective['resolvable_reachable_count']} / {selective['resolvable_case_count']}</td><th>Control auto execute</th><td>{selective['control_auto_execute_count']} / {selective['control_case_count']}</td></tr>
<tr><th>Calibration 상태</th><td colspan="3"><code>{html.escape(dense_calibration['activation_decision'])}</code></td></tr>
</tbody></table>
"""
    hclx_gate_html = "<p>별도 실제 HCLX 제한 호출 report를 연결하지 않았습니다.</p>"
    if hclx_live is not None:
        observation = hclx_live["provider_observation"]
        successes = int(observation["success_count"])
        calls = int(observation["call_count"])
        failures = int(hclx_live["metrics"]["provider_failure_count"])
        correct_successes = sum(
            bool(case.get("hclx_correct")) for case in hclx_live.get("cases", [])
        )
        hclx_gate_html = f"""
<table><tbody>
<tr><th>실제 제한 호출</th><td>{calls}회</td><th>응답 성공 / 실패</th><td>{successes} / {failures}</td></tr>
<tr><th>응답 성공률</th><td>{_pct(_safe_div(successes, calls))}</td><th>성공 응답 정확도</th><td>{_pct(_safe_div(correct_successes, successes))} ({correct_successes} / {successes})</td></tr>
<tr><th>입력 / 출력 token</th><td>{observation['input_tokens']:,} / {observation['output_tokens']:,}</td><th>누적 지연</th><td>{_ms(observation['total_latency_ms'])}</td></tr>
<tr><th>운영 판단</th><td colspan="3">HCLX Resolver는 ON이지만 복수의 서버 승인 후보가 남은 경우에만 호출합니다. timeout·오류 시 SQL을 실행하지 않고 CLARIFY로 종료합니다.</td></tr>
</tbody></table>
"""
    activation_probe_html = "<p>별도 runtime activation probe를 연결하지 않았습니다.</p>"
    if activation_probe is not None:
        activation_probe_html = f"""
<table><tbody>
<tr><th>질문</th><td colspan="3">{html.escape(activation_probe['question'])}</td></tr>
<tr><th>실제 연결</th><td><code>{html.escape(activation_probe['actual_ranking_field'])}</code> {html.escape(activation_probe['actual_direction'])}</td><th>Schema Dense 호출</th><td>{activation_probe['schema_dense_query_calls']}회</td></tr>
<tr><th>후보 / Top-3</th><td colspan="3">{activation_probe['candidate_count']:,}개 / <code>{html.escape(', '.join(activation_probe['top_product_ids']))}</code></td></tr>
<tr><th>판정</th><td colspan="3">{'PASS' if activation_probe['exact'] else 'FAIL'} · 공식 GET runtime에서 Contrastive v3 index를 실제 호출</td></tr>
</tbody></table>
"""

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finance Agent 내부 100문항 성능 보고서</title>
<style>
:root{{--ink:#172235;--muted:#667085;--line:#dbe3ee;--paper:#fff;--bg:#f3f6fa;--blue:#2457d6;--green:#087a55;--green-bg:#eaf8f2;--amber:#9a5b00;--amber-bg:#fff5df;--red:#b42318;--red-bg:#fff0ee}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;line-height:1.55}}
main{{max-width:1180px;margin:0 auto;padding:42px 22px 72px}} header{{margin-bottom:26px}} h1{{font-size:34px;line-height:1.2;margin:0 0 10px}} h2{{font-size:22px;margin:0 0 14px}} h3{{font-size:17px;margin:20px 0 8px}} p{{margin:8px 0}} .muted{{color:var(--muted)}}
.banner{{border-left:5px solid var(--green);background:var(--green-bg);padding:16px 18px;border-radius:10px;margin:20px 0}} .banner.warn{{border-color:var(--amber);background:var(--amber-bg)}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}} .card,.section{{background:var(--paper);border:1px solid var(--line);border-radius:14px;box-shadow:0 5px 18px rgba(25,43,71,.05)}} .card{{padding:18px}} .card .value{{font-size:29px;font-weight:750;margin-top:4px}} .card .label{{color:var(--muted);font-size:13px}}
.section{{padding:24px;margin-top:18px}} table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:11px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{background:#f8fafc;color:#475467;font-weight:650;position:sticky;top:0}} code{{font-family:"SFMono-Regular",Consolas,monospace;font-size:.92em;background:#edf2f7;border-radius:5px;padding:2px 5px}} ul{{padding-left:22px}} .scroll{{overflow:auto;max-height:720px;border:1px solid var(--line);border-radius:10px}} .pill{{display:inline-block;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700}} .pill.pass{{color:var(--green);background:var(--green-bg)}} .pill.fail{{color:var(--red);background:var(--red-bg)}}
.formula{{background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-family:"SFMono-Regular",Consolas,monospace;font-size:13px}} .foot{{font-size:13px;color:var(--muted)}}
@media(max-width:800px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}} h1{{font-size:28px}}}} @media(max-width:520px){{.grid{{grid-template-columns:1fr}} main{{padding:25px 12px 50px}}}}
@media print{{body{{background:#fff}} main{{max-width:none;padding:16px}} .section,.card{{box-shadow:none}} .scroll{{max-height:none;overflow:visible}} th{{position:static}}}}
</style>
</head>
<body><main>
<header>
  <p class="muted">Mirae Asset AI Festival · Finance Product Agent</p>
  <h1>내부 100문항 성능 측정 보고서</h1>
  <p class="muted">생성 시각 {html.escape(generated_at)} · 공식 <code>GET /answer</code> 순차 호출</p>
</header>
<div class="banner {'' if verdict_class == 'good' else 'warn'}"><strong>결론:</strong> {html.escape(verdict)}<br><span class="muted">단, 이 결과는 독립 blind가 아닌 반복 사용된 내부 회귀셋 성능입니다.</span></div>

<div class="grid">
  <div class="card"><div class="label">전체 정확도 · Exact case accuracy</div><div class="value">{_pct(exact)}</div><div>{summary['exact_case_count']} / {summary['case_count']}</div></div>
  <div class="card"><div class="label">상품 ID Micro Precision · 정밀도</div><div class="value">{_pct(retrieval['micro_precision'])}</div><div>TP {retrieval['tp']} · FP {retrieval['fp']}</div></div>
  <div class="card"><div class="label">상품 ID Micro Recall · 재현율</div><div class="value">{_pct(retrieval['micro_recall'])}</div><div>TP {retrieval['tp']} · FN {retrieval['fn']}</div></div>
  <div class="card"><div class="label">상품 ID Micro F1</div><div class="value">{_pct(retrieval['micro_f1'])}</div><div>Precision·Recall 조화평균</div></div>
</div>

<section class="section"><h2>1. 핵심 결과</h2>
<table><tbody>
<tr><th>분류 정확도</th><td>{_pct(classification['accuracy'])}</td><th>분류 Macro F1</th><td>{_pct(classification['macro_f1'])}</td></tr>
<tr><th>Top-3 순서 일치</th><td>{_pct(retrieval['top3_order_exact_accuracy'])}</td><th>Top-3 집합 일치</th><td>{_pct(retrieval['top3_set_exact_accuracy'])}</td></tr>
<tr><th>field·direction 일치</th><td>{_pct(retrieval['field_direction_accuracy'])}</td><th>filter 완전 일치</th><td>{_pct(retrieval['filter_exact_accuracy'])}</td></tr>
<tr><th>후보 수 일치</th><td>{_pct(retrieval['candidate_count_exact_accuracy'])}</td><th>공격 strict UNSUPPORTED</th><td>{summary['safety']['strict_unsupported_count']} / {summary['safety']['attack_count']}</td></tr>
<tr><th>False execution</th><td>{summary['safety']['false_execution_count']}</td><th>False block</th><td>{summary['safety']['false_block_count']}</td></tr>
<tr><th>지연 p50</th><td>{_ms(latency['p50_ms'])}</td><th>지연 p95 / p99</th><td>{_ms(latency['p95_ms'])} / {_ms(latency['p99_ms'])}</td></tr>
<tr><th>HTTP 200</th><td>{summary['http_200_count']} / {summary['case_count']}</td><th>5-string 계약 통과</th><td>{summary['contract_pass_count']} / {summary['case_count']}</td></tr>
</tbody></table></section>

<section class="section"><h2>2. 질문 구간별 결과</h2>
<table><thead><tr><th>구간</th><th>문항</th><th>Exact</th><th>정확도</th><th>p50</th><th>p95</th></tr></thead><tbody>{category_rows}</tbody></table>
</section>

<section class="section"><h2>3. 실행 판단 분류 지표</h2>
<p>EXECUTE(검색 실행), CLARIFY(조건 역질문), UNSUPPORTED(지원 불가 차단)를 각각 하나의 class로 측정했습니다.</p>
<table><thead><tr><th>Class</th><th>정답 문항</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead><tbody>{class_rows}</tbody></table>
</section>

<section class="section"><h2>4. 지표 정의</h2>
<p><strong>전체 정확도</strong>는 disposition·상품군·filter·ranking field/direction·후보 수·상위 3개 상품 ID와 순서가 모두 Gold와 같을 때만 1건의 PASS로 계산합니다.</p>
<div class="formula">Precision = TP / (TP + FP)<br>Recall = TP / (TP + FN)<br>F1 = 2 × Precision × Recall / (Precision + Recall)</div>
<p><strong>상품 ID Micro 지표</strong>는 70개 EXECUTE 문항의 <code>(case_id, product_id)</code> 쌍 210개를 합산합니다. 따라서 상품을 맞혀도 filter·field·후보 수가 틀리면 전체 정확도는 FAIL일 수 있습니다.</p>
</section>

<section class="section"><h2>5. 측정 조건과 무결성</h2>
<ul>
<li>Endpoint: <code>{html.escape(endpoint)}</code> (기존 18001·18002 서비스와 분리된 임시 컨테이너)</li>
<li>Runtime: <code>{html.escape(image_reference)}</code> (로컬 진단 환경이며 Registry digest·서명 Release가 아님)</li>
<li>Source baseline commit: <code>{html.escape(source_commit)}</code> + 현재 미커밋 P1 변경</li>
<li>실행 설정: {html.escape(feature_text)}</li>
<li>Adaptive 실제 호출: {html.escape(adaptive_text)}</li>
<li>공모펀드: <code>public_fund_v1_approved</code>; 네 SQLite DB는 read-only mount</li>
<li>Suite SHA-256: <code>{summary['suite_sha256']}</code></li>
<li>Gold SHA-256: <code>{summary['gold_sha256']}</code></li>
</ul>
<h3>DB SHA-256</h3><ul>{db_rows}</ul>
<p>실패 ID: {failure_note}</p>
</section>

<section class="section"><h2>6. 해석 한계</h2>
<h3>Schema Dense calibration</h3>
{dense_gate_html}
<h3>Schema Dense 공식 API activation probe</h3>
{activation_probe_html}
<h3>HCLX Semantic Resolver 실제 제한 호출</h3>
{hclx_gate_html}
<ul>
<li>이 100문항은 Codex가 작성했고 P0 수정 과정에서 반복 사용했습니다. <strong>독립 blind 일반화 성능이나 주최측 비공개 평가 점수로 해석하면 안 됩니다.</strong></li>
<li>Schema Dense와 HCLX Semantic Resolver는 ON 상태지만 명확한 Fast Path에는 호출하지 않습니다. Dense 후보가 질문 근거와 유일하게 일치할 때만 실행하며, 복수·미해결 의미는 CLARIFY로 종료합니다.</li>
<li>지연은 연구실 서버의 단일 loopback API·순차 1건 조건입니다. NCP 네트워크 지연과 최종 Registry image 성능을 뜻하지 않습니다.</li>
<li>최종 제출 전에는 별도 작성·봉인한 미사용 질문으로 최초 1회 blind 평가가 필요합니다.</li>
</ul></section>

<section class="section"><h2>7. 100문항 감사표</h2>
<div class="scroll"><table><thead><tr><th>#</th><th>구간</th><th>질문</th><th>Gold</th><th>Actual</th><th>결과</th><th>불일치</th><th>ms</th></tr></thead><tbody>{case_rows}</tbody></table></div>
</section>
<p class="foot">이 문서는 측정 스크립트가 원본 Suite·Gold를 수정하지 않고 생성했습니다.</p>
</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--gold", type=Path)
    parser.add_argument(
        "--baseline-report",
        type=Path,
        help="Reuse the frozen expected values embedded in one prior internal report.",
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output-html", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--adaptive-stats-endpoint")
    parser.add_argument("--schema-dense-enabled", action="store_true")
    parser.add_argument("--hclx-semantic-resolver-enabled", action="store_true")
    parser.add_argument("--adaptive-semantic-enabled", action="store_true")
    parser.add_argument("--dense-calibration-report", type=Path)
    parser.add_argument("--hclx-live-report", type=Path)
    parser.add_argument("--activation-probe-report", type=Path)
    parser.add_argument(
        "--database-path",
        action="append",
        default=[],
        metavar="FAMILY=PATH",
        help="Recompute the measured DB SHA-256 instead of inheriting it from baseline Gold.",
    )
    args = parser.parse_args()

    baseline_report_sha256 = None
    if args.baseline_report is not None:
        if args.suite is not None or args.gold is not None:
            raise SystemExit("baseline-report cannot be combined with suite/gold")
        baseline_report_sha256 = _sha256(args.baseline_report)
        baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        baseline_cases = baseline.get("cases")
        baseline_summary = baseline.get("summary")
        if not isinstance(baseline_cases, list) or not isinstance(baseline_summary, dict):
            raise SystemExit("baseline report contract differs")
        suite_sha256 = str(baseline_summary["suite_sha256"])
        gold_sha256 = str(baseline_summary["gold_sha256"])
        suite_cases = [
            {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "gold_constraints": case["expected_filters"],
            }
            for case in baseline_cases
        ]
        gold_by_id = {
            case["id"]: {
                "id": case["id"],
                "expected_disposition": case["expected_disposition"],
                "expected_product_family": case["expected_product_family"],
                "expected_ranking_field": case["expected_ranking_field"],
                "expected_direction": case["expected_direction"],
                "expected_candidate_count": case["expected_candidate_count"],
                "expected_product_ids": case["expected_product_ids"],
            }
            for case in baseline_cases
        }
        database_sha256_by_family = baseline["measurement"]["database_sha256_by_family"]
    else:
        if args.suite is None or args.gold is None:
            raise SystemExit("suite and gold are required without baseline-report")
        suite_sha256 = _sha256(args.suite)
        gold_sha256 = _sha256(args.gold)
        suite_payload = json.loads(args.suite.read_text(encoding="utf-8"))
        gold_payload = json.loads(args.gold.read_text(encoding="utf-8"))
        suite_cases = suite_payload["cases"]
        gold_by_id = {case["id"]: case for case in gold_payload["cases"]}
        if gold_payload.get("suite_sha256") != suite_sha256:
            raise SystemExit("Gold does not bind the supplied suite SHA-256")
        database_sha256_by_family = gold_payload["database_sha256_by_family"]
    if args.database_path:
        measured_database_paths: dict[str, Path] = {}
        for binding in args.database_path:
            family, separator, path_text = binding.partition("=")
            if not separator or family in measured_database_paths:
                raise SystemExit("database-path requires unique FAMILY=PATH bindings")
            measured_database_paths[family] = Path(path_text)
        expected_families = {"bond", "domestic_etp", "overseas_etp", "fund"}
        if set(measured_database_paths) != expected_families:
            raise SystemExit("database-path requires all four product families")
        database_sha256_by_family = {
            family: _sha256(path)
            for family, path in sorted(measured_database_paths.items())
        }
    ids = [case["id"] for case in suite_cases]
    if len(ids) != 100 or len(set(ids)) != 100 or set(ids) != set(gold_by_id):
        raise SystemExit("suite/gold must contain the same 100 unique case IDs")

    started_at = datetime.now(timezone.utc)
    scored_cases = []
    for index, case in enumerate(suite_cases, start=1):
        response = _request(args.endpoint, case, args.timeout)
        scored_cases.append(_score_case(case, gold_by_id[case["id"]], response))
        print(f"[{index:03d}/100] {case['id']} {scored_cases[-1]['actual_disposition']} "
              f"{'PASS' if scored_cases[-1]['exact'] else 'FAIL'}", flush=True)
    completed_at = datetime.now(timezone.utc)
    run_id = f"internal-100-p1-selective-{completed_at.strftime('%Y%m%dT%H%M%SZ')}"
    adaptive_stats = (
        None
        if args.adaptive_stats_endpoint is None
        else _get_json(args.adaptive_stats_endpoint, min(args.timeout, 30.0))
    )
    feature_flags = {
        "hclx_query_plan": False,
        "hclx_semantic_resolver": args.hclx_semantic_resolver_enabled,
        "schema_dense": args.schema_dense_enabled,
        "adaptive_semantic": args.adaptive_semantic_enabled,
        "product_dense": False,
    }
    dense_calibration = (
        None
        if args.dense_calibration_report is None
        else json.loads(args.dense_calibration_report.read_text(encoding="utf-8"))
    )
    hclx_live = (
        None
        if args.hclx_live_report is None
        else json.loads(args.hclx_live_report.read_text(encoding="utf-8"))
    )
    activation_probe = (
        None
        if args.activation_probe_report is None
        else json.loads(args.activation_probe_report.read_text(encoding="utf-8"))
    )
    summary = _summarize(
        scored_cases,
        suite_sha256=suite_sha256,
        gold_sha256=gold_sha256,
        run_id=run_id,
    )
    artifact = {
        "summary": summary,
        "measurement": {
            "started_at_utc": started_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "endpoint": args.endpoint,
            "image_reference": args.image_reference,
            "image_reference_kind": "local_docker_image_id_not_registry_digest",
            "source_commit": args.source_commit,
            "feature_flags": feature_flags,
            "adaptive_stats": adaptive_stats,
            "database_sha256_by_family": database_sha256_by_family,
            "baseline_report_sha256": baseline_report_sha256,
            "dense_calibration_report_sha256": (
                None
                if args.dense_calibration_report is None
                else _sha256(args.dense_calibration_report)
            ),
            "hclx_live_report_sha256": (
                None if args.hclx_live_report is None else _sha256(args.hclx_live_report)
            ),
            "activation_probe_report_sha256": (
                None
                if args.activation_probe_report is None
                else _sha256(args.activation_probe_report)
            ),
        },
        "cases": scored_cases,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_html.write_text(
        _render_html(
            summary,
            scored_cases,
            generated_at=completed_at.astimezone().isoformat(timespec="seconds"),
            endpoint=args.endpoint,
            image_reference=args.image_reference,
            source_commit=args.source_commit,
            db_sha256_by_family=database_sha256_by_family,
            feature_flags=feature_flags,
            adaptive_stats=adaptive_stats,
            dense_calibration=dense_calibration,
            hclx_live=hclx_live,
            activation_probe=activation_probe,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
