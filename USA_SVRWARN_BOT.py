import time
import pygame
import os
import feedparser
import re
import requests

# Initialize the Pygame Audio Engine
pygame.mixer.init()

# --- CONFIGURATION ---
IEM_RSS_URL = "https://weather.im/iembot-rss/room/botstalk.xml" 
CANADA_API_URL = "https://api.weather.gc.ca/collections/weather-alerts/items?f=json"
feedparser.USER_AGENT = "(MyWeatherBot, keystoneweather1825@gmail.com)"

# --- SOUND FILES ---
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
    "SPC_PRODUCT": "LSR Beep.wav", 
    "WPC_OUTLOOK": "LSR Beep.wav",
    "WPC_DISCUSSION": "LSR Beep.wav",
    "NHC_OUTLOOK": "LSR Beep.wav",
    "NHC_ADVISORY": "LSR Beep.wav",
    "GENERIC_WARNING": "generic_warning.mp3",
    "EC_ALERT": "ffw_standard.mp3" # New custom sound for Environment Canada alerts
}

def play_alert_sound(threat_level):
    filename = SOUND_FILES.get(threat_level, SOUND_FILES["GENERIC_WARNING"])
    if os.path.exists(filename):
        try:
            pygame.mixer.music.load(filename)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.2)
        except Exception as e:
            print(f"Error playing sound {filename}: {e}")
    else:
        print(f"⚠️ Audio file '{filename}' not found! Skipping sound.")

def is_target_event(title, text, is_canada=False):
    title_lower = title.lower()
    text_lower = text.lower()
    
    # 1. EXCLUSIONS 
    if "cancel" in title_lower or "expir" in title_lower:
        return False
        
    # The Canadian API only outputs active alerts, so we allow them all through
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
        
    # 2. ADVISORY SAFETY CATCH
    if "advisory" in title_lower:
        if not ("nhc" in title_lower or "hurricane" in title_lower or "tropical" in title_lower):
            return False

    # 3. TARGET INCLUSIONS 
    targets = [
        "tornado warning", "severe thunderstorm warning", "flash flood warning",
        "severe weather statement", "spc issues", "storm prediction center",
        "convective outlook", "mesoscale discussion", "mesoscale precipitation", 
        "excessive rainfall", "tropical weather outlook", "advisory", "hurricane", "tropical storm"
    ]
    
    return any(target in title_lower for target in targets)

def parse_vtec(text):
    match = re.search(r'/O\.(?:NEW|CON|EXT|EXA|EXB|UPG|COR)\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.([0-9]{4})\.([0-9]{2})', text)
    if match:
        office, phenom, signif, etn, yy = match.groups()
        storm_id = f"{office}_{phenom}_{signif}_{etn}"
        wfo = office[1:]
        year = f"20{yy}"
        url = f"https://mesonet.agron.iastate.edu/plotting/auto/plot/208/network:WFO::wfo:{wfo}::year:{year}::phenomenav:{phenom}::significancev:{signif}::etn:{etn}.png"
        return storm_id, url
    return None, None

def extract_tags_from_text(text):
    tags = []
    tor_detect = re.search(r'TORNADO\.\.\.(.+)', text)
    tor_threat = re.search(r'TORNADO DAMAGE THREAT\.\.\.(.+)', text)
    svr_threat = re.search(r'THUNDERSTORM DAMAGE THREAT\.\.\.(.+)', text)
    ffw_threat = re.search(r'FLASH FLOOD DAMAGE THREAT\.\.\.(.+)', text) 
    hail = re.search(r'MAX HAIL SIZE\.\.\.(.+)', text)
    wind = re.search(r'MAX WIND GUST\.\.\.(.+)', text)
    
    if tor_detect: tags.append(f"Tornado: {tor_detect.group(1).strip()}")
    if tor_threat: tags.append(f"Tor Threat: {tor_threat.group(1).strip()}")
    if svr_threat: tags.append(f"Svr Threat: {svr_threat.group(1).strip()}")
    if ffw_threat: tags.append(f"Flood Threat: {ffw_threat.group(1).strip()}")
    if hail: 
        hail_val = hail.group(1).strip().replace('<', '< ')
        tags.append(f"Hail: {hail_val}")
    if wind: tags.append(f"Wind: {wind.group(1).strip()}")
    
    return " | ".join(tags) if tags else "No explicit tags"

