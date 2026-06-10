"""
OpenRouter Provider
===================

Default provider for House-v3. Uses the OpenAI-compatible API
to access 200+ models through a single endpoint.

OpenRouter docs: https://openrouter.ai/docs

Supports:
    - Standard generation (with conversation history + memories)
    - Streaming generation
    - Tool/function calling
    - Tool-use continuation
"""

import logging
import time
from typing import Any, Dict, Generator, List, Optional

from .base import (
    BaseProvider,
    ErrorCategory,
    GenerationResult,
    ProviderConfig,
    ToolDefinition,
)
logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(BaseProvider):
    """
    OpenRouter provider using the OpenAI-compatible API.

    Model names use the OpenRouter format: "provider/model"
    Examples:
        - anthropic/claude-sonnet-4
        - google/gemini-2.5-flash
        - meta-llama/llama-4-maverick
        - qwen/qwen3-235b-a22b
    """

    def _create_client(self) -> Any:
        """Create OpenAI client pointed at OpenRouter."""
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "OpenRouter provider requires the 'openai' package. "
                "Install it with: pip install openai"
            )

        base_url = self.config.base_url or OPENROUTER_BASE_URL

        return OpenAI(
            api_key=self.config.api_key,
            base_url=base_url,
            # SDK defaults are 600s timeout + 2 internal retries, which stack
            # under _call_with_retry's 3 attempts — a hung connection could
            # hold a message for many minutes. Our retry layer is the only one.
            timeout=120.0,
            max_retries=0,
            default_headers={
                "HTTP-Referer": "https://github.com/house-v3",
                "X-Title": "House-v3",
            },
        )

    def validate_config(self) -> List[str]:
        """Validate OpenRouter-specific configuration."""
        warnings = super().validate_config()
        if not self.config.model:
            warnings.append("OpenRouter: No model specified")
        if self.config.api_key and not self.config.api_key.startswith("sk-or-"):
            warnings.append("OpenRouter: API key doesn't start with 'sk-or-' - may not be an OpenRouter key")
        return warnings

    # ── Core Generation ───────────────────────────────────────────

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
        json_mode: bool = False,
        plugins: Optional[List[Dict]] = None,
    ) -> GenerationResult:
        """Generate a complete response via OpenRouter."""
        max_tokens, temperature = self._resolve_params(max_tokens, temperature)
        messages = self._build_messages(
            prompt, system_prompt, contextual_primer,
            conversation_history, formatted_memories,
        )
        payload = self._build_payload(
            messages, max_tokens, temperature, tools, json_mode, plugins
        )

        def make_request():
            return self.client.chat.completions.create(**payload)

        start = time.monotonic()
        try:
            response = self._call_with_retry(make_request, "generate")
            latency = (time.monotonic() - start) * 1000

            logger.info(f"[OpenRouter] Model requested: {self.config.model} | Model used: {response.model} | Latency: {latency:.0f}ms")

            choice = response.choices[0]
            text = choice.message.content or ""

            # Extract tool calls if present
            tool_calls = None
            if choice.message.tool_calls:
                tool_calls = [
                    {
                        "tool_call_id": tc.id,
                        "tool_name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    for tc in choice.message.tool_calls
                ]

            # Extract usage
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return GenerationResult(
                text=text,
                raw_response=response,
                usage=usage,
                model=response.model,
                finish_reason=choice.finish_reason,
                tool_calls=tool_calls,
                latency_ms=latency,
            )

        except Exception as e:
            category = self.classify_error(e)
            if category == ErrorCategory.CONTENT_FILTERED:
                return GenerationResult(
                    text="[Content filtered - unable to respond to that topic right now.]",
                    latency_ms=(time.monotonic() - start) * 1000,
                )
            raise

    def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        contextual_primer: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Generator[str, None, None]:
        """Stream response text chunks."""
        max_tokens, temperature = self._resolve_params(max_tokens, temperature)
        messages = self._build_messages(prompt, system_prompt, contextual_primer)
        payload = self._build_payload(messages, max_tokens, temperature)
        payload["stream"] = True

        def init_stream():
            return self.client.chat.completions.create(**payload)

        try:
            stream = self._call_with_retry(init_stream, "stream_init")
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            category = self.classify_error(e)
            if category == ErrorCategory.CONTENT_FILTERED:
                yield "[Content filtered - unable to respond to that topic right now.]"
            else:
                yield "[Error: Unable to stream response. Please try again.]"

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
        """Continue generation after tool execution."""
        max_tokens, temperature = self._resolve_params(max_tokens, temperature)
        messages = self._build_messages(original_prompt, system_prompt, contextual_primer)

        # Add assistant's tool calls
        assistant_message = assistant_response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": assistant_message.tool_calls,
        })

        # Add tool results
        for result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": result["tool_call_id"],
                "name": result["tool_name"],
                "content": result["result"],
            })

        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        def make_request():
            return self.client.chat.completions.create(**payload)

        start = time.monotonic()
        try:
            response = self._call_with_retry(make_request, "tool_continuation")
            latency = (time.monotonic() - start) * 1000
            text = response.choices[0].message.content or ""

            return GenerationResult(
                text=text,
                raw_response=response,
                model=response.model,
                finish_reason=response.choices[0].finish_reason,
                latency_ms=latency,
            )
        except Exception as e:
            category = self.classify_error(e)
            if category == ErrorCategory.CONTENT_FILTERED:
                return GenerationResult(
                    text="[Content filtered - unable to respond to that topic right now.]",
                    latency_ms=(time.monotonic() - start) * 1000,
                )
            raise

    # ── Error Classification ──────────────────────────────────────

    def classify_error(self, error: Exception) -> ErrorCategory:
        """Classify OpenRouter/OpenAI errors."""
        error_str = str(error).lower()

        # Rate limiting
        if any(p in error_str for p in ["rate_limit", "429", "too many requests", "quota"]):
            return ErrorCategory.RATE_LIMIT

        # Content filtering
        if any(p in error_str for p in ["content_policy", "content_filter", "safety", "moderation"]):
            return ErrorCategory.CONTENT_FILTERED

        # Timeouts
        if any(p in error_str for p in ["timeout", "timed out", "deadline"]):
            return ErrorCategory.TIMEOUT

        # Server errors
        if any(p in error_str for p in ["500", "502", "503", "504", "internal_error", "overloaded"]):
            return ErrorCategory.SERVER_ERROR

        # Auth
        if any(p in error_str for p in ["401", "403", "invalid_api_key", "authentication"]):
            return ErrorCategory.AUTH_ERROR

        # Invalid request
        if any(p in error_str for p in ["400", "invalid", "malformed"]):
            return ErrorCategory.INVALID_REQUEST

        return ErrorCategory.UNKNOWN

    # ── Message Building ──────────────────────────────────────────

    def _build_messages(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        contextual_primer: Optional[str] = None,
        conversation_history: Optional[List[Dict]] = None,
        formatted_memories: Optional[str] = None,
    ) -> List[Dict]:
        """
        Build the messages array for the OpenAI-compatible API.

        Message order:
            1. System prompt (static persona + dynamic context)
            2. Conversation history (prior turns)
            3. User message (with memory context if available)
        """
        messages = []

        # System message: persona identity FIRST, then dynamic context
        # The model should read "who I am" before "what's happening right now"
        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if contextual_primer:
            system_parts.append(contextual_primer)

        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # Conversation history
        if conversation_history:
            for turn in conversation_history:
                messages.append({
                    "role": turn.get("role", "user"),
                    "content": turn.get("content", ""),
                })

        # User message with optional memory context
        user_content = prompt
        if formatted_memories:
            user_content = (
                f"[Relevant memories from our past conversations]\n"
                f"{formatted_memories}\n\n"
                f"---\n\n"
                f"{prompt}"
            )

        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_payload(
        self,
        messages: List[Dict],
        max_tokens: int,
        temperature: float,
        tools: Optional[List[ToolDefinition]] = None,
        json_mode: bool = False,
        plugins: Optional[List[Dict]] = None,
    ) -> Dict:
        """Build the API request payload."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        if plugins:
            payload["plugins"] = plugins

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        # Pass through any extra config (e.g. top_p, frequency_penalty)
        for key in ("top_p", "frequency_penalty", "presence_penalty", "stop"):
            if key in self.config.extra:
                payload[key] = self.config.extra[key]

        return payload
