import json

from django.core.management.base import BaseCommand

from mailcenter.reconciliation import reconcile_delivery


class Command(BaseCommand):
    help = "Review delivery status with Resend without sending or retrying any message."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(reconcile_delivery(limit=options["limit"], apply=options["apply"])))