def get_comparison_key(tags_string):
    return re.sub(r'[\d\.]+', '', tags_string).strip()

def determine_threat_level(title, text):
    title_upper = title.upper()
    text_upper = text.upper()
    
    # NHC Products
    if "TROPICAL WEATHER OUTLOOK" in title_upper: return "NHC_OUTLOOK"
    if "NHC" in title_upper and "ADVISORY" in title_upper: return "NHC_ADVISORY"
    if "HURRICANE" in title_upper or "TROPICAL STORM" in title_upper: return "NHC_ADVISORY"
    
    # WPC Products
    if "EXCESSIVE RAINFALL" in title_upper: return "WPC_OUTLOOK"
    if "MESOSCALE PRECIPITATION" in title_upper or ("WPC" in title_upper and "MESOSCALE" in title_upper): 
        return "WPC_DISCUSSION"
        
    # SPC Products
    if "CONVECTIVE OUTLOOK" in title_upper or \
       "MESOSCALE DISCUSSION" in title_upper or \
       ("SPC" in title_upper and "MESOSCALE" in title_upper) or \
       "SPC ISSUES" in title_upper or \
       "STORM PREDICTION CENTER" in title_upper:
        return "SPC_PRODUCT"
    
    # Standard Warning Logic
    if "TORNADO WARNING" in title_upper or ("STATEMENT" in title_upper and "TORNADO" in text_upper):
        if "DAMAGE THREAT...CATASTROPHIC" in text_upper: return "TORNADO_EMERGENCY"
        elif "DAMAGE THREAT...CONSIDERABLE" in text_upper: return "TORNADO_PDS"
        elif "TORNADO...OBSERVED" in text_upper: return "TORNADO_OBSERVED"
        else: return "TORNADO_STANDARD"
            
    elif "SEVERE THUNDERSTORM WARNING" in title_upper or ("STATEMENT" in title_upper and "SEVERE THUNDERSTORM" in text_upper):
        if "DAMAGE THREAT...DESTRUCTIVE" in text_upper: return "SVR_DESTRUCTIVE"
        elif "DAMAGE THREAT...CONSIDERABLE" in text_upper: return "SVR_CONSIDERABLE"
        else: return "SVR_STANDARD"
            
    elif "FLASH FLOOD WARNING" in title_upper:
        if "DAMAGE THREAT...CATASTROPHIC" in text_upper: return "FFW_EMERGENCY"
        elif "DAMAGE THREAT...CONSIDERABLE" in text_upper: return "FFW_CONSIDERABLE" 
        else: return "FFW_STANDARD"
            
    return "GENERIC_WARNING"

def process_new_or_updated_alert(storm_id, title, threat_level, raw_tags, radar_url, comparison_key, tracked_storms):
    # SCENARIO 1: Completely new event
    if storm_id not in tracked_storms:
        tracked_storms[storm_id] = comparison_key
        
        print("\n" + "="*70)
        print(f"🚨 NEW EVENT ISSUED")
        print(f"📰 HEADLINE:      {title}")
        print(f"📈 THREAT LEVEL:  {threat_level}")
        
        if raw_tags != "No explicit tags":
            print(f"🏷️ IMPACT TAGS:   {raw_tags}")
        if radar_url:
            print(f"🗺️ RADAR IMAGE:   {radar_url}")
            
        print("="*70 + "\n")
        
        play_alert_sound(threat_level)
        
    # SCENARIO 2: Event Update + Word Tags Changed
    elif tracked_storms[storm_id] != comparison_key:
        tracked_storms[storm_id] = comparison_key
        
        print("\n" + "="*70)
        print(f"⚠️ EVENT UPDATE (STRUCTURAL TAGS CHANGED)")
        print(f"📰 HEADLINE:      {title}")
        print(f"📈 THREAT LEVEL:  {threat_level}")
        print(f"🆕 REVISED TAGS:  {raw_tags}")
        
        if radar_url:
            print(f"🗺️ RADAR IMAGE:   {radar_url}")
            
        print("="*70 + "\n")
        
        play_alert_sound(threat_level)
        
    # SCENARIO 3: Event Update + Words match perfectly
    else:
        print(f"🔇 [SILENCED] Ignored update for: {title} (Numeric fluctuation only)")


