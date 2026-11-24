"""Serve a previously generated Agent Internet Live evidence file on loopback."""

import argparse
import json
import os
import stat
from html import escape
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_EVIDENCE_BYTES = 16_384
Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
RecordId = Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
LocationSequence = Annotated[int, Field(ge=1, le=2**53 - 1)]
EndpointStatus = Literal["authorized", "pending", "expired", "non_sovereign"]
Node = Literal["atlas", "nova-source", "nova-target"]
NODES = ["atlas", "nova-source", "nova-target"]
ROUTES = [
    ("before", "atlas", "nova-source"),
    ("before", "nova-source", "atlas"),
    ("after", "atlas", "nova-target"),
]
LABELS = {"atlas": "Server A", "nova-source": "Server B", "nova-target": "Server C"}
CHECK_LABELS = {
    "database_isolation": "Independent databases",
    "bad_credentials_rejected": "Invalid credential rejected",
    "wrong_peer_rejected": "Wrong peer rejected",
    "expired_credential_rejected": "Expired credential rejected",
    "invalid_signature_rejected": "Invalid signature rejected",
    "stale_location_rejected": "Stale location rejected",
    "idempotent_retry": "Duplicate returned original task",
    "conflicting_retry_rejected": "Conflicting retry rejected",
    "follow_alone_denied": "Follow alone denied task access",
    "moved_location_requires_new_enrollment": "Destination required new enrollment",
    "cleanup_completed": "Fixture resources cleaned up",
}
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Identities(StrictModel):
    atlas: Fingerprint
    nova: Fingerprint


class Exchange(StrictModel):
    phase: Literal["before", "after"]
    source: Node
    destination: Node
    task_id: RecordId
    state: Literal[
        "submitted",
        "working",
        "input-required",
        "completed",
        "canceled",
        "failed",
        "rejected",
        "auth-required",
        "unknown",
    ]
    retrieved: bool

    @property
    def completed(self) -> bool:
        return self.state == "completed" and self.retrieved


class Migration(StrictModel):
    root_fingerprint: Fingerprint
    reference_id_before: RecordId
    reference_id_after: RecordId
    follow_id_before: RecordId
    follow_id_after: RecordId
    reverse_follow_id: RecordId
    location_sequence_before: LocationSequence
    location_sequence_after: LocationSequence
    endpoint_authorization_before: EndpointStatus
    endpoint_authorization_after: EndpointStatus
    old_task_endpoint_retired: bool
    exclusive_control_verified: bool


class Checks(StrictModel):
    database_isolation: bool
    bad_credentials_rejected: bool
    wrong_peer_rejected: bool
    expired_credential_rejected: bool
    invalid_signature_rejected: bool
    stale_location_rejected: bool
    idempotent_retry: bool
    conflicting_retry_rejected: bool
    follow_alone_denied: bool
    moved_location_requires_new_enrollment: bool
    cleanup_completed: bool


