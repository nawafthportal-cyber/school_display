from unittest.mock import patch

from django.test import TestCase, override_settings

from core.health import readiness_status


class ReadinessTests(TestCase):
    def test_healthy_endpoint_checks_database_and_cache(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "ok", "checks": {"database": True, "cache": True, "channels": True}
        })
        self.assertIn("no-store", response["Cache-Control"])

    @patch("core.health.connection.cursor", side_effect=RuntimeError("secret-database-url"))
    def test_database_failure_returns_503_without_connection_details(self, cursor):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["checks"]["database"])
        self.assertNotIn("secret-database-url", response.content.decode())

    @patch("core.health.cache.get", return_value=None)
    def test_silent_cache_write_failure_is_not_healthy(self, get):
        self.assertFalse(readiness_status()["cache"])

    @patch("core.health.cache.set", side_effect=RuntimeError("cache-down"))
    def test_cache_failure_returns_503(self, set):
        self.assertEqual(self.client.get("/health/").status_code, 503)

    @override_settings(
        CHANNEL_LAYERS={"default": {"BACKEND": "channels_redis.core.RedisChannelLayer"}},
        CHANNELS_REDIS_URL="redis://unused:6379/0",
    )
    @patch("core.health.Redis.from_url")
    def test_channels_redis_is_checked_independently(self, factory):
        factory.return_value.__enter__.return_value.ping.side_effect = OSError("channels-down")
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["checks"]["cache"])
        self.assertFalse(response.json()["checks"]["channels"])

    @override_settings(DEBUG=False, RUNNING_TESTS=False)
    def test_in_memory_channels_are_not_production_ready(self):
        self.assertFalse(readiness_status()["channels"])
