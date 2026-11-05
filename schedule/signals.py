import logging
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone

from schedule.cache_utils import (
    bump_schedule_revision_for_school_id_debounced,
    get_schedule_revision_for_school_id,
    invalidate_display_snapshot_cache_for_school_id,
)
from display.ws_groups import school_group_name
from schedule.models import Break, ClassLesson, DaySchedule, DisplayTheme, Period, SchoolClosure, SchoolSettings

logger = logging.getLogger(__name__)


def _arm_force_refresh_for_school(
    school_id: int,
    *,
    ttl_sec: int = 120,
    schedule_revision: int | None = None,
) -> None:
    """Force active display clients to fetch one fresh snapshot.

    This works even when revision bump is debounced (same revision), by using
    token-scoped force-refresh flags consumed by /api/display/status.
    """
    school_id = int(school_id or 0)
    if not school_id:
        return

    try:
        from core.models import DisplayScreen

        screens = DisplayScreen.objects.filter(school_id=school_id, is_active=True).only("token")
    except Exception:
        return

    for screen in screens:
        token_value = (getattr(screen, "token", "") or "").strip()
        if not token_value:
            continue
        try:
            token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
            cache.set(f"display:force_refresh:{token_hash}", "1", timeout=int(ttl_sec))
            cache.delete(f"display_ctx:{token_value}")
            if schedule_revision is not None:
                cache.delete(f"display_ctx:{token_value}:rev:{int(schedule_revision)}")
        except Exception:
            continue


def _broadcast_invalidate_ws(school_id: int, revision: int) -> None:
    """
    Broadcast invalidate message to WebSocket clients (async via channel layer).
    
    Called from transaction.on_commit() to ensure DB commit before notification.
    Only runs if DISPLAY_WS_ENABLED=True.
    """
    if not getattr(settings, "DISPLAY_WS_ENABLED", False):
        return

    # Do not debounce/drop update events here. The display client already
    # coalesces snapshot fetches after receiving WS invalidations; suppressing
    # a second nearby event can leave ws-live screens stale until a manual
    # refresh when two dashboard saves happen close together.
    
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        
        group_name = school_group_name(school_id)
        
        # Broadcast to all clients in school group. The consumer emits the
        # push-first client event `snapshot_refresh`; older clients that still
        # understand `invalidate` are no longer the target path.
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "broadcast_invalidate",  # maps to DisplayConsumer.broadcast_invalidate
                "school_id": school_id,
                "revision": revision,
                "reason": "content_changed",
            }
        )
        
        logger.info(
            f"WS broadcast sent: group={group_name} revision={revision}"
        )
    except Exception as e:
        # Never crash signals due to WS errors
        logger.exception(f"WS broadcast failed school_id={school_id}: {e}")


