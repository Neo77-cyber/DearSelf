from django.core.management.base import BaseCommand, CommandError

from habits.services.telegram import call
from habits.services.telegram_bot import handle_update


class Command(BaseCommand):
    help = (
        "Run the bot locally by polling Telegram instead of using the webhook. "
        "Use a separate test bot: polling doesn't work while a webhook is set."
    )

    def handle(self, *args, **options):
        info = call("getWebhookInfo")
        if info is None:
            raise CommandError("Couldn't reach Telegram. Is TELEGRAM_BOT_TOKEN set?")
        if info.get("url"):
            raise CommandError(
                f"This bot has a webhook ({info['url']}). Use a separate test bot locally."
            )

        self.stdout.write("Polling Telegram. Ctrl+C to stop.")
        offset = None
        while True:
            updates = call("getUpdates", _timeout=35, offset=offset, timeout=25) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    self.stderr.write(f"Update {update['update_id']} failed: {e}")
