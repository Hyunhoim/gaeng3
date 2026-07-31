# 금융상품 Agent 주요 기능 흐름도

상태: 초안 v0.1

기준일: 2026-07-31

```mermaid
flowchart TD
    Q["사용자 자연어 질문"] --> R["Intent Router"]

    R --> S["SEARCH<br/>조건 검색·상세 조회"]
    R --> C["COMPARE<br/>같은 상품군 두 상품"]
    R --> A["AGGREGATE<br/>집계·계산·순위"]
    R --> E["EXPLAIN<br/>근거 설명"]
    R --> CL["CLARIFY<br/>모호한 조건 역질문"]
    R --> U["UNSUPPORTED<br/>예측·추천·미지원 요청"]

    S --> P["서버 QueryPlan 검증"]
    C --> P
    A --> P
    E --> P

    P --> T["상품군별 결정론적 도구"]
    T --> B["국내채권"]
    T --> DE["국내 ETP"]
    T --> OE["해외 ETP"]
    T --> F["공모펀드"]

    B --> V["Result Verifier"]
    DE --> V
    OE --> V
    F --> V

    V --> EV["Field-level Evidence"]
    EV --> AV["Answer Verifier"]
    AV --> OK["근거·기준일 포함 답변"]
    AV --> FB["검증 실패<br/>Deterministic Fallback"]

    CL --> CONTROL["실행 없이 제어 응답"]
    U --> CONTROL
```

## 실행 원칙

- SEARCH는 지원 조건·정렬·limit만 실행
- COMPARE는 같은 상품군의 정확한 두 상품과 승인 필드만 실행
- AGGREGATE는 허용 함수·그룹·통화 정책을 서버가 결정
- EXPLAIN은 검증된 정형 evidence를 사용하며 실제 문서 corpus는 승인 대기
- CLARIFY·UNSUPPORTED는 Oracle과 답변 모델을 불필요하게 호출하지 않음
- 답변 검증 실패는 근거 없는 재생성이 아니라 결정론적 fallback으로 종료