def _bump_and_invalidate(*, school_id: int, reason: str, model_label: str) -> None:
    """Source-of-truth invalidation.

    schedule_revision MUST bump on any change that can affect the display snapshot.

    Snapshot data sources (must be covered by signals):
    - schedule.SchoolSettings (theme/settings/display flags)
    - schedule.DaySchedule / schedule.Period / schedule.Break (timetable)
    - schedule.ClassLesson (period_classes overlay)
    - schedule.DutyAssignment (duty panel)
    - notices.Announcement (announcements ribbon)
    - notices.Excellence (honor/excellence board)
    - standby.StandbyAssignment (standby/waiting list)
    - core.School (name/logo/media that appears in snapshot.settings)
    """

    school_id = int(school_id or 0)
    if not school_id:
        return

    old_rev = get_schedule_revision_for_school_id(school_id) or 0
    did_bump = bump_schedule_revision_for_school_id_debounced(school_id=school_id)

    if not did_bump:
        # Debounce should collapse revision churn, not block immediate display updates.
        current_rev = get_schedule_revision_for_school_id(school_id) or 0
        invalidate_display_snapshot_cache_for_school_id(school_id)
        _arm_force_refresh_for_school(school_id, schedule_revision=int(current_rev or 0))

        transaction.on_commit(lambda: _broadcast_invalidate_ws(school_id, int(current_rev or 0)))

        logger.info(
            "schedule_revision debounce skip school_id=%s reason=%s model=%s",
            school_id,
            reason,
            model_label,
        )
        return

    new_rev = get_schedule_revision_for_school_id(school_id) or 0
    invalidate_display_snapshot_cache_for_school_id(school_id)
    _arm_force_refresh_for_school(school_id, schedule_revision=int(new_rev or 0))

    logger.info(
        "schedule_revision bumped school_id=%s old_rev=%s new_rev=%s reason=%s model=%s",
        school_id,
        int(old_rev),
        int(new_rev),
        reason,
        model_label,
    )

    def _enqueue_snapshot_materialization() -> None:
        try:
            from schedule.snapshot_materializer import (
                enqueue_snapshot_build,
                snapshot_async_build_enabled,
                snapshot_queue_available,
                snapshot_worker_status,
            )

            async_enabled = bool(snapshot_async_build_enabled())
            queue_available = bool(snapshot_queue_available())
            logger.info(
                "snapshot_queue signal_check school_id=%s rev=%s async_enabled=%s queue_available=%s reason=%s model=%s",
                school_id,
                int(new_rev or 0),
                async_enabled,
                queue_available,
                reason,
                model_label,
            )

            if not async_enabled or not queue_available:
                logger.info(
                    "snapshot_queue enqueue_skipped school_id=%s rev=%s async_enabled=%s queue_available=%s reason=%s model=%s",
                    school_id,
                    int(new_rev or 0),
                    async_enabled,
                    queue_available,
                    reason,
                    model_label,
                )
                return

            worker_status = snapshot_worker_status()
            if not bool(worker_status.get("alive")):
                logger.warning(
                    "snapshot_queue enqueue skipped: worker_unavailable school_id=%s rev=%s",
                    school_id,
                    int(new_rev or 0),
                )
                return

            day_key = timezone.localdate().isoformat()
            logger.info(
                "snapshot_queue enqueue_attempt school_id=%s rev=%s day_key=%s reason=%s source=signal model=%s",
                school_id,
                int(new_rev or 0),
                day_key,
                "signal_revision_bump",
                model_label,
            )
            queue_result = enqueue_snapshot_build(
                school_id=school_id,
                rev=int(new_rev),
                day_key=day_key,
                reason="signal_revision_bump",
            )
            try:
                job = queue_result.get("job") if isinstance(queue_result, dict) else None
                job_id = job.get("job_id") if isinstance(job, dict) else None
                logger.info(
                    "snapshot_queue enqueue_result school_id=%s rev=%s result_queued=%s result_reason=%s deduped=%s debounced=%s coalesced=%s latest_rev=%s job_id=%s source=signal",
                    school_id,
                    int(new_rev or 0),
                    queue_result.get("queued") if isinstance(queue_result, dict) else None,
                    queue_result.get("reason") if isinstance(queue_result, dict) else None,
                    queue_result.get("deduped") if isinstance(queue_result, dict) else None,
                    queue_result.get("debounced") if isinstance(queue_result, dict) else None,
                    queue_result.get("coalesced") if isinstance(queue_result, dict) else None,
                    queue_result.get("latest_rev") if isinstance(queue_result, dict) else None,
                    job_id,
                )
            except Exception:
                logger.info(
                    "snapshot_queue enqueue_result school_id=%s rev=%s result=%s source=signal",
                    school_id,
                    int(new_rev or 0),
                    queue_result,
                )
        except Exception:
            try:
                failed_day_key = timezone.localdate().isoformat()
            except Exception:
                failed_day_key = ""
            logger.error(
                "snapshot_queue enqueue_failed school_id=%s rev=%s day_key=%s reason=%s model=%s source=signal",
                school_id,
                int(new_rev or 0),
                failed_day_key,
                reason,
                model_label,
            )
            logger.exception("snapshot_queue enqueue failed school_id=%s rev=%s", school_id, new_rev)

    # Broadcast to WebSocket clients (after transaction commits)
    transaction.on_commit(lambda: _broadcast_invalidate_ws(school_id, new_rev))
    transaction.on_commit(_enqueue_snapshot_materialization)

