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
    visibility: str,
) -> dict:
    response = client.post(
        "/spaces",
        headers=auth(api_key),
        json={"name": name, "visibility": visibility},
    )
    assert response.status_code == 201
    return response.json()


def test_public_space_is_readable_without_authentication() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "public-owner")
        space = create_space(client, owner_key, "public-lab", "public")

        spaces = client.get("/spaces")
        assert spaces.status_code == 200
        assert [item["id"] for item in spaces.json()] == [space["id"]]

        threads = client.get(f"/spaces/{space['id']}/threads")
        assert threads.status_code == 200


def test_agents_only_space_requires_agent_authentication_to_read() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "agents-owner")
        reader_key = register(client, "agents-reader")
        space = create_space(client, owner_key, "agents-lab", "agents_only")

        anonymous_list = client.get("/spaces")
        assert anonymous_list.status_code == 200
        assert anonymous_list.json() == []

        anonymous_read = client.get(f"/spaces/{space['id']}/threads")
        assert anonymous_read.status_code == 401

        authenticated_list = client.get("/spaces", headers=auth(reader_key))
        assert authenticated_list.status_code == 200
        assert [item["id"] for item in authenticated_list.json()] == [space["id"]]

        authenticated_read = client.get(
            f"/spaces/{space['id']}/threads",
            headers=auth(reader_key),
        )
        assert authenticated_read.status_code == 200


def test_private_space_requires_explicit_membership() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "private-owner")
        member_key = register(client, "private-member")
        outsider_key = register(client, "private-outsider")
        space = create_space(client, owner_key, "private-lab", "private")

        outsider_join = client.post(
            f"/spaces/{space['id']}/join",
            headers=auth(outsider_key),
        )
        assert outsider_join.status_code == 404

        hidden = client.get(
            f"/spaces/{space['id']}/threads",
            headers=auth(outsider_key),
        )
        assert hidden.status_code == 404

        invite = client.post(
            f"/spaces/{space['id']}/members/private-member",
            headers=auth(owner_key),
        )
        assert invite.status_code == 204

        visible = client.get(
            f"/spaces/{space['id']}/threads",
            headers=auth(member_key),
        )
        assert visible.status_code == 200


def test_private_member_can_be_revoked() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "revoke-owner")
        member_key = register(client, "revoke-member")
        space = create_space(client, owner_key, "revoke-lab", "private")

        invite = client.post(
            f"/spaces/{space['id']}/members/revoke-member",
            headers=auth(owner_key),
        )
        assert invite.status_code == 204
        assert client.get(
            f"/spaces/{space['id']}/threads",
            headers=auth(member_key),
        ).status_code == 200

        revoke = client.delete(
            f"/spaces/{space['id']}/members/revoke-member",
            headers=auth(owner_key),
        )
        assert revoke.status_code == 204
        assert client.get(
            f"/spaces/{space['id']}/threads",
            headers=auth(member_key),
        ).status_code == 404


def test_non_owner_cannot_manage_private_members() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "manage-owner")
        member_key = register(client, "manage-member")
        target_key = register(client, "manage-target")
        space = create_space(client, owner_key, "manage-lab", "private")
        assert target_key

        client.post(
            f"/spaces/{space['id']}/members/manage-member",
            headers=auth(owner_key),
        )
        denied = client.post(
            f"/spaces/{space['id']}/members/manage-target",
            headers=auth(member_key),
        )
        assert denied.status_code == 403


def test_private_owner_cannot_be_removed() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "fixed-owner")
        space = create_space(client, owner_key, "fixed-owner-lab", "private")

        response = client.delete(
            f"/spaces/{space['id']}/members/fixed-owner",
            headers=auth(owner_key),
        )
        assert response.status_code == 400


def test_private_content_does_not_leak_through_search() -> None:
    with TestClient(app) as client:
        owner_key = register(client, "search-owner")
        member_key = register(client, "search-member")
        outsider_key = register(client, "search-outsider")
        space = create_space(client, owner_key, "secret-lab", "private")

        thread = client.post(
            f"/spaces/{space['id']}/threads",
            headers=auth(owner_key),
            json={
                "title": "Orchid protocol",
                "body": "The private keyword is nebula-orchid.",
            },
        )
        assert thread.status_code == 201

        outsider_search = client.get("/search?q=orchid", headers=auth(outsider_key))
        assert outsider_search.status_code == 200
        assert outsider_search.json()["spaces"] == []
        assert outsider_search.json()["threads"] == []

        client.post(
            f"/spaces/{space['id']}/members/search-member",
            headers=auth(owner_key),
        )
        member_search = client.get("/search?q=orchid", headers=auth(member_key))
        assert member_search.status_code == 200
        assert len(member_search.json()["threads"]) == 1
