<div align="center">

# 금융상품 AI Agent

**자연어 기반 금융상품 검색·비교·계산 및 근거 검증**

![Python](https://img.shields.io/badge/Python-315B89?style=flat-square) ![FastAPI](https://img.shields.io/badge/FastAPI-315B89?style=flat-square) ![SQLite](https://img.shields.io/badge/SQLite-315B89?style=flat-square) ![HyperCLOVA X](https://img.shields.io/badge/HyperCLOVA%20X-315B89?style=flat-square) ![Docker](https://img.shields.io/badge/Docker-315B89?style=flat-square) ![Ontology](https://img.shields.io/badge/Ontology-315B89?style=flat-square) ![Pytest](https://img.shields.io/badge/Pytest-315B89?style=flat-square)

[Overview](#overview) · [Features](#features) · [Architecture](#architecture) · [Tech Stack](#tech-stack) · [How to Run](#how-to-run)

</div>

---

## Overview

금융상품 Product Finder는 자연어로 입력된 금융 조건을 검증 가능한 검색 계획으로 변환하고, 공식 제공 데이터에 대해 검색·비교·집계한 결과를 근거 및 기준일과 함께 제공하는 AI Agent입니다. 미래에셋증권 AI Festival의 금융상품 Agent 과제를 기반으로 설계되었습니다.

LLM은 질문 의도 파악과 검증된 결과의 자연어 설명을 담당하며, 실제 필터링·순위·수치 계산은 Python과 SQLite가 수행합니다. 독립 검증기가 결과와 최종 문장의 사실성을 검사하여 근거 없는 답변을 제한합니다.

## Features

| 기능 | 설명 |
| --- | --- |
| SEARCH / DETAIL | 상품군별 조건 검색 및 정확한 상품 식별자 조회 |
| COMPARE | 동일 상품군 두 상품의 허용 필드 비교, 통화·기준일·결측 확인 |
| AGGREGATE | 개수·평균·최솟값·최댓값 및 허용 집계를 Decimal 기반으로 계산 |
| EXPLAIN | 필드 단위 evidence 및 기준일을 바탕으로 설명 |
| Relation Retrieval | 발행사·운용사·기초지수 등 관계 색인과 전체 엔터티 정확 일치 |
| Safety | 불명확한 조건에는 역질문, 미지원 요청에는 안전 응답 |
| Verification | Result Verifier, Answer Verifier 및 결정론적 fallback |
| Release | 코드·모델·프롬프트·데이터·관계 artifact 및 이미지 digest 검증 |

국내채권, 국내 ETF·ETN, 해외 ETF·ETN을 지원합니다. 공모펀드 검색·비교·집계 엔진은 구현되어 있으나 공식 실행은 기본 잠금 정책을 따릅니다. 상품군 간 직접 수치 비교, 근거 없는 수익률 예측·보장 및 단정적인 투자 추천은 지원 범위가 아닙니다.

## Architecture

~~~mermaid
flowchart TD
  X["공식 XLSX"] --> I["data-init / 정규화"]
  I --> D["SQLite / 관계 색인"]
  Q["자연어 질문"] --> B["FastAPI"]
  B --> R["안전 검사 / HCX Intent / Router"]
  R --> P["서버 QueryPlan"]
  P --> O["SQLite Oracle"]
  D --> O
  O --> V["독립 Result Verifier"]
  V --> E["Field-level Evidence"]
  E --> A["HyperCLOVA X 답변 생성"]
  A --> AV["Answer Verifier"]
  AV -->|검증 성공| OUT["근거 포함 응답"]
  AV -->|검증 실패| F["결정론적 Fallback"]
~~~

Agent Core는 Backend 이미지에 설치되어 동일 프로세스에서 실행됩니다. 데이터 초기화 서비스는 공식 파일을 읽기 전용으로 사용하고 정규화된 DB를 별도 volume으로 제공합니다. API와 관계 검색은 승인된 데이터·릴리스의 무결성을 확인합니다.

| Path | Responsibility |
| --- | --- |
| `src/finance_agent/` | Agent Core, 데이터 정규화, 검색·검증·평가 |
| `src/fastapi_backend/` | HTTP API, DTO, Audit, 릴리스 가드 |
| `ontology/` | 상품군 및 공통 Turtle 정의 |
| `compose.sh` | 전체 시스템 실행 |
| `docker-compose.yml` | 데이터 초기화 및 Backend 서비스 |

## Tech Stack

| Layer | Technology |
| --- | --- |
| Agent | Python, typed QueryPlan, HyperCLOVA X HCX-007 |
| Data | SQLite, FTS, Decimal, XLSX normalization |
| Knowledge | Ontology, RDF/Turtle, relation artifacts |
| API | FastAPI, Pydantic, JSON Schema |
| Infrastructure | Docker, Docker Compose, release manifest, SHA-256 |
| Verification | pytest, Oracle, independent verifiers |

## How to Run

### 1. Prerequisites

Git, Docker 및 Docker Compose, Bash 실행 환경, 공식 원천 XLSX 데이터와 필요한 HyperCLOVA X 인증 정보를 준비합니다. 원천 데이터와 생성 DB는 Git에 포함되지 않습니다.

~~~bash
git clone https://github.com/Hyunhoim/gaeng3.git
cd gaeng3
docker info
test -f src/fastapi_backend/.env || cp src/fastapi_backend/.env.example src/fastapi_backend/.env
~~~

환경 파일의 `FINANCE_RAW_DATA_DIR`를 실제 원천 데이터 경로로 설정하고 모델 인증 및 실행 정책은 예제 설정과 Backend 문서에 맞춥니다.

### 2. Start

~~~bash
./compose.sh up --detach --wait
./compose.sh ps --all
curl --fail http://127.0.0.1:18001/health
~~~

API 문서: http://127.0.0.1:18001/docs

### 3. API Example

~~~bash
curl --get --fail-with-body \
  --data-urlencode 'question_id=Q-001' \
  --data-urlencode 'question=현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘' \
  http://127.0.0.1:18001/answer
~~~

내부 통합 API는 `POST /answer`, 평가용 API는 `GET /answer`를 사용합니다.

### 4. Logs & Shutdown

~~~bash
./compose.sh logs data-init
./compose.sh logs --follow backend
./compose.sh down
~~~

[Agent 기술 문서](src/finance_agent/README.md) · [Backend 실행 가이드](src/fastapi_backend/README.md) · [API 명세](docs/submission/api-specification.md) · [상세 실행 안내](README_DEVELOPMENT.md)
