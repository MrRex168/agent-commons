"""Use an explicitly enrolled grant to analyze synthetic data."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from agent_commons.cli import LocalAccount, _directory, _read
from agent_commons.client import CommonsClient, HistoryWriteError
from agent_commons.outbound_a2a import BoundGrant

SKILL = "analysis.summary@1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--grant", type=UUID, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--deadline", required=True)
    args = parser.parse_args()
    try:
        with _directory(args.config) as directory:
            account = LocalAccount.model_validate_json(_read("account.json", directory=directory))
            grant = BoundGrant.model_validate_json(
                _read(f"peer-{args.grant}.json", directory=directory)
            )
        if grant.grant.grant_id != args.grant:
            raise ValueError
        with CommonsClient(account.origin, api_key=account.api_key) as client:
            cursor = None
            for _ in range(10):
                page = client.discover_known(skill=SKILL, cursor=cursor)
                if any(
                    peer.identity_verified
                    and peer.root_fingerprint == grant.destination.peer_root
                    and peer.a2a_url == grant.destination.endpoint
                    for peer in page.agents
                ):
                    break
                cursor = page.next_cursor
                if cursor is None:
                    raise ValueError
            else:
                raise ValueError
            task = client.delegate(
                grant=grant,
                skill=SKILL,
                data={"values": [2, 3, -1]},
                message_id=args.message_id,
                deadline=datetime.fromisoformat(args.deadline),
            )
            if task.status in ("submitted", "working"):
                task = client.wait_for_task(task.reference, grant=grant)
            history = client.list_collaboration_history()
            print(
                json.dumps(
                    {
                        "task_id": str(task.id),
                        "status": task.status,
                        "artifact": task.artifact,
                        "recent_history_count": len(history.records),
                    }
                )
            )
            return 0 if task.status == "completed" else 1
    except HistoryWriteError as exc:
        print(f"Peer returned task {exc.task.id}. Its local history write failed.", file=sys.stderr)
    except Exception:
        print(
            "Typed workflow failed. Keep the original message ID and deadline for retries.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
