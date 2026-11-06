"""Receipts use authenticated cloud delivery or a non-public local directory."""

from pathlib import Path

import cloudinary
import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.urls import reverse
from django.utils.deconstruct import deconstructible


class AuthenticatedCloudReceipts(Storage):
    def __init__(self):
        credentials = getattr(settings, "CLOUDINARY_STORAGE", {})
        if credentials:
            cloudinary.config(cloud_name=credentials.get("CLOUD_NAME"), api_key=credentials.get("API_KEY"), api_secret=credentials.get("API_SECRET"), secure=True)

    def _save(self, name, content):
        import cloudinary.uploader

        result = cloudinary.uploader.upload(
            content, type="authenticated", resource_type="image", use_filename=True,
            folder=str(Path(name).parent).replace("\\", "/"), tags=["private-subscription-receipt"],
        )
        return result["public_id"]

    def _get_url(self, name):
        return cloudinary.CloudinaryResource(
            name, type="authenticated", default_resource_type="image",
        ).build_url(sign_url=True, secure=True)

    def _open(self, name, mode="rb"):
        response = requests.get(self._get_url(name), timeout=20)
        response.raise_for_status()
        return ContentFile(response.content, name=name)

    def exists(self, name):
        import cloudinary.api
        try:
            cloudinary.api.resource(name, type="authenticated", resource_type="image")
        except cloudinary.api.NotFound:
            return False
        return True

    def size(self, name):
        import cloudinary.api
        return cloudinary.api.resource(name, type="authenticated", resource_type="image")["bytes"]

    def delete(self, name):
        import cloudinary.uploader

        return cloudinary.uploader.destroy(name, type="authenticated", resource_type="image", invalidate=True)


@deconstructible
class PrivateReceiptStorage(Storage):
    def backend(self):
        if getattr(settings, "USE_CLOUD_STORAGE", False):
            return AuthenticatedCloudReceipts()
        return FileSystemStorage(location=getattr(settings, "PRIVATE_RECEIPT_ROOT", settings.BASE_DIR / "private_receipts"))

    def _save(self, name, content):
        return self.backend().save(name, content)

    def _open(self, name, mode="rb"):
        return self.backend().open(name, mode)

    def exists(self, name):
        return self.backend().exists(name)

    def size(self, name):
        return self.backend().size(name)

    def delete(self, name):
        return self.backend().delete(name)

    def url(self, name):
        # Never expose a provider delivery signature through a field widget.
        return reverse("dashboard:subscription_receipt", kwargs={"asset": name})
