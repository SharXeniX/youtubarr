import logging
from django.utils import timezone


class DBLogHandler(logging.Handler):
    def emit(self, record):
        try:
            from .models import LogEntry
            LogEntry.objects.create(
                timestamp=timezone.now(),
                level=record.levelname,
                message=self.format(record),
            )
            # Keep only last 500 entries
            count = LogEntry.objects.count()
            if count > 500:
                cutoff = LogEntry.objects.order_by("-timestamp").values_list("id", flat=True)[500]
                LogEntry.objects.filter(id__lt=cutoff).delete()
        except Exception:
            pass  # don't break the app if logging fails
