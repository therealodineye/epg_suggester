# Dispatcharr EPG Suggester Plugin

A plugin for Dispatcharr that automatically scans your channels, identifies those missing an EPG assignment, and suggests appropriate EPG mappings based on intelligent channel name parsing.

## Features
* **Automated Scanning:** Finds all channels currently missing an EPG ID.
* **Intelligent Name Cleaning:** Strips common IPTV prefixes (e.g., `US|`, `UK:`) and suffixes (e.g., `vip`, `FHD`, `4K`) to isolate the core channel name.
* **Smart Matching:** Attempts exact and partial string matching against your available EPG sources to provide accurate suggestions.

## Installation
1. Save the plugin code as `plugin.py`.
2. Compress the file into a `.zip` archive (e.g., `epg_suggester.zip`).
3. Navigate to the **Plugins** section in your Dispatcharr web UI.
4. Click **Import Plugin**, select your `.zip` file, and upload.
5. Enable the plugin.

## Usage
1. Go to the **Plugins** page in Dispatcharr.
2. Locate the **EPG Suggester** plugin.
3. Execute the **Run EPG Suggestions** action.
4. Review the generated output in the UI to see the suggested mappings for your unmatched channels.

## Customization
If your M3U playlist uses unique naming conventions, you can modify the Regex patterns in the `_clean_channel_name` function within `plugin.py` to better fit your provider's format.