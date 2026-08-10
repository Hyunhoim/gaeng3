# Safety blind v2 remediation runner

This directory runs the already-consumed `safety_blind_v2` corpus only as a
`non_blind_remediation`. It never creates a second blind baseline. The canonical
source hashes, completed four-event receipt, and original first-report hash are
verified before preparation, immediately before execution, after execution, and
again during verification.

The source suite, private key/receipt, pre-run manifest, and first report are
read-only inputs. Every remediation uses a new mode-`0700` run directory created
with exclusive `mkdir`; `source-anchor.json`, `remediation-manifest.json`,
`state.jsonl`, `report.json`, and `verification.json` are mode `0600` and created
with `O_EXCL`. State transitions are file-locked, hash-chained, single-use, and
fsync'd.

The coordinator decrypts expectations only in process memory. Target children
receive only public request JSON. Scoring, response parsing, approved-universe
checks, minimal child environment, resource limits, process-group kill, and hard
wall timeout are reused directly from the frozen v2 evaluator/runner. Reports
contain case IDs, failure codes, timings, status, and hashes—not prompts, target
answers, expectations, canaries, or key material.

The CLI has three fail-closed steps:

```bash
python3 finance_agent/evaluation/safety_blind_v2_remediation/remediation.py prepare \
  --run-dir finance_agent/artifacts/evaluation/safety-blind-v2-remediation/UTC-RUN-ID \
  --repo-root . \
  --suite-dir finance_agent/evaluation/safety_blind_v2 \
  --key finance_agent/evaluation/safety_blind_v2/private/seal.key \
  --pre-run-manifest finance_agent/evaluation/safety_blind_v2/pre_run_manifest.json \
  --first-report finance_agent/artifacts/evaluation/safety-blind-v2-first-run.json \
  --approved-manifest finance_agent/packages/finance_agent_core/src/finance_agent_core/config/approved_dataset_manifest.json \
  --database overseas_etp=/path/to/overseas_etp.sqlite3 \
  --database domestic_etp=/path/to/domestic_etp.sqlite3 \
  --database bond=/path/to/bond.sqlite3 \
  --database fund=/path/to/fund.sqlite3 \
  --target-command-json '["python3","-I","/absolute/path/to/safety_blind_v2/http_adapter.py","--url","http://127.0.0.1:18004/answer","--request-json","{request_json}","--timeout-seconds","10"]' \
  --target-cwd . --per-case-timeout-seconds 12 \
  --ack-non-blind-remediation
```

Repeat the same bound source/execution arguments with `run`, then use `verify`
with `--run-dir` and the four source arguments. A completed evaluation with any
failed case returns exit status `1`; integrity/preflight failure returns `2`.
Canonical runs reject arbitrary local targets: they require the original v2 HTTP
adapter, its original pre-run evaluator-tree hash, Python isolated mode (`-I`),
and a loopback URL. The synthetic test API is explicitly marked non-production.

Run the isolated synthetic verification suite with:

```bash
python3 -m unittest discover \
  -s finance_agent/evaluation/safety_blind_v2_remediation/tests \
  -p 'test_*.py' -v
```

The inherited v2 process runner does not provide a filesystem namespace. It does
not pass sealed paths or values through argv/environment, but a deliberately
malicious same-user target could search readable host files. Strong adversarial
target isolation still requires a separate UID/container or read-restricted mount.
Python also cannot guarantee secure erasure of immutable decrypted objects; the
guarantee here is that plaintext is never serialized into remediation artifacts.
