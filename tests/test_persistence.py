from fastapi.testclient import TestClient

from agent_commons.db import Base, engine
from agent_commons.main import app


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def register(client: TestClient, name: str) -> str:
    response = client.post("/agents/register", json={"name": name})
    assert response.status_code == 201
    return response.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_agent_memory_can_be_saved_updated_and_restored() -> None:
    with TestClient(app) as client:
        key = register(client, "memory-agent")

        first = client.put(
            "/agents/me/memories/current-goal",
            headers=auth(key),
            json={"value": "Study coordination protocols"},
        )
        assert first.status_code == 200
        assert first.json()["value"] == "Study coordination protocols"

        updated = client.put(
            "/agents/me/memories/current-goal",
            headers=auth(key),
            json={"value": "Compare coordination protocols"},
        )
        assert updated.status_code == 200

        memories = client.get("/agents/me/memories", headers=auth(key))
        assert memories.status_code == 200
        assert len(memories.json()) == 1
        assert memories.json()[0]["value"] == "Compare coordination protocols"


def test_return_context_surfaces_new_replies_once() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "context-alpha")
        beta_key = register(client, "context-beta")

        space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "context-lab"},
        )
        space_id = space.json()["id"]
        client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))

        thread = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(alpha_key),
            json={"title": "Persistent context", "body": "What changed while I was away?"},
        )
        thread_id = thread.json()["id"]

        baseline = client.get("/agents/me/context", headers=auth(alpha_key))
        assert baseline.status_code == 200
        assert baseline.json()["new_replies"] == []

        client.post(
            f"/threads/{thread_id}/replies",
            headers=auth(beta_key),
            json={"body": "I found a useful coordination pattern."},
        )

        returned = client.get("/agents/me/context", headers=auth(alpha_key))
        assert returned.status_code == 200
        assert len(returned.json()["new_replies"]) == 1
        assert returned.json()["new_replies"][0]["body"].startswith("I found")

        again = client.get("/agents/me/context", headers=auth(alpha_key))
        assert again.status_code == 200
        assert again.json()["new_replies"] == []
