"""Thin wrapper around the Gemini API (google-genai SDK) for a manual function-calling loop.

The agent loop only depends on `send()`, so any object with the same method (e.g. a scripted
fake in the tests) can stand in for the real model.
"""

import sys
import time
from dataclasses import dataclass, field

import httpx
from google import genai
from google.genai import errors, types

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
OVERLOADED_STATUS = {429, 503}  # model busy/quota: worth switching to the fallback model


class LLMError(Exception):
    """Unrecoverable model/API failure."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class ModelTurn:
    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""       # visible text the model produced (if any)
    thoughts: str = ""   # thought summary, recorded in the trace as the agent's reasoning


class GeminiLLM:
    def __init__(self, api_key: str, model: str, system_prompt: str, tool_schemas: list[dict],
                 fallback_model: str | None = None, timeout_s: int = 180, max_retries: int = 8):
        # Retries are handled below (with model fallback), so disable the SDK's own retry layer.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=timeout_s * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.model = model
        self.fallback_model = fallback_model if fallback_model != model else None
        self.max_retries = max_retries
        self.history: list[types.Content] = []
        self.config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"])
                for t in tool_schemas
            ])],
            # We run the loop ourselves: the SDK must not execute anything automatically.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # Every turn must be a tool call; the agent ends the run by calling `finish`.
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            thinking_config=types.ThinkingConfig(include_thoughts=True),
        )

    def send(self, message: str | list[tuple[ToolCall, dict]]) -> ModelTurn:
        """Send a user message or a batch of tool results; return the model's next turn."""
        if isinstance(message, str):
            parts = [types.Part(text=message)]
        else:
            parts = [types.Part(function_response=types.FunctionResponse(id=c.id, name=c.name, response=r))
                     for c, r in message]
        self.history.append(types.Content(role="user", parts=parts))

        response = self._generate()
        candidate = (response.candidates or [None])[0]
        content = candidate.content if candidate else None
        if not content or not content.parts:
            return ModelTurn()
        # Append the model content unchanged so thought signatures are preserved for Gemini 3.
        self.history.append(content)

        turn = ModelTurn()
        for part in content.parts:
            if part.function_call:
                fc = part.function_call
                turn.calls.append(ToolCall(name=fc.name, args=dict(fc.args or {}), id=fc.id))
            elif part.text and part.thought:
                turn.thoughts += part.text
            elif part.text:
                turn.text += part.text
        return turn

    def _generate(self):
        while True:
            try:
                return self._generate_with_retries()
            except LLMError as e:
                if not (self.fallback_model and e.code in OVERLOADED_STATUS):
                    raise
                print(f"warning: {self.model} unavailable ({e.code}); switching to {self.fallback_model}",
                      file=sys.stderr)
                self.model, self.fallback_model = self.fallback_model, None

    def _generate_with_retries(self):
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.models.generate_content(model=self.model, contents=self.history,
                                                           config=self.config)
            except errors.APIError as e:
                # With a fallback available, give up on a busy/over-quota model quickly.
                limit = 2 if self.fallback_model and e.code in OVERLOADED_STATUS else self.max_retries
                if e.code not in RETRYABLE_STATUS or attempt >= limit:
                    raise LLMError(f"Gemini API error {e.code} ({self.model}): {e.message}", e.code) from e
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"Network error talking to Gemini: {e}") from e
            time.sleep(min(2 ** attempt * 2, 30))
        raise LLMError("unreachable")
