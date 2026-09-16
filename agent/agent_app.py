"""Brand-DNA Agent / Team Voice: a collaborative Flower AgentApp.

A team shares this agent in one conversation (run series). Every member adds
their own texts; the agent distills one Voice Contract per person and a shared,
versioned Team Contract. From then on every text written through the agent is
scored against the Team Contract by a critic before it reaches the user; if the
score stays below the threshold the task escalates once to a larger model.

Commands (sent as the chat prompt / `agent.input`):
  add-voice <name>: <text or public URL>   distill a voice, update the team contract
  write: <task>                            write under the team contract, scored
  show                                     current team contract, members, version
  help                                     this list
Free text with an existing team contract is treated as `write:`.

Safety: contracts are built only from texts the user supplies (or a URL the user
names, read through Flower's `web_fetch` connector); the conversation state keeps
contracts, never the original texts; every loop is bounded; the critic fails
closed.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from flwr.agentapp import AgentApp, AgentSession
from flwr.app import ConfigRecord, Context
from openai import OpenAI

app = AgentApp()

STATE_KEY = "team_voice"
MAX_TOOL_TURNS_LIMIT = 5
URL_RE = re.compile(r"^https?://\S+$")

CONTRACT_SCHEMA = {
    "tone": "how the author sounds (e.g. direct, warm, dry)",
    "register": "du/Sie, formal/informal, first person usage",
    "sentence_rhythm": "typical length, active/passive, how sentences open",
    "vocabulary": ["words and phrases the author actually uses"],
    "taboos": ["words, phrases or punctuation the author avoids"],
    "openings_closings": "how texts start and end",
    "language": "the language of the samples",
}


# ---------------------------------------------------------------- state ----


def load_state(context: Context) -> dict[str, Any]:
    """Read the shared team state from the run series (empty on first run)."""
    record = context.state.config_records.get(STATE_KEY)
    if record is None:
        return {"voices": [], "team": None, "version": "0.0", "accepted": 0}
    voices = [json.loads(v) for v in record.get("voices", [])]
    team_raw = record.get("team", "")
    return {
        "voices": voices,
        "team": json.loads(team_raw) if team_raw else None,
        "version": str(record.get("version", "0.0")),
        "accepted": int(record.get("accepted", 0)),
    }


def save_state(context: Context, state: dict[str, Any]) -> None:
    """Persist contracts (never raw texts) for the next run in the series."""
    context.state.config_records[STATE_KEY] = ConfigRecord(
        {
            "voices": [json.dumps(v, ensure_ascii=False) for v in state["voices"]],
            "team": json.dumps(state["team"], ensure_ascii=False) if state["team"] else "",
            "version": state["version"],
            "accepted": state["accepted"],
        }
    )


def bump(version: str, minor: bool) -> str:
    major, _, rest = version.partition(".")
    if minor:
        return f"{int(major)}.{int(rest or 0) + 1}"
    return f"{int(major) + 1}.0"


# ---------------------------------------------------------------- helpers --


def cfg(context: Context, key: str, default: Any = None) -> Any:
    value = context.run_config.get(key, default)
    if value is None:
        raise ValueError(f"{key} must be set in the run config")
    return value


def say(agent: AgentSession, text: str) -> None:
    """Write our own markdown into the chat transcript."""
    agent.events.emit({"type": "response.output_text.delta", "delta": text})


def ask(
    client: OpenAI,
    agent: AgentSession,
    model: str,
    stage: str,
    prompt: str,
    *,
    show: bool,
) -> str:
    """One streamed model call. `show` streams the text into the chat."""
    agent.events.emit({"type": "brand_dna.stage", "stage": stage, "model": model})
    stream = client.responses.create(model=model, input=prompt, stream=True)
    chunks: list[str] = []
    for event in stream:
        if event.type in {"error", "response.failed"}:
            raise RuntimeError(f"Model response failed in {stage}: {event}")
        if event.type == "response.output_text.delta":
            chunks.append(event.delta)
            if show:
                agent.events.emit(event.to_dict())
    return "".join(chunks).strip()


def json_block(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]}")
    return json.loads(match.group(0))


# ---------------------------------------------------------------- render ---


def pills(items: Any) -> str:
    """Render a list as inline-code pills: `a` `b` `c`."""
    if isinstance(items, str):
        items = [items]
    words = [str(i).strip() for i in (items or []) if str(i).strip()]
    return " ".join(f"`{w}`" for w in words) or "_none found_"


def field(label: str, value: Any) -> str:
    text = str(value or "").strip()
    return f"**{label}:** {text}\n\n" if text else ""


def render_voice(name: str, contract: dict) -> str:
    """One member's Voice Contract as a readable card."""
    return (
        f"### Voice of {name}\n\n"
        + field("Tone", contract.get("tone"))
        + field("Register", contract.get("register"))
        + field("Rhythm", contract.get("sentence_rhythm"))
        + field("Openings and closings", contract.get("openings_closings"))
        + f"**Vocabulary:** {pills(contract.get('vocabulary'))}\n\n"
        + f"**Taboos:** {pills(contract.get('taboos'))}\n\n"
    )


