import re
from apps.channels.models import Channel
# Adjust the EPG import based on your specific Dispatcharr version's schema
from apps.epgs.models import EpgChannel 

class Plugin:
    def __init__(self):
        self.key = "epg_suggester"
        self.name = "EPG Suggester"
        self.description = "Scans channels without EPGs and suggests mappings based on channel names."
        self.version = "1.0"
        self.actions = [
            {
                "id": "suggest_epg",
                "name": "Run EPG Suggestions",
                "description": "Analyzes missing EPGs and suggests matching."
            }
        ]

    def run(self, action, params, context):
        if action == "suggest_epg":
            return self.suggest_epg()
        return {"success": False, "error": "Unknown action"}

    def suggest_epg(self):
        try:
            # 1. Fetch channels missing EPG assignments
            missing_epg_channels = Channel.objects.filter(epg_id__isnull=True)
            
            if not missing_epg_channels.exists():
                return {"success": True, "message": "All channels already have an EPG assigned."}

            # 2. Fetch available EPG channels to match against
            available_epgs = EpgChannel.objects.all()
            
            suggestions = []
            for channel in missing_epg_channels:
                clean_name = self._clean_channel_name(channel.name)
                match = self._find_match(clean_name, available_epgs)
                
                if match:
                    suggestions.append(f"[{channel.name}] -> Suggestion: {match.name}")

            if not suggestions:
                return {"success": True, "message": "Scanned channels, but no confident EPG suggestions found."}

            # Return results to the Dispatcharr UI
            result_text = "EPG Suggestions Found:\n\n" + "\n".join(suggestions)
            return {"success": True, "message": result_text}

        except Exception as e:
            return {"success": False, "error": f"Error scanning EPGs: {str(e)}"}

    def _clean_channel_name(self, name):
        """
        Cleans strings like 'US| CNN vip' -> 'CNN'
        """
        # Remove country/region prefixes (e.g., "US| ", "UK: ", "CA - ")
        name = re.sub(r'^[A-Z]{2,3}\s*[\|\-:]\s*', '', name)
        # Remove common IPTV suffixes/qualities
        name = re.sub(r'\b(vip|fhd|hd|sd|4k|raw)\b', '', name, flags=re.IGNORECASE)
        # Strip extra whitespace and special characters at the ends
        return name.strip(' -|_[]()')

    def _find_match(self, clean_name, available_epgs):
        """
        Matches cleaned channel name against available EPG names.
        """
        clean_name_lower = clean_name.lower()
        if not clean_name_lower:
            return None
            
        # 1. Attempt exact match first
        for epg in available_epgs:
            if clean_name_lower == epg.name.lower():
                return epg
                
        # 2. Attempt partial match fallback
        for epg in available_epgs:
            epg_name_lower = epg.name.lower()
            if clean_name_lower in epg_name_lower or epg_name_lower in clean_name_lower:
                return epg
                
        return None
        