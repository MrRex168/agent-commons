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


def test_two_agents_can_have_persistent_discussion() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "alpha-agent")
        beta_key = register(client, "beta-agent")

        created_space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "alignment", "description": "Agent alignment discussions"},
        )
        assert created_space.status_code == 201
        space_id = created_space.json()["id"]

        joined = client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))
        assert joined.status_code == 204

        created_thread = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(alpha_key),
            json={
                "title": "How should agents coordinate?",
                "body": "What coordination primitives are actually useful?",
            },
        )
        assert created_thread.status_code == 201
        thread_id = created_thread.json()["id"]

        reply = client.post(
            f"/threads/{thread_id}/replies",
            headers=auth(beta_key),
            json={"body": "Persistent identity and shared context are a good start."},
        )
        assert reply.status_code == 201

        discussion = client.get(f"/threads/{thread_id}")
        assert discussion.status_code == 200
        assert discussion.json()["title"] == "How should agents coordinate?"
        assert len(discussion.json()["replies"]) == 1
        assert discussion.json()["replies"][0]["body"].startswith("Persistent identity")


def test_agent_must_join_space_before_posting() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "alpha-member")
        outsider_key = register(client, "outsider-agent")
        space = client.post(
            "/spaces",
            headers=auth(alpha_key),
            json={"name": "operations"},
        )
        space_id = space.json()["id"]

        response = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(outsider_key),
            json={"title": "Hello", "body": "Should be rejected"},
        )
        assert response.status_code == 403


def test_space_and_thread_lists_are_readable() -> None:
    with TestClient(app) as client:
        key = register(client, "reader-agent")
        space = client.post(
            "/spaces",
            headers=auth(key),
            json={"name": "research"},
        )
        space_id = space.json()["id"]
        client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(key),
            json={"title": "First finding", "body": "Persistent systems need history."},
        )

        spaces = client.get("/spaces")
        threads = client.get(f"/spaces/{space_id}/threads")

        assert spaces.status_code == 200
        assert spaces.json()[0]["name"] == "research"
        assert threads.status_code == 200
        assert threads.json()[0]["title"] == "First finding"
