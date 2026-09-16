"""Brand-DNA Agent: a Flower AgentApp that keeps generated text in the user's voice.

Four stages, mirroring the pitch schema:
  01 INPUT      three sample texts from the run config
  02 EXTRACTOR  voice features from the samples
  03 READER     a versioned Voice Contract (JSON), gate 1
  04 GENERATOR  writes the task under the contract
  05 CRITIC     scores the draft against the contract, gate 2;
                below threshold the task escalates to a larger model
The baseline (generic model output without a contract) is produced as well so a
frontend can show both side by side.
"""

import json
import os
import re

from flwr.agentapp import AgentApp, AgentSession
from flwr.app import Context
from openai import OpenAI

app = AgentApp()

CONTRACT_SCHEMA = {
    "version": "1.0",
    "tone": "how the author sounds (e.g. direct, warm, dry)",
    "register": "du/Sie, formal/informal, first person usage",
    "sentence_rhythm": "typical length, active/passive, how sentences open",
    "vocabulary": ["words and phrases the author actually uses"],
    "taboos": ["words, phrases or punctuation the author avoids"],
    "openings_closings": "how texts start and end",
    "language": "the language of the samples",
}


def _cfg(context: Context, key: str, default=None):
    value = context.run_config.get(key, default)
    if value is None:
        raise ValueError(f"{key} must be set in the run config")
    return value


def _ask(client: OpenAI, agent: AgentSession, model: str, stage: str, prompt: str) -> str:
    """One streamed model call; every event is republished to the frontend."""
    agent.events.emit({"type": "brand_dna.stage", "stage": stage, "model": model})
    stream = client.responses.create(model=model, input=prompt, stream=True)
    chunks: list[str] = []
    for event in stream:
        agent.events.emit(event.to_dict())
        if event.type in {"error", "response.failed"}:
            raise RuntimeError(f"Model response failed in {stage}: {event}")
        if event.type == "response.output_text.delta":
            chunks.append(event.delta)
    return "".join(chunks).strip()


def _json_block(text: str) -> dict:
    """Extract the first JSON object from a model answer (tolerates code fences)."""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]}")
    return json.loads(match.group(0))


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    model = str(_cfg(context, "agent.model")).strip()
    escalation_model = str(_cfg(context, "agent.escalation-model", model)).strip()
    threshold = float(_cfg(context, "agent.threshold", 0.8))
    task = str(_cfg(context, "agent.task")).strip()
    samples = [
        str(_cfg(context, f"agent.sample-{i}")).strip() for i in (1, 2, 3)
    ]
    samples = [s for s in samples if s]
    if len(samples) < 1:
        raise ValueError("agent.sample-1 … agent.sample-3 must hold at least one text")

    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
    )

    # 02 EXTRACTOR + 03 READER: samples -> Voice Contract (gate 1)
    joined = "\n\n---\n\n".join(f"SAMPLE {i+1}:\n{s}" for i, s in enumerate(samples))
    contract_raw = _ask(
        client, agent, model, "reader",
        "You are a voice analyst. Read the author's own texts below and distill a "
        "Voice Contract: a compact, machine-readable description of how this person "
        "writes. Describe only what the samples show, invent nothing. Answer with one "
        f"JSON object exactly in this shape:\n{json.dumps(CONTRACT_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        f"{joined}",
    )
    contract = _json_block(contract_raw)
    contract["version"] = "1.0"
    contract["source"] = {"samples": len(samples), "words": sum(len(s.split()) for s in samples)}
    agent.events.emit({"type": "brand_dna.contract", "contract": contract})

    # Baseline for the split-screen: the same task without any contract.
    baseline = _ask(client, agent, model, "baseline", task)

    # 04 GENERATOR + 05 CRITIC with escalation (gate 2)
    contract_json = json.dumps(contract, ensure_ascii=False, indent=2)
    attempts = []
    for attempt, gen_model in enumerate((model, escalation_model), start=1):
        draft = _ask(
            client, agent, gen_model, f"generator-{attempt}",
            "Write the following task strictly in the author's voice as defined by the "
            "Voice Contract. Respect every taboo. Use the contract's language. Output "
            f"only the text, no preamble.\n\nVOICE CONTRACT:\n{contract_json}\n\nTASK:\n{task}",
        )
        critic_raw = _ask(
            client, agent, model, f"critic-{attempt}",
            "You are a strict critic. Score how well the DRAFT follows the VOICE CONTRACT "
            "on a scale from 0.0 (generic model voice) to 1.0 (indistinguishable from the "
            "author). Punish every taboo violation and every generic phrase. Answer with "
            'one JSON object: {"score": <number>, "violations": [<short strings>], '
            '"verdict": "<one sentence>"}\n\n'
            f"VOICE CONTRACT:\n{contract_json}\n\nDRAFT:\n{draft}",
        )
        verdict = _json_block(critic_raw)
        score = float(verdict.get("score", 0.0))
        attempts.append({"model": gen_model, "score": score, "verdict": verdict, "text": draft})
        agent.events.emit({"type": "brand_dna.critic", "attempt": attempt, "model": gen_model, "score": score})
        if score >= threshold:
            break
        if gen_model == escalation_model:
            break  # escalation already used, ship best effort but flag it
        agent.events.emit({"type": "brand_dna.escalation", "from": model, "to": escalation_model, "score": score})

    best = max(attempts, key=lambda a: a["score"])
    result = {
        "contract_version": contract["version"],
        "threshold": threshold,
        "passed": best["score"] >= threshold,
        "escalated": len(attempts) > 1,
        "baseline": baseline,
        "agent": best["text"],
        "score": best["score"],
        "violations": best["verdict"].get("violations", []),
        "attempts": [{"model": a["model"], "score": a["score"]} for a in attempts],
    }
    agent.events.emit({"type": "brand_dna.result", **result})

    print("=== VOICE CONTRACT v" + contract["version"] + " ===")
    print(contract_json)
    print("\n=== GENERIC MODEL (no contract) ===")
    print(baseline)
    print(f"\n=== BRAND-DNA AGENT · score {best['score']:.2f} / threshold {threshold:.2f}"
          f"{' · escalated' if result['escalated'] else ''} ===")
    print(best["text"])
    if result["violations"]:
        print("\nremaining violations: " + "; ".join(map(str, result["violations"])))
