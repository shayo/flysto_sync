import os
import json
import time
import subprocess
import re
import requests
import zipfile
import io
from pathlib import Path
from typing import List, Dict, Optional
from lcd_helper import LCDDisplay
import threading

# Configuration
CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)



class FlyStoClient:
    def __init__(self, email: str, password: str):
        self._session = requests.Session()
        self._email = email
        self._password = password
        self._base_url = "https://www.flysto.net/api"
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.flysto.net/login"
        })
        self.is_authenticated = self._authenticate()

    def _authenticate(self) -> bool:
        try:
            response = self._session.post(
                f"{self._base_url}/login", 
                json={"email": self._email, "password": self._password}, 
                headers={"Content-Type": "text/plain;charset=UTF-8"}
            )
            return response.status_code == 204 and "USER_SESSION" in self._session.cookies
        except:
            return False

    def upload_log(self, file_path: Path) -> bool:
        if not self.is_authenticated: return False
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.write(file_path, arcname=file_path.name)
        
        try:
            response = self._session.post(
                f"{self._base_url}/log-upload", 
                params={"id": file_path.name}, 
                headers={"Content-Type": "application/zip"}, 
                data=zip_buffer.getvalue()
            )
            return response.status_code in [200, 201, 204]
        except:
            return False

class LocalDatabase:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> Dict:
        if self.db_path.exists():
            try:
                with open(self.db_path, 'r') as f: return json.load(f)
            except: return {}
        return {}

    def save(self):
        with open(self.db_path, 'w') as f: json.dump(self.data, f, indent=4)

    def is_recorded(self, filename: str) -> bool:
        return filename in self.data

    def mark_done(self, filename: str, metadata: Optional[Dict] = None):
        self.data[filename] = metadata or {"timestamp": time.time()}
        self.save()

class WiFiManager:
    def __init__(self, config: Dict):
        self.config = config
        self.interface = 'wlan0'

    def scan_networks(self) -> List[str]:
        try:
            result = subprocess.run(['sudo', 'iwlist', self.interface, 'scan'], capture_output=True, text=True, timeout=15)
            return list(set(re.findall(r'ESSID:"([^"]*)"', result.stdout)))
        except: return []

    def force_connect(self, ssid, password) -> bool:
        print(f"Force connecting to {ssid}...")
        subprocess.run(['sudo', 'nmcli', 'connection', 'delete', ssid], capture_output=True)
        subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'down'], capture_output=True)
        time.sleep(1)
        subprocess.run(['sudo', 'ip', 'link', 'set', self.interface, 'up'], capture_output=True)
        time.sleep(2)

        # High priority for internet, low for FlashAir
        is_internet = any(net['ssid'] == ssid for net in self.config['internet_networks'])
        priority = "100" if is_internet else "1"
        
        cmd = ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid, 'password', password, 'ifname', self.interface, 'name', ssid]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        
        if "successfully activated" in result.stdout.lower():
            subprocess.run(['sudo', 'nmcli', 'connection', 'modify', ssid, 'connection.autoconnect-priority', priority])
            time.sleep(5)
            return True
        return False

    def connect_to_any_internet(self, available_networks: List[str]) -> bool:
        """Attempts to connect to the first available internet network from the config."""
        for net in self.config['internet_networks']:
            if net['ssid'] in available_networks:
                print(f"Internet network {net['ssid']} found in scan.")
                if self.force_connect(net['ssid'], net['password']):
                    return True
        return False

class FlashAirClient:
    def __init__(self, ip: str):
        self.ip = ip.rstrip('/')

    def list_files(self, directory: str = "/") -> List[Dict]:
        try:
            response = requests.get(f"{self.ip}/command.cgi", params={'op': 100, 'DIR': directory}, timeout=10)
            lines = response.text.strip().split('\n')
            files = []
            for line in lines[1:]:
                parts = line.split(',')
                if len(parts) >= 6:
                    files.append({'filename': parts[1], 'size': int(parts[2]), 'date': int(parts[4]), 'is_directory': bool(int(parts[3]) & 0x10)})
            return files
        except: return []

    def download_file(self, remote_path: str, local_path: str) -> bool:
        try:
            with requests.get(f"{self.ip}/{remote_path.lstrip('/')}", stream=True, timeout=30) as r:
                r.raise_for_status()
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192): f.write(chunk)
            return True
        except: return False


# --- Define the actions ---
def handle_manual_sync(channel):
    print("Button 1 pressed: Starting manual sync...")
    # Because this runs in a separate thread, be careful with shared resources
    # Ideally, set a flag that the main loop checks
    lcd.update_status("MANUAL", "Sync Triggered!")

def handle_reboot(channel):
    lcd.update_status("SYSTEM", "Rebooting...")
    time.sleep(2)
    os.system("sudo reboot")
    
# --- Main Logic ---


