#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access, chmod, readFile, readdir, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const agentRoot = resolve(scriptDir, "..");
const markdownPath = resolve(
  agentRoot,
  "docs/evaluation-schema-embedding-cpu.md",
);
const artifactPath = resolve(
  agentRoot,
  "docs/evaluation-schema-embedding-cpu.artifact.json",
);
const htmlPath = resolve(agentRoot, "docs/evaluation-schema-embedding-cpu.html");

const markdown = await readFile(markdownPath, "utf8");
const generatedAt = "2026-08-12T21:00:00+09:00";

function splitReport(source) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const titleLine = lines[0].trim();
  const firstSectionIndex = lines.findIndex((line) => line.startsWith("## "));

  if (!titleLine.startsWith("# ") || firstSectionIndex < 0) {
    throw new Error("보고서의 H1 제목 또는 H2 섹션을 찾지 못했습니다");
  }

  const preamble = lines.slice(1, firstSectionIndex).join("\n").trim();
  const sections = [];
  let current = [];

  for (const line of lines.slice(firstSectionIndex)) {
    if (line.startsWith("## ") && current.length > 0) {
      sections.push(current.join("\n").trim());
      current = [];
    }
    current.push(line);
  }
  if (current.length > 0) {
    sections.push(current.join("\n").trim());
  }

  if (preamble) {
    const [heading, ...body] = sections[0].split("\n");
    sections[0] = [heading, "", preamble, "", ...body].join("\n").trim();
  }

  return { titleLine, sections };
}

