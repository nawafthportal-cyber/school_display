from __future__ import annotations

from email.utils import parseaddr

from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from dashboard.decorators import system_permission_required
from subscriptions.models import SubscriptionEmailNotification

from .forms import ComposeMailForm, WhatsAppContactForm
from .models import MailMessage
from .services import sender_address, sync_message_content
from .reconciliation import delivery_review_queryset
from .whatsapp import whatsapp_school_contacts


@system_permission_required("mail.view")
def system_mail_list(request):
    folder = (request.GET.get("folder") or "inbound").strip()
    if folder not in {"inbound", "outbound", "all"}:
        folder = "inbound"
    status = (request.GET.get("status") or "").strip()
    valid_statuses = {value for value, _label in MailMessage.Status.choices}
    query = (request.GET.get("q") or "").strip()

    queryset = MailMessage.objects.all()
    if request.GET.get("attention") == "1":
        queryset = queryset.filter(pk__in=delivery_review_queryset().values("pk"))
    if folder != "all":
        queryset = queryset.filter(direction=folder)
    if status in valid_statuses:
        queryset = queryset.filter(status=status)
    if query:
        queryset = queryset.filter(
            Q(subject__icontains=query)
            | Q(from_address__icontains=query)
            | Q(provider_id__icontains=query)
            | Q(internet_message_id__icontains=query)
        )

    paginator = Paginator(queryset, 30)
    page_obj = paginator.get_page(request.GET.get("page"))
    counts = {
        "inbound": MailMessage.objects.filter(direction=MailMessage.Direction.INBOUND).count(),
        "outbound": MailMessage.objects.filter(direction=MailMessage.Direction.OUTBOUND).count(),
        "unread": MailMessage.objects.filter(
            direction=MailMessage.Direction.INBOUND,
            is_read=False,
        ).count(),
        "failed": MailMessage.objects.filter(
            direction=MailMessage.Direction.OUTBOUND,
            status__in=(MailMessage.Status.FAILED, MailMessage.Status.BOUNCED, MailMessage.Status.SUPPRESSED, MailMessage.Status.COMPLAINED),
        ).count(),
    }
    status_summary = dict(
        MailMessage.objects.filter(direction=MailMessage.Direction.OUTBOUND)
        .values_list("status")
        .annotate(total=Count("id"))
    )
    mail_health = {
        "transactional_enabled": bool(settings.TRANSACTIONAL_EMAIL_ENABLED),
        "smtp_configured": bool(
            str(settings.EMAIL_HOST or "").strip()
            and str(settings.EMAIL_HOST_USER or "").strip()
            and str(settings.EMAIL_HOST_PASSWORD or "").strip()
        ),
        "webhook_configured": bool(str(settings.RESEND_WEBHOOK_SECRET or "").strip()),
        "inbound_enabled": bool(settings.RESEND_INBOUND_ENABLED),
        "inbound_address": settings.RESEND_INBOUND_ADDRESS,
    }
    queue = (
        SubscriptionEmailNotification.objects.select_related(
            "subscription__school", "invoice"
        )
        .order_by("-updated_at")[:12]
        if folder in {"outbound", "all"}
        else []
    )
    return render(
        request,
        "admin/mail_center_list.html",
        {
            "page_obj": page_obj,
            "folder": folder,
            "status_filter": status if status in valid_statuses else "",
            "status_choices": MailMessage.Status.choices,
            "query": query,
            "counts": counts,
            "status_summary": status_summary,
            "queue": queue,
            "mail_health": mail_health,
            "delivery_review_count": delivery_review_queryset().count(),
        },
    )


@system_permission_required("mail.view")
def system_mail_detail(request, pk: int):
    message = get_object_or_404(MailMessage.objects.prefetch_related("events"), pk=pk)
    if message.direction == MailMessage.Direction.INBOUND and not message.is_read:
        message.is_read = True
        message.read_at = timezone.now()
        message.read_by = request.user
        message.save(update_fields=("is_read", "read_at", "read_by", "updated_at"))
    return render(request, "admin/mail_center_detail.html", {"mail_message": message})


@system_permission_required("mail.manage")
@require_GET
def system_school_whatsapp(request):
    cards = whatsapp_school_contacts()
    return render(request, "admin/school_whatsapp.html", {
        "form": WhatsAppContactForm(),
        "school_cards": cards,
    })


