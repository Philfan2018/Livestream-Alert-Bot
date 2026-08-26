"""
USA_SVRWARN_BOT.py - Enhanced weather alert bot for US and Canada.
Monitors IEM RSS and Environment Canada API for severe weather alerts.
Plays custom sounds and logs updates.
"""

import time
import threading
import logging
import os
import sys
import re
import json
from datetime import datetime, timedelta
from collections import deque

import pygame
import feedparser
import requests

# -------------------- ENCODING FIX FOR WINDOWS CONSOLE --------------------
# Force stdout/stderr to UTF-8 to avoid Unicode errors with emojis
if sys.stdout.encoding != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

# -------------------- CONFIGURATION --------------------
IEM_RSS_URL = "https://weather.im/iembot-rss/room/botstalk.xml"
CANADA_API_URL = "https://api.weather.gc.ca/collections/weather-alerts/items?f=json"
POLL_INTERVAL = 30  # seconds between polls
STORM_TTL_HOURS = 6  # hours before an alert is considered expired and removed
USER_AGENT = "(MyWeatherBot, keystoneweather1825@gmail.com)"

# Sound files mapping
SOUND_FILES = {
    "TORNADO_EMERGENCY": "PDSTor_Destructive SVR Alert.wav",
    "TORNADO_PDS": "PDSTor_Destructive SVR Alert.wav",
    "TORNADO_OBSERVED": "Tornado_Confirmed.wav",
    "TORNADO_STANDARD": "tor_standard.mp3",
    "SVR_DESTRUCTIVE": "PDSTor_Destructive SVR Alert.wav",
    "SVR_CONSIDERABLE": "svr_considerable.wav",
    "SVR_STANDARD": "svr_standard.wav",
    "FFW_EMERGENCY": "PDSTor_Destructive SVR Alert.wav",
    "FFW_CONSIDERABLE": "svr_considerable.wav",
    "FFW_STANDARD": "ffw_standard.mp3",
    "SPC_PRODUCT": "LSR Beep.wav",      # Used for ALL SPC products
    "WPC_OUTLOOK": "LSR Beep.wav",
    "WPC_DISCUSSION": "LSR Beep.wav",
    "NHC_OUTLOOK": "LSR Beep.wav",
    "NHC_ADVISORY": "LSR Beep.wav",
    "GENERIC_WARNING": "generic_warning.mp3",
    "EC_ALERT": "ffw_standard.mp3"
}

# -------------------- LOGGING SETUP (clean console, full file) --------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# File handler: full timestamp and level
file_handler = logging.FileHandler("weather_bot.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

# Console handler: message only (no timestamp/level)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))

# Add both handlers
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.propagate = False  # Prevent duplicate logs from root logger

# -------------------- SOUND QUEUE (Non‑blocking, sequential) --------------------
class SoundQueue:
    def __init__(self):
        self.queue = deque()
        self.lock = threading.Lock()
        self.playing = False
        self.thread = None

    def _play_next(self):
        with self.lock:
            if not self.queue:
                self.playing = False
                return
            filename = self.queue.popleft()
        try:
            # Load and play sound – this blocks until it finishes
            sound = pygame.mixer.Sound(filename)
            sound.play()
            while pygame.mixer.get_busy():
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"Error playing sound {filename}: {e}")
        # Continue with next in queue
        self._play_next()

    def add_sound(self, filename):
        if not os.path.exists(filename):
            logger.warning(f"Audio file '{filename}' not found – skipping.")
            return
        with self.lock:
            self.queue.append(filename)
            if not self.playing:
                self.playing = True
                # Start playing in a separate thread
                threading.Thread(target=self._play_next, daemon=True).start()

# Global sound queue instance
sound_queue = SoundQueue()

def play_alert_sound_async(threat_level):
    """Queue a sound for playback (sequential, non‑blocking)."""
    filename = SOUND_FILES.get(threat_level, SOUND_FILES.get("GENERIC_WARNING"))
    sound_queue.add_sound(filename)

