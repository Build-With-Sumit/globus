# Voice setup — ElevenLabs Conversational AI

Globus's voice path uses [ElevenLabs Conversational AI](https://elevenlabs.io/conversational-ai)
as the audio layer (ASR + TTS + barge-in) and your Globus server as the
"custom LLM" — so the brain stays on your box, the voice stays in EL's
cloud, and you keep complete control over the data.

## Architecture

```
Browser (chat page) ─┐
   │                 │ WebSocket (audio)
   │                 ▼
   │             ElevenLabs cloud (ASR + TTS)
   │                 │
   │                 │ HTTPS: POST /api/globus/voice-llm/chat/completions
   │                 ▼
   └─────────────► Globus server (this repo)
                     │
                     ▼
                  Claude / DeepSeek / your LLM
```

The browser opens the EL session and sends:
- `agentId` = your ElevenLabs agent ID (config `ELEVENLABS_AGENT_ID`)
- `dynamicVariables.voice_token` = HMAC token issued by the server at page
  render (6h TTL, member-scoped)

EL then calls your server with the conversation transcript on every turn.
Your server verifies the token, runs the question through the chat
orchestrator (same brain as text chat), and returns an OpenAI-shape
response. EL TTS-es it back to the browser.

## Prerequisites

1. **Globus running** with `SITE` set to a publicly-reachable HTTPS URL.
   EL's cloud needs to be able to POST to your `/api/globus/voice-llm/*`
   endpoint. For local dev, use [ngrok](https://ngrok.com/) or
   [cloudflared](https://github.com/cloudflare/cloudflared) tunnels.
2. **An ElevenLabs account** ([elevenlabs.io](https://elevenlabs.io)).
   The Starter plan is enough for testing; Creator+ for production.

## 1. Create the ElevenLabs agent

In the ElevenLabs dashboard:

1. **Conversational AI** → **Agents** → **+ Create agent**.
2. Give it a name ("Globus", "Jarvis", whatever).
3. Pick a **voice** (the model you want it to sound like). The default
   voices work fine; pay for a voice clone later if it matters.
4. **First message** — leave blank, or set something short like
   "I'm listening." (Globus's persona itself doesn't introduce — the
   first audio is the orb saying it's connected.)

## 2. Wire the custom LLM

Still in the agent editor:

1. **LLM** tab → switch from "OpenAI" / "Anthropic" to **Custom LLM**.
2. **Server URL** → `https://your-globus-host/api/globus/voice-llm`
   (don't include `/chat/completions` — EL appends that itself).
3. **Model** → any string (e.g. `globus`). Not used; Globus
   ignores the field and uses its own LLM provider.
4. **API key** → leave blank. Authentication uses the per-call
   `voice_token` (see § 4 below).
5. **System prompt** → leave blank. Globus injects its own persona
   from `config/persona.md` server-side.

## 3. Tool/function calling

Globus runs its own tool loop server-side (orchestrator dispatches
`search_files`, `read_file`, `search_telegram`, etc. internally). You do
**NOT** need to declare tools in the ElevenLabs agent — turning them on
there would create a conflict.

Leave the **Tools** tab empty.

## 4. Security — allowlist + voice token

The custom-LLM endpoint authenticates by HMAC token, not API key.
Anyone calling `/api/globus/voice-llm/chat/completions` without a
fresh signed token gets 401. Tokens are scoped to one member and expire
after 6 hours.

In the EL agent **Security** tab:

1. **Allowed origins** → add your Globus hostname (`globus.example.com`,
   `localhost`, `127.0.0.1`). Without this, abusers who guess your
   agent ID could rack up usage on your EL account.
2. **Auth** → no extra config. The `voice_token` is passed as a
   `dynamicVariable` from the browser and forwarded by EL as an
   `Authorization: Bearer ...` header on every LLM call.

## 5. Configure Globus

Insert the agent ID into the `config` table:

```sql
INSERT INTO config (name, value) VALUES
  ('ELEVENLABS_AGENT_ID', 'agent_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx');
```

(Optional: also set `MEMBERS_ELEVENLABS_AGENT_ID` if you want different
agents for `/members/globus` vs a future public surface — they fall
back to `ELEVENLABS_AGENT_ID` if unset.)

Restart `globus.service`. The chat page now renders the voice orb
backed by your agent.

## 6. Test

1. Visit `/members/globus`.
2. The orange orb should be visible above the chat. Tap it.
3. Allow microphone access in the browser.
4. Speak: "What's in my vault?" (or anything else).
5. Globus answers via TTS within a few seconds.

If the orb stays grey or shows an error:
- **Browser console** → look for `[orb]` or `handleErrorEvent` logs.
- **ElevenLabs dashboard** → Conversational AI → History → click your
  agent → see if the call attempt arrived and what failed.
- **Globus server log** — should show a `/api/globus/voice-llm/...`
  hit per turn. A 401 here means the voice token is missing/expired
  (try reloading the chat page).

## What v0.4 does NOT include

- **Per-turn keepalive / holding audio** — prod uses this to keep the EL
  session alive during long tool calls. The OSS path will let EL time
  out the turn if a `read_file` takes >25s. Keep your vault sources
  fast.
- **DeepSeek-V3 fallback chain** — prod runs DeepSeek for cheap voice
  responses with Claude fallback. The OSS path uses whichever provider
  `GLOBUS_LLM_PROVIDER` points at (defaults to Claude OAuth proxy —
  good quality, free if you have a Claude Max sub).
- **Word-by-word SSE streaming** — OSS sends the full reply in one
  chunk; EL starts TTS as soon as it arrives. For installs with very
  long replies, port `deepseek_voice_stream()` from `voice_providers.py`
  into the route handler and switch the response generator. Most
  installs won't need this.

## Cost note

ElevenLabs' Conversational AI is billed per minute of audio. Starter
plan = 250 minutes/month; Creator = 1100; Pro = 11000. Plan accordingly.

When the cost matters more than the polish, the v1.0 roadmap migrates
voice to Cartesia (TTS) + Deepgram (STT) + LiveKit (transport) — same
brain, cheaper stack. See [ROADMAP.md](../ROADMAP.md).
