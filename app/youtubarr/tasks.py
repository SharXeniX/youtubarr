import os, time, requests
from dateutil import parser as dateparser
from django.conf import settings
from django.db import transaction
from celery import shared_task
from .models import AppSettings, Playlist, TrackItem, Artist, Snapshot
from .utils import guess_artist_from_title
import json
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)
YT_API_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_API_PLAYLISTS = "https://www.googleapis.com/youtube/v3/playlists"
MB_API = "https://musicbrainz.org/ws/2/artist/"
MB_HEADERS = {"User-Agent": settings.MB_USER_AGENT}
MB_OAUTH_PATH = "/data/mb_oauth.json"


def _get_api_key():
    s = AppSettings.load()
    return s.youtube_api_key or settings.YOUTUBE_API_KEY


def _get_oauth_headers():
    """Return YouTube Authorization headers if oauth.json exists and token is valid."""
    json_path = "/data/oauth.json"
    if not os.path.exists(json_path):
        return None
    with open(json_path) as f:
        oauth = json.load(f)
    if oauth.get("expires_at", 0) < time.time() + 60:
        logger.info("YouTube OAuth token expired, refreshing...")
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.YOUTUBE_OAUTH_CLIENT_ID,
            "client_secret": settings.YOUTUBE_OAUTH_CLIENT_SECRET,
            "refresh_token": oauth["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=30)
        if r.status_code == 200:
            data = r.json()
            oauth["access_token"] = data["access_token"]
            oauth["expires_at"] = int(time.time()) + data["expires_in"]
            oauth["expires_in"] = data["expires_in"]
            with open(json_path, "w") as f:
                json.dump(oauth, f, indent=2)
            logger.info("YouTube OAuth token refreshed successfully")
        else:
            logger.error("Failed to refresh YouTube OAuth token: %s", r.text[:200])
            return None
    return {"Authorization": f"Bearer {oauth['access_token']}"}


def fetch_playlist_items(playlist: Playlist):
    yt_playlist_id = "LL" if playlist.playlist_id == "LM" else playlist.playlist_id

    api_key = _get_api_key()
    oauth_headers = _get_oauth_headers()
    if not api_key and not oauth_headers:
        logger.error("No YouTube API key or OAuth token configured")
        return 0

    if yt_playlist_id == "LL" and not oauth_headers:
        logger.error("Liked Music requires OAuth. Connect your YouTube account in Settings.")
        return 0
    headers = oauth_headers or {}
    key_param = {"key": api_key} if api_key and not oauth_headers else {}

    # --- Fetch playlist metadata ---
    if playlist.playlist_id == "LM":
        playlist.title = playlist.title or "Liked Music"
        playlist.channel_title = playlist.channel_title or "YouTube Music"
        playlist.last_synced = timezone.now()
        playlist.save(update_fields=["title", "channel_title", "last_synced"])
    else:
        meta_params = {"part": "snippet", "id": yt_playlist_id, **key_param}
        rmeta = requests.get(YT_API_PLAYLISTS, params=meta_params, headers=headers, timeout=30)
        if rmeta.status_code == 200:
            meta = rmeta.json()
            items = meta.get("items", [])
            if items:
                sn = items[0].get("snippet", {})
                playlist.title = sn.get("title", playlist.title)
                playlist.channel_title = sn.get("channelTitle", playlist.channel_title)
                playlist.last_synced = timezone.now()
                playlist.save(update_fields=["title", "channel_title", "last_synced"])
        else:
            logger.warning("YouTube metadata API returned %s for %s", rmeta.status_code, playlist.playlist_id)

    # --- Fetch playlist items ---
    dismissed_vids = set(
        TrackItem.objects.filter(playlist=playlist, dismissed=True).values_list("video_id", flat=True)
    )
    is_liked = playlist.playlist_id == "LM"

    params = {
        "part": "snippet,contentDetails",
        "playlistId": yt_playlist_id,
        "maxResults": settings.YOUTUBE_QUOTA_SAFE_PAGE_SIZE,
        **key_param,
    }
    count = 0
    new_count = 0
    skipped_non_music = 0
    skipped_dismissed = 0
    page_num = 0
    url = YT_API_ITEMS
    while True:
        page_num += 1
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            logger.error("YouTube API returned %s for playlist %s (page %d): %s",
                         r.status_code, playlist.playlist_id, page_num, r.text[:200])
            break
        data = r.json()

        for it in data.get("items", []):
            sn = it.get("snippet", {})
            vd = sn.get("resourceId", {}).get("videoId")
            if not vd:
                continue
            title = sn.get("title", "")
            ch = sn.get("channelTitle", "")
            video_owner = sn.get("videoOwnerChannelTitle", "")
            published = sn.get("publishedAt")

            if is_liked and "- Topic" not in video_owner:
                skipped_non_music += 1
                continue

            if vd in dismissed_vids:
                skipped_dismissed += 1
                continue

            artist_guess = guess_artist_from_title(title, video_owner or ch)

            with transaction.atomic():
                ti, created = TrackItem.objects.get_or_create(
                    playlist=playlist,
                    video_id=vd,
                    defaults=dict(
                        title=title,
                        channel_title=ch,
                        position=sn.get("position", 0),
                        published_at=dateparser.parse(published) if published else None,
                        artist_name_guess=artist_guess,
                    )
                )
                if created:
                    new_count += 1
                else:
                    changed = []
                    if sn.get("position", ti.position) != ti.position:
                        ti.position = sn.get("position", ti.position)
                        changed.append("position")
                    if artist_guess and not ti.artist_name_guess:
                        ti.artist_name_guess = artist_guess
                        changed.append("artist_name_guess")
                    if changed:
                        ti.save(update_fields=changed)

            count += 1

        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    logger.info("Playlist %s (%s): %d items processed, %d new, %d skipped (non-music: %d, dismissed: %d)",
                playlist.playlist_id, playlist.title, count, new_count, skipped_non_music + skipped_dismissed,
                skipped_non_music, skipped_dismissed)
    return count


def _get_mb_headers():
    """Return MusicBrainz headers with OAuth Bearer token if available."""
    headers = dict(MB_HEADERS)
    if os.path.exists(MB_OAUTH_PATH):
        with open(MB_OAUTH_PATH) as f:
            mb_oauth = json.load(f)
        if mb_oauth.get("expires_at", 0) < time.time() + 60 and mb_oauth.get("refresh_token"):
            logger.info("MusicBrainz OAuth token expired, refreshing...")
            try:
                r = requests.post("https://musicbrainz.org/oauth2/token", data={
                    "grant_type": "refresh_token",
                    "refresh_token": mb_oauth["refresh_token"],
                    "client_id": settings.MB_OAUTH_CLIENT_ID,
                    "client_secret": settings.MB_OAUTH_CLIENT_SECRET,
                }, headers=MB_HEADERS, timeout=30)
                if r.status_code == 200:
                    data = r.json()
                    mb_oauth["access_token"] = data["access_token"]
                    mb_oauth["expires_at"] = int(time.time()) + data.get("expires_in", 3600)
                    if "refresh_token" in data:
                        mb_oauth["refresh_token"] = data["refresh_token"]
                    with open(MB_OAUTH_PATH, "w") as f:
                        json.dump(mb_oauth, f, indent=2)
                    logger.info("MusicBrainz OAuth token refreshed successfully")
                else:
                    logger.error("Failed to refresh MusicBrainz token: HTTP %s - %s", r.status_code, r.text[:200])
            except Exception as e:
                logger.error("MusicBrainz token refresh error: %s", e)
        headers["Authorization"] = f"Bearer {mb_oauth['access_token']}"
    return headers


def search_mb_artist_mbid(name: str) -> str | None:
    if not name:
        return None
    params = {"query": f'artist:"{name}"', "fmt": "json"}
    headers = _get_mb_headers()
    try:
        with requests.Session() as s:
            r = s.get(MB_API, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                arts = r.json().get("artists") or []
                if arts:
                    return arts[0]["id"]
            elif r.status_code == 503:
                logger.warning("MusicBrainz rate limited (503) for '%s'", name)
            elif r.status_code == 401:
                logger.error("MusicBrainz auth failed (401). Reconnect in Settings.")
            else:
                logger.warning("MusicBrainz returned %s for '%s'", r.status_code, name)
    except Exception as e:
        logger.error("MusicBrainz lookup failed for '%s': %s", name, e)
    return None


@shared_task
def refresh_playlists():
    logger.info("=== Starting playlist refresh ===")
    updated = 0
    playlists = list(Playlist.objects.filter(enabled=True))
    logger.info("Found %d enabled playlists", len(playlists))
    for pl in playlists:
        try:
            logger.info("Fetching playlist %s (%s)...", pl.playlist_id, pl.title or "untitled")
            updated += fetch_playlist_items(pl)
        except Exception as e:
            logger.error("Failed to fetch playlist %s: %s", pl.playlist_id, e)
    logger.info("=== Playlist refresh complete: %d total items ===", updated)
    return updated


@shared_task
def resolve_missing_mbids():
    names_no_artist = set(
        TrackItem.objects
        .filter(blacklisted=False, dismissed=False, artist__isnull=True)
        .exclude(artist_name_guess="")
        .values_list("artist_name_guess", flat=True)
        .distinct()
    )
    names_no_mbid = set(
        Artist.objects.filter(mbid__isnull=True).values_list("name", flat=True)
    ) | set(
        Artist.objects.filter(mbid="").values_list("name", flat=True)
    )
    names = names_no_artist | names_no_mbid

    if not names:
        logger.info("=== MBID resolve: nothing to do (all artists resolved) ===")
        return

    has_mb_oauth = os.path.exists(MB_OAUTH_PATH)
    logger.info("=== Starting MBID resolve: %d artists to look up (OAuth: %s) ===",
                len(names), "yes" if has_mb_oauth else "no")

    resolved = 0
    failed = 0
    for name in names:
        try:
            mbid = search_mb_artist_mbid(name)
        except Exception as e:
            logger.error("MusicBrainz exception for '%s': %s", name, e)
            failed += 1
            continue
        time.sleep(1.1)  # MusicBrainz rate limit: 1 req/sec
        art, created = Artist.objects.get_or_create(name=name)
        if mbid and not art.mbid:
            art.mbid = mbid
            art.save()
            resolved += 1
            logger.info("Resolved '%s' -> %s", name, mbid)
        elif not mbid:
            failed += 1

    # Link TrackItems that now have an Artist row
    linked = 0
    for ti in TrackItem.objects.filter(artist__isnull=True).exclude(artist_name_guess=""):
        try:
            ti.artist = Artist.objects.get(name=ti.artist_name_guess)
            ti.save(update_fields=["artist"])
            linked += 1
        except Artist.DoesNotExist:
            pass

    logger.info("=== MBID resolve complete: %d resolved, %d failed, %d items linked ===", resolved, failed, linked)


@shared_task
def build_snapshot():
    logger.info("Building Lidarr snapshot...")
    mbids = (Artist.objects.exclude(mbid__isnull=True)
             .exclude(mbid__exact="")
             .filter(trackitem__blacklisted=False, trackitem__dismissed=False)
             .values_list("mbid", flat=True)
             .distinct())
    payload = [{"MusicBrainzId": mbid} for mbid in mbids]
    Snapshot.objects.create(payload=payload)
    logger.info("Lidarr snapshot created: %d unique artists with MBIDs", len(payload))
    return len(payload)


@shared_task
def refresh_all_and_snapshot():
    logger.info(">>> Full sync triggered <<<")
    refresh_playlists.delay()
    resolve_missing_mbids.delay()
    build_snapshot.delay()
