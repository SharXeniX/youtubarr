from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from youtubarr import views

urlpatterns = [
    path("admin/", admin.site.urls),  # <-- use Django's admin
    path("", RedirectView.as_view(pattern_name="playlists"), name="home"),
    path("settings/", views.settings_view, name="settings"),
    path("playlists/", views.playlists_view, name="playlists"),
    path("items/", views.items_view, name="items"),

    # HTMX endpoints
    path("items/<int:item_id>/row/", views.item_row, name="item-row"),
    path("items/<int:item_id>/toggle-blacklist/", views.toggle_blacklist, name="toggle-blacklist"),
    path("items/<int:item_id>/edit/", views.edit_item, name="edit-item"),
    path("items/<int:item_id>/dismiss/", views.dismiss_item, name="dismiss-item"),
    path("items/<int:item_id>/restore/", views.restore_item, name="restore-item"),

    path("playlists/<int:playlist_id>/refresh/", views.refresh_playlist_view, name="refresh-playlist"),

    path("logs/", views.logs_view, name="logs"),
    path("api/v1/logs", views.logs_api, name="logs-api"),
    path("api/v1/logs/clear", views.logs_clear, name="logs-clear"),

    path("oauth/start", views.oauth_start, name="oauth-start"),
    path("oauth/callback", views.oauth_callback, name="oauth-callback"),
    path("oauth/musicbrainz/start", views.mb_oauth_start, name="mb-oauth-start"),
    path("oauth/musicbrainz/callback", views.mb_oauth_callback, name="mb-oauth-callback"),

    path("api/v1/lidarr", views.lidarr_youtubarr_view, name="lidarr-youtubarr"),
    path("healthz", views.healthz, name="health"),
]