class Evidence(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    topology: Literal["isolated-loopback-https"]
    nodes: list[Node] = Field(min_length=3, max_length=3)
    identities: Identities
    exchanges: list[Exchange] = Field(min_length=3, max_length=3)
    migration: Migration
    checks: Checks

    @model_validator(mode="after")
    def matches_demo_topology(self):
        if (
            self.nodes != NODES
            or [(task.phase, task.source, task.destination) for task in self.exchanges] != ROUTES
        ):
            raise ValueError("Unexpected demo topology or task ordering")
        if len({task.task_id for task in self.exchanges}) != 3:
            raise ValueError("Duplicate task IDs")
        if self.identities.atlas == self.identities.nova:
            raise ValueError("Expected distinct agent fingerprints")
        if self.migration.exclusive_control_verified:
            raise ValueError("This fixture cannot establish exclusive control")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate evidence field")
        value[key] = item
    return value


def load_evidence(path: Path) -> Evidence:
    # The configured path is the only input. Never open symlinks or block on a FIFO.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as evidence:
        metadata = os.fstat(evidence.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("Unsupported evidence file")
        raw = evidence.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("Evidence exceeds size limit")
    return Evidence.model_validate(
        json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    )


def _code(value: str | int) -> str:
    return f"<code>{escape(str(value))}</code>"


def _badge(passed: bool, positive: str, negative: str) -> str:
    css = "good" if passed else "caution"
    return f'<span class="badge {css}">{escape(positive if passed else negative)}</span>'


def _layout(content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Internet Live · Evidence</title>
<link rel="stylesheet" href="/dashboard.css">
</head>
<body>
<header class="mast"><div class="wrap brand">
  <div class="brand-name"><span class="brand-mark" aria-hidden="true"></span>Agent Commons</div>
  <span class="mode">Recorded integration evidence</span>
</div></header>
<main class="wrap">{content}</main>
<footer class="wrap footer">
  Agent Commons <span aria-hidden="true">/</span> Agent Internet Live
</footer>
</body></html>"""


def _topology(migration: Migration) -> str:
    before, after = migration.endpoint_authorization_before, migration.endpoint_authorization_after
    cards = [
        ("A", "Atlas", "Original caller", "Atlas remains on this instance."),
        ("B", "Nova", "Source instance", "Nova’s recorded location before migration."),
        ("C", "Nova", "Destination instance", "Nova’s recorded location after migration."),
    ]
    tiles = "".join(
        f'<article class="node"><span class="node-key" aria-hidden="true">{escape(key)}</span>'
        f'<div><span class="node-label">Server {escape(key)} · {escape(role)}</span>'
        f"<h3>{escape(name)}</h3><p>{escape(description)}</p></div></article>"
        for key, name, role, description in cards
    )
    return f"""<section class="panel" aria-labelledby="topology">
  <div class="section-head"><h2 id="topology">Server topology</h2>
    <span class="muted">Three isolated HTTPS instances</span></div>
  <div class="nodes">{tiles}</div>
  <div class="location">
    <div><strong>Nova’s signed location sequence</strong>
      <p>Server B {_code(before)} · Server C {_code(after)}</p></div>
    <div class="sequence">{_code(migration.location_sequence_before)}
      <span aria-label="to">→</span> {_code(migration.location_sequence_after)}</div>
  </div>
</section>"""


def _tasks(exchanges: list[Exchange], *, post_migration: bool = False) -> str:
    section_id = "post-migration" if post_migration else "exchanges"
    heading = "Post-migration task" if post_migration else "Authenticated A2A exchanges"
    rows = []
    for task in exchanges:
        phase = "After migration" if task.phase == "after" else "Before migration"
        outcome = _badge(task.completed, "Completed and retrieved", "Task check incomplete")
        route = f"{LABELS[task.source]} → {LABELS[task.destination]}"
        retrieval = "Retrieved" if task.retrieved else "Retrieval unconfirmed"
        rows.append(
            f'<li><div class="event-body">'
            f'<div class="event-header"><div><p class="eyebrow">{escape(phase)}</p>'
            f"<h3>{escape(route)}</h3></div>{outcome}</div>"
            f'<p class="event-detail">Task {_code(task.task_id)}</p>'
            f'<p class="muted">State {escape(task.state)} · {escape(retrieval)}</p></div></li>'
        )
    return f"""<section class="panel" aria-labelledby="{section_id}">
  <div class="section-head"><h2 id="{section_id}">{heading}</h2></div>
  <ol class="events">{"".join(rows)}</ol>
</section>"""


def _comparison(label: str, before: str, after: str) -> str:
    unchanged = before == after
    values = _code(before) if unchanged else f"Before {_code(before)}<br>After {_code(after)}"
    return (
        f'<div class="continuity-row"><dt>{escape(label)} '
        f"{_badge(unchanged, 'Preserved', 'Changed')}</dt><dd>{values}</dd></div>"
    )


def render(evidence: Evidence) -> str:
    migration = evidence.migration
    advancing = migration.location_sequence_after > migration.location_sequence_before
    authorized = (
        migration.endpoint_authorization_before
        == migration.endpoint_authorization_after
        == "authorized"
    )
    continuity = (
        migration.root_fingerprint == evidence.identities.nova
        and migration.reference_id_before == migration.reference_id_after
        and migration.follow_id_before == migration.follow_id_after
    )
    checks = evidence.checks.model_dump()
    passed = (
        advancing
        and authorized
        and continuity
        and migration.old_task_endpoint_retired
        and all(task.completed for task in evidence.exchanges)
        and all(checks.values())
    )
    checks_html = "".join(
        f"<li><span>{escape(label)}</span>{_badge(checks[name], 'Passed', 'Unconfirmed')}</li>"
        for name, label in CHECK_LABELS.items()
    )
    return _layout(f"""
<section class="hero" aria-labelledby="hero-title">
  <p class="eyebrow">Agent Internet Live</p>
  <div class="hero-title"><h1 id="hero-title">Atlas &amp; Nova</h1>
    {_badge(passed, "All evidence checks passed", "Evidence needs review")}</div>
  <p class="lede">Authenticated tasks, a signed server move, and the relationship that follows.</p>
  <p class="scope">This snapshot records an isolated integration run.
    It shows recorded results from localhost HTTPS instances. Exclusive control is unverified.</p>
</section>
{_topology(migration)}
<section class="panel" aria-labelledby="identity">
  <div class="section-head"><h2 id="identity">Sovereign identity and discovery</h2>
    {_badge(authorized, "Both Nova locations authorized", "Authorization incomplete")}</div>
  <div class="identities">
    <div><h3>Atlas · Server A</h3>{_code(evidence.identities.atlas)}</div>
    <div><h3>Nova · Servers B and C</h3>{_code(evidence.identities.nova)}</div>
  </div>
  <p class="muted">Nova’s initial remote reference {_code(migration.reference_id_before)}</p>
  <p class="muted">Atlas’s initial follow of Nova {_code(migration.follow_id_before)}</p>
  <p class="muted">Nova’s reverse follow of Atlas {_code(migration.reverse_follow_id)}</p>
</section>
<div class="detail-grid">
  {_tasks(evidence.exchanges[:2])}
  <section class="panel" aria-labelledby="migration">
    <div class="section-head"><h2 id="migration">Signed migration</h2>
      {_badge(advancing and authorized, "Location advanced", "Location update unconfirmed")}</div>
    <p class="muted">Nova’s migration route in the demo: Server B → Server C.</p>
    <dl class="continuity">
      {
        _comparison(
            "Nova’s sovereign fingerprint", evidence.identities.nova, migration.root_fingerprint
        )
    }
      {
        _comparison(
            "Atlas’s remote reference", migration.reference_id_before, migration.reference_id_after
        )
    }
      {_comparison("Atlas’s follow of Nova", migration.follow_id_before, migration.follow_id_after)}
    </dl>
    <p class="retirement">{
        _badge(
            migration.old_task_endpoint_retired,
            "Source task endpoint retired",
            "Source retirement unconfirmed",
        )
    }
    </p>
  </section>
</div>
{_tasks(evidence.exchanges[2:], post_migration=True)}
<section class="panel" aria-labelledby="checks">
  <div class="section-head"><h2 id="checks">Verification checks</h2>
    <span class="muted">{sum(checks.values())} of {len(checks)} passed</span></div>
  <ul class="checks">{checks_html}</ul>
</section>""")


def create_app(evidence_path: Path) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    stylesheet = Path(__file__).with_suffix(".css").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        try:
            evidence = load_evidence(evidence_path)
        except FileNotFoundError:
            message = "No demo evidence is available. Generate it and reload this page."
            status = 404
        except (OSError, ValueError, RecursionError):
            message = "The configured evidence file could not be read or failed validation."
            status = 422
        else:
            return HTMLResponse(render(evidence), headers=HEADERS)
        page = _layout(
            f'<section class="panel unavailable"><h1>Evidence unavailable</h1>'
            f"<p>{escape(message)}</p></section>"
        )
        return HTMLResponse(page, status_code=status, headers=HEADERS)

    @app.get("/dashboard.css")
    def css() -> Response:
        return Response(stylesheet, media_type="text/css", headers=HEADERS)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(create_app(args.evidence), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
