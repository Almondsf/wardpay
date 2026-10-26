"""
Celery task registry: every background task is declared here, wrapping a
plain function from its module (which stays testable without Celery).
Tasks must be idempotent: the outbox and late acknowledgement both mean a
task can run more than once.
"""

import uuid

from app.core import outbox
from app.core.db import SessionLocal
from app.core.tenancy import scope_to_school
from app.modules.accounting import export_jobs
from app.modules.accounts import tasks as accounts_tasks
from app.modules.fees import billing_runs
from app.modules.notifications import service as notifications
from app.modules.notifications.sms import SmsError
from app.modules.parents import tasks as parents_tasks
from app.modules.payments import accounts as payment_accounts
from app.modules.payments import settlements, webhooks
from app.modules.platform import signup as platform_signup
from app.modules.platform import tasks as platform_tasks
from app.modules.privacy import service as privacy
from app.workers.celery_app import celery_app


@celery_app.task(name="system.ping")
def ping() -> str:
    """Liveness check for the worker."""
    return "pong"


@celery_app.task(name="outbox.sweep")
def outbox_sweep() -> int:
    return outbox.sweep()


@celery_app.task(name="accounts.send_password_link", autoretry_for=(OSError,), retry_backoff=True, max_retries=5)
def send_password_link(user_id: str, purpose: str) -> None:
    accounts_tasks.send_password_link(user_id, purpose)


@celery_app.task(name="accounts.prune_sessions")
def prune_sessions() -> int:
    return accounts_tasks.prune_sessions()


@celery_app.task(name="payments.process_webhook")
def process_webhook(event_id: str) -> str:
    """
    Record the payment behind a gateway webhook. If the gateway can't be reached,
    the event stays 'received' and `payments.requeue_webhooks` tries again later.
    """
    try:
        return webhooks.process_event(uuid.UUID(event_id)).value
    except Exception as exc:
        webhooks.handle_failure(uuid.UUID(event_id), exc)
        raise


@celery_app.task(name="payments.requeue_webhooks")
def requeue_webhooks() -> int:
    return webhooks.requeue_stuck()


@celery_app.task(name="payments.provision_virtual_accounts")
def provision_virtual_accounts(school_id: str, student_ids: list[str]) -> int:
    with SessionLocal() as session:
        scope_to_school(session, uuid.UUID(school_id))
        result = payment_accounts.provision_virtual_accounts(
            session, uuid.UUID(school_id), [uuid.UUID(s) for s in student_ids]
        )
    return result.created


@celery_app.task(name="parents.send_code", autoretry_for=(SmsError,), retry_backoff=True, max_retries=3)
def send_parent_code(to: str, code: str) -> None:
    parents_tasks.send_code(to, code)


@celery_app.task(name="parents.prune")
def prune_parent_records() -> int:
    return parents_tasks.prune()


@celery_app.task(name="notifications.deliver")
def deliver_message(school_id: str, message_id: str) -> str:
    return notifications.deliver(uuid.UUID(school_id), uuid.UUID(message_id)).value


@celery_app.task(name="notifications.schedule_reminders")
def schedule_reminders() -> int:
    return notifications.schedule_reminders()


@celery_app.task(name="notifications.retry_unsent")
def retry_unsent_messages() -> int:
    return notifications.retry_unsent()


@celery_app.task(name="platform.send_subscription_invoice", autoretry_for=(OSError,), retry_backoff=True, max_retries=5)
def send_subscription_invoice(invoice_id: str) -> int:
    return platform_tasks.send_subscription_invoice(invoice_id)


@celery_app.task(name="platform.signup_existing_account", autoretry_for=(OSError,), retry_backoff=True, max_retries=5)
def signup_existing_account(user_id: str) -> None:
    platform_tasks.signup_existing_account(user_id)


@celery_app.task(name="platform.prune_pending_schools")
def prune_pending_schools() -> int:
    return platform_signup.prune_pending()


@celery_app.task(name="privacy.apply_retention")
def apply_retention() -> dict[str, int]:
    return privacy.apply_retention()


@celery_app.task(name=settlements.SYNC_TASK)
def sync_settlements(school_id: str) -> int:
    return settlements.sync_school(uuid.UUID(school_id)).settlements


@celery_app.task(name="payments.sync_all_settlements")
def sync_all_settlements() -> int:
    return settlements.sync_all()


@celery_app.task(name=export_jobs.BUILD_TASK)
def build_export(school_id: str, export_id: str) -> str:
    with SessionLocal() as session:
        scope_to_school(session, uuid.UUID(school_id))
        return export_jobs.build(session, uuid.UUID(export_id)).value


@celery_app.task(name=billing_runs.TASK)
def generate_bills(school_id: str, run_id: str) -> str:
    return billing_runs.run_school(uuid.UUID(school_id), uuid.UUID(run_id))
