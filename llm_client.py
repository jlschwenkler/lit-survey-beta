"""
llm_client.py — the single place every script talks to a language model.

WHY THIS FILE EXISTS
--------------------
The pipeline calls an LLM in ~10 scripts (relevance scoring, issue discovery,
reference parsing, engagement scoring, …). Rather than scatter SDK calls and
model names across all of them, every script imports `call_model()` from here.
That means:

  * To change which model a stage uses, edit this file (or set an env var).
  * To run the pipeline on a DIFFERENT PROVIDER (OpenAI, Gemini, a local model,
    an OpenAI-compatible gateway), you only implement one new backend HERE —
    the ten call sites do not change.

So although this beta ships configured for Anthropic's Claude API, the tool is
NOT Claude-only and is not tied to Claude Code. The scripts are plain Python;
drive them from any terminal or any coding assistant.

CONFIGURATION (environment variables)
-------------------------------------
  ANTHROPIC_API_KEY      required for the default (Anthropic) backend
  LLM_PROVIDER           "anthropic" (default). Add others below.
  LLM_FAST_MODEL         override the cheap/fast model  (default below)
  LLM_SMART_MODEL        override the stronger model     (default below)

Call sites pass a logical tier ("fast" or "smart") OR an explicit model id.
Logical tiers are the portable way to ask for capability without hardcoding a
vendor's model name in every script.
"""

import os

# --- Default model ids (Anthropic). Override per-run with env vars above. -----
# These are *defaults*; a single edit here re-points every stage of the pipeline.
DEFAULT_FAST_MODEL  = os.environ.get("LLM_FAST_MODEL",  "claude-haiku-4-5")
DEFAULT_SMART_MODEL = os.environ.get("LLM_SMART_MODEL", "claude-sonnet-4-6")

PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").lower()

# Map the logical tiers used throughout the pipeline to concrete model ids.
_TIERS = {"fast": DEFAULT_FAST_MODEL, "smart": DEFAULT_SMART_MODEL}


def resolve_model(model):
    """Turn a logical tier ('fast'/'smart') or an explicit model id into an id."""
    return _TIERS.get(model, model)


# --- Anthropic backend --------------------------------------------------------
_anthropic_client = None


def _anthropic_call(model, system, user, max_tokens):
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    resp = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip() if resp.content else ""


# --- OpenAI backend (stub — implement to run on OpenAI / compatible gateways) --
def _openai_call(model, system, user, max_tokens):
    raise NotImplementedError(
        "LLM_PROVIDER=openai is a stub. Implement _openai_call() in llm_client.py:\n"
        "    from openai import OpenAI\n"
        "    client = OpenAI()  # reads OPENAI_API_KEY\n"
        "    r = client.chat.completions.create(model=model, max_tokens=max_tokens,\n"
        "        messages=[{'role':'system','content':system},\n"
        "                  {'role':'user','content':user}])\n"
        "    return r.choices[0].message.content.strip()\n"
        "Then set LLM_FAST_MODEL / LLM_SMART_MODEL to OpenAI model ids."
    )


_BACKENDS = {"anthropic": _anthropic_call, "openai": _openai_call}


def call_model(system, user, model="fast", max_tokens=1024):
    """Single entry point for every LLM call in the pipeline.

    system / user : the system prompt and the user message (both strings).
    model         : a logical tier ("fast" | "smart") or an explicit model id.
    max_tokens    : output cap.

    Returns the model's text reply (stripped). Provider is chosen by the
    LLM_PROVIDER env var; default is Anthropic/Claude.
    """
    backend = _BACKENDS.get(PROVIDER)
    if backend is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER={PROVIDER!r}. Known: {sorted(_BACKENDS)}."
        )
    return backend(resolve_model(model), system, user, max_tokens)
