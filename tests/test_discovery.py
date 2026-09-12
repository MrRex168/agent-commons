from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def register(client: TestClient, name: str, description: str | None = None) -> str:
    payload = {"name": name}
    if description is not None:
        payload["description"] = description
    response = client.post("/agents/register", json=payload)
    assert response.status_code == 201
    return response.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_mention_creates_notification_that_can_be_read() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "alpha-agent")
        beta_key = register(client, "beta-agent")

        space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "agent-lab"},
        )
        space_id = space.json()["id"]
        client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))

        thread = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(alpha_key),
            json={"title": "Coordination", "body": "What should we test next?"},
        )
        thread_id = thread.json()["id"]

        reply = client.post(
            f"/threads/{thread_id}/replies",
            headers=auth(beta_key),
            json={"body": "@alpha-agent I found a useful coordination pattern."},
        )
        assert reply.status_code == 201

        notifications = client.get("/agents/me/notifications", headers=auth(alpha_key))
        assert notifications.status_code == 200
        assert len(notifications.json()) == 1
        notification = notifications.json()[0]
        assert notification["kind"] == "mention"
        assert notification["thread_id"] == thread_id
        assert notification["reply_id"] == reply.json()["id"]

        marked = client.post(
            f"/agents/me/notifications/{notification['id']}/read",
            headers=auth(alpha_key),
        )
        assert marked.status_code == 200
        assert marked.json()["read_at"] is not None

        unread = client.get("/agents/me/notifications", headers=auth(alpha_key))
        assert unread.json() == []

        all_notifications = client.get(
            "/agents/me/notifications?unread_only=false",
            headers=auth(alpha_key),
        )
        assert len(all_notifications.json()) == 1


def test_return_context_includes_unread_mentions() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "return-alpha")
        beta_key = register(client, "return-beta")

        space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "return-lab"},
        )
        space_id = space.json()["id"]
        client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))

        client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(beta_key),
            json={"title": "Question", "body": "@return-alpha can you review this?"},
        )

        context = client.get("/agents/me/context", headers=auth(alpha_key))
        assert context.status_code == 200
        assert len(context.json()["notifications"]) == 1
        assert context.json()["notifications"][0]["kind"] == "mention"


def test_search_discovers_agents_spaces_threads_and_replies() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "search-alpha", "Studies orchestration patterns")
        beta_key = register(client, "search-beta")

        space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "orchestration-lab", "description": "Agent orchestration research"},
        )
        space_id = space.json()["id"]
        client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))

        thread = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(alpha_key),
            json={"title": "Orchestration protocol", "body": "Testing delegation behavior."},
        )
        client.post(
            f"/threads/{thread.json()['id']}/replies",
            headers=auth(beta_key),
            json={"body": "The orchestration result looks stable."},
        )

        results = client.get("/search?q=orchestration")
        assert results.status_code == 200
        data = results.json()
        assert any(agent["name"] == "search-alpha" for agent in data["agents"])
        assert any(space["name"] == "orchestration-lab" for space in data["spaces"])
        assert any(thread["title"] == "Orchestration protocol" for thread in data["threads"])
        assert any("orchestration" in reply["body"].lower() for reply in data["replies"])
