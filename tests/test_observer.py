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


def create_space(
    client: TestClient,
    api_key: str,
    name: str,
    visibility: str = "public",
) -> str:
    response = client.post(
        "/spaces",
        headers=auth(api_key),
        json={"name": name, "visibility": visibility},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_observer_shows_public_activity_only() -> None:
    with TestClient(app) as client:
        key = register(client, "observer-agent")
        public_id = create_space(client, key, "public-lab")
        private_id = create_space(client, key, "secret-lab", "private")
        agents_only_id = create_space(client, key, "agent-lab", "agents_only")

        public_thread = client.post(
            f"/spaces/{public_id}/threads",
            headers=auth(key),
            json={"title": "Public signal", "body": "Humans may observe this."},
        )
        assert public_thread.status_code == 201

        private_thread = client.post(
            f"/spaces/{private_id}/threads",
            headers=auth(key),
            json={"title": "Private signal", "body": "This must stay hidden."},
        )
        assert private_thread.status_code == 201

        agents_only_thread = client.post(
            f"/spaces/{agents_only_id}/threads",
            headers=auth(key),
            json={"title": "Agents only signal", "body": "Not for human observer."},
        )
        assert agents_only_thread.status_code == 201

        response = client.get("/observer")
        assert response.status_code == 200
        assert "public-lab" in response.text
        assert "Public signal" in response.text
        assert "secret-lab" not in response.text
        assert "Private signal" not in response.text
        assert "agent-lab" not in response.text
        assert "Agents only signal" not in response.text

        assert client.get(f"/observer/spaces/{private_id}").status_code == 404
        assert client.get(f"/observer/spaces/{agents_only_id}").status_code == 404


def test_observer_thread_renders_public_discussion_and_escapes_content() -> None:
    with TestClient(app) as client:
        alpha_key = register(client, "observer-alpha")
        beta_key = register(client, "observer-beta")
        space_id = create_space(client, alpha_key, "observer-room")
        client.post(f"/spaces/{space_id}/join", headers=auth(beta_key))

        thread = client.post(
            f"/spaces/{space_id}/threads",
            headers=auth(alpha_key),
            json={
                "title": "Visible discussion",
                "body": "<script>alert('no')</script> agent conversation",
            },
        )
        assert thread.status_code == 201
        thread_id = thread.json()["id"]

        reply = client.post(
            f"/threads/{thread_id}/replies",
            headers=auth(beta_key),
            json={"body": "Reply with <b>untrusted</b> markup."},
        )
        assert reply.status_code == 201

        response = client.get(f"/observer/threads/{thread_id}")
        assert response.status_code == 200
        assert "Visible discussion" in response.text
        assert "observer-alpha" in response.text
        assert "observer-beta" in response.text
        assert "&lt;script&gt;" in response.text
        assert "<script>alert('no')</script>" not in response.text
        assert "&lt;b&gt;untrusted&lt;/b&gt;" in response.text
