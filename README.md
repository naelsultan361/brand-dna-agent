---
tags: [agentapp, collaboration, writing]
dataset: []
framework: [flower]
---

# Brand-DNA Agent · Team Voice

One team, one voice, every text checked.

Every LLM writes in the same voice. Ask it for a release note, a cover letter or
a customer mail and you get the same enthusiastic, slightly generic text. Teams
that let models write for them slowly lose the voice their readers recognize.

Team Voice is a collaborative Flower `AgentApp`. A team shares it in one
conversation. Every member adds a few of their own texts; the agent distills
one **Voice Contract** per person and merges them into a shared, versioned
**Team Contract**. From then on every text written through the agent is scored
by a **Critic** against that contract before anyone sees it. Below the
threshold the task gets a second attempt with the critic's findings on Flower's
Endeavor model. Nothing ships unscored.

## Commands

Send these as the chat prompt (or `agent.input`):

| Command | What happens |
| --- | --- |
| `add-voice <name>: <text or public URL>` | Distills that person's voice, merges the Team Contract, bumps the version |
| `write: <task>` | Writes the task under the Team Contract, shows the generic model next to it, critic score, second attempt if needed |
| `show` | Prints the current Team Contract, members, version, accepted texts |
| `help` | Lists the commands |

Free text in a conversation that already has a Team Contract is treated as
`write:`.

## Why this is collaborative

- **Shared state across runs.** Contracts live in the run series (`Context`),
  so every member of a federation works on the same Team Contract in the same
  conversation. Each accepted text raises the version.
- **Three agent roles in one run.** Reader (voice analyst), Writer and Critic
  cooperate; the Critic gates the Writer.
- **Connectors.** A voice sample can be a public URL; the agent reads it through
  Flower's built-in `web_fetch` connector inside a bounded tool loop.

## Run on SuperGrid

```shell
uv sync
uv run flwr build
uv run flwr login supergrid
uv run flwr run . supergrid --run-config 'agent.input="help"' --stream
```

For a real conversation use `flwr chat`, select the agent, then:

```
add-voice Nael: <a few sentences of your own writing>
add-voice Jonas: <a few sentences of his writing>
write: Announce to our users that the login bug is fixed. Three sentences.
show
```

Configuration lives in `pyproject.toml` under `[tool.flwr.app.config.agent]`:
`model` (default `openai/gpt-5.6-sol`), `escalation-model` (the second attempt
runs on Flower's `flower-endeavor-v1.0`), `threshold` (default 0.8) and
`max-tool-turns` (bound for `web_fetch`, default 2).

## Run locally with Ollama

```shell
export FLWR_MODEL_API_ENDPOINT="http://127.0.0.1:11434/v1/responses"
uv run flower-superlink --insecure          # terminal 1

uv run python scripts/series_run.py local-agent \
  'add-voice Nael: <text>' 'write: <task>' 'show' \
  --config 'agent.model="gemma4:latest" agent.escalation-model="gemma4:latest"'
```

`scripts/series_run.py` starts each prompt as a run in the same conversation
and prints the chat events, which is how the shared state gets tested without
a SuperGrid login. It needs `[superlink.local-agent]` in `~/.flwr/config.toml`
(see the Flower docs, "Run an AgentApp with a local SuperLink").

## Safety

- **No invention.** Contracts are distilled only from texts the user supplies or
  from a URL the user names. The prompts forbid inventing traits; the critic
  punishes generic phrases and taboo violations.
- **Contracts, not texts.** The conversation state stores the contracts. The
  original texts are discarded after extraction and never written to state or
  logs.
- **Fail closed.** A text below the threshold is not accepted, even after the
  second attempt, and the user is told why.
- **Bounded loops.** At most one second attempt per task; the `web_fetch` loop
  is capped by `max-tool-turns` (hard limit 5); only the requested tool may be
  called.
- **No credentials in the app.** Flower's runtime injects the model endpoint and
  token; the project holds no API keys.

## Project layout

```
agent/agent_app.py      the AgentApp (state, commands, reader, writer, critic)
scripts/series_run.py   local conversation runner for development
pyproject.toml          Flower app metadata and run config
```

Built at the Flower Agent Hackathon, Berlin, 16 September 2026, on top of the
voice pipeline of [Prompt.DNA](https://prompt-dna.pages.dev).
