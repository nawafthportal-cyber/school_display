from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from core.utils import validate_display_token
from subscriptions.features import school_disabled_features

from .models import StandbyAssignment
from .api_serializers import StandbySerializer


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def today_standby(request):
    screen, school = validate_display_token(request)
    if not screen or not school:
        return Response({"detail": "Forbidden"}, status=403)

    today = timezone.localdate()
    qs = (
        StandbyAssignment.objects.filter(school=school, date=today)
        .order_by("period_index", "teacher_name")
    )
    if "standby" in school_disabled_features(school.pk):
        qs = qs.none()

    data = {
        "date": today.isoformat(),
        "items": StandbySerializer(qs, many=True).data,
    }
    return Response(data, status=200)
