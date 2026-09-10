"""
Integration tests for the /extensions management routes: DB-backed
persistence, RBAC action enrollment, the enable/disable route-mounting lifecycle,
and internal-key rotation.

The field-ingress side app and its device-key auth channel are deliberately not
implemented (skipped for now, not just deferred to a later stage) so there is no
device-key coverage here.

Route mounting is verified by inspecting the live route tables of the two apps
(`main.app`, `extensions.setup.internal_app`) rather than by calling the mounted
routes themselves — actual dispatch through a mounted route is covered separately
in test_extensions_dispatch.py (see extensions/upstreams/http.py, extensions/upstreams/iotedge.py).
"""
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.models.action import Action
from extensions.setup import internal_app
from tests.integration.test_abac_authorization import AbacFixtures


def _unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _sample_registration(name: str, visibility: str = "public") -> dict:
    return {
        "name": name,
        "description": "Demo extension for tests",
        "upstreams": {
            "svc": {"type": "http", "base_url": "http://localhost:9000"},
        },
        "routes": [
            {
                "upstream": "svc",
                "path": f"/{name}/ping",
                "method": "GET",
                "upstream_path": "/ping",
                "visibility": visibility,
            },
        ],
    }


def _live_route_names(app, name: str) -> set:
    prefix = f"extension_route__{name}__"
    return {r.name for r in app.router.routes if getattr(r, "name", "").startswith(prefix)}