# -------------------- UTILITY FUNCTIONS --------------------
def is_target_event(title, text, is_canada=False):
    """Determine if this alert should be processed."""
    title_lower = title.lower()
    text_lower = text.lower()

    # 1. Exclusions
    if "cancel" in title_lower or "expir" in title_lower:
        return False

    if is_canada:
        return True

    if "fire weather" in title_lower or "fire weather" in text_lower:
        return False
    if "for portions of" in title_lower or "for portions of" in text_lower:
        return False
    if "zone forecast package" in title_lower or "zfp" in title_lower:
        return False
    if "zone forecast package" in text_lower or "zfp" in text_lower:
        return False

    # Advisory safety catch
    if "advisory" in title_lower:
        if not ("nhc" in title_lower or "hurricane" in title_lower or "tropical" in title_lower):
            return False

    # --- Ensure ANY SPC product gets through ---
    targets = [
        "tornado warning", "severe thunderstorm warning", "flash flood warning",
        "severe weather statement", "spc issues", "storm prediction center",
        "convective outlook", "mesoscale discussion", "mesoscale precipitation",
        "excessive rainfall", "tropical weather outlook", "advisory", "hurricane",
        "tropical storm", "watch",
        "spc"                      # <-- catches any mention of SPC
    ]
    return any(target in title_lower for target in targets)

def parse_vtec(text):
    """Extract VTEC info and generate a radar image URL."""
    match = re.search(
        r'/O\.(?:NEW|CON|EXT|EXA|EXB|UPG|COR)\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.([0-9]{4})\.([0-9]{2})',
        text,
        re.IGNORECASE
    )
    if match:
        office, phenom, signif, etn, yy = match.groups()
        storm_id = f"{office}_{phenom}_{signif}_{etn}"
        wfo = office[1:]  # strip leading 'K'
        year = f"20{yy}"
        url = (
            f"https://mesonet.agron.iastate.edu/plotting/auto/plot/208/"
            f"network:WFO::wfo:{wfo}::year:{year}::phenomenav:{phenom}::significancev:{signif}::etn:{etn}.png"
        )
        return storm_id, url
    return None, None

def extract_tags_from_text(text):
    """Extract damage threat, hail, wind, etc. from the alert text."""
    tags = []
    tor_detect = re.search(r'TORNADO\.\.\.(.+)', text, re.IGNORECASE)
    tor_threat = re.search(r'TORNADO DAMAGE THREAT\.\.\.(.+)', text, re.IGNORECASE)
    svr_threat = re.search(r'THUNDERSTORM DAMAGE THREAT\.\.\.(.+)', text, re.IGNORECASE)
    ffw_threat = re.search(r'FLASH FLOOD DAMAGE THREAT\.\.\.(.+)', text, re.IGNORECASE)
    hail = re.search(r'MAX HAIL SIZE\.\.\.(.+)', text, re.IGNORECASE)
    wind = re.search(r'MAX WIND GUST\.\.\.(.+)', text, re.IGNORECASE)

    if tor_detect:
        tags.append(f"Tornado: {tor_detect.group(1).strip()}")
    if tor_threat:
        tags.append(f"Tor Threat: {tor_threat.group(1).strip()}")
    if svr_threat:
        tags.append(f"Svr Threat: {svr_threat.group(1).strip()}")
    if ffw_threat:
        tags.append(f"Flood Threat: {ffw_threat.group(1).strip()}")
    if hail:
        hail_val = hail.group(1).strip().replace('<', '< ')
        tags.append(f"Hail: {hail_val}")
    if wind:
        tags.append(f"Wind: {wind.group(1).strip()}")

    return " | ".join(tags) if tags else "No explicit tags"

def get_comparison_key(tags_string):
    """Remove numeric values to compare only structural tags."""
    return re.sub(r'[\d\.]+', '', tags_string).strip()

