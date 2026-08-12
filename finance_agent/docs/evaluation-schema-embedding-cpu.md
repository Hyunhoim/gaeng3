# Schema Dense CPU 임베딩 모델 비교

측정일: 2026-08-12

상태: 공개 개발 평가 완료·BGE-M3 blind 후보·production OFF

## 1. 결론

- CPU에서 7개 오픈소스 임베딩 모델을 같은 질문과 같은 100개 schema field로 비교
- 최종 1순위는 `BAAI/bge-m3`와 `lexical_first` 결합 방식
- 기존 Lexical만 사용했을 때보다 정확한 field 묶음은 **92.27% → 96.69%**로
  `+4.42%p`, Recall@5는 **96.96% → 98.86%**로 `+1.90%p` 개선
- Lexical이 top-5에서 놓친 field 16개 중 BGE-M3가 15개를 후보 안에서 발견
- 실제 CPU query p95는 약 **91.49ms**, 19개 차단 질문의 embedding 호출은 **0회**
- 공개 질문을 보며 모델과 결합 방식을 고른 결과이므로 독립 blind 성능이나 공모전
  예상 점수로 주장하지 않음
- production 연결은 독립 blind와 OOD 기권 임계값 검증 전까지 계속 차단

## 2. 무엇을 비교했는가

Schema Dense는 상품 자체를 벡터로 찾는 기능이 아니다. 사용자 표현을 DB의 어떤
field로 해석할지 돕는 보조 검색이다. 예를 들어 “운용 비용”을 `total_expense_ratio`,
“설정된 지 얼마나 됐는지”를 `inception_date` 후보와 연결하는 역할이다.

- 대상: 네 상품군의 canonical schema field 문서 100개
- 질문: 공개 core suite 200개
- 현재 정책상 실행 질문: 181개, gold field 527개
- 현재 정책상 차단 질문: 19개
- 평가 범위: 정답 상품군을 미리 고정한 field-linking 컴포넌트
- 포함하지 않은 것: Router 상품군 판단, 실제 상품 SQL 검색, HCLX 답변 품질,
  독립 blind, OOD abstention

## 3. 모델 고정 기준

| 후보 | 고정 revision | 라이선스 | 입력·pooling |
| --- | --- | --- | --- |
| BGE-M3 | `5617a9f…` | MIT | 일반 입력·CLS |
| KURE-v1 | `d14c8a9…` | MIT | 일반 입력·CLS |
| Qwen3-Embedding-0.6B | `97b0c61…` | Apache-2.0 | schema 지시문·last token |
| KoE5 | `bc6d284…` | MIT | `query:`/`passage:`·mean |
| multilingual-E5-large-instruct | `274baa4…` | MIT | schema 지시문·mean |
| Snowflake Arctic L v2 | `ac6544c…` | Apache-2.0 | `query:`·CLS |
| Nomic v2 MoE | `1066b65…` | Apache-2.0 | `search_query:`/`search_document:`·mean |

Nomic은 별도 원격 Python 코드를 요구한다. 참조 코드 저장소
`nomic-ai/nomic-bert-2048`도 Apache-2.0과 revision `7710840…`로 별도 고정하고,
파일·네트워크·프로세스 실행 여부를 검사했다. 고정 snapshot 로컬 경로만 모델에
전달해 원격 코드가 가중치 `main`을 다시 조회하지 못하게 했다.

## 4. 1차 RRF 비교

RRF는 Lexical과 Dense 순위에 같은 가중치를 주어 합치는 일반적인 결합 방식이다.

| 모델 | Dense Recall@5 | 결합 exact | 결합 Recall@5 | 놓친 field 회수 | CPU p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BGE-M3 | 75.33% | 93.92% | 97.91% | 93.75% | 93.75ms |
| KURE-v1 | 72.87% | 92.27% | 97.91% | 93.75% | 89.89ms |
| Nomic v2 MoE | 69.83% | 91.71% | 98.29% | 93.75% | 51.11ms |
| KoE5 | 70.97% | 84.53% | 97.72% | 75.00% | 95.22ms |
| multilingual-E5 instruct | 63.38% | 86.74% | 97.72% | 75.00% | 88.49ms |
| Arctic L v2 | 66.60% | 87.29% | 97.72% | 93.75% | 93.57ms |
| Qwen3 Embedding | 57.31% | 67.96% | 96.02% | 62.50% | 80.61ms |

