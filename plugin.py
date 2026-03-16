import re as _re

_CTRY_RE = _re.compile(r'^([a-z]{2,5}): ')

# Known ISO-3166 country codes - prefixes matching these are kept, others stripped
_COUNTRY_CODES = {
    'us','uk','gb','au','ca','de','fr','it','es','nl','be','ch','at',
    'no','se','dk','fi','pl','pt','ro','al','sr','hr','si','sk','cz',
    'hu','rs','ba','me','mk','bg','gr','tr','il','ar','br','mx','nz',
    'za','ie','is','lu','ee','lv','lt','ua','by','md','ge','am','az',
    'kz','uz','pk','in','sg','my','th','ph','id','jp','kr','cn','hk',
    'tw','ae','sa','qa','kw','bh','om','eg','ma','tn','dz','ly','ng',
    'ke','gh','tz','et','cm','ci','sn','rw','ug','ao','mz',
}

_UNICODE_RE = _re.compile(r'[\u1d00-\u1dbf\u2c60-\u2c7f\u2070-\u209f\u00b2-\u00b3\u00b9]+')
_QUALITY_RE = _re.compile(r'\b(?:4k|uhd|fhd|hd|sd|hevc|h265|h264|hdr|sdr|1080[pi]?|720[pi]?)\b', _re.IGNORECASE)
_MISC_RE    = _re.compile(r'\b(?:vip|backup\d*|bkup|plus|premium|extra|alt|raw|\+1|\+2)\b|\([^)]{0,15}\)|\[[^\]]{0,15}\]|\s*\*+\s*', _re.IGNORECASE)
_WS_RE      = _re.compile(r'\s+')
_PREFIX_RE  = _re.compile(r'^([A-Za-z]{2,5})\s*[|\-:]\s*')


