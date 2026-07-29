# 공모펀드 데이터 계약 보고서 전달 기록

보고서 정본은 [공모펀드 원천 데이터 계약](../../docs/public-fund-contract.md)이다.

2026-07-29 portable HTML packaging을 시도했으나, 사용 가능한 report validator가
모든 chart source에 실제 SQL을 요구해 생성하지 않았다. 이 분석의 실제 근거는
SQL이 아니라 다음 두 자료다.

- 공식 `PRFD01N001` XLSX를 `finance_agent_core.audit`로 전수 검사한 JSON
- `field_registry.yaml` 1.3의 공모펀드 capability 계약

존재하지 않는 SQL을 출처로 기록하면 provenance가 거짓이 되므로 HTML artifact를
커밋하지 않는다. 다음 중 하나가 충족되면 다시 생성할 수 있다.

1. report validator가 local file + Python transformation provenance를 지원
2. 감사 JSON을 정식 분석 테이블에 적재하고 동일 수치를 SQL로 재검증

현재 재현 경로는 계약 문서의 `finance-data-audit --dataset fund` 명령과
[공모펀드 계약 감사 노트북](../../notebooks/public-fund-contract-audit.ipynb)이다.