Qwen3 Embedding은 이 과제와 입력 구성에서 기존 Lexical Recall까지 낮췄으므로 제외한다.
한국어 특화라는 설명만으로 KURE·KoE5가 자동 우승하지도 않았다. 공개 벤치마크보다
우리 field 문서와 질문으로 직접 비교해야 한다는 근거다.

## 5. Lexical 우선 결합

팀 로드맵은 Lexical을 기본값으로 두고 Dense를 의미 보조로 사용한다. 따라서 기존
Lexical 후보의 순서를 바꾸지 않고, Lexical이 찾지 못한 Dense 후보만 뒤에 추가하는
`lexical_first`를 상위 4개에 적용했다.

| 순위 | 모델 | exact | exact 개선 | Recall@5 | Recall 개선 | CPU p95 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | BGE-M3 | **96.69%** | **+4.42%p** | **98.86%** | **+1.90%p** | 91.49ms |
| 2 | KURE-v1 | **96.69%** | **+4.42%p** | 98.67% | +1.71%p | 95.05ms |
| 3 | Nomic v2 MoE | 96.13% | +3.87%p | **98.86%** | **+1.90%p** | **51.20ms** |
| 4 | Arctic L v2 | 96.13% | +3.87%p | 98.67% | +1.71%p | 91.00ms |

BGE-M3는 exact가 공동 최고이고 Recall@5도 공동 최고다. Nomic보다 느리지만 사전
문턱 250ms 안이며 원격 코드와 두 번째 revision을 관리할 필요가 없어 최종 1순위다.

## 6. 안전과 해석 한계

- 현재 정책상 차단 질문 embedding 무호출: 19/19
- field registry 밖 후보: 0건
- 질문의 상품군 밖 후보: 0건
- production feature: OFF
- hard filter·정렬·수치 계산·상품 선택 권한: 계속 QueryPlan·SQL·서버 규칙이 담당
- 모델 점수만으로 실행하거나 답을 만들 권한: 없음
- 실험 모델과 revision을 고르는 데 공개 질문을 사용했으므로 다음 평가는 반드시
  겹치지 않는 독립 질문으로 최초 1회 수행
- 현재 top-1 score와 top-1/top-2 margin의 OOD 기권 기준은 없음
- p95는 Xeon Silver 4510, 12 thread, 단일 요청의 warm component 측정이며 Backend
  HTTP·동시 요청·Docker 시작 시간을 포함하지 않음

## 7. 재현 방법

`finance_agent/`에서 별도 Conda + pip 환경을 사용한다.

```bash
conda env create -f environment.embedding-eval.yml
conda run -n gaeng3-embedding-eval \
  python -m pip install --no-build-isolation -r requirements/embedding-eval.txt

conda run -n gaeng3-embedding-eval \
  finance-benchmark-schema-embeddings \
  --model bge-m3 \
  --fusion-strategy lexical_first \
  --cpu-threads 12 \
  --batch-size 16 \
  --require-contract
```

가중치는 Hugging Face cache에만 두고 Git에 저장하지 않는다. 전체 결과 JSON은
`artifacts/evaluation/schema-embedding/`에 생성되며 Git에서 제외된다. Git에는
[집계 baseline](../evaluation/baselines/schema-embedding-cpu-public-v1.json)만 보존한다.

## 8. 다음 단계

1. 금융 도메인 담당자가 공개 200문항과 표현이 겹치지 않는 Schema Dense blind를 작성
2. 질문과 정답 field ID를 hash 봉인하고 BGE-M3 + `lexical_first`를 최초 1회 실행
3. Lexical 대비 exact 또는 Recall@5 `+2%p`, 안전 무호출 100%, p95 250ms 이하 확인
4. 별도 OOD·모호 질문으로 score·margin 기권 임계값을 정한 뒤 잠금
5. 위 조건을 모두 통과하면 Backend 규칙 뒤의 shadow 후보로만 연결
6. shadow에서도 SQL hard filter와 서버의 최종 QueryPlan 승인 권한은 변경하지 않음
