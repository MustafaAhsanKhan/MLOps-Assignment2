"""Thin wrapper around the Gemini API (google-genai SDK) for a manual function-calling loop.

The agent loop only depends on `send()`, so any object with the same method (e.g. a scripted
fake in the tests) can stand in for the real model.
"""

import time
from dataclasses import dataclass, field

import httpx
from google import genai
from google.genai import errors, types

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Unrecoverable model/API failure."""


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
                 timeout_s: int = 180, max_retries: int = 5):
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.model = model
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
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.models.generate_content(model=self.model, contents=self.history,
                                                           config=self.config)
            except errors.APIError as e:
                if e.code not in RETRYABLE_STATUS or attempt == self.max_retries:
                    raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"Network error talking to Gemini: {e}") from e
            time.sleep(min(2 ** attempt * 2, 60))
        raise LLMError("unreachable")
