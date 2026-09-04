# 금융상품 Agent 사람 평가 rubric v1

마지막 갱신: 2026-07-30

## 1. 목적

자동 검증이 확인하기 어려운 명확성·가독성·비교 편의성을 사람이 평가한다.
사람 점수는 Oracle·Answer Verifier 정확도를 대신하지 않고 별도 축으로 보고한다.

## 2. 평가 항목

| 항목 | 가중치 | critical minimum |
| --- | ---: | ---: |
| 질문 충족도 | 20% | 3/5 |
| 사실·수치 근거성 | 25% | 4/5 |
| 안전·불확실성 처리 | 20% | 4/5 |
| 명확성과 가독성 | 15% | 3/5 |
| 비교·구조의 유용성 | 10% | 3/5 |
| 근거 추적 가능성 | 10% | 4/5 |

각 항목은 1~5점과 구체적인 근거 note를 함께 기록한다. 가중 평균이 높아도 한
critical 항목의 reviewer 평균이 minimum보다 낮으면 해당 case gate는 실패다.

## 3. 운영 절차

- case마다 서로 다른 evaluator 최소 2명
- 가능한 evaluator 조합: 금융 도메인 담당자 + Product/Frontend/Backend 담당자
- evaluator는 다른 사람의 점수를 보기 전에 독립 작성
- Agent 설명 답변과 결정론적 답변의 선호를 `agent`, `deterministic`, `tie`로 기록
- 자동 지표, latency와 사람 점수를 같은 숫자로 합치지 않음
- 실패 note를 수정 backlog와 연결하고 최초 점수와 사후 점수를 분리 보존

정본 rubric JSON은 package의 `human_answer_rubric_v1.json`이며, scorecard와
batch Pydantic validator가 criterion 순서·누락·중복 evaluator·최소 reviewer를
검사한다.

## 4. 권장 표본

external blind 최초 실행이 끝난 뒤 상품군·intent·성공·역질문·미지원·not-found가
섞이도록 최소 20~30 case를 층화 표집한다. 비교 case와 비전공자가 이해하기
어려운 용어 설명 case를 반드시 포함한다.

실제 사람 평가는 팀원이 응답을 읽고 수행해야 하므로 저장소 내부 자동화로 완료할
수 없는 외부 게이트다. 현재는 rubric·validator·집계기만 완료했다.
