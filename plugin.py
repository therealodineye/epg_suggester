"""
EPG Suggester Plugin for Dispatcharr
=====================================
Scans channels without an EPG assignment and suggests the best matching
EPG entries using fuzzy name matching.

Dispatcharr calls:
    Plugin().run(action, params, context)
    - action   : str matching an action "id" in plugin.json
    - params   : dict (mostly unused, settings come from context)
    - context  : dict with keys "settings" and "logger"

This plugin uses the Django ORM directly (no HTTP calls, no credentials needed).
"""

from __future__ import annotations

import csv
import difflib
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("plugins.epg_suggester")

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_GEO_PREFIX_RE = re.compile(
    r"^(?:[A-Z]{2,4}\s*[|\-:]\s*"
    r"|(?:USA?|UK|AU|CA|DE|FR|IT|ES|NL|BE|CH|AT|PL|SE|NO|DK|FI|NZ|ZA|BR|MX)\s*[|\-:]\s*)",
    re.IGNORECASE,
)

_QUALITY_TAGS_RE = re.compile(
    r"\b(?:4k|uhd|fhd|hd|sd|hevc|h265|h264|avc|hdr|sdr|1080[pi]?|720[pi]?|480[pi]?)\b",
    re.IGNORECASE,
)

_MISC_TAGS_RE = re.compile(
    r"\b(?:vip|backup\d*|bkup|standby|plus|premium|extra|alt|reserve"
    r"|english|hindi|arabic|spanish|french|german|italian|portuguese"
    r"|\+1|\+2|24\/7|24x7)\b"
    r"|[\[\(][a-z0-9 ]{0,10}[\]\)]"
    r"|\s*\*+\s*",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")


def _normalise(name: str, strip_geo: bool, strip_quality: bool, strip_misc: bool) -> str:
    n = name.strip()
    if strip_geo:
        n = _GEO_PREFIX_RE.sub("", n).strip()
    if strip_quality:
        n = _QUALITY_TAGS_RE.sub(" ", n)
    if strip_misc:
        n = _MISC_TAGS_RE.sub(" ", n)
    return _WS_RE.sub(" ", n).strip().lower()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(ch: str, epg: str) -> int:
    if not ch or not epg:
        return 0
    if ch == epg:
        return 100

    ratio = difflib.SequenceMatcher(
        None,
        " ".join(sorted(ch.split())),
        " ".join(sorted(epg.split())),
    ).ratio()
    score = int(ratio * 90)

    ch_set, epg_set = set(ch.split()), set(epg.split())
    if ch_set and epg_set:
        overlap = len(ch_set & epg_set) / max(len(ch_set), len(epg_set))
        score = max(score, int(overlap * 60))

    if ch in epg or epg in ch:
        score = min(99, score + 20)

    return min(score, 99)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class Plugin:
    name        = "EPG Suggester"
    version     = "1.2.0"
    description = (
        "Scans channels without EPG assignments and suggests the best "
        "matching EPG entries using fuzzy name matching."
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_cfg(self, settings: dict) -> dict:
        return {
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
            "strip_misc":    bool(settings.get("ignore_misc_tags",    True)),
            "auto_apply":           bool(settings.get("auto_apply", False)),
            "auto_apply_threshold": max(0, min(100, int(settings.get("auto_apply_threshold", 85)))),
        }

    def _fetch_data(self, cfg: dict, log) -> tuple[list, list]:
        """Return (unmatched_channels, epg_index) using Django ORM."""
        from apps.channels.models import Channel
        from apps.epg.models import EPGData

        # --- channels without EPG ---
        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)

        gf = [g.lower() for g in cfg["group_filter"]]
        if gf:
            qs = qs.filter(channel_group__name__iregex="|".join(re.escape(g) for g in gf))

        channels = list(qs.values("id", "name", "channel_group__name"))
        log.info("EPG Suggester: %d unmatched channels", len(channels))

        # --- EPG data ---
        epg_qs = EPGData.objects.select_related("epg_source").values(
            "id", "name", "tvg_id", "epg_source__name"
        )

        sf = [s.lower() for s in cfg["epg_sources_filter"]]
        if sf:
            epg_qs = epg_qs.filter(epg_source__name__iregex="|".join(re.escape(s) for s in sf))

        epg_index = []
        for entry in epg_qs:
            raw = (entry["name"] or "").strip()
            if not raw:
                continue
            epg_index.append({
                "id":          entry["id"],
                "name":        raw,
                "tvg_id":      entry["tvg_id"] or "",
                "source_name": entry["epg_source__name"] or "",
                "norm":        _normalise(raw, cfg["strip_geo"], cfg["strip_quality"], cfg["strip_misc"]),
            })
        log.info("EPG Suggester: %d EPGData entries in index", len(epg_index))

        return channels, epg_index

    def _build_results(self, channels: list, epg_index: list, cfg: dict) -> list:
        results = []
        for ch in channels:
            raw  = ch["name"] or ""
            norm = _normalise(raw, cfg["strip_geo"], cfg["strip_quality"], cfg["strip_misc"])
            scored = sorted(
                [
                    {**e, "score": _score(norm, e["norm"])}
                    for e in epg_index
                    if _score(norm, e["norm"]) >= cfg["min_score"]
                ],
                key=lambda x: x["score"],
                reverse=True,
            )
            results.append({
                "channel_id":    ch["id"],
                "channel_name":  raw,
                "channel_norm":  norm,
                "channel_group": ch.get("channel_group__name") or "",
                "suggestions":   scored[: cfg["max_suggestions"]],
            })
        return results

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _do_scan_and_suggest(self, cfg: dict, log) -> str:
        channels, epg_index = self._fetch_data(cfg, log)
        results = self._build_results(channels, epg_index, cfg)

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
            lines.append(f"   Normalised: \"{r['channel_norm']}\"")
            if r["suggestions"]:
                for i, sg in enumerate(r["suggestions"], 1):
                    lines.append(
                        f"   [{i}] score={sg['score']:3d}  \"{sg['name']}\""
                        f"  tvg_id={sg['tvg_id']}  source={sg['source_name']}"
                        f"  (id={sg['id']})"
                    )
            else:
                lines.append("   ⚠️  No suggestions above threshold")
        lines += ["─" * 60,
                  "✅ Done. Use 'Export CSV' for a spreadsheet, or 'Apply' to assign."]
        return "\n".join(lines)

    def _do_export_csv(self, cfg: dict, log) -> str:
        channels, epg_index = self._fetch_data(cfg, log)
        results = self._build_results(channels, epg_index, cfg)

        os.makedirs("/data/exports", exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"/data/exports/epg_suggester_{ts}.csv"

        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# EPG Suggester Export\n")
            fh.write(f"# Generated: {datetime.now().isoformat()}\n")
            fh.write(f"# min_score={cfg['min_score']}  max_suggestions={cfg['max_suggestions']}\n")
            fh.write(
                f"# strip_geo={cfg['strip_geo']}  strip_quality={cfg['strip_quality']}"
                f"  strip_misc={cfg['strip_misc']}\n"
            )
            fh.write(f"# group_filter: {', '.join(cfg['group_filter']) or '(all)'}\n")
            fh.write(f"# epg_source_filter: {', '.join(cfg['epg_sources_filter']) or '(all)'}\n#\n")

            w = csv.writer(fh)
            w.writerow([
                "channel_id", "channel_name", "channel_norm", "channel_group",
                "rank", "score", "epg_name", "tvg_id", "epg_source", "epg_data_id",
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
            f"✅ CSV exported to {path}\n"
            f"   {len(results)} channels scanned, {n_with} with at least one suggestion.\n"
            f"   Review before running 'Apply Best Suggestions'."
        )

    def _do_apply(self, cfg: dict, log) -> str:
        if not cfg["auto_apply"]:
            return (
                "⚠️  Auto-Apply is DISABLED.\n"
                "Enable '⚡ Auto-Apply Best Match' in settings first.\n"
                "Always export and review the CSV before applying!"
            )

        from apps.channels.models import Channel

        threshold = cfg["auto_apply_threshold"]
        channels, epg_index = self._fetch_data(cfg, log)
        results = self._build_results(channels, epg_index, cfg)

        applied, skipped, failed = 0, 0, 0
        lines = [f"⚡ Auto-Apply (threshold={threshold})", ""]

        for r in results:
            if not r["suggestions"]:
                lines.append(f"⏭  {r['channel_name']}  → no suggestion")
                skipped += 1
                continue
            top = r["suggestions"][0]
            if top["score"] < threshold:
                lines.append(
                    f"⏭  {r['channel_name']}  → best score {top['score']} < {threshold}, skipped"
                )
                skipped += 1
                continue
            try:
                Channel.objects.filter(pk=r["channel_id"]).update(epg_data_id=top["id"])
                lines.append(
                    f"✅ {r['channel_name']}\n"
                    f"   → \"{top['name']}\"  score={top['score']}  source={top['source_name']}"
                )
                applied += 1
            except Exception as e:
                lines.append(f"❌ {r['channel_name']}  → {e}")
                failed += 1
                log.exception("EPG Suggester: failed to assign epg for channel %s", r["channel_id"])

        lines += [
            "", "─" * 50,
            f"Applied : {applied}",
            f"Skipped : {skipped}",
            f"Failed  : {failed}",
        ]
        return "\n".join(lines)

    def _do_show_unmatched(self, cfg: dict, log) -> str:
        from apps.channels.models import Channel

        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)
        gf = [g.lower() for g in cfg["group_filter"]]
        if gf:
            qs = qs.filter(channel_group__name__iregex="|".join(re.escape(g) for g in gf))

        channels = list(qs.values("id", "name", "channel_group__name").order_by("channel_group__name", "name"))

        if not channels:
            return "🎉 All channels already have an EPG assignment!"

        lines = [f"📺 {len(channels)} channels without EPG:\n"]
        current_group = None
        for c in channels:
            grp = c.get("channel_group__name") or "No Group"
            if grp != current_group:
                lines.append(f"\n  [{grp}]")
                current_group = grp
            lines.append(f"    id={c['id']:6d}  {c['name'] or ''}")
        lines.append("\nRun '🔍 Scan & Suggest EPG' to get match suggestions.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Dispatcher — the ONLY method Dispatcharr calls
    # ------------------------------------------------------------------

    def run(self, action: str, params: dict, context: dict):
        log      = context.get("logger", logger)
        settings = context.get("settings", {})
        cfg      = self._parse_cfg(settings)

        log.info("EPG Suggester: action='%s'", action)

        try:
            if action == "scan_and_suggest":
                return self._do_scan_and_suggest(cfg, log)
            elif action == "export_suggestions_csv":
                return self._do_export_csv(cfg, log)
            elif action == "apply_suggestions":
                return self._do_apply(cfg, log)
            elif action == "show_unmatched":
                return self._do_show_unmatched(cfg, log)
            else:
                return {"status": "error", "message": f"Unknown action: '{action}'"}
        except Exception as e:
            log.exception("EPG Suggester: unhandled exception in action '%s'", action)
            return {"status": "error", "message": f"Error in '{action}': {e}"}
