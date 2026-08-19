# 금융상품 Agent 주요 기능 흐름도

상태: 초안 v0.2 · P0-7 내부 경로 반영

기준일: 2026-08-20

```mermaid
flowchart TD
    Q["사용자 자연어 질문"] --> R["Intent Router"]

    R --> S["SEARCH<br/>조건 검색·상세 조회"]
    R --> C["COMPARE<br/>같은 상품군 두 상품"]
    R --> A["AGGREGATE<br/>집계·계산·순위"]
    R --> E["EXPLAIN<br/>근거 설명"]
    R --> CL["CLARIFY<br/>모호한 조건 역질문"]
    R --> U["UNSUPPORTED<br/>예측·추천·미지원 요청"]

    S --> MF{"복수 상품군?"}
    MF -->|"아니오"| P["서버 QueryPlan 검증"]
    MF -->|"예"| MP["상품군별 단일 QueryPlan<br/>공통 조건만 허용"]
    MP --> T
    C --> P
    A --> P
    E --> P

    P -.->|"관계·문서 공개 연결 대기"| KP["P0-7 내부 KnowledgeQueryPlan"]
    KP --> KR["승인 관계·문서 색인"]
    KR --> KCV["Claim Verifier<br/>개수·순서·값·evidence exact 검사"]
    KCV -->|"통과"| OK
    KCV -->|"불일치·오류"| FB

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
    EV --> FA["상품군별 evidence-only 답변<br/>또는 deterministic renderer"]
    FA --> AV["상품군별 Answer Verifier"]
    AV --> XF["서버 답변 조합·Cross-Family 검증<br/>(복수 검색 시)"]
    XF --> OK["근거·기준일 포함 답변"]
    XF --> FB["하나라도 실패<br/>전체 Deterministic Fallback"]

    CL --> CONTROL["실행 없이 제어 응답"]
    U --> CONTROL
```

## 실행 원칙

- SEARCH는 지원 조건·정렬·limit만 실행
- 복수 상품군 SEARCH는 각 상품군을 독립 검증하고 부분 결과를 보존
- 복수 상품군 답변 생성에는 해당 family의 질문·계획·evidence·manifest만 전달
- 서버가 family별 답변을 조합하고 교차 상품군 문구·비교·집계를 검증
- 상품군 간 직접 수치 비교·합산·우열 판단과 서로 다른 family 조건은 차단
- COMPARE는 같은 상품군의 정확한 두 상품과 승인 필드만 실행
- AGGREGATE는 허용 함수·그룹·통화 정책을 서버가 결정
- EXPLAIN은 검증된 정형 evidence를 사용하며 실제 문서 corpus는 승인 대기
- 관계·문서 내부 경로는 모델의 구조화 주장만 evidence와 정확히 대조하고,
  공개 Router·`GET /answer`·Agent Release 연결 전까지 사용자 경로에는 비활성
- CLARIFY·UNSUPPORTED는 Oracle과 답변 모델을 불필요하게 호출하지 않음
- 답변 검증 실패는 근거 없는 재생성이 아니라 결정론적 fallback으로 종료
