"""
EPG Suggester Plugin for Dispatcharr
=====================================
Scans channels without an EPG assignment and suggests the best matching
EPG entries using fuzzy name matching.

Dispatcharr calls:  Plugin().run(action, params, context)

The run() method dispatches to  _{action_id}_action()  handlers.

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
    r"[A-Z]{2,4}\s*[|\-:]\s*"
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
    r"|[\[\(][a-z0-9 ]{0,10}[\]\)]"
    r"|\s*\*+\s*",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(name: str, strip_geo: bool, strip_quality: bool, strip_misc: bool) -> str:
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
    if not channel_norm or not epg_norm:
        return 0
    if channel_norm == epg_norm:
        return 100

    ch_tokens  = sorted(channel_norm.split())
    epg_tokens = sorted(epg_norm.split())
    ch_sorted  = " ".join(ch_tokens)
    epg_sorted = " ".join(epg_tokens)

    ratio = difflib.SequenceMatcher(None, ch_sorted, epg_sorted).ratio()
    score = int(ratio * 90)

    ch_set  = set(ch_tokens)
    epg_set = set(epg_tokens)
    if ch_set and epg_set:
        overlap = len(ch_set & epg_set) / max(len(ch_set), len(epg_set))
        score = max(score, int(overlap * 60))

    if channel_norm in epg_norm or epg_norm in channel_norm:
        score = min(99, score + 20)

    return min(score, 99)


# ---------------------------------------------------------------------------
# Dispatcharr API client
# ---------------------------------------------------------------------------

class _DispatcharrClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._token: Optional[str] = None
        self._username = username
        self._password = password

    def _auth(self) -> dict:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        # Try the newer endpoint first, fall back to legacy
        for endpoint in ["/api/accounts/token/", "/api/auth/token/"]:
            r = self._session.post(
                f"{self.base_url}{endpoint}",
                json={"username": self._username, "password": self._password},
                timeout=15,
            )
            if r.status_code != 404:
                break
        r.raise_for_status()
        data = r.json()
        self._token = data.get("access") or data.get("token")
        if not self._token:
            raise RuntimeError(f"Auth succeeded but no token in response: {data}")
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict | None = None):
        r = self._session.get(
            f"{self.base_url}{path}",
            headers=self._auth(),
            params=params or {},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict):
        r = self._session.patch(
            f"{self.base_url}{path}",
            headers=self._auth(),
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _paginate(self, path: str, page_size: int = 500) -> list[dict]:
        results, page = [], 1
        while True:
            data = self._get(path, {"page": page, "page_size": page_size})
            batch = data.get("results", data) if isinstance(data, dict) else data
            if not batch:
                break
            results.extend(batch)
            if isinstance(data, dict) and not data.get("next"):
                break
            if len(batch) < page_size:
                break
            page += 1
        return results

    def get_channels(self)    -> list[dict]: return self._paginate("/api/channels/channels/", 500)
    def get_epg_data(self)    -> list[dict]: return self._paginate("/api/epg/epg-data/", 1000)

    def assign_epg(self, channel_id: int, epg_data_id: int) -> dict:
        return self._patch(f"/api/channels/channels/{channel_id}/", {"epg_data": epg_data_id})


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _build_epg_index(epg_data, source_filter, strip_geo, strip_quality, strip_misc):
    sf_lower = [s.lower().strip() for s in source_filter if s.strip()]
    index = []
    for entry in epg_data:
        sname = (entry.get("epg_source_name") or entry.get("source_name") or "").strip()
        if sf_lower and sname.lower() not in sf_lower:
            continue
        raw_name = (entry.get("name") or "").strip()
        if not raw_name:
            continue
        norm = _normalise(raw_name, strip_geo, strip_quality, strip_misc)
        index.append({
            "id":          entry["id"],
            "name":        raw_name,
            "tvg_id":      entry.get("tvg_id") or "",
            "source_name": sname,
            "norm":        norm,
        })
    return index


def _suggest(channel_norm, epg_index, min_score, max_n):
    scored = [
        {**e, "score": _score(channel_norm, e["norm"])}
        for e in epg_index
    ]
    scored = [e for e in scored if e["score"] >= min_score]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_n]


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin:
    name        = "EPG Suggester"
    version     = "1.1.0"
    description = (
        "Scans channels without EPG assignments and suggests the best matching "
        "EPG entries using fuzzy name matching. Strips IPTV prefixes, quality "
        "tags, and noise before scoring."
    )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _cfg(settings: dict) -> dict:
        """Return typed settings with safe defaults."""
        return {
            "url":      settings.get("dispatcharr_url", "http://127.0.0.1:9191").rstrip("/"),
            "username": settings.get("username", "admin"),
            "password": settings.get("password", ""),
            "min_score":       max(0, min(100, int(settings.get("min_score", 50)))),
            "max_suggestions": max(1, min(10,  int(settings.get("max_suggestions", 3)))),
            "epg_sources_filter": [
                s.strip() for s in settings.get("epg_sources_filter", "").split(",") if s.strip()
            ],
            "group_filter": [
                s.strip() for s in settings.get("group_filter", "").split(",") if s.strip()
            ],
            "strip_geo":     bool(settings.get("ignore_geo_prefixes", True)),
            "strip_quality": bool(settings.get("ignore_quality_tags", True)),
            "strip_misc":    bool(settings.get("ignore_misc_tags", True)),
            "auto_apply":           bool(settings.get("auto_apply", False)),
            "auto_apply_threshold": max(0, min(100, int(settings.get("auto_apply_threshold", 85)))),
        }

    def _do_scan(self, cfg: dict, log) -> tuple[list, list, list]:
        """Fetch channels + EPG data, return (unmatched, epg_index, results)."""
        client = _DispatcharrClient(cfg["url"], cfg["username"], cfg["password"])

        log.info("EPG Suggester: fetching channels…")
        channels = client.get_channels()
        log.info("EPG Suggester: %d channels total", len(channels))

        gf = [g.lower() for g in cfg["group_filter"]]
        if gf:
            channels = [
                c for c in channels
                if (c.get("channel_group_name") or c.get("group") or "").lower() in gf
            ]
            log.info("EPG Suggester: %d after group filter", len(channels))

        unmatched = [c for c in channels if not c.get("epg_data")]
        log.info("EPG Suggester: %d channels have no EPG", len(unmatched))

        log.info("EPG Suggester: fetching EPG data…")
        epg_data  = client.get_epg_data()
        log.info("EPG Suggester: %d EPGData entries fetched", len(epg_data))

        epg_index = _build_epg_index(
            epg_data,
            cfg["epg_sources_filter"],
            cfg["strip_geo"],
            cfg["strip_quality"],
            cfg["strip_misc"],
        )
        log.info("EPG Suggester: %d entries in search index", len(epg_index))

        results = []
        for ch in unmatched:
            raw  = ch.get("name") or ch.get("channel_name") or ""
            norm = _normalise(raw, cfg["strip_geo"], cfg["strip_quality"], cfg["strip_misc"])
            results.append({
                "channel_id":    ch["id"],
                "channel_name":  raw,
                "channel_norm":  norm,
                "channel_group": ch.get("channel_group_name") or ch.get("group") or "",
                "suggestions":   _suggest(norm, epg_index, cfg["min_score"], cfg["max_suggestions"]),
            })

        return unmatched, epg_index, results

    # ------------------------------------------------------------------ action handlers

    def _scan_and_suggest_action(self, settings: dict, log) -> str:
        cfg = self._cfg(settings)
        if not cfg["password"]:
            return "❌ Password is required. Please fill it in and save settings."
        try:
            _, _, results = self._do_scan(cfg, log)
        except requests.exceptions.ConnectionError as e:
            return f"❌ Cannot connect to Dispatcharr at {cfg['url']}\n{e}"
        except requests.exceptions.HTTPError as e:
            return f"❌ API error: {e}"
        except Exception as e:
            log.exception("EPG Suggester: unexpected error")
            return f"❌ Unexpected error: {e}"

        total   = len(results)
        matched = sum(1 for r in results if r["suggestions"])

        lines = [
            "📺 EPG Suggester – Scan Results",
            f"   Channels without EPG : {total}",
            f"   With suggestions     : {matched}",
            f"   No match found       : {total - matched}",
            f"   Min score used       : {cfg['min_score']}",
            "",
        ]
        for r in results:
            lines.append("─" * 60)
            lines.append(f"📺 {r['channel_name']}  [group: {r['channel_group']}]")
            lines.append(f"   Normalised to: \"{r['channel_norm']}\"")
            if r["suggestions"]:
                for i, sg in enumerate(r["suggestions"], 1):
                    lines.append(
                        f"   [{i}] score={sg['score']:3d}  {sg['name']}"
                        f"  tvg_id={sg['tvg_id']}  source={sg['source_name']}"
                        f"  (epg_data id={sg['id']})"
                    )
            else:
                lines.append("   ⚠️  No suggestions above threshold")
        lines += ["─" * 60, "✅ Done. Use 'Export CSV' for a spreadsheet view."]
        return "\n".join(lines)

    def _export_suggestions_csv_action(self, settings: dict, log) -> str:
        cfg = self._cfg(settings)
        if not cfg["password"]:
            return "❌ Password is required."
        try:
            _, _, results = self._do_scan(cfg, log)
        except Exception as e:
            log.exception("EPG Suggester: error during export")
            return f"❌ Error: {e}"

        os.makedirs("/data/exports", exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"/data/exports/epg_suggester_{ts}.csv"

        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# EPG Suggester Export\n")
            fh.write(f"# Generated: {datetime.now().isoformat()}\n")
            fh.write(f"# Min score: {cfg['min_score']}  Max suggestions: {cfg['max_suggestions']}\n")
            fh.write(f"# Strip geo: {cfg['strip_geo']}  quality: {cfg['strip_quality']}  misc: {cfg['strip_misc']}\n")
            fh.write(f"# Group filter: {', '.join(cfg['group_filter']) or '(all)'}\n")
            fh.write(f"# EPG source filter: {', '.join(cfg['epg_sources_filter']) or '(all)'}\n#\n")

            w = csv.writer(fh)
            w.writerow([
                "channel_id", "channel_name", "channel_norm", "channel_group",
                "suggestion_rank", "score", "epg_name", "tvg_id", "epg_source", "epg_data_id",
            ])
            for r in results:
                if r["suggestions"]:
                    for rank, sg in enumerate(r["suggestions"], 1):
                        w.writerow([
                            r["channel_id"], r["channel_name"], r["channel_norm"], r["channel_group"],
                            rank, sg["score"], sg["name"], sg["tvg_id"], sg["source_name"], sg["id"],
                        ])
                else:
                    w.writerow([
                        r["channel_id"], r["channel_name"], r["channel_norm"], r["channel_group"],
                        "", "", "NO_MATCH", "", "", "",
                    ])

        n_with = sum(1 for r in results if r["suggestions"])
        return (
            f"✅ Exported to {path}\n"
            f"   {len(results)} channels scanned, {n_with} with at least one suggestion.\n"
            f"   Review before running 'Apply Best Suggestions'."
        )

    def _apply_suggestions_action(self, settings: dict, log) -> str:
        cfg = self._cfg(settings)
        if not cfg["password"]:
            return "❌ Password is required."
        if not cfg["auto_apply"]:
            return (
                "⚠️  Auto-Apply is DISABLED in settings.\n"
                "Enable '⚡ Auto-Apply Best Match' and set the threshold, then retry.\n"
                "TIP: always Export CSV and review first!"
            )

        threshold = cfg["auto_apply_threshold"]
        try:
            _, _, results = self._do_scan(cfg, log)
        except Exception as e:
            log.exception("EPG Suggester: error during apply scan")
            return f"❌ Error during scan: {e}"

        client = _DispatcharrClient(cfg["url"], cfg["username"], cfg["password"])
        applied, skipped, failed = 0, 0, 0
        lines = [f"⚡ Auto-Apply (threshold={threshold})", ""]

        for r in results:
            if not r["suggestions"]:
                lines.append(f"⏭  {r['channel_name']}  → no suggestion")
                skipped += 1
                continue
            top = r["suggestions"][0]
            if top["score"] < threshold:
                lines.append(f"⏭  {r['channel_name']}  → score {top['score']} < {threshold}, skipped")
                skipped += 1
                continue
            try:
                client.assign_epg(r["channel_id"], top["id"])
                lines.append(
                    f"✅ {r['channel_name']}\n"
                    f"   → \"{top['name']}\"  score={top['score']}  source={top['source_name']}"
                )
                applied += 1
            except Exception as e:
                lines.append(f"❌ {r['channel_name']}  → {e}")
                failed += 1

        lines += ["", "─" * 50,
                  f"Applied : {applied}",
                  f"Skipped : {skipped}",
                  f"Failed  : {failed}"]
        return "\n".join(lines)

    def _show_unmatched_action(self, settings: dict, log) -> str:
        cfg = self._cfg(settings)
        if not cfg["password"]:
            return "❌ Password is required."
        try:
            client   = _DispatcharrClient(cfg["url"], cfg["username"], cfg["password"])
            channels = client.get_channels()
        except Exception as e:
            return f"❌ Error fetching channels: {e}"

        gf = [g.lower() for g in cfg["group_filter"]]
        if gf:
            channels = [c for c in channels
                        if (c.get("channel_group_name") or c.get("group") or "").lower() in gf]

        unmatched = [c for c in channels if not c.get("epg_data")]
        if not unmatched:
            return "🎉 All channels already have an EPG assignment!"

        lines = [f"📺 {len(unmatched)} channels without EPG:\n"]
        for c in unmatched:
            grp = c.get("channel_group_name") or c.get("group") or "?"
            lines.append(f"  id={c['id']:6d}  [{grp}]  {c.get('name') or ''}")
        lines.append("\nRun 'Scan & Suggest EPG' to get match suggestions.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ dispatcher

    def run(self, action: str, params: dict, context: dict) -> str:
        """
        Single entry point called by Dispatcharr for every button press.
        Dispatches to  _{action_id}_action(settings, log).
        """
        log      = context.get("logger", logger)
        settings = params.get("settings", params)

        handler_name = f"_{action}_action"
        handler = getattr(self, handler_name, None)

        if handler is None:
            available = [
                m[1:-7] for m in dir(self)
                if m.startswith("_") and m.endswith("_action") and callable(getattr(self, m))
            ]
            return (
                f"❌ Unknown action: '{action}'\n"
                f"Available actions: {', '.join(available)}"
            )

        log.info("EPG Suggester: running action '%s'", action)
        try:
            return handler(settings, log)
        except Exception as e:
            log.exception("EPG Suggester: unhandled exception in action '%s'", action)
            return f"❌ Unhandled error in '{action}': {e}"
