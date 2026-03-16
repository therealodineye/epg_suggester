"""
EPG Suggester Plugin for Dispatcharr
=====================================
Scans channels without an EPG assignment and suggests the best matching
EPG entries using fuzzy name matching.

Matching pipeline:
  1. Normalise channel name  →  strip geo-prefixes, quality tags, misc noise
  2. Score every EPGData entry with a multi-strategy scorer:
       - Exact match after normalisation          → 100 pts
       - Token-set ratio (difflib)                → up to 90 pts
       - Word-overlap ratio                       → up to 60 pts
       - Contains / substring bonus               → up to 20 pts
  3. Return top-N suggestions above the configured threshold.

Installation path:  data/plugins/epg_suggester/
"""

from __future__ import annotations

import csv
import difflib
import logging
import re
import os
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger("plugins.epg_suggester")

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_GEO_PREFIX_RE = re.compile(
    r"^(?:"
    r"[A-Z]{2,4}\s*[|\-:]\s*"   # US| USA: UK| etc.
    r"|(?:USA?|UK|AU|CA|DE|FR|IT|ES|NL|BE|CH|AT|PL|SE|NO|DK|FI|NZ|ZA|BR|MX)\s*[|\-:]\s*"
    r")",
    re.IGNORECASE,
)

_QUALITY_TAGS_RE = re.compile(
    r"\b(?:4k|uhd|fhd|hd|sd|hevc|h265|h264|avc|hdr|sdr|1080[pi]?|720[pi]?|480[pi]?)\b",
    re.IGNORECASE,
)