@receiver(post_save, sender=SchoolSettings)
def clear_display_cache_on_settings_change(sender, instance, **kwargs):
    """
    Clears the display context cache for all screens associated with a school
    when its SchoolSettings are updated.
    """
    school_id = int(getattr(instance, "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason="post_save", model_label="schedule.SchoolSettings")


@receiver(post_save, sender=DisplayTheme)
@receiver(post_delete, sender=DisplayTheme)
def clear_display_cache_on_theme_change(sender, instance, **kwargs):
    _bump_and_invalidate(school_id=instance.school_id, reason="custom_theme", model_label="schedule.DisplayTheme")


@receiver(post_save, sender=DaySchedule)
@receiver(post_delete, sender=DaySchedule)
def clear_display_cache_on_day_schedule_change(sender, instance, **kwargs):
    school_id = int(getattr(getattr(instance, "settings", None), "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.DaySchedule")


@receiver(post_save, sender=SchoolClosure)
@receiver(post_delete, sender=SchoolClosure)
def clear_display_cache_on_closure_change(sender, instance, **kwargs):
    """رفع رقم المراجعة يُسقط كاش الإجازات من نفسه.

    مفتاح كاش الإجازات يحمل ``schedule_revision``، فمدير المدرسة الذي يسجّل
    إجازة الغد يراها نافذة فوراً بدل أن ينتظر انتهاء مهلة الكاش.
    """
    school_id = int(getattr(getattr(instance, "settings", None), "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.SchoolClosure")


@receiver(post_save, sender=Period)
@receiver(post_delete, sender=Period)
def clear_display_cache_on_period_change(sender, instance, **kwargs):
    day = getattr(instance, "day", None)
    school_id = int(getattr(getattr(day, "settings", None), "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.Period")


@receiver(post_save, sender=Break)
@receiver(post_delete, sender=Break)
def clear_display_cache_on_break_change(sender, instance, **kwargs):
    day = getattr(instance, "day", None)
    school_id = int(getattr(getattr(day, "settings", None), "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.Break")


@receiver(post_save, sender=ClassLesson)
@receiver(post_delete, sender=ClassLesson)
def clear_display_cache_on_class_lesson_change(sender, instance, **kwargs):
    school_id = int(getattr(getattr(instance, "settings", None), "school_id", 0) or 0)
    _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.ClassLesson")


# -----------------------------
# Core entities that affect snapshot rendering
# -----------------------------

try:
    from schedule.models import SchoolClass

    @receiver(post_save, sender=SchoolClass)
    @receiver(post_delete, sender=SchoolClass)
    def clear_display_cache_on_school_class_change(sender, instance, **kwargs):
        settings_obj = getattr(instance, "settings", None)
        school_id = int(getattr(settings_obj, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.SchoolClass")
except Exception:
    SchoolClass = None


try:
    from schedule.models import Subject

    @receiver(post_save, sender=Subject)
    @receiver(post_delete, sender=Subject)
    def clear_display_cache_on_subject_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.Subject")
except Exception:
    Subject = None


try:
    from schedule.models import Teacher

    @receiver(post_save, sender=Teacher)
    @receiver(post_delete, sender=Teacher)
    def clear_display_cache_on_teacher_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.Teacher")
except Exception:
    Teacher = None


# -----------------------------
# Additional snapshot sources
# -----------------------------

try:
    from schedule.models import DutyAssignment

    @receiver(post_save, sender=DutyAssignment)
    @receiver(post_delete, sender=DutyAssignment)
    def clear_display_cache_on_duty_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="schedule.DutyAssignment")
except Exception:
    DutyAssignment = None


try:
    from notices.models import Announcement, Excellence

    @receiver(post_save, sender=Announcement)
    @receiver(post_delete, sender=Announcement)
    def clear_display_cache_on_announcement_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="notices.Announcement")

    @receiver(post_save, sender=Excellence)
    @receiver(post_delete, sender=Excellence)
    def clear_display_cache_on_excellence_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="notices.Excellence")
except Exception:
    Announcement = None
    Excellence = None


try:
    from standby.models import StandbyAssignment

    @receiver(post_save, sender=StandbyAssignment)
    @receiver(post_delete, sender=StandbyAssignment)
    def clear_display_cache_on_standby_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "school_id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason=sender.__name__, model_label="standby.StandbyAssignment")
except Exception:
    StandbyAssignment = None


try:
    from core.models import School

    @receiver(post_save, sender=School)
    def clear_display_cache_on_school_change(sender, instance, **kwargs):
        school_id = int(getattr(instance, "id", 0) or 0)
        _bump_and_invalidate(school_id=school_id, reason="post_save", model_label="core.School")
except Exception:
    School = None


try:
    from notices.models import EmergencyAlert

    def _emergency_alert_school_ids(instance) -> list[int]:
        try:
            return [int(pk) for pk in instance.schools.values_list("id", flat=True)]
        except Exception:
            return []

    @receiver(post_save, sender=EmergencyAlert)
    def clear_display_cache_on_emergency_alert_change(sender, instance, **kwargs):
        # عند الإنشاء تكون علاقة schools فارغة بعد؛ إشارة m2m_changed أدناه
        # تغطي لحظة الربط. هذا المستقبل يغطي تعديل/إلغاء تنبيه مرتبط أصلًا.
        for school_id in _emergency_alert_school_ids(instance):
            _bump_and_invalidate(school_id=school_id, reason="post_save", model_label="notices.EmergencyAlert")

    @receiver(pre_delete, sender=EmergencyAlert)
    def _stash_emergency_alert_schools_before_delete(sender, instance, **kwargs):
        # حذف التنبيه يزيل صفوف M2M قبل post_delete، فنلتقط المدارس هنا.
        instance._display_school_ids = _emergency_alert_school_ids(instance)

    @receiver(post_delete, sender=EmergencyAlert)
    def clear_display_cache_on_emergency_alert_delete(sender, instance, **kwargs):
        for school_id in getattr(instance, "_display_school_ids", None) or []:
            _bump_and_invalidate(school_id=school_id, reason="post_delete", model_label="notices.EmergencyAlert")

    @receiver(m2m_changed, sender=EmergencyAlert.schools.through)
    def clear_display_cache_on_emergency_alert_schools_change(sender, instance, action, reverse, pk_set, **kwargs):
        if action == "pre_clear" and not reverse:
            instance._display_school_ids = _emergency_alert_school_ids(instance)
            return
        if action not in ("post_add", "post_remove", "post_clear"):
            return

        school_ids: set[int] = set()
        if reverse:
            school_ids.add(int(getattr(instance, "id", 0) or 0))
        else:
            for pk in pk_set or ():
                school_ids.add(int(pk))
            if action == "post_clear":
                school_ids.update(getattr(instance, "_display_school_ids", None) or [])

        for school_id in school_ids:
            if school_id:
                _bump_and_invalidate(
                    school_id=school_id,
                    reason=f"m2m_{action}",
                    model_label="notices.EmergencyAlert",
                )

    @receiver(m2m_changed, sender=EmergencyAlert.screens.through)
    def clear_display_cache_on_emergency_alert_screens_change(sender, instance, action, reverse, **kwargs):
        if action not in ("post_add", "post_remove", "post_clear"):
            return
        if reverse:
            # instance هنا شاشة عرض؛ مدرستها معروفة مباشرة.
            school_id = int(getattr(instance, "school_id", 0) or 0)
            if school_id:
                _bump_and_invalidate(school_id=school_id, reason=f"m2m_{action}", model_label="notices.EmergencyAlert")
            return
        for school_id in _emergency_alert_school_ids(instance):
            _bump_and_invalidate(school_id=school_id, reason=f"m2m_{action}", model_label="notices.EmergencyAlert")
except Exception:
    EmergencyAlert = None
