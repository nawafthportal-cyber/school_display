# schedule/api_urls.py
from django.urls import path
from . import api_views

app_name = "display_api"

urlpatterns = [
    # Status / polling endpoint
    path("status/", api_views.status, name="status"),
    path("status/<str:token>/", api_views.status, name="status_token"),

    # Canonical endpoint
    path("snapshot/", api_views.snapshot, name="snapshot"),
    path("snapshot/<str:token>/", api_views.snapshot, name="snapshot_token"),

    # Backward compatible aliases
    path("today/", api_views.snapshot, name="today"),
    path("today/<str:token>/", api_views.snapshot, name="today_token"),

    path("live/", api_views.snapshot, name="live"),
    path("live/<str:token>/", api_views.snapshot, name="live_token"),

    # Farewell beacon — lets an outage alert name its cause
    path("goodbye/", api_views.goodbye, name="goodbye"),
    path("goodbye/<str:token>/", api_views.goodbye, name="goodbye_token"),
    path("emergency-receipt/<int:alert_id>/", api_views.emergency_receipt, name="emergency_receipt"),

    # Health (keep original if simple)
    path("ping/", api_views.ping, name="ping"),

    # Debug / load-test metrics
    path("metrics/", api_views.metrics, name="metrics"),
    
    # WebSocket monitoring (for ops team)
    path("ws-metrics/", api_views.ws_metrics, name="ws_metrics"),
]
