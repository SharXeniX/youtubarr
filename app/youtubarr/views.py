import json, time, secrets
from urllib.parse import urlencode

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.contrib import messages
from django.conf import settings
from .models import AppSettings, Playlist, TrackItem, Snapshot, LogEntry
from .tasks import refresh_all_and_snapshot

import requests as http_requests
import logging

logger = logging.getLogger(__name__)

def settings_view(request):
    import os
    s = AppSettings.load()
    if request.method == "POST":
        s.youtube_api_key = request.POST.get("youtube_api_key","").strip()
        s.save()
        logger.info("YouTube API key updated via Settings")
        messages.success(request, "YouTube API key updated.")
        return redirect("settings")
    oauth_connected = os.path.exists("/data/oauth.json")
    oauth_configured = bool(settings.YOUTUBE_OAUTH_CLIENT_ID and settings.YOUTUBE_OAUTH_CLIENT_SECRET)
    mb_oauth_connected = os.path.exists("/data/mb_oauth.json")
    mb_oauth_configured = bool(settings.MB_OAUTH_CLIENT_ID and settings.MB_OAUTH_CLIENT_SECRET)
    return render(request, "settings.html", {
        "settings": s,
        "env_has_key": bool(settings.YOUTUBE_API_KEY),
        "lidarr_token": getattr(settings, "LIDARR_TOKEN", None),
        "oauth_connected": oauth_connected,
        "oauth_configured": oauth_configured,
        "mb_oauth_connected": mb_oauth_connected,
        "mb_oauth_configured": mb_oauth_configured,
    })

@require_http_methods(["GET","POST"])
def playlists_view(request):
    if request.method == "POST":
        pid = (request.POST.get("playlist_id") or "").strip()
        if pid:
            _, created = Playlist.objects.get_or_create(playlist_id=pid)
            if created:
                logger.info("Playlist added: %s — sync triggered", pid)
                refresh_all_and_snapshot.delay()
                messages.success(request, f"Added {pid} — sync started.")
            else:
                logger.info("Playlist add attempted but already exists: %s", pid)
                messages.success(request, f"{pid} already exists.")
        else:
            messages.error(request, "Playlist ID required.")
        return redirect("playlists")
    pls = Playlist.objects.all().order_by("-last_synced","playlist_id")
    return render(request, "playlists.html", {"playlists": pls})

def items_view(request):
    from django.core.paginator import Paginator
    from django.db.models import Q
    from collections import OrderedDict

    # Persist filters in session
    if request.GET:
        request.session["items_filters"] = {
            "q": request.GET.get("q", ""),
            "sort": request.GET.get("sort", "-published_at"),
            "status": request.GET.get("status", ""),
            "page": request.GET.get("page", "1"),
        }
    filters = request.session.get("items_filters", {})
    q = request.GET.get("q", filters.get("q", ""))
    sort = request.GET.get("sort", filters.get("sort", "-published_at"))
    status = request.GET.get("status", filters.get("status", ""))
    page_num = request.GET.get("page", filters.get("page", "1"))

    # Update session
    request.session["items_filters"] = {"q": q, "sort": sort, "status": status, "page": page_num}

    ALLOWED_SORTS = {
        "title": "title", "-title": "-title",
        "artist": "artist_name_guess", "-artist": "-artist_name_guess",
        "published_at": "published_at", "-published_at": "-published_at",
        "playlist": "playlist__playlist_id", "-playlist": "-playlist__playlist_id",
        "video": "video_id", "-video": "-video_id",
        "mbid": "artist__mbid", "-mbid": "-artist__mbid",
        "blacklisted": "blacklisted", "-blacklisted": "-blacklisted",
    }
    order = ALLOWED_SORTS.get(sort, "-published_at")

    qs = TrackItem.objects.select_related("playlist", "artist")

    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(artist_name_guess__icontains=q) | Q(video_id__icontains=q))
    if status == "dismissed":
        qs = qs.filter(dismissed=True)
    elif status == "blacklisted":
        qs = qs.filter(blacklisted=True)
    elif status == "active":
        qs = qs.filter(dismissed=False, blacklisted=False)

    qs = qs.order_by(order, "-id")

    # Merge duplicates by video_id: group items, collect playlist names
    all_items = list(qs)
    merged = OrderedDict()
    for it in all_items:
        if it.video_id in merged:
            merged[it.video_id]["playlists"].append(it.playlist.title or it.playlist.playlist_id)
            merged[it.video_id]["item_ids"].append(it.id)
        else:
            merged[it.video_id] = {
                "item": it,
                "playlists": [it.playlist.title or it.playlist.playlist_id],
                "item_ids": [it.id],
            }

    merged_list = list(merged.values())
    paginator = Paginator(merged_list, 50)
    page = paginator.get_page(page_num)

    return render(request, "items.html", {
        "page": page,
        "items": page.object_list,
        "q": q,
        "sort": sort,
        "status": status,
        "total": paginator.count,
    })

