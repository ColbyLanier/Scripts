---
title: Voice Chat Design
project: token-api
status: active
created: 2026-03-02
---

# Voice Chat for Claude Code

## Problem

Claude Code's interaction model is text-in/text-out. The existing TTS system is notification-only (stop hooks, alerts). There's no conversational voice loop — Claude can't speak responses and receive spoken input in a continuous flow.

## User Setup

- **STT**: Wispr Flow (local, high-quality) + lavalier mic + Bluetooth ring dictation remote
- **TTS**: Token-API system — Windows SAPI (9 accent-profiled voices) with Mac `say` fallback, queue-based
- **Discord**: Fully operational message bot (discord.js v14, admin perms, MessageContent intent, SSE streaming, ask/wait/poll). NO voice capability currently (no @discordjs/voice, no opus).
- **Personas**: Mechanicus (autonomous), Custodes (conversational), Inquisition (Minimax fleet)

## Requirements

1. Claude speaks responses via TTS (conversational, not just notifications)
2. User responds by voice (dictation → text)
3. Continuous back-and-forth loop with natural turn-taking
4. Multi-persona support (different voices per persona)
5. Optional headless mode (no terminal required)
6. Chat history visible in TUI when needed

---

## Approach 1: AskUserQuestion + TTS Loop (Phase 1 — Ship Now)

### Architecture

```
┌─────────────────────────────────────────────────┐
│ Claude Code Session                             │
│                                                 │
│  1. Claude processes input                      │
│  2. Claude calls /api/notify/tts (non-blocking  │
│     narration — "here's what I found...")        │
│  3. Claude calls AskUserQuestion (blocking)     │
│     with TTS hook on the question text           │
│  4. User dictates via Wispr Flow (continuous)   │
│  5. User presses Enter when ready to submit     │
│  6. → back to step 1                            │
└─────────────────────────────────────────────────┘
```

### Key Mechanics

**Two TTS modes:**
- **Non-blocking speak**: `POST /api/notify/tts` — Claude narrates while working (results, thinking out loud)
- **Blocking ask**: AskUserQuestion with TTS hook — speaks the question, waits for response

**Dictation flow (type-ahead):**
- User dictates continuously via Wispr Flow (Bluetooth ring to start/stop)
- Wispr types into whatever is focused (the AskUserQuestion input)
- User presses Enter whenever ready — if prompt hasn't appeared yet, Enter buffers
- Between questions, dictation continues accumulating; no need to stop/start

**Background auto-fill script:**
- Detects AskUserQuestion prompt appearance
- Auto-selects "Write your own answer" option
- Keeps cursor in text input field for Wispr to type into
- Possibly auto-submits on Enter keypress

### What Exists vs What's New

| Component | Status |
|-----------|--------|
| TTS system (SAPI + Mac) | Exists, mature |
| TTS queue + profiles | Exists |
| AskUserQuestion tool | Exists in Claude Code |
| TTS hook on AskUserQuestion | Needs hook config |
| Auto-fill background script | **New** — AHK or terminal script |
| Type-ahead Enter buffering | **New** — part of auto-fill script |
| Voice conversation skill | **New** — skill that drives the loop |

### Pros
- Ships fast — mostly existing infrastructure
- Wispr Flow handles all STT complexity
- Natural turn-taking via blocking AskUserQuestion
- Works in any Claude Code session

### Cons
- Requires terminal focus (not headless)
- Single-persona (whatever voice the instance has)
- Auto-fill script needs platform-specific work (AHK on Windows/WSL)

---

## Approach 2: Discord Voice Channel (Phase 2 — Mechanicus Project)

### Architecture

```
┌──────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│ Discord App  │────▶│ Discord Bot (daemon) │────▶│ Token-API        │
│ Voice Channel│◀────│ + @discordjs/voice   │◀────│ Conversation Eng │
│              │     │ + Whisper STT        │     │                  │
│ User speaks  │     │ Transcribe → forward │     │ Claude processes │
│ Bot speaks   │     │ Receive → TTS → play │     │ → response text  │
└──────────────┘     └─────────────────────┘     └──────────────────┘
```

### Key Mechanics

