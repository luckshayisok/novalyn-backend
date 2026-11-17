"""Expiry reminders, the notification outbox and the in-app inbox."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.time import utcnow
from app.integrations.email import senders
from app.models import EmailStatus, Notification, OutboxEvent, OutboxStatus
from app.services.notifications import Dispatcher
from app.services.reminders import ReminderScanner, due_reminder
from tests.helpers import add_member, emails_to, error_code, in_days


@pytest.fixture
def team(actor):
    owner = actor("owner@example.com", "Olivia Owner")
    org_id = owner.create_org("Acme")["id"]
    manager, member, viewer = (
        actor("cm@example.com", "Carla"),
        actor("member@example.com", "Mia"),
        actor("viewer@example.com", "Vic"),
    )
    add_member(owner, org_id, manager, "compliance_manager")
    add_member(owner, org_id, member, "member")
    add_member(owner, org_id, viewer, "viewer")
    return {"owner": owner, "manager": manager, "member": member, "viewer": viewer, "org": org_id}


def _scan(db):
    return ReminderScanner(db).run()


def _events(db, kind=None):
    query = select(OutboxEvent).order_by(OutboxEvent.created_at)
    if kind:
        query = query.where(OutboxEvent.event_type == kind)
    return list(db.scalars(query))


def _inbox(actor_, org_id, **params):
    response = actor_.get(f"/organizations/{org_id}/notifications", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- reminder rule


@pytest.mark.parametrize(
    ("days", "expected"),
    [(90, None), (60, 60), (45, 60), (30, 30), (29, 30), (8, 30), (7, 7), (2, 7), (1, 1), (0, 0)],
)
def test_due_reminder_is_the_tightest_threshold_crossed(days, expected):
    assert due_reminder(days, [60, 30, 7, 1, 0]) == expected


# --------------------------------------------------------------------- scanning


def test_scan_sends_one_reminder_to_owners_managers_and_the_document_owner(team, db):
    owner, org_id = team["owner"], team["org"]
    member_membership = owner.membership_id(org_id, "member@example.com")
    owner.uploaded(
        org_id, name="Fire certificate", expiry_date=in_days(25), owner_membership_id=member_membership
    )

    created = _scan(db)

    assert created == 1
    (event,) = _events(db, "document.expiring")
    assert event.payload["days"] == 25
    assert event.payload["offset"] == 30
    assert event.status == OutboxStatus.DONE
    recipients = {n.user.email for n in db.scalars(select(Notification))}
    # Owner and compliance manager always; the member because they own this document; not the viewer.
    assert recipients == {"owner@example.com", "cm@example.com", "member@example.com"}
    message = emails_to("cm@example.com")[-1]
    assert message.subject.startswith("Fire certificate expires in 25 days")
    assert "/o/acme/documents/" in message.text
    assert [m for m in emails_to("viewer@example.com") if "expires" in m.subject] == []


def test_scanning_again_does_not_repeat_a_reminder(team, db):
    owner, org_id = team["owner"], team["org"]
    owner.uploaded(org_id, expiry_date=in_days(25))

    assert _scan(db) == 1
    assert _scan(db) == 0
    assert len(_events(db)) == 1


def test_each_threshold_is_sent_once_as_the_date_approaches(team, db, monkeypatch):
    from app.services import expiry

    owner, org_id = team["owner"], team["org"]
    owner.uploaded(org_id, expiry_date=in_days(61))
    sent = []
    for day in range(0, 70):
        moment = utcnow() + timedelta(days=day)
        monkeypatch.setattr(expiry, "utcnow", lambda moment=moment: moment)
        _scan(db)
        sent = [(e.event_type, e.payload.get("offset")) for e in _events(db)]

    assert sent[:5] == [
        ("document.expiring", 60),
        ("document.expiring", 30),
        ("document.expiring", 7),
        ("document.expiring", 1),
        ("document.expiring", 0),
    ]
    assert sent[5] == ("document.expired", None)
    # Day 61 is the expiry day; seven days later (day 68) the first weekly overdue alert goes out.
    assert sent[6:] == [("document.overdue", None)]


def test_expired_documents_get_one_alert_then_weekly_overdue_alerts_up_to_twelve_weeks(team, db):
    owner, org_id = team["owner"], team["org"]
    owner.uploaded(org_id, name="Just expired", expiry_date=in_days(-2))
    owner.uploaded(org_id, name="Overdue", expiry_date=in_days(-15))
    owner.uploaded(org_id, name="Long gone", expiry_date=in_days(-120))

    _scan(db)

    kinds = {
        event.payload["document_name"]: (event.event_type, event.dedupe_key.split(":")[-1])
        for event in _events(db)
    }
    assert kinds["Just expired"][0] == "document.expired"
    assert kinds["Overdue"] == ("document.overdue", "2")
    assert "Long gone" not in kinds
    assert emails_to("owner@example.com")[-1].subject.startswith(
        ("Just expired has expired", "Overdue expired")
    )


def test_a_new_expiry_date_starts_a_new_cycle(team, db):
    owner, org_id = team["owner"], team["org"]
    document = owner.uploaded(org_id, expiry_date=in_days(5))
    _scan(db)
    owner.patch(f"/organizations/{org_id}/documents/{document['id']}", json={"expiry_date": in_days(6)})

    assert _scan(db) == 1


def test_archived_documents_and_disabled_reminders_are_skipped(team, db):
    owner, org_id = team["owner"], team["org"]
    archived = owner.uploaded(org_id, expiry_date=in_days(3))
    owner.post(f"/organizations/{org_id}/documents/{archived['id']}/archive")

    assert _scan(db) == 0

    owner.uploaded(org_id, expiry_date=in_days(3))
    owner.patch(f"/organizations/{org_id}", json={"reminders_enabled": False})
    assert _scan(db) == 0


def test_custom_offsets_are_used(team, db):
    owner, org_id = team["owner"], team["org"]
    owner.patch(f"/organizations/{org_id}", json={"reminder_offsets": [90, 14]})
    owner.uploaded(org_id, expiry_date=in_days(80))

    _scan(db)

    (event,) = _events(db)
    assert event.payload["offset"] == 90


def test_unconfirmed_addresses_get_the_in_app_notice_but_no_email(actor, db):
    owner = actor("owner@example.com")
    org_id = owner.create_org("Acme")["id"]
    late = actor("late@example.com", verified=False)
    add_member(owner, org_id, late, "admin")  # accepting the invitation confirms the address...
    from app.models import User

    user = db.scalars(select(User).where(User.email == "late@example.com")).one()
    user.email_verified_at = None  # ...so undo it to test the rule
    db.commit()
    owner.uploaded(org_id, expiry_date=in_days(3))

    _scan(db)

    notice = db.scalars(select(Notification).where(Notification.user_id == user.id)).one()
    assert notice.email_status == EmailStatus.SKIPPED
    assert [m for m in emails_to("late@example.com") if "expires" in m.subject] == []


# ------------------------------------------------------------------ delivery


class FlakySender:
    def __init__(self, failures):
        self.failures = failures
        self.sent = []

    def send(self, message):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("smtp down")
        self.sent.append(message)


def test_failed_emails_are_retried_later_without_duplicating_notifications(team, db, monkeypatch):
    owner, org_id = team["owner"], team["org"]
    flaky = FlakySender(failures=10)
    monkeypatch.setattr(senders, "get_email_sender", lambda: flaky)
    monkeypatch.setattr("app.services.notifications.get_email_sender", lambda: flaky)
    owner.uploaded(org_id, expiry_date=in_days(3))

    _scan(db)
    (event,) = _events(db)
    db.refresh(event)
    assert event.status == OutboxStatus.PENDING
    assert event.next_attempt_at > utcnow()
    assert db.scalars(select(Notification)).all()  # in-app notices exist already

    flaky.failures = 0
    event.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()
    Dispatcher(db).run()

    db.refresh(event)
    assert event.status == OutboxStatus.DONE
    assert len(db.scalars(select(Notification)).all()) == 2  # owner and manager, still once each
    assert {m.to for m in flaky.sent} == {"owner@example.com", "cm@example.com"}


def test_an_email_that_keeps_failing_is_given_up_after_five_attempts(team, db, monkeypatch):
    owner, org_id = team["owner"], team["org"]
    always = FlakySender(failures=10_000)
    monkeypatch.setattr("app.services.notifications.get_email_sender", lambda: always)
    owner.uploaded(org_id, expiry_date=in_days(3))
    _scan(db)
    (event,) = _events(db)

    for _ in range(6):
        db.refresh(event)
        event.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
        Dispatcher(db).run()

    db.refresh(event)
    assert event.status == OutboxStatus.DONE
    assert {n.email_status for n in db.scalars(select(Notification))} == {EmailStatus.FAILED}


def test_a_stuck_event_is_picked_up_again(team, db):
    owner, org_id = team["owner"], team["org"]
    owner.uploaded(org_id, expiry_date=in_days(3))
    _scan(db)
    (event,) = _events(db)
    db.execute(Notification.__table__.delete())
    event.status = OutboxStatus.PROCESSING
    event.claimed_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert Dispatcher(db).run() == 1
    db.refresh(event)
    assert event.status == OutboxStatus.DONE


# ----------------------------------------------------------- workflow notices


def test_uploads_by_members_ask_reviewers_in_the_app_only(team, db):
    member, org_id = team["member"], team["org"]
    before = len(emails_to("cm@example.com"))

    member.uploaded(org_id, name="New permit")

    inbox = _inbox(team["manager"], org_id)
    assert inbox["items"][0]["title"] == "New permit is waiting for review"
    assert len(emails_to("cm@example.com")) == before
    assert _inbox(member, org_id)["total"] == 0  # not told about their own upload


def test_rejecting_a_document_tells_the_person_who_uploaded_it(team):
    member, manager, org_id = team["member"], team["manager"], team["org"]
    document = member.uploaded(org_id, name="Blurry permit")

    manager.post(
        f"/organizations/{org_id}/documents/{document['id']}/reject", json={"reason": "Unreadable scan"}
    )

    item = _inbox(member, org_id)["items"][0]
    assert item["title"] == "Blurry permit was rejected"
    assert "Unreadable scan" in item["body"]
    assert item["link"] == f"documents/{document['id']}"
    assert emails_to("member@example.com")[-1].subject.startswith("Blurry permit was rejected")


# ----------------------------------------------------------------------- inbox


def test_inbox_read_state_and_isolation(team, db, actor):
    owner, manager, org_id = team["owner"], team["manager"], team["org"]
    owner.uploaded(org_id, expiry_date=in_days(3))
    owner.uploaded(org_id, expiry_date=in_days(-1))
    _scan(db)
    url = f"/organizations/{org_id}/notifications"

    assert owner.get(f"{url}/unread-count").json() == {"unread": 2}
    first = _inbox(owner, org_id)["items"][0]
    assert owner.post(f"{url}/{first['id']}/read").json()["read_at"] is not None
    assert owner.get(f"{url}/unread-count").json() == {"unread": 1}
    assert _inbox(owner, org_id, unread="true")["total"] == 1

    # Another member cannot read or mark someone else's notification.
    assert manager.post(f"{url}/{first['id']}/read").status_code == 404
    assert owner.post(f"{url}/read-all").status_code == 204
    assert owner.get(f"{url}/unread-count").json() == {"unread": 0}
    assert manager.get(f"{url}/unread-count").json() == {"unread": 2}

    outsider = actor("outsider@example.com")
    outsider.create_org("Other")
    assert outsider.get(url).status_code == 404


# -------------------------------------------------------------------- settings


def test_reminder_settings(team):
    owner, member, org_id = team["owner"], team["member"], team["org"]
    url = f"/organizations/{org_id}"

    assert owner.get(url).json()["reminder_offsets"] == [60, 30, 7, 1, 0]
    body = owner.patch(url, json={"reminder_offsets": [7, 90, 7, 0]}).json()
    assert body["reminder_offsets"] == [90, 7, 0]
    assert owner.patch(url, json={"reminder_offsets": [400]}).status_code == 422
    assert owner.patch(url, json={"reminder_offsets": list(range(11))}).status_code == 422
    denied = member.patch(url, json={"reminders_enabled": False})
    assert denied.status_code == 403
    assert error_code(denied) == "permission_denied"
