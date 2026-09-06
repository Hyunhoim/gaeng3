# 폐기된 수동 배포 기록 (2026-09-04)

> 이 문서와 `src/fastapi_backend/scripts/manual-release.sh`는 과거 개발 경로를
> 다시 실행하지 않도록 남긴 폐기 표지다. 스크립트는 어떤 환경에서도 이미지·DB·
> credential·container를 조립하지 않고 종료 코드 64로 중단한다.

2026-09-04에는 NCP 서버 한 대에서 GitHub CI 없이 이미지와 데이터를 직접 조립하는
절차를 시험했다. 이 경로에는 현재 Registry 1.8 KURE Schema Dense, 독립적으로 승인된
Document Dense, keyless 서명, 정확한 OCI digest와 rollback Binding이 모두 결속되지
않는다. 따라서 개발 smoke에도 최종 Runtime과 동일한 결과를 보장하지 않으며,
`evaluation`·`production` 배포 수단으로 사용할 수 없다.

## 현재 유일한 최종 배포 경로

1. 보호된 `main`의 정확한 commit에서 `.github/workflows/immutable-ncp-release.yml`을 실행한다.
2. workflow가 Registry 1.8 Schema Dense index·calibration·KURE snapshot과 승인 DB·관계
   artifact를 SHA-256으로 검증한다.
3. Document Dense cache-first를 선택한 경우, 독립 승인된 corpus·activation·temporal
   relation과 Git LFS로 배포된 대용량 index가 모두 commit 및 보호 Environment SHA와
   일치해야 한다. 하나라도 없으면 release는 registry push 전에 종료된다.
4. workflow가 만든 정확한 `image@sha256`, AgentReleaseManifest, DeploymentBinding,
   Sigstore 검증 자료를 NCP 서버에 배치한다.
5. 저장소 루트의 `compose-release.sh`로만 기동하고 `/health`와 공식 GET `/answer`를
   확인한다.

Document Dense의 현재 개발 후보를 `/tmp`에서 복사하거나 임의 SHA로 승인해서는 안 된다.
승인 artifact가 아직 없다면 외부 검색 mode는 `verified_live_official`로 동결하고,
cache-first를 켜지 않는 것이 fail-closed 동작이다.
