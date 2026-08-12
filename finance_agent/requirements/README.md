# Agent Python 의존성

Conda는 Python 실행 환경을 관리하고, 이 디렉터리의 파일은 pip 패키지 버전을 관리

| 파일 | 사용 범위 |
| --- | --- |
| `base.txt` | Agent Core 실행에 필요한 최소 의존성 |
| `constraints.txt` | 공통 버전 상한·하한과 재현 가능한 설치 기준 |
| `dev.txt` | 테스트, lint, build, Turtle 문법 검사와 Safety Blind 봉인 검증을 포함한 기본 개발 환경 |
| `local-llm.txt` | 개발 전용 로컬 Qwen·vLLM 환경 |
| `embedding-eval.txt` | CPU Schema Dense 모델 비교 전용 환경 |

일반 개발은 `environment.yml`과 `dev.txt`, 로컬 모델 실험은
`environment.local-llm.yml`과 `local-llm.txt`를 함께 사용

Schema Dense 모델 비교는 운영·일반 개발 환경에 PyTorch를 추가하지 않도록
`environment.embedding-eval.yml`과 `embedding-eval.txt`를 별도로 사용

정확한 설치 명령은 [finance_agent README](../README.md#5-개발-환경), 로컬 모델의
제출 경계는 [로컬 LLM 문서](../docs/local-llm.md)에서 확인

`rdflib`는 제출 필수 Ontology의 Turtle 문법을 실제 parser로 검사하는 개발
의존성이며 생성형 모델이나 LLM이 아님

`cryptography`는 Safety Blind 평가의 정답을 평문으로 Git에 남기지 않고
AES-GCM으로 봉인하고 검증하는 개발·평가 의존성이다. Agent 운영 요청 경로에서는
사용하지 않는다.
