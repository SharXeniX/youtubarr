import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
app = Celery('core')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

minutes = int(os.environ.get("REFRESH_MINUTES", "60"))
app.conf.beat_schedule = {
    "refresh_playlists_and_snapshot": {
        "task": "youtubarr.tasks.refresh_all_and_snapshot",
        "schedule": minutes * 60,
    },
}