**Bot joins voice channel:**
- New `@discordjs/voice` dependency + opus codec + sodium
- Bot connects to designated voice channel
- Receives user audio stream → pipes to Whisper STT
- Transcribed text forwarded to Token-API conversation engine

**Bot speaks responses:**
- Receive response text from conversation engine
- Run through TTS (SAPI/Mac `say`) → audio file
- Play audio file into Discord voice channel via `AudioPlayer`

**Multi-persona:**
- Each persona (Mechanicus, Custodes, Inquisition) is a separate bot account
- Each has distinct voice profile (already mapped in TTS profiles)
- User talks to specific persona by joining their voice channel or @mentioning

**Headless advantage:**
- No terminal needed — user speaks into Discord from phone/desktop/anywhere
- Chat history appears in Discord text channel alongside voice
- TUI gets a "voice chat" panel showing recent transcript

### New Components

| Component | Complexity |
|-----------|------------|
| @discordjs/voice integration | Medium — well-documented library |
| Opus/sodium native deps | Low — npm install |
| VoiceStates gateway intent | Low — config change |
| Whisper STT pipeline | Medium — local Whisper or API |
| Audio file generation from TTS | Medium — pipe SAPI/say output to file |
| Conversation engine (API) | Medium — new endpoint managing conversation state |
| TUI voice chat panel | Low — new panel page |

### Pros
- Headless — talk from anywhere (phone, desktop, walking around)
- Multi-persona native — each bot has its own voice channel presence
- Chat history in Discord + TUI
- Thematically perfect (Mechanicus lives in Discord)
- Fun factor is high

### Cons
- Significant new code in discord-daemon
- STT latency (Whisper processing)
- Audio quality through Discord compression
- More moving parts (voice connection stability)

---

## Approach 3: Hybrid (Recommended)

Ship Phase 1 immediately. Write Phase 2 spec for autonomous agents.

### Phase 1 Deliverables (This Week)
1. **Voice conversation skill** — Claude Code skill that drives the TTS + AskUserQuestion loop
2. **TTS hook on AskUserQuestion** — hook config that speaks question text via TTS
3. **Auto-fill script** — background process handling the "Other" selection + Enter buffering
4. **Non-blocking TTS calls** — Claude narrates freely between questions

### Phase 2 Spec (Mechanicus Overnight)
1. **@discordjs/voice integration** — bot joins/leaves voice channels
2. **Whisper STT pipeline** — transcribe Discord audio stream
3. **Audio TTS output** — generate audio files from existing TTS for playback
4. **Conversation engine endpoint** — `POST /api/voice/conversation` managing state
5. **TUI voice panel** — transcript display in info panel rotation

### Shared Components
Both phases use:
- Same TTS voice profiles and queue
- Same conversation state management
- Same response generation (Claude API or Claude Code session)

### Minimax Token Sink
The Inquisition persona could use Minimax for:
- Secondary STT processing (redundancy/comparison)
- Voice synthesis (Minimax has TTS APIs — alternative voice generation)
- Conversation summarization (cheaper than Claude for transcript processing)

---

## Open Questions

1. **AHK or terminal-native?** The auto-fill script for Phase 1 — is AHK the right tool or should it be a terminal-native solution?
2. **Whisper deployment** — local whisper.cpp, OpenAI Whisper API, or Minimax STT for Phase 2?
3. **Conversation state** — should voice chat sessions persist as session documents, or a new `voice_conversations` table?
4. **Wake word** — should there be a "hey Claude" trigger or is the Bluetooth ring sufficient for Phase 1?
5. **Multiple simultaneous personas** — can user talk to Custodes and Mechanicus in same voice channel, or separate channels?

---

## Activity Log

### 2026-03-02 18:30 -- voice-chat-brainstorm
Initial brainstorming session. Explored existing Discord integration (message-only, no voice),
TTS system (mature, 9 SAPI voices + Mac fallback), and user's STT setup (Wispr Flow + lav mic + BT ring).
Proposed 3 approaches: TTS loop (immediate), Discord voice (autonomous project), hybrid (recommended).
Key insight: AskUserQuestion blocking behavior IS the turn-taking mechanism for Phase 1.
Key insight: Discord voice is architecturally correct long-term because personas already live there.