def render_contract(team: dict, version: str, voices: list[dict], accepted: int, *, with_json: bool = True) -> str:
    """The Team Contract as a readable card, machine-readable JSON below it on request."""
    members = ", ".join(v["name"] for v in voices) or ", ".join(team.get("members", []))
    out = (
        f"## Team Contract v{version}\n\n"
        f"**Members:** {members} · **Accepted texts:** {accepted}\n\n"
        + field("Tone", team.get("tone"))
        + field("Register", team.get("register"))
        + field("Rhythm", team.get("sentence_rhythm"))
        + field("Openings and closings", team.get("openings_closings"))
        + f"**Vocabulary:** {pills(team.get('vocabulary'))}\n\n"
        + f"**Taboos:** {pills(team.get('taboos'))}\n\n"
    )
    notes = team.get("individual_notes") or {}
    if isinstance(notes, dict) and notes:
        out += "**Individual notes:**\n\n" + "".join(
            f"- **{who}:** {str(note).strip()}\n" for who, note in notes.items()
        ) + "\n"
    if with_json:
        out += (
            "_machine-readable contract_\n\n"
            f"```json\n{json.dumps(team, ensure_ascii=False, indent=2)}\n```\n"
        )
    return out


def fetch_text(
    client: OpenAI, agent: AgentSession, model: str, url: str, max_turns: int
) -> str:
    """Read a public page through Flower's web_fetch connector (bounded loop)."""
    tools = agent.connectors.tools(["web_fetch"])
    allowed = {t["name"] for t in tools if isinstance(t.get("name"), str)}
    items: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": (
                f"Fetch {url} and return only the author's own prose from that page "
                "as plain text. Drop navigation, boilerplate, comments by others."
            ),
        }
    ]
    for _ in range(max_turns):
        response = client.responses.create(
            model=model, input=items, tools=tools, tool_choice="auto"
        )
        output = [item.to_dict() for item in response.output]
        calls = [i for i in output if i.get("type") == "function_call"]
        if not calls:
            break
        outputs = []
        for call in calls:
            if call.get("name") not in allowed:
                outputs.append(
                    {"type": "function_call_output", "call_id": call["call_id"],
                     "output": json.dumps({"error": "tool not exposed"})}
                )
                continue
            try:
                outputs.append(agent.connectors.call(call))
            except (RuntimeError, ValueError) as exc:
                outputs.append(
                    {"type": "function_call_output", "call_id": call["call_id"],
                     "output": json.dumps({"error": str(exc)})}
                )
        items.extend(output)
        items.extend(outputs)
    final = client.responses.create(
        model=model,
        input=items,
        instructions="Return only the author's prose from the fetched page as plain text.",
    )
    return final.output_text.strip()


# ---------------------------------------------------------------- stages ---


def distill_voice(client: OpenAI, agent: AgentSession, model: str, name: str, text: str) -> dict:
    raw = ask(
        client, agent, model, "reader",
        "You are a voice analyst. Read the author's own text below and distill a "
        "Voice Contract: a compact, machine-readable description of how this person "
        "writes. Describe only what the text shows, invent nothing. Spelling conventions "
        "such as ae/ue/oe instead of umlauts or a missing sharp s come from the input device, "
        "so treat them neither as a taboo nor as a style trait. Answer with one "
        f"JSON object exactly in this shape:\n{json.dumps(CONTRACT_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        f"AUTHOR: {name}\nTEXT:\n{text}",
        show=False,
    )
    contract = json_block(raw)
    return {"name": name, "contract": contract, "words": len(text.split())}


def merge_team(client: OpenAI, agent: AgentSession, model: str, voices: list[dict]) -> dict:
    if len(voices) == 1:
        team = dict(voices[0]["contract"])
        team["members"] = [voices[0]["name"]]
        team["individual_notes"] = {}
        return team
    raw = ask(
        client, agent, model, "team",
        "You are a voice analyst for a team. Below are Voice Contracts of several team "
        "members. Derive ONE Team Contract with exactly the top-level keys of SHAPE (no wrapper object) that captures what "
        "they share, so that any member can write for the team and it still sounds like "
        "the team. Add a field \"individual_notes\": an object mapping member name to one "
        "sentence with that person's distinctive deviation. Add \"members\": list of names. "
        "Use only what the contracts contain, invent nothing. Answer with one JSON object.\n\n"
        f"SHAPE:\n{json.dumps(CONTRACT_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        + "\n\n".join(
            f"MEMBER {v['name']}:\n{json.dumps(v['contract'], ensure_ascii=False)}" for v in voices
        ),
        show=False,
    )
    team = flatten_contract(json_block(raw))
    team.setdefault("members", [v["name"] for v in voices])
    return team