# ---- HTMX helpers ----

def item_row(request, item_id):
    it = get_object_or_404(TrackItem.objects.select_related("playlist","artist"), id=item_id)
    # Collect all playlists this video appears in
    playlists = list(
        TrackItem.objects.filter(video_id=it.video_id)
        .select_related("playlist")
        .values_list("playlist__title", "playlist__playlist_id")
    )
    pl_names = [title or pid for title, pid in playlists]
    return render(request, "partials/item_row.html", {"it": it, "playlists": pl_names})

@require_http_methods(["POST"])
def toggle_blacklist(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    # checkbox sends "on" when checked; missing when unchecked
    val = request.POST.get("blacklisted") == "on"
    if it.blacklisted != val:
        it.blacklisted = val
        it.save(update_fields=["blacklisted"])
        logger.info("Item %s '%s' blacklisted=%s", it.video_id, it.title[:40], val)
    return item_row(request, item_id)

@require_http_methods(["POST"])
def edit_item(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    title = request.POST.get("title", it.title)
    artist_guess = request.POST.get("artist_name_guess", it.artist_name_guess)
    changed = []
    if title != it.title:
        it.title = title
        changed.append("title")
    if artist_guess != it.artist_name_guess:
        it.artist_name_guess = artist_guess
        changed.append("artist_name_guess")
    if changed:
        it.save(update_fields=changed)
        logger.info("Item %s edited: %s", it.video_id, ", ".join(changed))
    return item_row(request, item_id)

@require_http_methods(["POST"])
def dismiss_item(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    it.dismissed = True
    it.save(update_fields=["dismissed"])
    logger.info("Item dismissed: %s '%s'", it.video_id, it.title[:40])
    return item_row(request, item_id)

def logs_view(request):
    return render(request, "logs.html")

def logs_api(request):
    page_num = int(request.GET.get("page", 1))
    per_page = int(request.GET.get("per_page", 50))
    after = request.GET.get("after")
    level = request.GET.get("level", "")

    qs = LogEntry.objects.all()
    if level:
        qs = qs.filter(level=level.upper())

    if after:
        # Poll mode: only new entries since last seen id
        qs = qs.filter(id__gt=int(after))
        entries = qs[:100]
        return JsonResponse({
            "entries": [
                {"id": e.id, "timestamp": e.timestamp.strftime("%Y-%m-%d %H:%M:%S"), "level": e.level, "message": e.message}
                for e in entries
            ],
        })

    # Paginated mode
    from django.core.paginator import Paginator
    paginator = Paginator(qs, per_page)
    page = paginator.get_page(page_num)
    return JsonResponse({
        "entries": [
            {"id": e.id, "timestamp": e.timestamp.strftime("%Y-%m-%d %H:%M:%S"), "level": e.level, "message": e.message}
            for e in page.object_list
        ],
        "page": page.number,
        "num_pages": paginator.num_pages,
        "total": paginator.count,
        "has_next": page.has_next(),
        "has_prev": page.has_previous(),
    })

@require_http_methods(["POST"])
def logs_clear(request):
    count = LogEntry.objects.count()
    LogEntry.objects.all().delete()
    logger.info("Logs cleared (%d entries deleted)", count)
    return JsonResponse({"ok": True})

@require_http_methods(["POST"])
def restore_item(request, item_id):
    it = get_object_or_404(TrackItem, id=item_id)
    it.dismissed = False
    it.save(update_fields=["dismissed"])
    logger.info("Item restored: %s '%s'", it.video_id, it.title[:40])
    return item_row(request, item_id)

def healthz(request):
    return HttpResponse("ok")

def lidarr_youtubarr_view(request):
    # token via ?token=... or X-Api-Key header
    token = request.GET.get("token") or request.headers.get("X-Api-Key")
    if not (settings.LIDARR_TOKEN and token == settings.LIDARR_TOKEN):
        return HttpResponseForbidden("missing/invalid token")
    snap = Snapshot.objects.order_by("-created_at").first()
    return JsonResponse(snap.payload if snap else [], safe=False)

@require_http_methods(["POST"])
def refresh_playlist_view(request, playlist_id):
    pl = get_object_or_404(Playlist, id=playlist_id)
    logger.info("Manual refresh triggered for playlist %s (%s)", pl.playlist_id, pl.title or "untitled")
    refresh_all_and_snapshot.delay()
    messages.success(request, f"Sync started for {pl.title or pl.playlist_id}.")
    return redirect("playlists")

def _oauth_callback_url():
    """Build the public OAuth callback URL."""
    return settings.OAUTH_REDIRECT_BASE.rstrip("/") + "/oauth/callback"

def oauth_start(request):
    """Redirect user to Google OAuth consent screen."""
    client_id = settings.YOUTUBE_OAUTH_CLIENT_ID
    if not client_id:
        messages.error(request, "YOUTUBE_OAUTH_CLIENT_ID not configured.")
        return redirect("settings")

    callback_url = _oauth_callback_url()

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/youtube",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


def oauth_callback(request):
    """Handle Google OAuth callback, exchange code for token, save oauth.json."""
    error = request.GET.get("error")
    if error:
        messages.error(request, f"OAuth error: {error}")
        return redirect("settings")

    state = request.GET.get("state")
    if state != request.session.pop("oauth_state", None):
        messages.error(request, "OAuth state mismatch. Try again.")
        return redirect("settings")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "No authorization code received.")
        return redirect("settings")

    callback_url = _oauth_callback_url()

    r = http_requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.YOUTUBE_OAUTH_CLIENT_ID,
        "client_secret": settings.YOUTUBE_OAUTH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": callback_url,
    }, timeout=30)
    token_data = r.json()

    if "access_token" not in token_data:
        messages.error(request, f"Token exchange failed: {token_data.get('error_description', token_data)}")
        return redirect("settings")

    oauth = {
        "scope": token_data["scope"],
        "token_type": token_data["token_type"],
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": int(time.time()) + token_data["expires_in"],
        "expires_in": token_data["expires_in"],
    }
    with open("/data/oauth.json", "w") as f:
        json.dump(oauth, f, indent=2)

    logger.info("YouTube OAuth connected successfully")
    messages.success(request, "YouTube OAuth connected successfully!")
    return redirect("settings")


