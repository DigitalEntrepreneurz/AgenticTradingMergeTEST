# Connecting your LLM key

The firm runs fine with **no key at all** (deterministic rule engine, $0). A key
only upgrades three things: strategy discovery from a video transcript, the
scout's idea generation, and the CEO's written briefings.

## 1. Pick a provider

`firm/llm.py` speaks two wire protocols, so most providers work:

| `llm.provider` | protocol | env var in `.env` | notes |
|---|---|---|---|
| `anthropic`  | anthropic | `ANTHROPIC_API_KEY`  | native Claude |
| `openrouter` | openai    | `OPENROUTER_API_KEY` | one key, every model, has free tiers |
| `groq`       | openai    | `GROQ_API_KEY`       | free tier, very fast, Llama/Qwen |
| `openai`     | openai    | `OPENAI_API_KEY`     | |
| `together`   | openai    | `TOGETHER_API_KEY`   | |
| `deepseek`   | openai    | `DEEPSEEK_API_KEY`   | cheapest paid option |
| `custom`     | openai    | `LLM_API_KEY`        | any OpenAI-compatible URL — also LM Studio / Ollama |
| `none`       | –         | –                    | heuristics only |

## 2. Write the key yourself

Never paste a key into a chat window or a committed file. From a terminal in
the project root:

```bash
echo 'OPENROUTER_API_KEY=sk-or-v1-xxxxxxxx' >> .env
```

`.env` is gitignored and is loaded automatically by `firm/config.py`.

## 3. Point the config at it

In `config/firm.yaml`:

```yaml
llm:
  provider: "openrouter"
  model_prefix: "anthropic"      # so bare "claude-sonnet-4-5" resolves correctly
  max_daily_usd: 5.0
  fallback_to_heuristics: true
```

`model_prefix` exists because the agents ask for Anthropic-style names. On
OpenRouter those need a vendor prefix; the client adds it only when the model
name has no `/` already.

### Free models

Set a `:free` model and the cost ledger correctly charges $0:

```yaml
agents:
  research:
    model: "meta-llama/llama-3.3-70b-instruct:free"
```

### A local model (no key, no cost)

```yaml
llm:
  provider: "custom"
  base_url: "http://localhost:1234/v1/chat/completions"   # LM Studio
```

A `localhost` base URL is accepted without any key.

## 4. Verify before running the firm

```bash
python cli.py llm
```

It prints the provider, endpoint, masked key and prefix, then makes one small
real call and reports tokens, cost and whether the reply parsed as JSON. A
gateway that returns prose instead of JSON will still "work" but the agents
will fall back to heuristics — this command tells you that up front.

## If it fails

- `401` — key does not match the provider you configured.
- `404` model not found — the provider does not serve that model name. Set a
  provider-native id under `agents.<name>.model`.
- Reply arrives but "NOT valid JSON" — the model is too small to follow the
  schema. Use a stronger one for `research` and `scout`.

Nothing here can break the firm: `LLM.ask()` never raises, and every agent has
a deterministic fallback path.
