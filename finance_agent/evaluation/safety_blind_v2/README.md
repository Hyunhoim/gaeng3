# Finance Agent safety blind v2

This is a newly authored 192-case safety evaluation for the documented
`default_locked` deployment profile. It is independent and blind only with respect to
the target's runtime behavior: OpenAI Codex authored it, no finance-domain human
authored or independently held the answers, and it is neither human-domain blind nor
double-blind. No Agent response or target execution was observed before sealing.

The public questions are in `questions.jsonl`. Exact expectations exist only in the
AES-256-GCM envelope `expectations.aesgcm.json`; the 32-byte key and the hash-chained
chronology receipt are in the ignored `private/` directory. The directory is mode
`0700`, and both key and receipt are mode `0600`. Receipt writes use `O_APPEND`, an
exclusive file lock, hash chaining, and `fsync`.

`seal_manifest.json` pins the public questions, ciphertext, plaintext commitment,
key fingerprint, actual seal time, authorship declaration, deployment profile, and
every permitted contract consulted. No product row was inspected and no exact positive
product ID was selected before the seal. `pre_run_manifest.json` pins the evaluator,
opaque runtime-code measurement, Git/dirty-tree state, sealed bundle, receipt head, and
passing evaluator-only test attestation. Opaque runtime measurement happened only after
the corpus was sealed and did not inspect or execute the target implementation.

The single-use runner performs all preflight work before consuming the run receipt. It
requires all four normalized databases to match the approved manifest hashes, builds
read-only product-ID universes from the documented normalized product tables, and
checks every returned ID and evidence source. Every case is a new child process with a
hard wall timeout, CPU/file-descriptor/output limits, a minimal environment, no shell,
and process-group termination. Control cases require a null plan/count/manifest and
empty products, comparisons, aggregates, documents, citations, dates, and multi-family
execution state. Reports contain hashes and failure codes, never raw answers or sealed
expectations. A started receipt is never reusable, including after interruption.

Run evaluator-only verification (this does not import or execute the Agent):

```bash
python3 -m unittest discover \
  -s /home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2/tests \
  -p 'test_*.py' -v
```

The first runtime run must use the default-locked Backend already listening on
`127.0.0.1:18001`. From any directory, the exact single-use command is:

```bash
python3 /home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2/runner.py run \
  --repo-root /home/hyunholim/projects/finance-agent \
  --suite-dir /home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2 \
  --pre-run-manifest /home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2/pre_run_manifest.json \
  --key /home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2/private/seal.key \
  --approved-manifest /home/hyunholim/projects/finance-agent/finance_agent/packages/finance_agent_core/src/finance_agent_core/config/approved_dataset_manifest.json \
  --database overseas_etp=/home/hyunholim/projects/finance-agent/finance_agent/artifacts/normalized/overseas_etp.sqlite3 \
  --database domestic_etp=/home/hyunholim/projects/finance-agent/finance_agent/artifacts/normalized/domestic_etp.sqlite3 \
  --database bond=/home/hyunholim/projects/finance-agent/finance_agent/artifacts/normalized/bond.sqlite3 \
  --database fund=/home/hyunholim/projects/finance-agent/finance_agent/artifacts/normalized/fund.sqlite3 \
  --target-command-json '["python3","/home/hyunholim/projects/finance-agent/finance_agent/evaluation/safety_blind_v2/http_adapter.py","--url","http://127.0.0.1:18001/answer","--request-json","{request_json}","--timeout-seconds","10"]' \
  --target-cwd /home/hyunholim/projects/finance-agent \
  --per-case-timeout-seconds 12 \
  --report /home/hyunholim/projects/finance-agent/finance_agent/artifacts/evaluation/safety-blind-v2-first-run.json
```

If any database, source, evaluator, runtime-code, Git, or dirty-tree hash differs, the
runner exits during preflight without appending `run_started`. Once `run_started` is
appended, the first-run slot is consumed permanently.
