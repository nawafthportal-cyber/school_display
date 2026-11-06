import logging

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from PIL import Image, UnidentifiedImageError

from dashboard.access import has_system_permission
from subscriptions.models import SubscriptionRequest

logger = logging.getLogger(__name__)


@login_required(login_url="dashboard:login")
@never_cache
def subscription_receipt(request, asset):
    receipts = SubscriptionRequest.objects.filter(receipt_image=asset)
    if not has_system_permission(request.user, "subscription_requests.view"):
        receipts = receipts.filter(school__users__user=request.user)
    obj = get_object_or_404(receipts.distinct())
    try:
        file_obj = obj.receipt_image.open("rb")
    except Exception:
        logger.error("Private receipt unavailable request_id=%s", obj.pk)
        raise Http404("الإيصال غير متاح مؤقتاً.") from None
    try:
        image = Image.open(file_obj)
        content_type = Image.MIME.get(image.format, "application/octet-stream")
    except (UnidentifiedImageError, OSError):
        content_type = "application/octet-stream"
    file_obj.seek(0)
    response = FileResponse(file_obj, as_attachment=not content_type.startswith("image/"), filename="receipt", content_type=content_type)
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response