@system_permission_required("mail.manage")
def system_mail_compose(request):
    initial = {}
    reply_message = None
    reply_pk = request.GET.get("reply") or request.POST.get("reply")
    if reply_pk:
        reply_message = MailMessage.objects.filter(
            pk=reply_pk,
            direction=MailMessage.Direction.INBOUND,
        ).first()
        if reply_message is not None:
            initial = {
                "recipients": parseaddr(reply_message.from_address)[1] or reply_message.from_address,
                "subject": (
                    reply_message.subject
                    if reply_message.subject.casefold().startswith("re:")
                    else f"Re: {reply_message.subject}"
                ),
            }

    if request.method == "POST":
        form = ComposeMailForm(request.POST)
        if form.is_valid():
            recipients = form.cleaned_data["recipients"]
            subject = form.cleaned_data["subject"]
            body = form.cleaned_data["body"]
            headers = {}
            if reply_message and reply_message.internet_message_id:
                headers = {
                    "In-Reply-To": reply_message.internet_message_id,
                    "References": reply_message.internet_message_id,
                }
            html_body = render_to_string(
                "emails/platform_manual.html",
                {"subject": subject, "body": body},
            )
            sent_messages = []
            failed_count = 0
            # Send a separate message to each manager so recipients from different
            # schools never see one another's email addresses.
            for recipient in recipients:
                local = MailMessage.objects.create(
                    direction=MailMessage.Direction.OUTBOUND,
                    status=MailMessage.Status.QUEUED,
                    from_address=sender_address(settings.DEFAULT_FROM_EMAIL),
                    to_addresses=[recipient],
                    reply_to_addresses=[sender_address(settings.EMAIL_REPLY_TO)],
                    subject=subject,
                    text_body=body,
                    preview=" ".join(body.split())[:500],
                )
                email = EmailMultiAlternatives(
                    subject=subject,
                    body=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[recipient],
                    reply_to=[sender_address(settings.EMAIL_REPLY_TO)],
                    headers=headers,
                )
                email.attach_alternative(html_body, "text/html")
                try:
                    sent = email.send(fail_silently=False)
                    if sent != 1:
                        raise RuntimeError(f"Email backend returned sent_count={sent}")
                except Exception as exc:
                    failed_count += 1
                    local.status = MailMessage.Status.FAILED
                    local.content_fetch_error = str(exc)[:500]
                    local.save(update_fields=("status", "content_fetch_error", "updated_at"))
                else:
                    local.status = MailMessage.Status.SENT
                    local.sent_at = timezone.now()
                    local.save(update_fields=("status", "sent_at", "updated_at"))
                    sent_messages.append(local)

            if sent_messages:
                messages.success(
                    request,
                    f"تم قبول {len(sent_messages)} رسالة للإرسال، وستُحدّث حالة التسليم تلقائيًا.",
                )
                if failed_count:
                    messages.warning(request, f"تعذر إرسال {failed_count} رسالة، وتم حفظ الأخطاء في السجل.")
                return redirect("dashboard:system_mail_detail", pk=sent_messages[0].pk)
            messages.error(request, "تعذر إرسال الرسالة. تم حفظ الخطأ في سجل البريد.")
    else:
        form = ComposeMailForm(initial=initial)
    return render(
        request,
        "admin/mail_center_compose.html",
        {"form": form, "reply_message": reply_message},
    )


@require_POST
@system_permission_required("mail.view")
def system_mail_sync(request, pk: int):
    message = get_object_or_404(MailMessage, pk=pk)
    if sync_message_content(message):
        messages.success(request, "تم تحديث محتوى الرسالة من Resend.")
    else:
        messages.warning(request, "تعذر تحديث المحتوى الآن. راجع إعداد مفتاح Resend.")
    return redirect("dashboard:system_mail_detail", pk=message.pk)


@require_POST
@system_permission_required("mail.manage")
def system_mail_queue_retry(request, pk: int):
    notification = get_object_or_404(SubscriptionEmailNotification, pk=pk)
    notification.status = SubscriptionEmailNotification.Status.PENDING
    notification.attempts = 0
    notification.available_at = timezone.now()
    notification.locked_at = None
    notification.last_error = ""
    notification.save(
        update_fields=(
            "status", "attempts", "available_at", "locked_at", "last_error", "updated_at"
        )
    )
    messages.success(request, "أعيدت الرسالة إلى قائمة الانتظار.")
    return redirect(f"{reverse('dashboard:system_mail_list')}?folder=outbound")