class TestExtensionsManagementRoutesAsAdmin:
    """Default fake_jwt_user is an admin — bypasses ABAC, exercises the route shapes."""

    async def test_register_list_get_replace_delete_round_trip(self, client):
        name = _unique_name("demo_ext")
        registration = _sample_registration(name)

        register_response = await client.post("/extensions", json=registration)
        assert register_response.status_code == 201
        assert register_response.json()["enabled"] is False

        list_response = await client.get("/extensions")
        assert list_response.status_code == 200
        assert any(ext["name"] == name for ext in list_response.json())

        get_response = await client.get(f"/extensions/{name}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == name

        replacement = _sample_registration(name)
        replacement["description"] = "Updated description"
        replace_response = await client.put(f"/extensions/{name}", json=replacement)
        assert replace_response.status_code == 200
        assert replace_response.json()["description"] == "Updated description"
        # enabled must never change via PUT replace
        assert replace_response.json()["enabled"] is False

        delete_response = await client.delete(f"/extensions/{name}")
        assert delete_response.status_code == 204

        get_after_delete = await client.get(f"/extensions/{name}")
        assert get_after_delete.status_code == 404

    async def test_register_duplicate_name_conflicts(self, client):
        name = _unique_name("dup_ext")
        registration = _sample_registration(name)

        first = await client.post("/extensions", json=registration)
        assert first.status_code == 201

        second = await client.post("/extensions", json=registration)
        assert second.status_code == 409

    async def test_unknown_extension_returns_404(self, client):
        name = _unique_name("missing_ext")
        assert (await client.get(f"/extensions/{name}")).status_code == 404
        assert (await client.delete(f"/extensions/{name}")).status_code == 404
        assert (await client.post(f"/extensions/{name}/enable")).status_code == 404
        assert (await client.post(f"/extensions/{name}/disable")).status_code == 404
        assert (await client.post(f"/extensions/{name}/internal-key/rotate")).status_code == 404

    async def test_enable_mounts_public_route_disable_unmounts_it(self, client):
        from main import app as public_app

        name = _unique_name("lifecycle_ext")
        await client.post("/extensions", json=_sample_registration(name))

        # freshly registered: inert, no live route yet
        assert not _live_route_names(public_app, name)

        enable_response = await client.post(f"/extensions/{name}/enable")
        assert enable_response.status_code == 200
        assert enable_response.json()["enabled"] is True
        assert len(_live_route_names(public_app, name)) == 1

        disable_response = await client.post(f"/extensions/{name}/disable")
        assert disable_response.status_code == 200
        assert disable_response.json()["enabled"] is False
        assert not _live_route_names(public_app, name)

        # disable never touches the persisted manifest — re-enabling brings the
        # same route back
        reenable_response = await client.post(f"/extensions/{name}/enable")
        assert reenable_response.status_code == 200
        assert len(_live_route_names(public_app, name)) == 1

        await client.delete(f"/extensions/{name}")
        assert not _live_route_names(public_app, name)

    async def test_enable_mounts_internal_route_on_internal_app(self, client):
        from main import app as public_app

        name = _unique_name("visibility_ext")
        registration = _sample_registration(name, visibility="public")
        registration["routes"].append(
            {
                "upstream": "svc",
                "path": f"/{name}/internal-ping",
                "method": "GET",
                "upstream_path": "/ping",
                "visibility": "internal",
            }
        )
        await client.post("/extensions", json=registration)
        await client.post(f"/extensions/{name}/enable")

        assert len(_live_route_names(public_app, name)) == 1
        assert len(_live_route_names(internal_app, name)) == 1

        await client.post(f"/extensions/{name}/disable")
        assert not _live_route_names(public_app, name)
        assert not _live_route_names(internal_app, name)

    async def test_rotate_internal_key_invalidates_previous_key(self, client):
        name = _unique_name("keyed_ext")
        await client.post("/extensions", json=_sample_registration(name))

        first = await client.post(f"/extensions/{name}/internal-key/rotate")
        assert first.status_code == 200
        first_key = first.json()["internal_key"]

        second = await client.post(f"/extensions/{name}/internal-key/rotate")
        assert second.status_code == 200
        second_key = second.json()["internal_key"]

        assert first_key != second_key

        current = await client.get(f"/extensions/{name}")
        # internal_key_hash is never exposed via GET
        assert "internal_key_hash" not in current.json()

    async def test_deregister_cleans_up_orphaned_actions(self, client, db_session):
        name = _unique_name("cleanup_ext")
        action_name = f"{name}.custom_action"
        registration = _sample_registration(name)
        registration["actions"] = [{"name": action_name, "description": "custom"}]

        await client.post("/extensions", json=registration)

        result = await db_session.execute(select(Action).where(Action.name == action_name))
        assert result.scalar_one_or_none() is not None

        await client.delete(f"/extensions/{name}")

        result = await db_session.execute(select(Action).where(Action.name == action_name))
        assert result.scalar_one_or_none() is None

    async def test_replace_refuses_to_drop_action_still_granted_to_a_role(self, client, db_session):
        name = _unique_name("guarded_ext")
        action_name = f"{name}.custom_action"
        registration = _sample_registration(name)
        registration["actions"] = [{"name": action_name, "description": "custom"}]

        await client.post("/extensions", json=registration)
        await AbacFixtures(db_session).setup(roles={"holder": [action_name]})

        replacement = _sample_registration(name)  # drops the action
        response = await client.put(f"/extensions/{name}", json=replacement)
        assert response.status_code == 409

    async def test_register_and_replace_with_new_required_action_on_a_route(self, client):
        """Regression test: a route's `required_action` FK depends on that action row
        already existing — actions must be enrolled before routes referencing them are
        persisted, on both register and replace (not just on register)."""
        name = _unique_name("required_action_ext")
        registration = _sample_registration(name)
        registration["actions"] = [{"name": f"{name}.read", "description": "read"}]
        registration["routes"][0]["required_action"] = f"{name}.read"

        register_response = await client.post("/extensions", json=registration)
        assert register_response.status_code == 201

        replacement = _sample_registration(name)
        replacement["actions"] = [{"name": f"{name}.write", "description": "write"}]
        replacement["routes"][0]["required_action"] = f"{name}.write"
        replace_response = await client.put(f"/extensions/{name}", json=replacement)
        assert replace_response.status_code == 200


class TestExtensionsManagementRoutesAuthorization:
    """Requires extension.register|deregister|read — mirrors TestPlatformAuthorizationEndpoints."""

    @pytest.fixture
    def fake_jwt_user(self):
        return {
            "oid": "extensions-test-oid",
            "sub": "extensions-test-oid",
            "preferred_username": "extensions-test@test.com",
            "name": "Extensions Test",
            "roles": [],
        }

    async def test_user_without_register_action_cannot_register(self, client, db_session):
        await AbacFixtures(db_session).setup(
            users={"tester": "extensions-test-oid"},
            roles={"device-reader": ["device.read"]},
            teams={"t": {"roles": ["device-reader"], "users": ["tester"]}},
        )

        response = await client.post("/extensions", json=_sample_registration(_unique_name("denied_ext")))
        assert response.status_code == 403

    async def test_user_with_register_action_can_register(self, client, db_session):
        await AbacFixtures(db_session).setup(
            users={"tester": "extensions-test-oid"},
            roles={"ext-registerer": ["extension.register"]},
            teams={"t": {"roles": ["ext-registerer"], "users": ["tester"]}},
        )

        response = await client.post("/extensions", json=_sample_registration(_unique_name("allowed_ext")))
        assert response.status_code == 201

    async def test_user_with_read_action_can_list_but_not_register(self, client, db_session):
        await AbacFixtures(db_session).setup(
            users={"tester": "extensions-test-oid"},
            roles={"ext-reader": ["extension.read"]},
            teams={"t": {"roles": ["ext-reader"], "users": ["tester"]}},
        )

        list_response = await client.get("/extensions")
        assert list_response.status_code == 200

        register_response = await client.post("/extensions", json=_sample_registration(_unique_name("blocked_ext")))
        assert register_response.status_code == 403

    async def test_user_without_deregister_action_cannot_disable_or_delete(self, client, db_session):
        await AbacFixtures(db_session).setup(
            users={"tester": "extensions-test-oid"},
            roles={"ext-registerer": ["extension.register"]},
            teams={"t": {"roles": ["ext-registerer"], "users": ["tester"]}},
        )

        name = _unique_name("undeletable_ext")
        register_response = await client.post("/extensions", json=_sample_registration(name))
        assert register_response.status_code == 201

        assert (await client.post(f"/extensions/{name}/disable")).status_code == 403
        assert (await client.delete(f"/extensions/{name}")).status_code == 403