def flatten_contract(team: dict) -> dict:
    """Hoist the contract fields to the top level if the model nested them
    (e.g. under "team_contract"); keeps members and individual_notes."""
    if "tone" in team:
        return team
    for key, value in list(team.items()):
        if isinstance(value, dict) and "tone" in value:
            flat = dict(value)
            for extra in ("members", "individual_notes"):
                if extra in team:
                    flat.setdefault(extra, team[extra])
            return flat
    return team


def write_and_check(
    client: OpenAI, agent: AgentSession, model: str, escalation_model: str,
    threshold: float, team: dict, version: str, task: str,
) -> dict:
    contract_json = json.dumps(team, ensure_ascii=False, indent=2)

    say(agent, "### Generic model (no contract)\n\n")
    baseline = ask(client, agent, model, "baseline", task, show=True)

    attempts: list[dict] = []
    feedback = ""
    for attempt, gen_model in enumerate((model, escalation_model), start=1):
        if attempt == 1:
            label = "Brand-DNA Agent"
        elif gen_model != model:
            label = f"Brand-DNA Agent · escalated to `{gen_model}`"
        else:
            label = "Brand-DNA Agent · revised with critic feedback"
        say(agent, f"\n\n### {label} · Team Contract v{version}\n\n")
        draft = ask(
            client, agent, gen_model, f"generator-{attempt}",
            "Write the following task strictly in the team's voice as defined by the "
            "Team Contract. Respect every taboo. Use the contract's language. Output only "
            f"the text, no preamble.\n\nTEAM CONTRACT:\n{contract_json}\n\nTASK:\n{task}{feedback}",
            show=True,
        )
        verdict_raw = ask(
            client, agent, model, f"critic-{attempt}",
            "You are a strict critic. Score how well the DRAFT follows the TEAM CONTRACT "
            "from 0.0 (generic model voice) to 1.0 (indistinguishable from the team). "
            "Punish every taboo violation and every generic phrase. Leave the spelling of "
            "umlauts and sharp s out of the score; that comes from the keyboard, not the voice. "
            "Answer with one JSON "
            'object: {"score": <number>, "violations": [<short strings>], "verdict": "<one sentence>"}'
            f"\n\nTEAM CONTRACT:\n{contract_json}\n\nDRAFT:\n{draft}",
            show=False,
        )
        verdict = json_block(verdict_raw)
        score = float(verdict.get("score", 0.0))
        violations = [str(v) for v in verdict.get("violations", [])]
        attempts.append({"model": gen_model, "score": score, "verdict": verdict, "text": draft})
        agent.events.emit({"type": "brand_dna.critic", "attempt": attempt, "model": gen_model, "score": score})
        mark = "✅" if score >= threshold else "❌"
        say(agent, f"\n\n**Critic:** {score:.2f} / {threshold:.2f} {mark}\n\n_{verdict.get('verdict', '')}_\n")
        if score >= threshold or attempt == 2:
            break
        agent.events.emit({"type": "brand_dna.escalation", "from": model, "to": escalation_model, "score": score})
        say(agent, f"\n_Below threshold. {'Escalating to `' + escalation_model + '`' if escalation_model != model else 'Revising'} with the critic's findings._\n")
        feedback = "\n\nA previous draft failed the critic. Fix these violations: " + "; ".join(violations)

    best = max(attempts, key=lambda a: a["score"])
    violations = best["verdict"].get("violations", [])
    if violations:
        say(agent, "\n**Remaining violations:**\n\n" + "".join(f"- {v}\n" for v in map(str, violations)))
    return {
        "baseline": baseline,
        "text": best["text"],
        "score": best["score"],
        "passed": best["score"] >= threshold,
        "escalated": len(attempts) > 1,
        "violations": violations,
    }


# ---------------------------------------------------------------- main -----


HELP = (
    "## Brand-DNA Agent · Team Voice\n\n"
    "One team, one voice, every text checked.\n\n"
    "- `add-voice <name>: <text or public URL>` distills that person's voice and updates the Team Contract\n"
    "- `write: <task>` writes under the Team Contract; a critic scores it before you see it\n"
    "- `show` prints the current Team Contract, members and version\n\n"
    "Start with `add-voice` for each team member, then `write:`. Once a Team Contract exists, "
    "free text counts as `write:`.\n\n"
    "The agent stores contracts only; your original texts are read once and then discarded.\n"
)


