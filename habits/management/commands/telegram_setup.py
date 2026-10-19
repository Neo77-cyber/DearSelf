from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from habits.services.telegram import call


class Command(BaseCommand):
    help = "Point the Telegram bot at this site's webhook and register its commands."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_WEBHOOK_SECRET:
            raise CommandError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET first.")

        url = settings.SITE_URL.rstrip("/") + reverse("telegram_webhook")
        if not url.startswith("https://"):
            raise CommandError(f"Telegram needs an https webhook, got {url}. Set SITE_URL.")

        if call(
            "setWebhook",
            url=url,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        ) is None:
            raise CommandError("setWebhook failed; see the log above.")
        call(
            "setMyCommands",
            commands=[
                {"command": "done", "description": "Clock in everything for today"},
                {"command": "status", "description": "Your streaks"},
                {"command": "dashboard", "description": "A one-time link to the website"},
            ],
        )
        self.stdout.write(self.style.SUCCESS(f"Webhook set to {url}"))
