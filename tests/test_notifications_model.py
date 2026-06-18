from src.config.constants import NotificationType
from src.db.models import Notification


def test_notification_type_values():
    assert NotificationType.JOB_COMPLETED == "job_completed"
    assert NotificationType.JOB_FAILED == "job_failed"
    assert NotificationType.PAYMENT_FAILED == "payment_failed"
    assert {t.value for t in NotificationType} == {
        "job_completed", "job_failed", "payment_failed",
    }


def test_notification_model_columns():
    cols = Notification.__table__.columns.keys()
    assert set(cols) == {
        "id", "user_id", "type", "job_id", "detail", "read_at", "created_at",
    }
    assert Notification.__tablename__ == "notifications"
    # user_id is FK to users with cascade delete
    fks = list(Notification.__table__.c.user_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"