def determine_threat_level(title, text):
    """
    Return a threat level key for sound selection.
    SPC products always get SPC_PRODUCT sound.
    """
    title_upper = title.upper()
    text_upper = text.upper()

    # --- TOP PRIORITY: anything from the Storm Prediction Center ---
    if "SPC" in title_upper or "STORM PREDICTION CENTER" in title_upper:
        return "SPC_PRODUCT"

    # Other national products
    if "TROPICAL WEATHER OUTLOOK" in title_upper:
        return "NHC_OUTLOOK"
    if "NHC" in title_upper and "ADVISORY" in title_upper:
        return "NHC_ADVISORY"
    if "HURRICANE" in title_upper or "TROPICAL STORM" in title_upper:
        return "NHC_ADVISORY"

    if "EXCESSIVE RAINFALL" in title_upper:
        return "WPC_OUTLOOK"
    if "MESOSCALE PRECIPITATION" in title_upper or ("WPC" in title_upper and "MESOSCALE" in title_upper):
        return "WPC_DISCUSSION"

    # Local warnings (NWS offices) - use flexible regex for damage threat
    # Tornado Warning
    if "TORNADO WARNING" in title_upper or ("STATEMENT" in title_upper and "TORNADO" in text_upper):
        if re.search(r'DAMAGE THREAT.*CATASTROPHIC', text_upper):
            return "TORNADO_EMERGENCY"
        elif re.search(r'DAMAGE THREAT.*CONSIDERABLE', text_upper):
            return "TORNADO_PDS"
        elif "TORNADO...OBSERVED" in text_upper:
            return "TORNADO_OBSERVED"
        else:
            return "TORNADO_STANDARD"

    # Severe Thunderstorm Warning
    if "SEVERE THUNDERSTORM WARNING" in title_upper or ("STATEMENT" in title_upper and "SEVERE THUNDERSTORM" in text_upper):
        if re.search(r'DAMAGE THREAT.*DESTRUCTIVE', text_upper):
            return "SVR_DESTRUCTIVE"
        elif re.search(r'DAMAGE THREAT.*CONSIDERABLE', text_upper):
            return "SVR_CONSIDERABLE"
        else:
            return "SVR_STANDARD"

    # Flash Flood Warning
    if "FLASH FLOOD WARNING" in title_upper:
        if re.search(r'DAMAGE THREAT.*CATASTROPHIC', text_upper):
            return "FFW_EMERGENCY"
        elif re.search(r'DAMAGE THREAT.*CONSIDERABLE', text_upper):
            return "FFW_CONSIDERABLE"
        else:
            return "FFW_STANDARD"

    return "GENERIC_WARNING"

def clean_old_storms(tracked_storms, ttl_hours=STORM_TTL_HOURS):
    """Remove storms older than ttl_hours."""
    cutoff = datetime.now() - timedelta(hours=ttl_hours)
    to_remove = [sid for sid, data in tracked_storms.items() if data['timestamp'] < cutoff]
    for sid in to_remove:
        del tracked_storms[sid]
        logger.debug(f"Removed expired storm: {sid}")
    return len(to_remove)

def process_alert(storm_id, title, threat_level, raw_tags, radar_url, comparison_key,
                  tracked_storms, risk_color=None, source="US"):
    """
    Process a new or updated alert.
    tracked_storms stores: {storm_id: {'key': comparison_key, 'timestamp': datetime}}
    """
    now = datetime.now()
    if storm_id not in tracked_storms:
        # New storm
        tracked_storms[storm_id] = {'key': comparison_key, 'timestamp': now}
        logger.info("=" * 70)
        logger.info(f"🚨 NEW EVENT ISSUED [{source}]")
        logger.info(f"📰 HEADLINE: {title}")
        if risk_color and risk_color.lower() != "unknown":
            logger.info(f"🎨 RISK COLOR: {risk_color.upper()}")
        logger.info(f"📈 THREAT LEVEL: {threat_level}")
        if raw_tags != "No explicit tags":
            logger.info(f"🏷️ IMPACT TAGS: {raw_tags}")
        if radar_url:
            logger.info(f"🗺️ RADAR IMAGE: {radar_url}")
        logger.info("=" * 70)
        play_alert_sound_async(threat_level)

    elif tracked_storms[storm_id]['key'] != comparison_key:
        # Updated storm (tags changed)
        tracked_storms[storm_id]['key'] = comparison_key
        tracked_storms[storm_id]['timestamp'] = now
        logger.info("=" * 70)
        logger.info(f"⚠️ EVENT UPDATE (TAGS CHANGED) [{source}]")
        logger.info(f"📰 HEADLINE: {title}")
        if risk_color and risk_color.lower() != "unknown":
            logger.info(f"🎨 RISK COLOR: {risk_color.upper()}")
        logger.info(f"📈 THREAT LEVEL: {threat_level}")
        logger.info(f"🆕 REVISED TAGS: {raw_tags}")
        if radar_url:
            logger.info(f"🗺️ RADAR IMAGE: {radar_url}")
        logger.info("=" * 70)
        play_alert_sound_async(threat_level)
    else:
        logger.debug(f"[{source}] No change for {storm_id} – ignored.")

