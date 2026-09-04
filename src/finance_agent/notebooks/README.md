# Agent 분석 노트북

`notebooks/`는 원천 데이터 구조와 품질 규칙을 사람이 다시 확인할 수 있는 재현
가능한 탐색 작업을 보관하는 곳

| 노트북 | 역할 |
| --- | --- |
| `public-fund-contract-audit.ipynb` | 공모펀드 product grain과 필드 품질 감사 재현 |

## 사용 원칙

- 공식 원천 XLSX는 읽기 전용으로 사용
- 실행 결과와 중간 데이터는 `artifacts/` 아래에 저장하고 Git에는 포함하지 않음
- 노트북에서 발견한 규칙은 코드 테스트와 정본 문서에 다시 반영한 뒤 사용
- 노트북 출력만으로 구현 완료나 평가 성능을 주장하지 않음

정본은 [공모펀드 원천 데이터 계약](../docs/public-fund-contract.md), 전달용 기록은
[공모펀드 보고서](../reports/public-fund-contract/README.md)에서 확인
