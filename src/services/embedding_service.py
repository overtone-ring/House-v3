"""
Embedding Service
=================

ONNX-based text embedding using nomic-embed-text-v1.5.
Singleton pattern — one model instance shared across the process.

Produces 768-dimensional normalized embeddings for:
    - Documents: prefixed with "search_document: "
    - Queries: prefixed with "search_query: "

Dependencies:
    - onnxruntime
    - transformers (AutoTokenizer)
    - numpy

The model directory should be at the path specified in config
(memory.embedding_model) or the default data/models/nomic-embed-text-v1.5-onnx/.
"""

import asyncio
import gc
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Suppress tokenizer parallelism warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"


class EmbeddingService:
    """
    Async embedding service wrapping an ONNX model.

    Usage:
        service = await EmbeddingService.get_instance()
        doc_embedding = await service.embed_document("some text")
        query_embedding = await service.embed_query("search terms")
    """

    # Class-level singleton
    _instance: Optional["EmbeddingService"] = None
    _lock = asyncio.Lock()

    # Model constants
    DIMENSIONS = 768
    MAX_CHARS = 8000
    MAX_TOKENS = 8192

    def __init__(self):
        self._tokenizer = None
        self._ort_session = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")
        self._initialized = False
        self._model_path: Optional[str] = None

    @classmethod
    async def get_instance(cls, model_path: Optional[str] = None) -> "EmbeddingService":
        """Get or create the singleton embedding service."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
                if model_path:
                    cls._instance._model_path = model_path
                await cls._instance._initialize()
            return cls._instance

    async def _initialize(self) -> None:
        """Initialize the model (runs blocking I/O in executor)."""
        if self._initialized:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._initialized = True
        logger.info(f"EmbeddingService initialized ({self.DIMENSIONS}d)")

    def _load_model(self) -> None:
        """Load ONNX model and tokenizer (blocking)."""
        from transformers import AutoTokenizer
        import onnxruntime as ort
        from ..utils.paths import get_project_root

        # Resolve model path
        model_dir = self._model_path
        if not model_dir:
            # Search common locations
            candidates = [
                Path("data/models/nomic-embed-text-v1.5-onnx"),
                get_project_root() / "data" / "models" / "nomic-embed-text-v1.5-onnx",
            ]
            for candidate in candidates:
                if candidate.exists():
                    model_dir = str(candidate)
                    break

        if not model_dir or not Path(model_dir).exists():
            searched = [model_dir] if model_dir else [str(c) for c in candidates]
            raise FileNotFoundError(
                f"Embedding model not found. Looked in: {searched}. "
                f"Download nomic-embed-text-v1.5-onnx to data/models/."
            )

        logger.info(f"Loading embedding model from {model_dir}")
        self._tokenizer = AutoTokenizer.from_pretrained(model_dir)

        # Search for ONNX model file — may be in root or onnx/ subfolder
        candidates = [
            os.path.join(model_dir, "model.onnx"),
            os.path.join(model_dir, "model_quantized.onnx"),
            os.path.join(model_dir, "onnx", "model.onnx"),
            os.path.join(model_dir, "onnx", "model_quantized.onnx"),
        ]
        model_file = next((f for f in candidates if os.path.exists(f)), candidates[-1])

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 2
        self._ort_session = ort.InferenceSession(model_file, sess_options)

    # ── Embedding Methods ─────────────────────────────────────────

    async def embed_document(self, text: str) -> List[float]:
        """Embed a document (for storage/indexing)."""
        return await self._embed_async(text, "search_document: ")

    async def embed_query(self, text: str) -> List[float]:
        """Embed a query (for search)."""
        return await self._embed_async(text, "search_query: ")

    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Batch embed documents."""
        return list(await asyncio.gather(*[self.embed_document(t) for t in texts]))

    async def embed_queries(self, texts: List[str]) -> List[List[float]]:
        """Batch embed queries."""
        return list(await asyncio.gather(*[self.embed_query(t) for t in texts]))

    async def _embed_async(self, text: str, prefix: str) -> List[float]:
        """Run embedding in executor thread."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self._embed_sync, text, prefix
        )

    def _embed_sync(self, text: str, prefix: str) -> List[float]:
        """Synchronous embedding (runs in thread pool)."""
        # Truncate if needed
        text = text[:self.MAX_CHARS]
        full_text = f"{prefix}{text}"

        # Tokenize
        encoded = self._tokenizer(
            full_text,
            padding=True,
            truncation=True,
            max_length=self.MAX_TOKENS,
            return_tensors="np",
        )

        input_ids = encoded["input_ids"].astype(np.int64)
        attention_mask = encoded["attention_mask"].astype(np.int64)

        # Run ONNX inference
        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}

        # Check for token_type_ids
        if "token_type_ids" in [inp.name for inp in self._ort_session.get_inputs()]:
            inputs["token_type_ids"] = np.zeros_like(input_ids)

        outputs = self._ort_session.run(None, inputs)
        token_embeddings = outputs[0]

        # Mean pooling + L2 normalization
        pooled = self._mean_pooling(token_embeddings, attention_mask)
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm

        return pooled.tolist()

    def _mean_pooling(
        self, token_embeddings: np.ndarray, attention_mask: np.ndarray
    ) -> np.ndarray:
        """Mean pooling with attention mask."""
        mask_expanded = np.expand_dims(attention_mask, -1).astype(np.float32)
        sum_embeddings = np.sum(token_embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        return (sum_embeddings / sum_mask).flatten()

    # ── Utility ───────────────────────────────────────────────────

    @staticmethod
    def cosine_similarity(v1: List[float], v2: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        a = np.array(v1)
        b = np.array(v2)
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0

    # ── Lifecycle ─────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Release model resources."""
        self._ort_session = None
        self._tokenizer = None
        self._executor.shutdown(wait=False)
        gc.collect()
        logger.info("EmbeddingService shut down")

    @classmethod
    async def shutdown_instance(cls) -> None:
        """Shutdown the singleton instance."""
        if cls._instance:
            await cls._instance.shutdown()
            cls._instance = None


# ── Module-level convenience ──────────────────────────────────────────

async def get_embedding_service(model_path: Optional[str] = None) -> EmbeddingService:
    """Get the singleton EmbeddingService instance."""
    return await EmbeddingService.get_instance(model_path)