class Plugin:
    name = "EPG Suggester"
    version = "2.0.0"
    description = "Suggests EPG entries for channels without EPG assigned, using fuzzy name matching."

    def run(self, action, params, context):
        import logging
        log = logging.getLogger("plugins.epg_suggester")
        settings = context.get("settings", {})
        cfg = self._parse_settings(settings)
        log.info("EPG Suggester: action=%s", action)

        if action == "show_unmatched":
            return self._show_unmatched(cfg, log)
        elif action == "scan_and_suggest":
            return self._scan(cfg, log)
        elif action == "export_suggestions_csv":
            return self._export(cfg, log)
        elif action == "apply_suggestions":
            return self._apply(cfg, log)
        else:
            return {"status": "error", "message": "Unknown action: " + action}

    def _parse_settings(self, settings):
        return {
            "qual":   bool(settings.get("ignore_quality_tags", True)),
            "misc":   bool(settings.get("ignore_misc_tags", True)),
            "min_s":  max(0, min(100, int(settings.get("min_score", 60)))),
            "max_n":  max(1, min(10,  int(settings.get("max_suggestions", 3)))),
            "sf":     [x.strip() for x in settings.get("epg_sources_filter", "").split(",") if x.strip()],
            "gf":     [x.strip() for x in settings.get("group_filter", "").split(",") if x.strip()],
            "auto":   bool(settings.get("auto_apply", False)),
            "thresh": max(0, min(100, int(settings.get("auto_apply_threshold", 85)))),
        }

    @staticmethod
    def _norm(name, cfg):
        n = name.strip()
        n = _UNICODE_RE.sub(' ', n)
        m = _PREFIX_RE.match(n)
        if m:
            prefix = m.group(1).lower()
            rest = n[m.end():]
            n = (prefix + ': ' + rest) if prefix in _COUNTRY_CODES else rest
        if cfg["qual"]:
            n = _QUALITY_RE.sub(' ', n)
        if cfg["misc"]:
            n = _MISC_RE.sub(' ', n)
        return _WS_RE.sub(' ', n).strip().lower()

    @staticmethod
    def _fast_score(ct, cs, cn, et, es, en, min_s):
        import difflib
        if cn == en: return 100
        # Number guard: channels with different numbers are different channels
        # e.g. "history 2" should NOT match "history 1"
        ch_nums = set(t for t in ct if t.isdigit())
        ep_nums = set(t for t in et if t.isdigit())
        if ch_nums and ep_nums and not (ch_nums & ep_nums):
            return 0
        inter = len(cs & es) if cs and es else 0
        union = max(len(cs), len(es)) if (cs or es) else 1
        overlap_s = int(inter / union * 90)
        sub = 20 if (cn in en or en in cn) else 0
        if overlap_s + sub < min_s:
            return overlap_s + sub
        if overlap_s >= 40 or sub:
            ratio = difflib.SequenceMatcher(
                None,
                ' '.join(sorted(ct)),
                ' '.join(sorted(et))
            ).ratio()
            overlap_s = max(overlap_s, int(ratio * 90))
        return min(99, overlap_s + sub)

    def _build_index(self, epg_entries, cfg):
        """Pre-tokenise all EPG entries and group by country prefix for fast lookup."""
        by_country = {}
        no_country = []
        for e in epg_entries:
            raw = (e["name"] or "").strip()
            if not raw:
                continue
            norm = self._norm(raw, cfg)
            tok  = norm.split()
            tset = set(tok)
            entry = {
                "id": e["id"], "name": raw, "tvg_id": e["tvg_id"] or "",
                "source": e["epg_source__name"] or "",
                "norm": norm, "tok": tok, "tset": tset,
            }
            m = _CTRY_RE.match(norm)
            if m:
                by_country.setdefault(m.group(1), []).append(entry)
            else:
                no_country.append(entry)
        return by_country, no_country

    def _suggest(self, ch_norm, by_country, no_country, cfg):
        ct = ch_norm.split()
        cs = set(ct)
        m  = _CTRY_RE.match(ch_norm)
        min_s = cfg["min_s"]

        if m:
            # Country-prefixed: only compare against same country + no-prefix entries
            candidates = by_country.get(m.group(1), []) + no_country
        else:
            # No prefix (provider stripped): compare against everything
            candidates = no_country + [e for grp in by_country.values() for e in grp]

        scored = []
        for e in candidates:
            s = self._fast_score(ct, cs, ch_norm, e["tok"], e["tset"], e["norm"], min_s)
            if s >= min_s:
                scored.append((s, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(e, score=s) for s, e in scored[:cfg["max_n"]]]

    def _get_channels(self, cfg, log):
        from apps.channels.models import Channel
        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)
        if cfg["gf"]:
            qs = qs.filter(channel_group__name__in=cfg["gf"])
        channels = list(qs.values("id", "name", "channel_group__name"))
        log.info("EPG Suggester: %d unmatched channels", len(channels))
        return channels

    def _get_epg(self, cfg, log):
        from apps.epg.models import EPGData
        qs = EPGData.objects.select_related("epg_source").values("id", "name", "tvg_id", "epg_source__name")
        if cfg["sf"]:
            qs = qs.filter(epg_source__name__in=cfg["sf"])
        entries = list(qs)
        log.info("EPG Suggester: %d EPG entries fetched", len(entries))
        return entries

    def _show_unmatched(self, cfg, log):
        from apps.channels.models import Channel
        qs = Channel.objects.select_related("channel_group").filter(epg_data__isnull=True)
        if cfg["gf"]:
            qs = qs.filter(channel_group__name__in=cfg["gf"])
        channels = list(qs.values("id", "name", "channel_group__name").order_by("channel_group__name", "name"))
        if not channels:
            return "All channels already have EPG assigned!"
        lines = [str(len(channels)) + " channels without EPG:\n"]
        grp = None
        for c in channels:
            g = c.get("channel_group__name") or "No Group"
            if g != grp:
                lines.append("\n[" + g + "]")
                grp = g
            lines.append("  id=" + str(c["id"]) + "  " + (c["name"] or ""))
        return "\n".join(lines)

    def _run_matching(self, cfg, log):
        """Core: fetch data, build index, run matching. Returns (channels, results)."""
        channels   = self._get_channels(cfg, log)
        epg_raw    = self._get_epg(cfg, log)
        by_country, no_country = self._build_index(epg_raw, cfg)
        log.info("EPG Suggester: index built (%d country groups, %d no-prefix)",
                 len(by_country), len(no_country))
        results = []
        for ch in channels:
            raw  = ch["name"] or ""
            norm = self._norm(raw, cfg)
            sugg = self._suggest(norm, by_country, no_country, cfg)
            results.append({
                "channel_id":   ch["id"],
                "channel_name": raw,
                "channel_norm": norm,
                "channel_group": ch.get("channel_group__name") or "",
                "suggestions":  sugg,
            })
        matched = sum(1 for r in results if r["suggestions"])
        log.info("EPG Suggester: matching done. %d/%d matched", matched, len(results))
        return results

    def _scan(self, cfg, log):
        import os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        lines = ["EPG Suggester v2.0.0 - Scan Results",
                 str(len(results)) + " channels without EPG  |  matched: " + str(matched), ""]
        for r in results:
            lines.append("---")
            lines.append("Channel: " + r["channel_name"] + "  [" + r["channel_group"] + "]")
            lines.append("  norm: " + r["channel_norm"])
            if r["suggestions"]:
                for i, s in enumerate(r["suggestions"], 1):
                    lines.append("  [" + str(i) + "] score=" + str(s["score"])
                                 + "  " + s["name"] + "  source=" + s["source"]
                                 + "  id=" + str(s["id"]))
            else:
                lines.append("  No suggestions above score " + str(cfg["min_s"]))
        os.makedirs("/data/exports", exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = "/data/exports/epg_suggester_scan_" + ts + ".txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.info("EPG Suggester: scan saved to %s", out)
        preview = "\n".join(lines[:60])
        if len(lines) > 60:
            preview += "\n\n... full results in " + out
        return preview

    def _export(self, cfg, log):
        import csv, os
        from datetime import datetime
        results = self._run_matching(cfg, log)
        matched = sum(1 for r in results if r["suggestions"])
        os.makedirs("/data/exports", exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = "/data/exports/epg_suggester_" + ts + ".csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# EPG Suggester v2.0.0 | " + datetime.now().isoformat() + "\n")
            fh.write("# min_score=" + str(cfg["min_s"]) + "  max_suggestions=" + str(cfg["max_n"]) + "\n#\n")
            w = csv.writer(fh)
            w.writerow(["channel_id", "channel_name", "channel_norm", "channel_group",
                        "rank", "score", "epg_name", "tvg_id", "epg_source", "epg_data_id"])
            for r in results:
                if r["suggestions"]:
                    for rank, s in enumerate(r["suggestions"], 1):
                        w.writerow([r["channel_id"], r["channel_name"], r["channel_norm"],
                                    r["channel_group"], rank, s["score"], s["name"],
                                    s["tvg_id"], s["source"], s["id"]])
                else:
                    w.writerow([r["channel_id"], r["channel_name"], r["channel_norm"],
                                r["channel_group"], "", "", "NO_MATCH", "", "", ""])
        log.info("EPG Suggester: CSV saved to %s  (%d/%d matched)", path, matched, len(results))
        return ("CSV saved to " + path + "\nMatched: " + str(matched) + " / " + str(len(results))
                + "\n\ndocker cp dispatcharr:" + path + " ./")

    def _apply(self, cfg, log):
        from apps.channels.models import Channel
        if not cfg["auto"]:
            return "Auto-Apply is DISABLED. Enable it in settings first."
        results  = self._run_matching(cfg, log)
        applied = skipped = failed = 0
        for r in results:
            if not r["suggestions"] or r["suggestions"][0]["score"] < cfg["thresh"]:
                skipped += 1
                continue
            top = r["suggestions"][0]
            try:
                Channel.objects.filter(pk=r["channel_id"]).update(epg_data_id=top["id"])
                log.info("EPG Suggester: APPLY  %s -> %s (score=%d)",
                         r["channel_name"], top["name"], top["score"])
                applied += 1
            except Exception as e:
                log.error("EPG Suggester: FAIL  %s -> %s", r["channel_name"], e)
                failed += 1
        return "Applied: " + str(applied) + "  Skipped: " + str(skipped) + "  Failed: " + str(failed)