# -------------------- MAIN LOOP --------------------
def main():
    pygame.mixer.init()

    seen_rss_guids = set()
    tracked_storms = {}

    logger.info("📡 Initializing Weather Bot (US & Canadian Alerts)...")
    try:
        feed = feedparser.parse(IEM_RSS_URL)
        if feed.bozo and not feed.entries:
            logger.error(f"IEM Server error: {feed.bozo_exception}")
        else:
            for entry in feed.entries[::-1]:
                guid = entry.id if 'id' in entry else entry.link
                if guid:
                    seen_rss_guids.add(guid)
                title = entry.title
                description = entry.description if 'description' in entry else ""
                if is_target_event(title, description):
                    vtec_data = parse_vtec(description)
                    storm_id = vtec_data[0] if vtec_data[0] else guid
                    raw_tags = extract_tags_from_text(description)
                    key = get_comparison_key(raw_tags)
                    tracked_storms[storm_id] = {'key': key, 'timestamp': datetime.now()}
    except Exception as e:
        logger.error(f"Could not initialize US RSS history: {e}")

    try:
        resp = requests.get(CANADA_API_URL, timeout=10, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        for feature in data.get('features', []):
            props = feature.get('properties', {})
            guid = props.get('id')
            if not guid:
                continue
            stable_id = str(guid).split('_')[0]
            title = props.get('alert_name_en', '')
            description = props.get('alert_text_en', '')
            if is_target_event(title, description, is_canada=True):
                raw_tags = extract_tags_from_text(description)
                key = get_comparison_key(raw_tags)
                tracked_storms[stable_id] = {'key': key, 'timestamp': datetime.now()}
    except Exception as e:
        logger.error(f"Could not initialize Canada API history: {e}")

    logger.info(f"Loaded {len(seen_rss_guids)} past US updates. Tracking {len(tracked_storms)} active storms/products.")
    logger.info("👂 Monitoring for Warnings, TAG CHANGES, and National Products...")

    cleanup_counter = 0

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            cleanup_counter += 1
            if cleanup_counter >= 10:
                removed = clean_old_storms(tracked_storms)
                if removed:
                    logger.info(f"🧹 Removed {removed} expired storms.")
                cleanup_counter = 0

            # ---------- US RSS ----------
            try:
                feed = feedparser.parse(IEM_RSS_URL)
                for entry in feed.entries[::-1]:
                    guid = entry.id if 'id' in entry else entry.link
                    if guid in seen_rss_guids:
                        continue
                    seen_rss_guids.add(guid)

                    title = entry.title
                    description = entry.description if 'description' in entry else ""
                    if not is_target_event(title, description):
                        continue

                    vtec_data = parse_vtec(description)
                    storm_id = vtec_data[0] if vtec_data[0] else guid
                    radar_url = vtec_data[1]
                    raw_tags = extract_tags_from_text(description)
                    comparison_key = get_comparison_key(raw_tags)
                    threat_level = determine_threat_level(title, description)

                    process_alert(
                        storm_id=storm_id,
                        title=title,
                        threat_level=threat_level,
                        raw_tags=raw_tags,
                        radar_url=radar_url,
                        comparison_key=comparison_key,
                        tracked_storms=tracked_storms,
                        source="US"
                    )
            except Exception as e:
                logger.error(f"Error during US polling: {e}")

            # ---------- Canada API ----------
            try:
                resp = requests.get(CANADA_API_URL, timeout=10, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                data = resp.json()
                for feature in data.get('features', []):
                    props = feature.get('properties', {})
                    guid = props.get('id')
                    if not guid:
                        continue
                    stable_id = str(guid).split('_')[0]
                    title = props.get('alert_name_en', '')
                    description = props.get('alert_text_en', '')
                    if not is_target_event(title, description, is_canada=True):
                        continue

                    raw_tags = extract_tags_from_text(description)
                    comparison_key = get_comparison_key(raw_tags)
                    threat_level = "EC_ALERT"
                    display_title = f"[CANADA] {title}"
                    risk_color = props.get('risk_colour_en', 'Unknown')

                    process_alert(
                        storm_id=stable_id,
                        title=display_title,
                        threat_level=threat_level,
                        raw_tags=raw_tags,
                        radar_url=None,
                        comparison_key=comparison_key,
                        tracked_storms=tracked_storms,
                        risk_color=risk_color,
                        source="Canada"
                    )
            except requests.exceptions.RequestException as e:
                logger.error(f"Canada API connection error: {e}")
            except json.JSONDecodeError as e:
                logger.error(f"Canada API returned invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Unexpected error during Canada polling: {e}")

    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
