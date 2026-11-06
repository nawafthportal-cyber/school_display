"""Preserve existing receipt assets while removing public delivery access."""

import json
import hashlib

import requests

import cloudinary.api
import cloudinary.uploader
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from subscriptions.models import SubscriptionRequest
from subscriptions.receipt_storage import AuthenticatedCloudReceipts


class Command(BaseCommand):
    help = "Inventory legacy receipts; --apply moves public Cloudinary originals to authenticated delivery."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if not getattr(settings, "USE_CLOUD_STORAGE", False):
            raise CommandError("This command is only for Cloudinary receipts.")
        backend = AuthenticatedCloudReceipts()
        receipts = SubscriptionRequest.objects.exclude(receipt_image="").order_by("pk")
        result = {"receipts": receipts.count(), "secured": 0, "already_private": 0, "dry_run": not options["apply"]}
        for obj in receipts:
            name = obj.receipt_image.name
            try:
                private = cloudinary.api.resource(name, type="authenticated", resource_type="image")
            except cloudinary.api.NotFound:
                private = None
            if private:
                result["already_private"] += 1
                continue
            public = cloudinary.api.resource(name, type="upload", resource_type="image")
            if options["apply"]:
                original = requests.get(public["secure_url"], timeout=20)
                original.raise_for_status()
                digest = hashlib.sha256(original.content).digest()
                moved = cloudinary.uploader.rename(name, name, type="upload", to_type="authenticated",
                                                   resource_type="image", invalidate=True, overwrite=False)
                if moved.get("asset_id") != public.get("asset_id"):
                    raise CommandError(f"Receipt asset identity check failed for request {obj.pk}")
                verified = cloudinary.api.resource(name, type="authenticated", resource_type="image")
                if (verified.get("etag"), verified.get("bytes")) != (public.get("etag"), public.get("bytes")):
                    raise CommandError(f"Receipt content preservation check failed for request {obj.pk}")
                with backend.open(name, "rb") as secured_file:
                    if hashlib.sha256(secured_file.read()).digest() != digest:
                        raise CommandError(f"Receipt content hash changed for request {obj.pk}")
                try:
                    cloudinary.api.resource(name, type="upload", resource_type="image")
                except cloudinary.api.NotFound:
                    result["secured"] += 1
                else:
                    raise CommandError(f"Public delivery still exists for request {obj.pk}")
        self.stdout.write(json.dumps(result))
