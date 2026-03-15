---
name: continue
description: "/continue — Resume work after /compact"
---

# /continue — Resume After /compact

Restores context from session files and MEMORY.md, then continues where you left off.

## Usage

```
/continue
```

## Execution Steps

### Step 1: Load Session State

Read these files IN PARALLEL to restore context:

1. **Today's session file** (most important):
   ```
   Read: data/sessions/YYYY-MM-DD-session.md
   ```
   This contains the `/save` output with: what we were doing, active work, VPS status, next steps.

2. **MEMORY.md** (already in system prompt, but reference for current state):
   The "Current State" section has: active bot config, Linear tickets, file structure.

3. **Git state**:
   ```bash
   git -C /Users/koen/Projects/polymarket-bot status --short
   git -C /Users/koen/Projects/polymarket-bot log --oneline -3
   git -C /Users/koen/Projects/polymarket-bot branch --show-current
   ```

### Step 2: Check Compact Summary

The `/compact` command creates a summary that's in your context. Cross-reference it with the session file:
- Session file = reliable (written by /save before /compact)
- Compact summary = supplementary (auto-generated, may miss nuance)

### Step 3: Kill Zombie Processes

```bash
pgrep -f 'claude.*--disallowedTools' | xargs kill 2>/dev/null || true
```

### Step 4: Determine What To Do

**Priority order for deciding next action:**

1. **Session file "Next Steps"** — most reliable, explicitly saved
2. **Session file "What We Were Doing"** — context for current task
3. **Compact summary "Current Work"** — auto-generated context
4. **Compact summary "Pending Tasks"** — if no active task

**Decision rules:**

| Session file says | Compact says | Action |
|-------------------|--------------|--------|
| Clear next step | Anything | Execute next step |
| "Waiting for user decision" | Anything | ASK user |
| Options A/B/C pending | Anything | PRESENT options |
| Mid-implementation | Anything | Resume implementation |
| No session file found | Clear task | Execute from compact |
| No session file found | Unclear | ASK "Waar wil je mee verder?" |

### Step 5: Brief Status + Act

Output a 2-3 line status, then IMMEDIATELY take action:

```
Restored from session save (HH:MM UTC).
[What we were doing]. Continuing with [next step].
```

Then execute — don't wait for permission unless the session file says to ask.

## Key Behavior

| DO | DON'T |
|----|-------|
| Read session file FIRST | Rely only on compact summary |
| Act immediately on clear next steps | Ask "should I continue?" |
| Ask user when session says "waiting for decision" | Guess what user wants |
| Check git for uncommitted work | Assume clean state |

## Fallback

If no session file exists for today:
1. Check yesterday's session file
2. Read MEMORY.md "Current State" section
3. Check git log for recent activity
4. ASK: "Geen session save gevonden. Waar wil je mee verder?"

## Notes

- Works best when `/save` was run before `/compact`
- Without `/save`, falls back to compact summary + MEMORY.md
- Session files: `data/sessions/YYYY-MM-DD-session.md`
