"""OpenRouter synthesis provider (V1): the generic chat-completions adapter bound to OpenRouter's documented contract.
Offline-tested only; no live request has ever been made through it.

Contract source: OpenRouter's official documentation, read on 2026-10-20 (the pages "API Reference: Overview",
"API Reference: Errors" and "Structured Outputs" on openrouter.ai/docs). From them:
  * `POST https://openrouter.ai/api/v1/chat/completions` with `Authorization: Bearer <key>`.
  * Body fields used: `model` (an id with an organisation prefix such as `openai/gpt-5.2`), `messages`, `max_tokens`
    (1 up to the model's context length), `temperature` (0 to 2), `stream`, and, only when asked for,
    `response_format` of type `json_schema` (`name`, `strict`, `schema`) plus `provider.require_parameters`.
  * Reply: `choices[0].message.content`, `choices[0].finish_reason` (`stop`, `length`, `content_filter`, `tool_calls`
    or `error`), `usage`.
  * Errors: the body is `{"error": {"code": <number>, "message", "metadata"}}` and the HTTP status equals `code` for
    request-level failures: 400 bad parameters, 401 bad credentials, 402 insufficient credits, 403 permissions or
    moderation or a spent budget, 408 timeout, 429 rate limited, 502 model down or invalid reply, 503 no provider
    meets the routing needs. Otherwise the status can be 200 and the error is in the body: a top-level `error` object
    with no `choices`, or `choices[].error` with `finish_reason` `error` (possibly with partial content).
  * Structured outputs are supported by select models only, per endpoint, and a model without support makes the
    request fail. That is why they are opt-in here.
NOT in the documentation read, and therefore not assumed: any default model (the owner must choose one), the model's
context length or price, and how long a request may take. The documentation was not exercised against the live service.

Everything else is inherited unchanged from `synthesis_provider.chat`: only the validated findings, the evidence text
(no links, no scores) and the question are sent; the reply is accepted only in the documented synthesis shape with
validated citations; the four closed synthesis error codes; one request, no retry, no fallback; no secret or provider
text in any result. The endpoint is fixed to the documented one and cannot be changed by configuration. The time limit
is the best-effort budget of `research.tavily.deadline_transport`, NOT a hard end-to-end deadline. Output is
unverified model text: a citation of a supplied reference is not a check of the claim behind it.
"""

import re

from orchestration.synthesis import ERROR_FAILED, ERROR_REJECTED, allowed_refs
from synthesis_provider.chat import ChatSynthesisProvider

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_KEY_ENV_VAR = "OPENROUTER_API_KEY"
LABEL_PREFIX = "openrouter"

# The model the OWNER selected for the synthesis stage (2026-10-20). It is used only when the server is started without
# `--synthesis-model`; the provider class itself has no default and still requires a model. OpenRouter's model page for
# this exact id was found on 2026-10-20 and lists `response_format` among its parameters, but it does not state
# `json_schema` (structured output) support, and does not list `temperature`; neither was confirmed, so strict
# structured output stays opt-in and the reply is always validated locally. No fallback model exists.
SELECTED_MODEL = "anthropic/claude-haiku-5.5"

# The completion-token limit sent with every request. It was 800 until a live synthesis-only run (2026-10-21, owner
# observed) ended with finish reason `length`, 800 of 800 completion tokens and only 1,067 characters of model text, so
# the reply was cut off and refused as `synthesis_response_malformed`. 1,067 characters is far fewer than 800 tokens of
# visible JSON, which suggests (does NOT prove) that some completion tokens were not visible text: the model's listing
# names `reasoning` among its supported parameters, and a reasoning model's hidden tokens normally count toward the
# limit. 2,400 leaves room for the longest reply the prompt asks for (about 900 tokens) plus that overhead. It is a local
# choice: the model's maximum output is not stated on the page read, and it is inside this adapter's own 4,096 cap.
# A reply that is still cut off is reported as truncated, never accepted.
DEFAULT_MAX_TOKENS = 2400

# A permissive check of the documented "organisation/model" form (a variant suffix such as ":free" is allowed). It is
# not a list of real models: whether a model exists is for the service to say.
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:+-]{0,100}$")


def model_is_valid(model):
    return isinstance(model, str) and _MODEL_PATTERN.fullmatch(model) is not None


def _schema(inputs):
    """The reply schema for strict structured output, with the citation values limited to the allowed references."""
    return {
        "type": "object",
        "properties": {
            "statements": {"type": "array", "items": {
                "type": "object",
                "properties": {"text": {"type": "string"},
                               "cites": {"type": "array", "items": {"type": "string",
                                                                    "enum": sorted(allowed_refs(inputs))}}},
                "required": ["text", "cites"], "additionalProperties": False}},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["statements", "uncertainties"],
        "additionalProperties": False,
    }


class OpenRouterSynthesisProvider(ChatSynthesisProvider):
    """The OpenRouter chat-completions endpoint, called once per `synthesize`. The model has no default."""

    LABEL_PREFIX = LABEL_PREFIX
    # OpenRouter's endpoint metadata for the selected model (read 2026-10-20) lists `temperature` on none of its 10
    # endpoints, and `require_parameters` would then leave no endpoint to route to. So the request never carries it.
    SEND_TEMPERATURE = False

    def __init__(self, model=None, key_env_var=DEFAULT_KEY_ENV_VAR, transport=None, environ=None,
                 timeout_seconds=60.0, max_tokens=DEFAULT_MAX_TOKENS, structured_output=False):
        super().__init__(ENDPOINT, model, key_env_var, transport=transport, environ=environ,
                         timeout_seconds=timeout_seconds, max_tokens=max_tokens)
        self._structured = structured_output is True

    @staticmethod
    def _model_ok(model):
        return model_is_valid(model)

    def _extra_body(self, inputs):
        if not self._structured:
            return {}
        return {"response_format": {"type": "json_schema",
                                    "json_schema": {"name": "synthesis", "strict": True, "schema": _schema(inputs)}},
                "provider": {"require_parameters": True}}

    def _document_error(self, document):
        """OpenRouter can answer HTTP 200 with the error in the body (no output, or a choice that ended in error)."""
        if not isinstance(document, dict):
            return None
        error = document.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 500 and code != 408:
                return ERROR_REJECTED
            return ERROR_FAILED
        choices = document.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and (choice.get("error") is not None
                                                 or choice.get("finish_reason") == "error"):
                    return ERROR_FAILED
        return None