def main():
    print("📡 Initializing Weather Bot (US & Canadian Alerts Enabled)...")
    seen_rss_guids = set()
    tracked_storms = {} 
    
    # --- 1. LOAD US RSS HISTORY ---
    try:
        feed = feedparser.parse(IEM_RSS_URL)
        if feed.bozo and not feed.entries:
            print(f"❌ IEM Server Rejected Connection: {feed.bozo_exception}")
        else:
            for entry in feed.entries[::-1]:
                guid = entry.id if 'id' in entry else entry.link
                if guid: seen_rss_guids.add(guid)
                    
                title = entry.title
                description = entry.description if 'description' in entry else ""
                
                if is_target_event(title, description):
                    vtec_data = parse_vtec(description)
                    storm_id = vtec_data[0] if vtec_data[0] else guid 
                    raw_tags = extract_tags_from_text(description)
                    tracked_storms[storm_id] = get_comparison_key(raw_tags)
    except Exception as e:
        print(f"❌ Could not initialize US RSS feed history: {e}")

    # --- 2. LOAD CANADA API HISTORY ---
    try:
        response = requests.get(CANADA_API_URL, timeout=10)
        data = response.json()
        for feature in data.get('features', []):
            props = feature.get('properties', {})
            guid = props.get('id')
            if guid: seen_rss_guids.add(guid)
                
            title = props.get('alert_name_en', '')
            desc = props.get('alert_text_en', '')
            
            if is_target_event(title, desc, is_canada=True):
                raw_tags = extract_tags_from_text(desc)
                tracked_storms[guid] = get_comparison_key(raw_tags)
    except Exception as e:
        print(f"❌ Could not initialize Canada API history: {e}")
        
    print(f"✅ Loaded {len(seen_rss_guids)} past updates. Tracking {len(tracked_storms)} active storms/products.")
    print("👂 Monitoring for Warnings, TAG CHANGES, and National Products...\n")

    while True:
        time.sleep(30) 
        
        # --- POLL US RSS ---
        try:
            feed = feedparser.parse(IEM_RSS_URL)
            entries = feed.entries[::-1]
            
            for entry in entries:
                guid = entry.id if 'id' in entry else entry.link
                if guid and guid not in seen_rss_guids:
                    seen_rss_guids.add(guid)
                    
                    title = entry.title
                    description = entry.description if 'description' in entry else ""
                    
                    if is_target_event(title, description):
                        vtec_data = parse_vtec(description)
                        storm_id = vtec_data[0] if vtec_data[0] else guid
                        radar_url = vtec_data[1]
                        raw_tags = extract_tags_from_text(description)
                        comparison_key = get_comparison_key(raw_tags)
                        threat_level = determine_threat_level(title, description)
                        
                        process_new_or_updated_alert(storm_id, title, threat_level, raw_tags, radar_url, comparison_key, tracked_storms)
        except Exception as e:
            print(f"Connection error during US polling: {e}")

        # --- POLL CANADA API ---
        try:
            response = requests.get(CANADA_API_URL, timeout=10)
            data = response.json()
            
            for feature in data.get('features', []):
                props = feature.get('properties', {})
                guid = props.get('id')
                
                if guid and guid not in seen_rss_guids:
                    seen_rss_guids.add(guid)
                    
                    title = props.get('alert_name_en', '')
                    description = props.get('alert_text_en', '')
                    
                    if is_target_event(title, description, is_canada=True):
                        storm_id = guid
                        radar_url = None # Canada API does not supply automated radar VTEC links
                        raw_tags = extract_tags_from_text(description)
                        comparison_key = get_comparison_key(raw_tags)
                        
                        threat_level = "EC_ALERT"
                        display_title = f"[CANADA] {title}"
                        
                        process_new_or_updated_alert(storm_id, display_title, threat_level, raw_tags, radar_url, comparison_key, tracked_storms)
        except Exception as e:
            print(f"Connection error during Canada API polling: {e}")

if __name__ == "__main__":
    main()