const { titleLine, sections } = splitReport(markdown);
const title = titleLine.replace(/^#\s+/, "");

const benchmarkSource = {
  id: "schema-embedding-public-v1",
  label:
    "Schema Dense CPU 공개 비교 baseline v1 · 실행 질문 181개 · 정답 필드 527개 · lexical_first",
  path: "finance_agent/evaluation/baselines/schema-embedding-cpu-public-v1.json",
};

const statisticsSource = {
  id: "schema-embedding-statistics-v1",
  label: "Schema Dense paired 통계 분석 v1 · 고정 seed 20260812 · bootstrap 10,000회",
  path: "finance_agent/evaluation/analysis/schema-embedding-cpu-public-v1-statistics.json",
};

const reportSource = {
  id: "schema-embedding-report-snapshot-v1",
  label:
    "Schema Dense CPU HTML 차트·표 snapshot · 동결 baseline 값의 표시용 재구성",
  path: "finance_agent/evaluation/analysis/schema-embedding-cpu-public-v1-report.sql",
};

const modelRows = [
  {
    rank: 1,
    model: "BGE-M3",
    model_id: "BAAI/bge-m3",
    fusion: "lexical_first",
    exact_count: 175,
    executable_questions: 181,
    exact_accuracy: 175 / 181,
    baseline_exact_accuracy: 167 / 181,
    recall5_hits: 521,
    gold_fields: 527,
    recall5: 521 / 527,
    recall10_hits: 527,
    recall10: 1,
    p95_latency_ms: 91.101182,
    peak_rss_gib: 2496880 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "잠정 1순위",
  },
  {
    rank: 2,
    model: "KURE-v1",
    model_id: "nlpai-lab/KURE-v1",
    fusion: "lexical_first",
    exact_count: 175,
    executable_questions: 181,
    exact_accuracy: 175 / 181,
    recall5_hits: 520,
    gold_fields: 527,
    recall5: 520 / 527,
    recall10_hits: 527,
    recall10: 1,
    p95_latency_ms: 92.125515,
    peak_rss_gib: 2493488 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "blind 비교 유지",
  },
  {
    rank: 3,
    model: "Nomic Embed v2 MoE",
    model_id: "nomic-ai/nomic-embed-text-v2-moe",
    fusion: "lexical_first",
    exact_count: 174,
    executable_questions: 181,
    exact_accuracy: 174 / 181,
    recall5_hits: 521,
    gold_fields: 527,
    recall5: 521 / 527,
    recall10_hits: 527,
    recall10: 1,
    p95_latency_ms: 49.740146,
    peak_rss_gib: 4162624 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "운영 부담 검토",
  },
  {
    rank: 4,
    model: "Arctic Embed L v2",
    model_id: "Snowflake/snowflake-arctic-embed-l-v2.0",
    fusion: "lexical_first",
    exact_count: 174,
    executable_questions: 181,
    exact_accuracy: 174 / 181,
    recall5_hits: 520,
    gold_fields: 527,
    recall5: 520 / 527,
    recall10_hits: 527,
    recall10: 1,
    p95_latency_ms: 96.774167,
    peak_rss_gib: 2363120 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "후순위",
  },
  {
    rank: 5,
    model: "Qwen3 Embedding 0.6B",
    model_id: "Qwen/Qwen3-Embedding-0.6B",
    fusion: "lexical_first",
    exact_count: 172,
    executable_questions: 181,
    exact_accuracy: 172 / 181,
    recall5_hits: 517,
    gold_fields: 527,
    recall5: 517 / 527,
    recall10_hits: 524,
    recall10: 524 / 527,
    p95_latency_ms: 82.642354,
    peak_rss_gib: 2424592 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "후순위",
  },
  {
    rank: 6,
    model: "multilingual-E5-large-instruct",
    model_id: "intfloat/multilingual-e5-large-instruct",
    fusion: "lexical_first",
    exact_count: 171,
    executable_questions: 181,
    exact_accuracy: 171 / 181,
    recall5_hits: 520,
    gold_fields: 527,
    recall5: 520 / 527,
    recall10_hits: 526,
    recall10: 526 / 527,
    p95_latency_ms: 86.225982,
    peak_rss_gib: 1634012 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "후순위",
  },
  {
    rank: 7,
    model: "KoE5",
    model_id: "nlpai-lab/KoE5",
    fusion: "lexical_first",
    exact_count: 170,
    executable_questions: 181,
    exact_accuracy: 170 / 181,
    recall5_hits: 520,
    gold_fields: 527,
    recall5: 520 / 527,
    recall10_hits: 527,
    recall10: 1,
    p95_latency_ms: 95.942304,
    peak_rss_gib: 2405560 / 1024 / 1024,
    configurations_compared: 14,
    models_compared: 7,
    decision: "채택 기준 미달",
  },
  {
    rank: 8,
    model: "Lexical only",
    model_id: "deterministic-lexical-baseline",
    fusion: "lexical_only",
    exact_count: 167,
    executable_questions: 181,
    exact_accuracy: 167 / 181,
    recall5_hits: 511,
    gold_fields: 527,
    recall5: 511 / 527,
    recall10_hits: null,
    recall10: null,
    p95_latency_ms: null,
    peak_rss_gib: null,
    configurations_compared: 14,
    models_compared: 7,
    decision: "기존 기준선",
  },
];

const titleBlock = {
  id: "report-title",
  type: "markdown",
  body: titleLine,
  layout: "full",
};

const sectionBlocks = sections.map((body, index) => ({
  id: `section-${index}`,
  type: "markdown",
  body,
  layout: "full",
}));

const blocks = [
  titleBlock,
  sectionBlocks[0],
  {
    id: "headline-metrics",
    type: "metric-strip",
    cardIds: [
      "exact-accuracy-card",
      "recall-card",
      "latency-card",
      "comparison-card",
    ],
    layout: "full",
  },
  {
    id: "model-comparison-chart",
    type: "chart",
    chartId: "lexical-first-exact-chart",
    layout: "full",
  },
  {
    id: "model-comparison-table",
    type: "table",
    tableId: "lexical-first-model-table",
    layout: "full",
  },
  ...sectionBlocks.slice(1),
];

const artifact = {
  surface: "report",
  manifest: {
    version: 1,
    surface: "report",
    title,
    description:
      "Schema Dense에 사용할 CPU 임베딩 모델 7개를 두 결합 방식으로 비교한 팀 공유용 상세 보고서",
    generatedAt,
    blocks,
    cards: [
      {
        id: "exact-accuracy-card",
        description: "실행 가능 질문 181개에서 정답 필드 묶음을 정확히 찾은 비율",
        dataset: "model_results",
        filter: { model_id: "BAAI/bge-m3" },
        sourceId: reportSource.id,
        metrics: [
          {
            label: "BGE-M3 Exact",
            field: "exact_accuracy",
            format: "percent",
          },
          {
            label: "Lexical only",
            field: "baseline_exact_accuracy",
            format: "percent",
          },
        ],
      },
      {
        id: "recall-card",
        description: "상위 10개 후보에 포함된 정답 필드 527개의 비율",
        dataset: "model_results",
        filter: { model_id: "BAAI/bge-m3" },
        sourceId: reportSource.id,
        metrics: [
          { label: "Recall@10", field: "recall10", format: "percent" },
          { label: "Recall@5", field: "recall5", format: "percent" },
        ],
      },
      {
        id: "latency-card",
        description: "CPU 전용 환경에서 측정한 질문별 임베딩 처리시간",
        dataset: "model_results",
        filter: { model_id: "BAAI/bge-m3" },
        sourceId: reportSource.id,
        metrics: [
          {
            label: "CPU p95 (ms)",
            field: "p95_latency_ms",
            format: "number",
          },
        ],
      },
      {
        id: "comparison-card",
        description: "7개 모델을 RRF와 lexical_first 두 방식으로 비교",
        dataset: "model_results",
        filter: { model_id: "BAAI/bge-m3" },
        sourceId: reportSource.id,
        metrics: [
          {
            label: "비교 설정 수",
            field: "configurations_compared",
            format: "number",
          },
          { label: "모델 수", field: "models_compared", format: "number" },
        ],
      },
    ],
    charts: [
      {
        id: "lexical-first-exact-chart",
        title: "Lexical 우선 결합의 모델별 Exact field-set accuracy",
        subtitle:
          "실행 가능 질문 181개 기준이며, BGE-M3와 KURE-v1은 175/181로 공동 최고",
        showDescription: true,
        question: "어떤 임베딩 모델이 정답 필드 묶음을 가장 정확하게 찾았는가",
        rationale:
          "모델 7개와 기존 Lexical 기준선의 단일 비율을 비교하므로 가로 막대가 가장 직접적임",
        intent: "comparison",
        comparisonContext: {
          baseline: "Lexical only 167/181",
          denominator: "실행 가능 질문 181개",
          grain: "모델·결합 방식",
          unit: "비율",
        },
        type: "horizontalBar",
        dataset: "model_results",
        sourceId: reportSource.id,
        encodings: {
          x: {
            field: "model",
            type: "nominal",
            label: "모델",
          },
          y: {
            field: "exact_accuracy",
            type: "quantitative",
            format: "percent",
            label: "Exact field-set accuracy",
          },
          tooltip: [
            { field: "exact_count", type: "quantitative", label: "정답 질문" },
            {
              field: "executable_questions",
              type: "quantitative",
              label: "실행 질문",
            },
            { field: "recall5", type: "quantitative", format: "percent", label: "Recall@5" },
            { field: "p95_latency_ms", type: "quantitative", label: "CPU p95 (ms)" },
            { field: "decision", type: "text", label: "판정" },
          ],
        },
        valueFormat: "percent",
        layout: "full",
        maxRows: 8,
        referenceLines: [
          {
            axis: "y",
            value: 167 / 181,
            label: "Lexical 기준선",
            color: "neutral",
            lineStyle: "dashed",
          },
        ],
        emptyState: "표시할 실험 결과가 없습니다",
        compatibleTypes: ["horizontalBar", "bar", "leaderboard"],
        surface: { viewMode: "visualization", showControls: false },
      },
    ],
    tables: [
      {
        id: "lexical-first-model-table",
        title: "Lexical 우선 결합 모델별 상세 결과",
        subtitle: "실행 질문 181개·정답 필드 527개 기준, CPU 전용 재실험",
        showDescription: true,
        dataset: "model_results",
        defaultSort: { field: "exact_accuracy", direction: "desc" },
        density: "dense",
        sourceId: reportSource.id,
        layout: "full",
        columns: [
          { field: "model", label: "모델", type: "text" },
          { field: "exact_count", label: "Exact 문항", format: "number" },
          { field: "exact_accuracy", label: "Exact 비율", format: "percent" },
          { field: "recall5_hits", label: "Recall@5 필드", format: "number" },
          { field: "recall5", label: "Recall@5", format: "percent" },
          { field: "recall10_hits", label: "Recall@10 필드", format: "number" },
          { field: "recall10", label: "Recall@10", format: "percent" },
          { field: "p95_latency_ms", label: "CPU p95 (ms)", format: "number" },
          { field: "peak_rss_gib", label: "Peak RSS (GiB)", format: "number" },
          { field: "decision", label: "현재 판정", type: "text" },
        ],
      },
    ],
    sources: [reportSource, benchmarkSource, statisticsSource],
  },
  snapshot: {
    version: 1,
    generatedAt,
    status: "ready",
    datasets: { model_results: modelRows },
  },
  sources: [reportSource, benchmarkSource, statisticsSource],
};

await writeFile(artifactPath, `${JSON.stringify(artifact, null, 2)}\n`, "utf8");

async function findPortableBuilder() {
  const explicitRoot = process.env.DATA_ANALYTICS_PLUGIN_ROOT;
  const pluginCacheRoot = explicitRoot
    ? resolve(explicitRoot)
    : resolve(
        homedir(),
        ".codex/plugins/cache/openai-curated-remote/data-analytics",
      );
  const directCandidate = resolve(
    pluginCacheRoot,
    "skills/build-report/scripts/deliver_portable_artifact.mjs",
  );

  try {
    await access(directCandidate);
    return directCandidate;
  } catch {
    // The cache root normally contains a versioned directory.
  }

  const entries = await readdir(pluginCacheRoot, { withFileTypes: true });
  for (const entry of entries
    .filter((item) => item.isDirectory())
    .sort((left, right) => right.name.localeCompare(left.name))) {
    const candidate = resolve(
      pluginCacheRoot,
      entry.name,
      "skills/build-report/scripts/deliver_portable_artifact.mjs",
    );
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Continue until an installed Data Analytics plugin is found.
    }
  }
  throw new Error(
    "Data Analytics portable HTML builder를 찾지 못했습니다. " +
      "DATA_ANALYTICS_PLUGIN_ROOT를 설치된 plugin root로 지정하세요",
  );
}

const builderPath = await findPortableBuilder();
const exitCode = await new Promise((resolveExit, reject) => {
  const child = spawn(
    process.execPath,
    [builderPath, "--input", artifactPath, "--output", htmlPath],
    { stdio: "inherit" },
  );
  child.once("error", reject);
  child.once("exit", (code) => resolveExit(code ?? 1));
});

if (exitCode !== 0) {
  throw new Error(`portable HTML builder가 종료 코드 ${exitCode}로 실패했습니다`);
}

await chmod(artifactPath, 0o644);
await chmod(htmlPath, 0o644);
console.log(htmlPath);
