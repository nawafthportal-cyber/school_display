from django.db import models as db_models
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from core.utils import validate_display_token
from subscriptions.features import school_disabled_features

from .models import Announcement, Excellence
from .api_serializers import AnnouncementSerializer, ExcellenceSerializer


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def active_announcements(request):
    screen, school = validate_display_token(request)
    if not screen or not school:
        return Response({"detail": "Forbidden"}, status=403)
    if "announcements" in school_disabled_features(school.pk):
        return Response({"items": []})

    now = timezone.now()
    qs = Announcement.objects.filter(
        school=school,
        is_active=True,
    ).filter(
        db_models.Q(starts_at__lte=now) | db_models.Q(starts_at__isnull=True),
    ).filter(
        db_models.Q(expires_at__gt=now) | db_models.Q(expires_at__isnull=True),
    ).order_by("-starts_at")[:100]

    return Response(
        {"items": AnnouncementSerializer(qs, many=True).data},
        status=200,
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def active_excellence(request):
    screen, school = validate_display_token(request)
    if not screen or not school:
        return Response({"detail": "Forbidden"}, status=403)
    if "excellence" in school_disabled_features(school.pk):
        return Response({"items": []})

    now = timezone.now()
    qs = Excellence.objects.filter(
        school=school,
    ).filter(
        db_models.Q(start_at__lte=now) | db_models.Q(start_at__isnull=True),
    ).filter(
        db_models.Q(end_at__isnull=True) | db_models.Q(end_at__gte=now),
    ).order_by("priority", "-start_at")[:100]

    serializer = ExcellenceSerializer(
        qs,
        many=True,
        context={"request": request},
    )

    return Response({"items": serializer.data}, status=200)
