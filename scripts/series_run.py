"""Start one or more runs of this app in ONE conversation (run series).

`flwr run` cannot continue a series; `flwr chat` needs a SuperGrid login. This
script reuses Flower's own CLI building blocks so the shared state of the
Brand-DNA Agent can be tested against a local SuperLink:

    uv run python scripts/series_run.py local-agent \
        'add-voice Nael: <text>' 'write: <task>' 'show' \
        --config 'agent.model="gemma4:latest"'

Every prompt becomes a run in the same series; text deltas are printed live.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from flwr.cli.build import build_fab_from_disk
from flwr.cli.chat.chat_app import parse_task_event
from flwr.common.config import parse_config_args
from flwr.cli.flower_config import read_superlink_connection
from flwr.cli.utils import init_http_client_from_connection
from flwr.supercore.fab import Fab
from flwr.common.serde import fab_to_proto, user_config_to_proto
from flwr.proto.control_pb2 import StartRunRequest, StreamRunEventsRequest

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"brand_dna.result", "brand_dna.contract", "run.finished"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("superlink")
    ap.add_argument("prompts", nargs="+")
    ap.add_argument("--config", action="append", default=[])
    ap.add_argument("--series", type=int, default=None, help="continue an existing series")
    args = ap.parse_args()

    connection = read_superlink_connection(args.superlink)
    client = init_http_client_from_connection(connection)
    fab_bytes = build_fab_from_disk(ROOT)
    fab = Fab(hashlib.sha256(fab_bytes).hexdigest(), fab_bytes, {})

    series_id = args.series
    for prompt in args.prompts:
        # parse_config_args keeps only one list element, so join all pairs into one string
        safe = prompt.replace('"', "'")
        overrides = parse_config_args([" ".join(args.config + [f'agent.input="{safe}"'])])
        req = StartRunRequest(
            fab=fab_to_proto(fab),
            override_config=user_config_to_proto(overrides),
            federation=connection.federation or "",
        )
        if series_id is not None:
            req.series_id = series_id
        res = client.StartRun(req)
        if res.HasField("series_id"):
            series_id = res.series_id
        print(f"\n━━━ run {res.run_id} · series {series_id} · «{prompt[:60]}»\n")

        for msg in client.StreamRunEvents(StreamRunEventsRequest(run_id=res.run_id)):
            event_type, payload = parse_task_event(msg.task_event)
            if event_type == "response.output_text.delta":
                print(payload.get("delta", ""), end="", flush=True)
            elif event_type.startswith("brand_dna."):
                print(f"\n  ⟶ {event_type} {({k: v for k, v in payload.items() if k not in {'type', 'baseline', 'text'}})}")
            elif event_type in {"error", "response.failed"}:
                print(f"\n  ✗ {payload}")
        print()


if __name__ == "__main__":
    main()