def mb_oauth_start(request):
    """Redirect user to MusicBrainz OAuth consent screen."""
    client_id = settings.MB_OAUTH_CLIENT_ID
    if not client_id:
        messages.error(request, "MB_OAUTH_CLIENT_ID not configured.")
        return redirect("settings")

    callback_url = settings.OAUTH_REDIRECT_BASE.rstrip("/") + "/oauth/musicbrainz/callback"

    state = secrets.token_urlsafe(32)
    request.session["mb_oauth_state"] = state

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "profile",
        "access_type": "offline",
        "state": state,
    })
    return redirect(f"https://musicbrainz.org/oauth2/authorize?{params}")


def mb_oauth_callback(request):
    """Handle MusicBrainz OAuth callback."""
    error = request.GET.get("error")
    if error:
        messages.error(request, f"MusicBrainz OAuth error: {error}")
        return redirect("settings")

    state = request.GET.get("state")
    if state != request.session.pop("mb_oauth_state", None):
        messages.error(request, "OAuth state mismatch. Try again.")
        return redirect("settings")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "No authorization code received.")
        return redirect("settings")

    callback_url = settings.OAUTH_REDIRECT_BASE.rstrip("/") + "/oauth/musicbrainz/callback"

    r = http_requests.post("https://musicbrainz.org/oauth2/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.MB_OAUTH_CLIENT_ID,
        "client_secret": settings.MB_OAUTH_CLIENT_SECRET,
        "redirect_uri": callback_url,
    }, headers={"User-Agent": settings.MB_USER_AGENT}, timeout=30)
    token_data = r.json()

    if "access_token" not in token_data:
        messages.error(request, f"Token exchange failed: {token_data.get('error_description', token_data)}")
        return redirect("settings")

    mb_oauth = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data["token_type"],
        "expires_at": int(time.time()) + token_data.get("expires_in", 3600),
    }
    with open("/data/mb_oauth.json", "w") as f:
        json.dump(mb_oauth, f, indent=2)

    logger.info("MusicBrainz OAuth connected successfully")
    messages.success(request, "MusicBrainz account connected!")
    return redirect("settings")