"""Verify authorized serial outcomes after membership advisory-lock correction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event, select
from test_api import client as client
from test_api import headers

from whatsapp_bot.access import Actor
from whatsapp_bot.models import Membership


def test_concurrent_self_change_and_demotion_have_authorized_serial_result(
    client, engine, tenant_a, tenant_session, monkeypatch
):
    with tenant_session(tenant_a) as session:
        owner_id = session.scalar(select(Membership.id).where(Membership.subject == "test-owner"))
        session.add_all(
            [
                Membership(tenant_id=tenant_a, subject="test-agent", role="owner"),
                Membership(tenant_id=tenant_a, subject="third-owner", role="owner"),
            ]
        )

    authorized, release, revocation_at_lock = Event(), Event(), Event()
    original = Actor.require

    def pause_after_authorization(actor, *roles):
        original(actor, *roles)
        if actor.subject == "test-owner" and not authorized.is_set():
            authorized.set()
            assert release.wait(timeout=5), "Reviewer timed out releasing authorized request"

    def observe_exclusive_lock(connection, cursor, statement, parameters, context, executemany):
        if authorized.is_set() and "pg_advisory_xact_lock(" in statement:
            revocation_at_lock.set()

    monkeypatch.setattr(Actor, "require", pause_after_authorization)
    event.listen(engine, "before_cursor_execute", observe_exclusive_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.patch,
                f"/commerce/v1/memberships/{owner_id}",
                json={"role": "supervisor"},
                headers=headers(tenant_a),
            )
            try:
                assert authorized.wait(timeout=5)
                second = pool.submit(
                    client.patch,
                    f"/commerce/v1/memberships/{owner_id}",
                    json={"role": "agent"},
                    headers=headers(tenant_a, "agent"),
                )
                assert revocation_at_lock.wait(timeout=5), "Revocation did not acquire the authorization lock"
                assert not second.done(), "Revocation crossed an in-flight authorized membership change"
            finally:
                release.set()
            first_response = first.result(timeout=5)
            second_response = second.result(timeout=5)
        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        with tenant_session(tenant_a) as session:
            assert session.get(Membership, owner_id).role == "agent"
        # The old token authenticates the same subject, but no longer authorizes
        # another membership mutation after the demotion has committed.
        response = client.patch(
            f"/commerce/v1/memberships/{owner_id}",
            json={"role": "supervisor"},
            headers=headers(tenant_a),
        )
        assert response.status_code == 403, response.text
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe_exclusive_lock)
