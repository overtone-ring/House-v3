"""
Base Provider Interface
=======================

All LLM providers must implement this interface.
Handles the contract between House and any LLM backend.
"""

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Error Classification ──────────────────────────────────────────────

class ErrorCategory(Enum):
    """Classifies API errors for retry/routing decisions."""
    RATE_LIMIT = "rate_limit"
    CONTENT_FILTERED = "content_filtered"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    INVALID_REQUEST = "invalid_request"
    AUTH_ERROR = "auth_error"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self in (ErrorCategory.RATE_LIMIT, ErrorCategory.TIMEOUT, ErrorCategory.SERVER_ERROR)


# ── Data Contracts ────────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """Configuration for a provider instance."""
    provider_name: str
    model: str
    api_key: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 8192
    temperature: float = 0.7
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Standardized result from any provider."""
    text: str
    raw_response: Any = None
    usage: Optional[Dict[str, int]] = None  # {prompt_tokens, completion_tokens, total_tokens}
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    latency_ms: float = 0.0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolDefinition:
    """Standardized tool/function definition for tool-use flows."""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


# ── Retry Logic ───────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    """Retry behavior configuration."""
    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 30.0
    exponential_base: float = 2.0

    def get_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter cap."""
        delay = self.base_delay * (self.exponential_base ** attempt)
        return min(delay, self.max_delay)


DEFAULT_RETRY = RetryConfig()


# ── Base Provider ─────────────────────────────────────────────────────

class BaseProvider(ABC):
    """
    Abstract base for all LLM providers.

    Subclasses must implement:
        - generate()           : Single-shot generation
        - generate_stream()    : Streaming generation (yield chunks)
        - classify_error()     : Map provider-specific errors to ErrorCategory

    Optional overrides:
        - continue_with_tool_results() : Tool-use continuation
        - validate_config()            : Provider-specific config validation
    """

    def __init__(self, config: ProviderConfig, retry: Optional[RetryConfig] = None):
        self.config = config
        self.retry = retry or DEFAULT_RETRY
        self._client: Any = None

    @property
    def name(self) -> str:
        return self.config.provider_name

    @property
    def model(self) -> str:
        return self.config.model

    # ── Abstract Interface ────────────────────────────────────────

    @abstractmethod
    def _create_client(self) -> Any:
        """Create the underlying API client. Called lazily on first use."""
        ...

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        contextual_primer: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        conversation_history: Optional[List[Dict]] = None,
        formatted_memories: Optional[str] = None,
        tools: Optional[List[ToolDefinition]] = None,
    ) -> GenerationResult:
        """
        Generate a complete response.

        Args:
            prompt: The user's message
            system_prompt: Static system/persona prompt
            contextual_primer: Dynamic context (affective state, relational data)
            max_tokens: Override for max output tokens
            temperature: Override for sampling temperature
            conversation_history: Prior turns [{role, content}, ...]
            formatted_memories: Pre-formatted memory context string
            tools: Tool definitions for function-calling flows

        Returns:
            GenerationResult with text, usage stats, and optional tool_calls
        """
        ...

    @abstractmethod
    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        contextual_primer: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Generator[str, None, None]:
        """
        Stream response text chunks.

        Yields:
            Text chunks as they arrive from the model.
        """
        ...

    @abstractmethod
    def classify_error(self, error: Exception) -> ErrorCategory:
        """Map a provider-specific exception to an ErrorCategory."""
        ...

    # ── Optional Overrides ────────────────────────────────────────

    def continue_with_tool_results(
        self,
        original_prompt: str,
        system_prompt: Optional[str],
        contextual_primer: Optional[str],
        assistant_response: Any,
        tool_results: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> GenerationResult:
        """Continue generation after tool execution. Override per-provider."""
        raise NotImplementedError(
            f"{self.name} does not support tool-use continuation. "
            "Override continue_with_tool_results() to add support."
        )

    def validate_config(self) -> List[str]:
        """Validate provider-specific config. Returns list of warnings."""
        warnings = []
        if not self.config.api_key:
            warnings.append(f"{self.name}: No API key configured")
        return warnings

    # ── Shared Infrastructure ─────────────────────────────────────

    @property
    def client(self) -> Any:
        """Lazy client initialization."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _call_with_retry(self, func, context_label: str = "generate") -> Any:
        """
        Execute func() with retry logic based on error classification.

        Args:
            func: Callable to execute
            context_label: Label for log messages

        Returns:
            Result of func()

        Raises:
            The original exception if non-retryable or retries exhausted.
        """
        last_exception = None

        for attempt in range(self.retry.max_retries + 1):
            try:
                start = time.monotonic()
                result = func()
                elapsed = (time.monotonic() - start) * 1000
                logger.debug(f"[{self.name}] {context_label} completed in {elapsed:.0f}ms")
                return result

            except Exception as e:
                last_exception = e
                category = self.classify_error(e)

                if not category.retryable:
                    logger.warning(f"[{self.name}] Non-retryable {category.value}: {e}")
                    raise

                if attempt < self.retry.max_retries:
                    delay = self.retry.get_delay(attempt)
                    logger.warning(
                        f"[{self.name}] {category.value} on {context_label}, "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{self.retry.max_retries})"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"[{self.name}] Exhausted {self.retry.max_retries} retries "
                        f"for {context_label} ({category.value})"
                    )
                    raise

        raise last_exception  # Should never reach here

    def _resolve_params(
        self,
        max_tokens: Optional[int],
        temperature: Optional[float],
    ) -> Tuple[int, float]:
        """Resolve generation parameters with defaults."""
        return (
            max_tokens if max_tokens is not None else self.config.max_tokens,
            temperature if temperature is not None else self.config.temperature,
        )
