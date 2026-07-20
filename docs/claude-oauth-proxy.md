# Claude OAuth proxy integration contract

Globus supports a provider named `claude-oauth` for installations that already
operate a local, OpenAI-compatible bridge to the Claude CLI.

This repository does **not** bundle or install that bridge. Proxy projects,
authentication flows, and subscription terms can change independently, so
choose and operate one you trust rather than treating an unpinned third-party
container as part of Globus.

## Endpoint Globus expects

The current adapter sends:

```text
POST http://127.0.0.1:8787/v1/chat/completions
Content-Type: application/json
```

The request uses the OpenAI chat-completions shape and may include `tools` and
`tool_choice`. The response must also use the OpenAI-compatible
`choices[0].message` shape.

Keep this endpoint on loopback. It has access to whichever Claude session the
bridge uses and should never be exposed directly to the internet.

Configure Globus with:

```dotenv
GLOBUS_LLM_PROVIDER=claude-oauth
GLOBUS_OAUTH_MODEL=sonnet
```

Then verify the bridge itself before starting Globus:

```bash
curl --fail --silent http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data '{
    "model": "sonnet",
    "messages": [{"role": "user", "content": "Reply with OK."}],
    "max_tokens": 10
  }'
```

## Docker note

The default container also calls `127.0.0.1:8787`. A proxy in a separate
Compose service is therefore not reachable without an explicit network design
or a configurable proxy URL. For the documented Docker quick start, use one of
the direct providers below unless you deliberately place the proxy in the same
network namespace.

## Direct-provider alternatives

Anthropic:

```dotenv
GLOBUS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
```

DeepSeek:

```dotenv
GLOBUS_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
```

The Truth Layer demo remains credential-free regardless of the chat provider:

```bash
python -m globus_truth
```