class SyncOrchestrator:
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.config = self.load_config()
        self.cycle_counter = 0
        
        # Initialization
        self.lcd = LCDDisplay()
        self.local_db = LocalDatabase(self.config['local_db_path'])
        self.flysto_db = LocalDatabase(self.config['flysto_db_path'])
        self.wifi = WiFiManager(self.config)
        
        # Control Flags
        self.manual_sync_requested = False
        self.is_running = False # Prevents overlapping syncs
        
        # Setup Hardware Interrupts
        self.lcd.set_callbacks(
            key1_func=self.handle_manual_sync_btn,
            key3_func=self.handle_reboot_btn
        )

    def load_config(self):
        with open(self.config_path, 'r') as f:
            return json.load(f)

    def handle_manual_sync_btn(self, channel):
        """Callback for Button 1 - sets flag for main loop."""
        if not self.is_running:
            print("Manual sync requested via button.")
            self.manual_sync_requested = True
        else:
            print("Sync already in progress, ignoring button.")

    def handle_reboot_btn(self, channel):
        """Callback for Button 3 - System Reboot."""
        self.lcd.update_status("SYSTEM", "Rebooting...")
        time.sleep(2)
        os.system("sudo reboot")

    def run_sync_cycle(self):
        """The core sync logic logic."""
        self.is_running = True
        self.manual_sync_requested = False
        self.config = self.load_config() # Refresh config in case user edited it via web

        try:
            print(f"Starting Cycle {self.cycle_counter}")
            self.lcd.update_status("SCANNING", f"Cycle {self.cycle_counter}")
            available_networks = self.wifi.scan_networks()

            # --- Phase 1: FlashAir ---
            if self.config['flashair_wifi_ssid'] in available_networks:
                self.lcd.update_status("WIFI", "Connecting FlashAir")
                if self.wifi.force_connect(self.config['flashair_wifi_ssid'], self.config['flashair_wifi_password']):
                    
                    self.lcd.update_status("FLASHAIR", "Listing files...")
                    fa_client = FlashAirClient(self.config['flashair_ip'])
                    files = fa_client.list_files(self.config['flashair_data_log_dir'])
                    
                    to_download = [f for f in files if not f['is_directory'] and not self.local_db.is_recorded(f['filename'])]
                    
                    for i, f_info in enumerate(to_download):
                        fname = f_info['filename']
                        progress = (i + 1) / len(to_download)
                        self.lcd.update_status("DOWNLOADING", fname, progress)
                        
                        remote = f"{self.config['flashair_data_log_dir']}/{fname}"
                        local = Path(self.config['local_repo_path']) / fname
                        if fa_client.download_file(remote, str(local)):
                            self.local_db.mark_done(fname, {"size": f_info['size'], "date": f_info['date']})
            
            # --- Phase 2: Internet/FlySto ---
            local_files = list(Path(self.config['local_repo_path']).glob('*.csv'))
            pending = [f for f in local_files if not self.flysto_db.is_recorded(f.name)]
            if len(pending) > 0:

                self.lcd.update_status("WIFI", "Internet?")
                available_networks = self.wifi.scan_networks()
            
                if self.wifi.connect_to_any_internet(available_networks):
                    self.lcd.update_status("FLYSTO", "Syncing")
                    flysto = FlyStoClient(self.config['flysto_email'], self.config['flysto_password'])
                
                    for i, file_path in enumerate(pending):
                        progress = (i + 1) / len(pending)
                        self.lcd.update_status("UPLOADING", file_path.name, progress)
                        if flysto.upload_log(file_path):
                            self.flysto_db.mark_done(file_path.name)
                
                    self.lcd.update_status("COMPLETE", f"Uploaded {len(pending)} files")
                    time.sleep(5)
                else:
                    self.lcd.update_status("ERROR", "No Internet found")
                    time.sleep(3)

        except Exception as e:
            print(f"Orchestrator Error: {e}")
            self.lcd.update_status("CRASH", str(e)[:40])
            time.sleep(5)
        
        finally:
            self.lcd.clear()
            self.is_running = False

    def get_uptime_str(self):
        """Reads system uptime and returns HH:MM:SS format."""
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            seconds = int(uptime_seconds % 60)
            
            # Formats as 01:25:09
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        except Exception:
            return "N/A"
                        
    def start(self, interval_seconds=60):
        """Main loop that runs forever."""
        print("Starting Sync Orchestrator...")
        last_sync = 0
        
        while True:
            current_time = time.time()
            
            # Run if interval passed OR if user pressed the button
            if (current_time - last_sync > interval_seconds) or self.manual_sync_requested:
                self.cycle_counter += 1
                self.run_sync_cycle()
                last_sync = time.time()
            
            # Idle status on LCD
            uptime = self.get_uptime_str()
            self.lcd.update_status("IDLE", f"Alive: {uptime}")
            time.sleep(10) # Low-power check frequency

# --- Application Entry Point ---
if __name__ == "__main__":
    orchestrator = SyncOrchestrator()
    # Runs a sync every 30 minutes, or instantly on button press
    orchestrator.start(interval_seconds=30)