def parse_command(prompt: str, has_team: bool) -> tuple[str, str, str]:
    """Return (command, name, payload)."""
    text = prompt.strip()
    low = text.lower()
    if low in {"help", "?"}:
        return "help", "", ""
    if low in {"show", "status", "contract"}:
        return "show", "", ""
    m = re.match(r"^add[- _]?voice\s+([^:]+?)\s*:\s*(.+)$", text, flags=re.S | re.I)
    if m:
        return "add-voice", m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^write\s*:\s*(.+)$", text, flags=re.S | re.I)
    if m:
        return "write", "", m.group(1).strip()
    if has_team:
        return "write", "", text
    return "help", "", ""


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    try:
        _main(agent, context)
    except Exception as exc:  # surface the failure in the chat, then fail the run
        say(agent, f"\n\n**Run failed:** {exc}\n")
        agent.events.emit({"type": "error", "error": {"message": str(exc)}})
        raise
    # Our own markdown has no model stream behind it, so close the chat turn explicitly.
    agent.events.emit({"type": "response.completed"})


def _main(agent: AgentSession, context: Context) -> None:
    prompt = str(cfg(context, "agent.input")).strip()
    model = str(cfg(context, "agent.model")).strip()
    escalation_model = str(cfg(context, "agent.escalation-model", model)).strip()
    threshold = float(cfg(context, "agent.threshold", 0.8))
    max_turns = int(cfg(context, "agent.max-tool-turns", 2))
    if not 0 <= max_turns <= MAX_TOOL_TURNS_LIMIT:
        raise ValueError(f"agent.max-tool-turns must be between 0 and {MAX_TOOL_TURNS_LIMIT}")

    state = load_state(context)
    command, name, payload = parse_command(prompt, state["team"] is not None)

    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
    )

    if command == "help":
        say(agent, HELP)
        if state["team"] is not None:
            say(agent, f"\nTeam Contract v{state['version']} exists with {len(state['voices'])} member(s).\n")
        print(HELP)
        return

    if command == "show":
        if state["team"] is None:
            say(agent, "No Team Contract yet. Add a voice first: `add-voice <name>: <text>`\n")
            return
        out = render_contract(state["team"], state["version"], state["voices"], state["accepted"])
        say(agent, out)
        print(out)
        return

    if command == "add-voice":
        source = "url" if URL_RE.match(payload) else "text"
        if source == "url":
            say(agent, f"Reading {payload} through `web_fetch` …\n\n")
            try:
                text = fetch_text(client, agent, model, payload, max_turns)
            except Exception as exc:  # connector unavailable in this runtime
                say(agent, f"Could not read the URL here ({exc}). Paste the text instead.\n")
                return
        else:
            text = payload
        if len(text.split()) < 15:
            say(agent, "That is too little text to read a voice from. Give me at least a few sentences.\n")
            return

        say(agent, f"Distilling **{name}**'s voice from {len(text.split())} words …\n\n")
        voice = distill_voice(client, agent, model, name, text)
        voice["source"] = source
        state["voices"] = [v for v in state["voices"] if v["name"].lower() != name.lower()] + [voice]
        state["team"] = merge_team(client, agent, model, state["voices"])
        state["version"] = bump(state["version"], minor=state["version"] != "0.0")
        save_state(context, state)
        agent.events.emit({"type": "brand_dna.contract", "version": state["version"], "members": [v["name"] for v in state["voices"]]})

        out = render_voice(name, voice["contract"])
        if len(state["voices"]) == 1:
            out += f"## Team Contract v{state['version']}\n\nStarts as {name}'s voice. Add more members and the contract becomes the team's.\n\n"
        else:
            out += render_contract(state["team"], state["version"], state["voices"], state["accepted"], with_json=False)
        out += "\n_Stored: the contract. Discarded: the original text. `show` prints the full contract._\n"
        say(agent, out)
        print(out)
        return

    # command == "write"
    if state["team"] is None:
        say(agent, "No Team Contract yet. Add a voice first: `add-voice <name>: <text>`\n")
        return
    result = write_and_check(
        client, agent, model, escalation_model, threshold, state["team"], state["version"], payload
    )
    if result["passed"]:
        state["accepted"] += 1
        state["version"] = bump(state["version"], minor=True)
        save_state(context, state)
        say(agent, f"\n_Accepted. Team Contract → v{state['version']}._\n")
    else:
        say(agent, "\n_Not accepted: still below threshold after the second attempt. Nothing ships unscored._\n")
    agent.events.emit({"type": "brand_dna.result", "version": state["version"], **{k: v for k, v in result.items() if k != "baseline"}})
    print(json.dumps({"score": result["score"], "passed": result["passed"], "escalated": result["escalated"], "version": state["version"]}))
