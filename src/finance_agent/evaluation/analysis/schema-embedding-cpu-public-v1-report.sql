-- Team-share HTML chart/table snapshot derived from the frozen public baseline.
-- The exact values are preserved in:
--   evaluation/baselines/schema-embedding-cpu-public-v1.json
-- This query only reshapes those reviewed values for portable report rendering.

SELECT
    rank,
    model,
    model_id,
    fusion,
    exact_count,
    executable_questions,
    exact_accuracy,
    baseline_exact_accuracy,
    recall5_hits,
    gold_fields,
    recall5,
    recall10_hits,
    recall10,
    p95_latency_ms,
    peak_rss_gib,
    configurations_compared,
    models_compared,
    decision
FROM (
    VALUES
        (1, 'BGE-M3', 'BAAI/bge-m3', 'lexical_first', 175, 181, 175.0 / 181, 167.0 / 181, 521, 527, 521.0 / 527, 527, 1.0, 91.101182, 2496880.0 / 1024 / 1024, 14, 7, '잠정 1순위'),
        (2, 'KURE-v1', 'nlpai-lab/KURE-v1', 'lexical_first', 175, 181, 175.0 / 181, NULL, 520, 527, 520.0 / 527, 527, 1.0, 92.125515, 2493488.0 / 1024 / 1024, 14, 7, 'blind 비교 유지'),
        (3, 'Nomic Embed v2 MoE', 'nomic-ai/nomic-embed-text-v2-moe', 'lexical_first', 174, 181, 174.0 / 181, NULL, 521, 527, 521.0 / 527, 527, 1.0, 49.740146, 4162624.0 / 1024 / 1024, 14, 7, '운영 부담 검토'),
        (4, 'Arctic Embed L v2', 'Snowflake/snowflake-arctic-embed-l-v2.0', 'lexical_first', 174, 181, 174.0 / 181, NULL, 520, 527, 520.0 / 527, 527, 1.0, 96.774167, 2363120.0 / 1024 / 1024, 14, 7, '후순위'),
        (5, 'Qwen3 Embedding 0.6B', 'Qwen/Qwen3-Embedding-0.6B', 'lexical_first', 172, 181, 172.0 / 181, NULL, 517, 527, 517.0 / 527, 524, 524.0 / 527, 82.642354, 2424592.0 / 1024 / 1024, 14, 7, '후순위'),
        (6, 'multilingual-E5-large-instruct', 'intfloat/multilingual-e5-large-instruct', 'lexical_first', 171, 181, 171.0 / 181, NULL, 520, 527, 520.0 / 527, 526, 526.0 / 527, 86.225982, 1634012.0 / 1024 / 1024, 14, 7, '후순위'),
        (7, 'KoE5', 'nlpai-lab/KoE5', 'lexical_first', 170, 181, 170.0 / 181, NULL, 520, 527, 520.0 / 527, 527, 1.0, 95.942304, 2405560.0 / 1024 / 1024, 14, 7, '채택 기준 미달'),
        (8, 'Lexical only', 'deterministic-lexical-baseline', 'lexical_only', 167, 181, 167.0 / 181, NULL, 511, 527, 511.0 / 527, NULL, NULL, NULL, NULL, 14, 7, '기존 기준선')
) AS report_rows(
    rank,
    model,
    model_id,
    fusion,
    exact_count,
    executable_questions,
    exact_accuracy,
    baseline_exact_accuracy,
    recall5_hits,
    gold_fields,
    recall5,
    recall10_hits,
    recall10,
    p95_latency_ms,
    peak_rss_gib,
    configurations_compared,
    models_compared,
    decision
)
ORDER BY exact_accuracy DESC, recall5 DESC, model ASC;
