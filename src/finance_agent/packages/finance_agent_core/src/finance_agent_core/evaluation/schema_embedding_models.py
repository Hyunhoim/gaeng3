from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from importlib import metadata as importlib_metadata
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata


class SchemaEmbeddingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaEmbeddingModelSpec(SchemaEmbeddingModel):
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{2,63}$")
    phase: Literal["core", "extended", "extended_remote_code"]
    model_id: str = Field(min_length=3, max_length=256)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    license_id: Literal["mit", "apache-2.0"]
    dimension: int = Field(ge=8, le=65536)
    pooling: Literal["cls", "mean", "last-token"]
    query_template: str = Field(min_length=6, max_length=1000)
    document_template: str = Field(min_length=6, max_length=1000)
    trust_remote_code: bool = False
    remote_code_model_id: str | None = Field(default=None, min_length=3, max_length=256)
    remote_code_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    max_sequence_length: int = Field(ge=32, le=8192)
    rationale: str = Field(min_length=3, max_length=300)

    @field_validator("query_template", "document_template")
    @classmethod
    def require_single_text_slot(cls, value: str) -> str:
        if value.count("{text}") != 1:
            raise ValueError("embedding template must contain exactly one {text} slot")
        return value

    @model_validator(mode="after")
    def remote_code_phase_must_be_explicit(self) -> SchemaEmbeddingModelSpec:
        if self.trust_remote_code != (self.phase == "extended_remote_code"):
            raise ValueError("trust_remote_code candidates require the dedicated review phase")
        has_remote_pin = bool(self.remote_code_model_id and self.remote_code_revision)
        if self.trust_remote_code != has_remote_pin:
            raise ValueError("remote model code requires a separately pinned code repository")
        return self

    def prepare_query(self, text: str) -> str:
        return self.query_template.format(text=text)

    def prepare_document(self, text: str) -> str:
        return self.document_template.format(text=text)


class SchemaEmbeddingModelRegistry(SchemaEmbeddingModel):
    schema_version: Literal["1.0"] = "1.0"
    registry_id: Literal["schema-embedding-models-v1"] = "schema-embedding-models-v1"
    candidates: tuple[SchemaEmbeddingModelSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_aliases_and_models(self) -> SchemaEmbeddingModelRegistry:
        aliases = [item.alias for item in self.candidates]
        model_ids = [item.model_id for item in self.candidates]
        if len(aliases) != len(set(aliases)):
            raise ValueError("embedding model aliases must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("embedding model IDs must be unique")
        return self

    def require(self, alias: str) -> SchemaEmbeddingModelSpec:
        match = next((item for item in self.candidates if item.alias == alias), None)
        if match is None:
            supported = ", ".join(item.alias for item in self.candidates)
            raise ValueError(f"unknown embedding model alias {alias!r}; supported: {supported}")
        return match


def load_schema_embedding_model_registry() -> SchemaEmbeddingModelRegistry:
    resource = files("finance_agent_core.evaluation").joinpath("schema_embedding_models_v1.json")
    return SchemaEmbeddingModelRegistry.model_validate_json(resource.read_bytes())


class SentenceTransformerCpuProvider:
    """Evaluation-only adapter that always runs a frozen model on CPU."""

    def __init__(
        self,
        spec: SchemaEmbeddingModelSpec,
        *,
        batch_size: int = 16,
        cpu_threads: int = 12,
        cache_dir: Path | None = None,
        local_files_only: bool = False,
    ) -> None:
        if not 1 <= batch_size <= 256:
            raise ValueError("batch_size must be between 1 and 256")
        if not 1 <= cpu_threads <= 256:
            raise ValueError("cpu_threads must be between 1 and 256")

        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional environment
            raise RuntimeError(
                "CPU embedding evaluation dependencies are absent; install "
                "requirements/embedding-eval.txt in gaeng3-embedding-eval"
            ) from exc

        torch.set_num_threads(cpu_threads)
        self.spec = spec
        self.batch_size = batch_size
        self.cpu_threads = cpu_threads
        self.document_calls = 0
        self.document_text_count = 0
        self.query_calls = 0
        self.query_text_count = 0
        model_source = spec.model_id
        model_revision: str | None = spec.revision
        if spec.trust_remote_code:
            from huggingface_hub import snapshot_download

            model_source = snapshot_download(
                repo_id=spec.model_id,
                revision=spec.revision,
                cache_dir=str(cache_dir) if cache_dir else None,
                local_files_only=local_files_only,
            )
            model_revision = None
        load_started = time.perf_counter()
        self._model = SentenceTransformer(
            model_source,
            revision=model_revision,
            device="cpu",
            trust_remote_code=spec.trust_remote_code,
            cache_folder=str(cache_dir) if cache_dir else None,
            local_files_only=local_files_only,
            model_kwargs=(
                {"code_revision": spec.remote_code_revision} if spec.remote_code_revision else None
            ),
            config_kwargs=(
                {"code_revision": spec.remote_code_revision} if spec.remote_code_revision else None
            ),
        )
        self.model_load_ms = (time.perf_counter() - load_started) * 1000
        self._model.max_seq_length = spec.max_sequence_length
        dimension_getter = getattr(self._model, "get_embedding_dimension", None)
        if dimension_getter is None:
            dimension_getter = self._model.get_sentence_embedding_dimension
        actual_dimension = dimension_getter()
        if actual_dimension != spec.dimension:
            raise RuntimeError(
                f"embedding dimension differs for {spec.alias}: "
                f"expected {spec.dimension}, got {actual_dimension}"
            )
        if str(self._model.device) != "cpu":
            raise RuntimeError(f"CPU evaluation loaded an unexpected device: {self._model.device}")
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id=f"sentence_transformers_{spec.alias.replace('-', '_')}",
            model_id=spec.model_id,
            model_revision=spec.revision,
            license_id=spec.license_id,
            dimension=spec.dimension,
            pooling=spec.pooling,
        )

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    @property
    def library_versions(self) -> dict[str, str]:
        names = ("torch", "transformers", "sentence-transformers", "huggingface-hub")
        return {name: importlib_metadata.version(name) for name in names}

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return vectors.astype("float32", copy=False).tolist()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.document_text_count += len(texts)
        return self._encode([self.spec.prepare_document(text) for text in texts])

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        self.query_text_count += 1
        return self._encode([self.spec.prepare_query(text)])[0]

    def smoke_probe(self) -> dict[str, object]:
        started = time.perf_counter()
        vectors = self._encode(
            [
                self.spec.prepare_query("총보수가 낮은 미국 ETF"),
                self.spec.prepare_document("total_expense_ratio | 총보수율 | 운용 보수"),
            ]
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "vector_count": len(vectors),
            "dimension": len(vectors[0]),
            "finite": all(math.isfinite(value) for vector in vectors for value in vector),
            "elapsed_ms": round(elapsed_ms, 6),
        }


def render_model_registry() -> str:
    registry = load_schema_embedding_model_registry()
    return json.dumps(registry.model_dump(mode="json"), ensure_ascii=False, indent=2)


__all__ = [
    "SchemaEmbeddingModelRegistry",
    "SchemaEmbeddingModelSpec",
    "SentenceTransformerCpuProvider",
    "load_schema_embedding_model_registry",
    "render_model_registry",
]