_MISC_TAGS_RE = re.compile(
    r"\b(?:vip|backup|backup\d*|bkup|standby|plus|premium|extra|alt|reserve"
    r"|english|hindi|arabic|spanish|french|german|italian|portuguese"
    r"|\+1|\+2|24\/7|24x7)\b"
    r"|[\[\(][a-z0-9 ]{0,10}[\]\)]"   # short bracket content like [A] (B) [HD]
    r"|\s*\*+\s*",                     # asterisks used as flair
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(name: str, strip_geo: bool, strip_quality: bool, strip_misc: bool) -> str:
    """Return a cleaned, lower-cased version of *name* ready for comparison."""
    n = name.strip()
    if strip_geo:
        n = _GEO_PREFIX_RE.sub("", n).strip()
    if strip_quality:
        n = _QUALITY_TAGS_RE.sub(" ", n)
    if strip_misc:
        n = _MISC_TAGS_RE.sub(" ", n)
    n = _WHITESPACE_RE.sub(" ", n).strip().lower()
    return n


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def _score(channel_norm: str, epg_norm: str) -> int:
    """
    Return an integer score 0-100 indicating how well *epg_norm* matches
    *channel_norm*.  Higher is better.
    """
    if not channel_norm or not epg_norm:
        return 0

    # 1. Exact match
    if channel_norm == epg_norm:
        return 100

    # 2. difflib SequenceMatcher – token-set-style via sorted tokens
    ch_tokens = sorted(channel_norm.split())
    epg_tokens = sorted(epg_norm.split())
    ch_sorted = " ".join(ch_tokens)
    epg_sorted = " ".join(epg_tokens)

    ratio = difflib.SequenceMatcher(None, ch_sorted, epg_sorted).ratio()
    score = int(ratio * 90)  # up to 90 pts

    # 3. Word overlap bonus – rewards sharing many unique words
    ch_set = set(ch_tokens)
    epg_set = set(epg_tokens)
    if ch_set and epg_set:
        overlap = len(ch_set & epg_set) / max(len(ch_set), len(epg_set))
        score = max(score, int(overlap * 60))

    # 4. Substring / contains bonus
    if channel_norm in epg_norm or epg_norm in channel_norm:
        score = max(score, score + 20)

    return min(score, 99)  # exact match is the only 100


# ---------------------------------------------------------------------------
# Dispatcharr API client
# ---------------------------------------------------------------------------

class DispatcharrClient:
    """Thin wrapper around the Dispatcharr REST API."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._username = username
        self._password = password

    # ------------------------------------------------------------------
    def _auth(self) -> dict:
        """Return Authorization header dict, authenticating if needed."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        r = self._session.post(
            f"{self.base_url}/api/auth/token/",
            json={"username": self._username, "password": self._password},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self._token = data.get("access") or data.get("token")
        if not self._token:
            raise RuntimeError("Authentication succeeded but no token returned. Response: " + str(data))
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict | None = None) -> list | dict:
        headers = self._auth()
        r = self._session.get(
            f"{self.base_url}{path}",
            headers=headers,
            params=params or {},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        headers = self._auth()
        r = self._session.patch(
            f"{self.base_url}{path}",
            headers=headers,
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    def get_channels(self) -> list[dict]:
        """Return all channels (paginated internally)."""
        results, page = [], 1
        while True:
            data = self._get("/api/channels/channels/", {"page": page, "page_size": 500})
            batch = data.get("results", data) if isinstance(data, dict) else data
            if not batch:
                break
            results.extend(batch)
            if isinstance(data, dict) and not data.get("next"):
                break
            if len(batch) < 500:
                break
            page += 1
        return results

    def get_epg_data(self) -> list[dict]:
        """Return all EPGData entries (paginated internally)."""
        results, page = [], 1
        while True:
            data = self._get("/api/epg/epg-data/", {"page": page, "page_size": 1000})
            batch = data.get("results", data) if isinstance(data, dict) else data
            if not batch:
                break
            results.extend(batch)
            if isinstance(data, dict) and not data.get("next"):
                break
            if len(batch) < 1000:
                break
            page += 1
        return results

    def get_epg_sources(self) -> list[dict]:
        """Return all EPG source accounts."""
        data = self._get("/api/epg/epg-sources/")
        return data.get("results", data) if isinstance(data, dict) else data

    def assign_epg(self, channel_id: int, epg_data_id: int) -> dict:
        """PATCH a channel to assign a specific epg_data entry."""
        return self._patch(f"/api/channels/channels/{channel_id}/", {"epg_data": epg_data_id})


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------

def _build_epg_index(
    epg_data: list[dict],
    source_filter: list[str],
    strip_geo: bool,
    strip_quality: bool,
    strip_misc: bool,
) -> list[dict]:
    """
    Normalise all EPGData entries and optionally filter by source name.
    Returns a list of dicts with keys: id, name, tvg_id, source_name, norm.
    """
    source_filter_lower = [s.lower().strip() for s in source_filter if s.strip()]
    index = []
    for entry in epg_data:
        sname = (entry.get("epg_source_name") or entry.get("source_name") or "").strip()
        if source_filter_lower and sname.lower() not in source_filter_lower:
            continue
        raw_name = (entry.get("name") or "").strip()
        if not raw_name:
            continue
        norm = _normalise(raw_name, strip_geo, strip_quality, strip_misc)
        index.append({
            "id": entry["id"],
            "name": raw_name,
            "tvg_id": entry.get("tvg_id") or "",
            "source_name": sname,
            "norm": norm,
        })
    return index


def _suggest_for_channel(
    channel_norm: str,
    epg_index: list[dict],
    min_score: int,
    max_n: int,
) -> list[dict]:
    """
    Score every EPG entry against *channel_norm* and return the top-N
    above *min_score*, sorted descending by score.
    """
    scored = []
    for entry in epg_index:
        s = _score(channel_norm, entry["norm"])
        if s >= min_score:
            scored.append({**entry, "score": s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_n]


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name = "EPG Suggester"
    version = "1.0.0"
    description = (
        "Scans channels without EPG assignments and suggests the best matching "
        "EPG entries using fuzzy name matching. Strips IPTV prefixes, quality "
        "tags, and noise before scoring."
    )

    # ------------------------------------------------------------------ helpers

    def _get_settings(self, settings: dict) -> dict:
        """Coerce and return typed settings with defaults."""
        return {
            "url":               settings.get("dispatcharr_url", "http://127.0.0.1:9191").rstrip("/"),
            "username":          settings.get("username", "admin"),
            "password":          settings.get("password", ""),
            "min_score":         max(0, min(100, int(settings.get("min_score", 50)))),
            "max_suggestions":   max(1, min(10, int(settings.get("max_suggestions", 3)))),
            "epg_sources_filter": [
                s.strip() for s in settings.get("epg_sources_filter", "").split(",") if s.strip()
            ],
            "group_filter": [
                s.strip() for s in settings.get("group_filter", "").split(",") if s.strip()
            ],
            "strip_geo":     bool(settings.get("ignore_geo_prefixes", True)),
            "strip_quality": bool(settings.get("ignore_quality_tags", True)),
            "strip_misc":    bool(settings.get("ignore_misc_tags", True)),
            "auto_apply":        bool(settings.get("auto_apply", False)),
            "auto_apply_threshold": max(0, min(100, int(settings.get("auto_apply_threshold", 85)))),
        }

    def _run_scan(self, s: dict) -> tuple[list[dict], list[dict], list[dict]]:
        """
        Core scan.  Returns (unmatched_channels, epg_index, scan_results).
        scan_results is a list of dicts with channel info + suggestions list.
        """
        client = DispatcharrClient(s["url"], s["username"], s["password"])

        logger.info("EPG Suggester: fetching channels…")
        channels = client.get_channels()
        logger.info("EPG Suggester: fetched %d channels", len(channels))

        # Filter by group if requested
        group_filter_lower = [g.lower() for g in s["group_filter"]]
        if group_filter_lower:
            channels = [
                c for c in channels
                if (c.get("channel_group_name") or c.get("group") or "").lower() in group_filter_lower
            ]
            logger.info("EPG Suggester: %d channels after group filter", len(channels))

        # Find channels with no EPG assigned
        unmatched = [c for c in channels if not c.get("epg_data")]
        logger.info("EPG Suggester: %d channels have no EPG", len(unmatched))

        logger.info("EPG Suggester: fetching EPG data…")
        epg_data = client.get_epg_data()
        logger.info("EPG Suggester: fetched %d EPGData entries", len(epg_data))

        epg_index = _build_epg_index(
            epg_data,
            s["epg_sources_filter"],
            s["strip_geo"],
            s["strip_quality"],
            s["strip_misc"],
        )
        logger.info("EPG Suggester: %d EPGData entries in search index", len(epg_index))

        results = []
        for ch in unmatched:
            raw_name = ch.get("name") or ch.get("channel_name") or ""
            ch_norm = _normalise(raw_name, s["strip_geo"], s["strip_quality"], s["strip_misc"])
            suggestions = _suggest_for_channel(ch_norm, epg_index, s["min_score"], s["max_suggestions"])
            results.append({
                "channel_id":    ch["id"],
                "channel_name":  raw_name,
                "channel_norm":  ch_norm,
                "channel_group": ch.get("channel_group_name") or ch.get("group") or "",
                "suggestions":   suggestions,
            })

        return unmatched, epg_index, results

    # ------------------------------------------------------------------ actions

    def scan_and_suggest(self, settings: dict) -> str:
        """Action: scan and return a human-readable report."""
        s = self._get_settings(settings)
        if not s["password"]:
            return "❌ Password is required. Please configure it in plugin settings."

        try:
            _, _, results = self._run_scan(s)
        except requests.exceptions.ConnectionError as e:
            return f"❌ Cannot connect to Dispatcharr at {s['url']}\n{e}"
        except requests.exceptions.HTTPError as e:
            return f"❌ API error: {e}"
        except Exception as e:
            logger.exception("EPG Suggester: unexpected error during scan")
            return f"❌ Unexpected error: {e}"

        total    = len(results)
        matched  = sum(1 for r in results if r["suggestions"])
        unmatched = total - matched

        lines = [
            f"📺 EPG Suggester – Scan Results",
            f"   Channels without EPG : {total}",
            f"   With suggestions     : {matched}",
            f"   No match found       : {unmatched}",
            f"   Min score used       : {s['min_score']}",
            f"   EPG entries searched : (see export for counts)",
            "",
        ]

        for r in results:
            lines.append(f"{'─'*60}")
            lines.append(f"📺 {r['channel_name']}  (group: {r['channel_group']})")
            lines.append(f"   Normalised: \"{r['channel_norm']}\"")
            if r["suggestions"]:
                for i, sg in enumerate(r["suggestions"], 1):
                    lines.append(
                        f"   [{i}] score={sg['score']:3d}  {sg['name']}"
                        f"  (tvg_id: {sg['tvg_id']}  source: {sg['source_name']})"
                        f"  [epg_data id={sg['id']}]"
                    )
            else:
                lines.append("   ⚠️  No suggestions above threshold")

        lines.append(f"{'─'*60}")
        lines.append("✅ Scan complete. Use 'Export CSV' for a spreadsheet-friendly view.")
        lines.append("   Use 'Apply Best Suggestions' (with Auto-Apply enabled) to assign top matches.")
        return "\n".join(lines)

    def export_suggestions_csv(self, settings: dict) -> str:
        """Action: export suggestions to CSV."""
        s = self._get_settings(settings)
        if not s["password"]:
            return "❌ Password is required. Please configure it in plugin settings."

        try:
            _, _, results = self._run_scan(s)
        except Exception as e:
            logger.exception("EPG Suggester: error during export")
            return f"❌ Error: {e}"

        os.makedirs("/data/exports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"/data/exports/epg_suggester_{ts}.csv"

        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(f"# EPG Suggester Export\n")
            fh.write(f"# Generated: {datetime.now().isoformat()}\n")
            fh.write(f"# Min score: {s['min_score']} | Max suggestions: {s['max_suggestions']}\n")
            fh.write(f"# Strip geo: {s['strip_geo']} | Strip quality: {s['strip_quality']} | Strip misc: {s['strip_misc']}\n")
            fh.write(f"# Group filter: {', '.join(s['group_filter']) or '(all)'}\n")
            fh.write(f"# EPG source filter: {', '.join(s['epg_sources_filter']) or '(all)'}\n#\n")

            writer = csv.writer(fh)
            writer.writerow([
                "channel_id", "channel_name", "channel_norm", "channel_group",
                "suggestion_rank", "score", "epg_name", "tvg_id", "epg_source", "epg_data_id",
            ])

            for r in results:
                if r["suggestions"]:
                    for rank, sg in enumerate(r["suggestions"], 1):
                        writer.writerow([
                            r["channel_id"],
                            r["channel_name"],
                            r["channel_norm"],
                            r["channel_group"],
                            rank,
                            sg["score"],
                            sg["name"],
                            sg["tvg_id"],
                            sg["source_name"],
                            sg["id"],
                        ])
                else:
                    writer.writerow([
                        r["channel_id"],
                        r["channel_name"],
                        r["channel_norm"],
                        r["channel_group"],
                        "",
                        "",
                        "NO_MATCH",
                        "",
                        "",
                        "",
                    ])

        n_with = sum(1 for r in results if r["suggestions"])
        return (
            f"✅ CSV exported to {path}\n"
            f"   {len(results)} channels scanned, {n_with} with suggestions.\n"
            f"   Review the file before using 'Apply Best Suggestions'."
        )

    def apply_suggestions(self, settings: dict) -> str:
        """Action: apply top suggestion to each unmatched channel (if auto_apply is on)."""
        s = self._get_settings(settings)
        if not s["password"]:
            return "❌ Password is required."
        if not s["auto_apply"]:
            return (
                "⚠️  Auto-Apply is disabled in settings.\n"
                "Enable '⚡ Auto-Apply Best Match' first, then re-run this action.\n"
                "TIP: Export and review the CSV before enabling auto-apply!"
            )

        threshold = s["auto_apply_threshold"]

        try:
            _, _, results = self._run_scan(s)
        except Exception as e:
            logger.exception("EPG Suggester: error during apply")
            return f"❌ Error during scan: {e}"

        client = DispatcharrClient(s["url"], s["username"], s["password"])

        applied, skipped, failed = 0, 0, 0
        log_lines = [
            f"⚡ EPG Suggester – Auto-Apply (threshold={threshold})",
            "",
        ]

        for r in results:
            if not r["suggestions"]:
                log_lines.append(f"⏭  {r['channel_name']}  → no suggestion")
                skipped += 1
                continue

            top = r["suggestions"][0]
            if top["score"] < threshold:
                log_lines.append(
                    f"⏭  {r['channel_name']}  → best score {top['score']} < {threshold}, skipped"
                )
                skipped += 1
                continue

            try:
                client.assign_epg(r["channel_id"], top["id"])
                log_lines.append(
                    f"✅ {r['channel_name']}\n"
                    f"   → \"{top['name']}\"  (score={top['score']}, source={top['source_name']})"
                )
                applied += 1
            except Exception as e:
                log_lines.append(f"❌ {r['channel_name']}  → API error: {e}")
                failed += 1

        log_lines += [
            "",
            f"{'─'*50}",
            f"Applied : {applied}",
            f"Skipped : {skipped}  (below threshold or no match)",
            f"Failed  : {failed}",
        ]
        return "\n".join(log_lines)

    def show_unmatched(self, settings: dict) -> str:
        """Action: quickly list all channels with no EPG assignment."""
        s = self._get_settings(settings)
        if not s["password"]:
            return "❌ Password is required."

        try:
            client = DispatcharrClient(s["url"], s["username"], s["password"])
            channels = client.get_channels()
        except Exception as e:
            return f"❌ Error fetching channels: {e}"

        group_filter_lower = [g.lower() for g in s["group_filter"]]
        if group_filter_lower:
            channels = [
                c for c in channels
                if (c.get("channel_group_name") or c.get("group") or "").lower() in group_filter_lower
            ]

        unmatched = [c for c in channels if not c.get("epg_data")]

        if not unmatched:
            return "🎉 All channels have an EPG assignment! Nothing to do."

        lines = [f"📺 {len(unmatched)} channels without EPG assignment:\n"]
        for c in unmatched:
            grp = c.get("channel_group_name") or c.get("group") or "?"
            lines.append(f"  id={c['id']:6d}  [{grp}]  {c.get('name') or ''}")
        lines.append(f"\n✅ Run 'Scan & Suggest EPG' to get match suggestions.")
        return "\n".join(lines)
