# stationmaster-pi

> WD MyCloud (or any SMB NAS) → Jellyfin → ErsatzTV → 100+ live IP channels in Jellyfin's Live TV.
> One Raspberry Pi 5. One command. ~30 minutes from a freshly flashed Pi.

This is the Raspberry Pi port of [stationmaster](https://github.com/one-repo-to-rule-them-all/stationmaster) (the Windows version). It installs the same Jellyfin + ErsatzTV stack but uses `apt`, `systemd`, and a CIFS NAS mount instead of `winget`, Windows Services, and mapped drives.

---

## What you get

- **Jellyfin** installed, configured, with admin + media-only users, generated passwords saved to `.env`
- **ErsatzTV Legacy** (linux-arm64) installed as a systemd service, libraries pointing at your NAS
- **Live TV in Jellyfin** with EPG guide, M3U tuner, XMLTV guide provider
- **Two movie libraries**: Kids Movies + Movies (Adult), separate for future parental controls
- **Auto-generated channels**: one per TV show (10+ episodes), one per movie franchise (2+ films), plus category channels (Fitness, Stand Up Comedy, etc.)
- **Self-healing boot**: NAS remounts on startup, ErsatzTV auto-restarts on crash, everything survives a reboot
- **`tools/diagnose.py`**: one-shot health checker that shows exactly what's broken and how to fix it

---

## Hardware requirements

| Component | Minimum | Recommended |
|---|---|---|
| Pi model | Raspberry Pi 4 (4GB) | **Pi 5 (8GB or 16GB)** |
| Storage | 32GB SD card | **NVMe SSD via HAT** or USB SSD |
| Cooling | Passive heatsink | **Active cooling** (Pi 5 official fan case) |
| Power | Official Pi 4 15W adapter | **Official Pi 5 27W USB-C adapter** |
| Network | Wi-Fi | **Ethernet** (SMB over Wi-Fi can buffer) |

Your Pi 5 16GB / 256GB SSD spec is well above what stationmaster needs. You have headroom for other services (Pi-hole, Home Assistant, etc.) alongside it.

---

## Quick start (Pi already set up)

If your Pi is already running Raspberry Pi OS 64-bit, SSH is working, and you're logged in:

```bash
# 1. Clone
git clone https://github.com/your-org/stationmaster-pi.git
cd stationmaster-pi

# 2. Copy and edit config
cp .env.example .env
nano .env   # set your NAS UNC path; everything else has sensible defaults

# 3. Install Python deps
pip3 install -r requirements.txt --break-system-packages

# 4. Run the installer
python3 bootstrap.py
```

That's it. ~30 minutes, mostly waiting for the NAS scan. When it finishes, open `http://localhost:8096` and Live TV is ready.

---

## Full setup guide (starting from an unboxed Pi)

Follow this section if you're starting from scratch — Pi hardware in hand, nothing installed yet.

---

### Step 0 — Flash the OS

**You need: Raspberry Pi OS Lite (64-bit). The 64-bit image is required.**

1. Download **Raspberry Pi Imager** from https://www.raspberrypi.com/software/ and install it on your laptop/desktop.

2. Open Imager and select:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)** — *not the Desktop version*
   - **Storage:** your SD card or SSD

3. Click the **gear icon (⚙)** or "Edit Settings" before writing. Configure:
   - **Hostname:** `stationmaster` (you'll SSH in as `stationmaster.local`)
   - **Username:** `cmpe8803`
   - **Password:** choose a strong password
   - **Enable SSH:** yes, use password authentication
   - **Locale:** set your timezone and keyboard layout

4. Write the image. When done, insert the storage into the Pi and power it on.

> **NVMe users:** If you're booting from NVMe via a HAT, flash to the NVMe drive using a USB-to-NVMe adapter on your laptop, or flash to SD first, boot, then use `rpi-clone` to migrate.

---

### Step 1 — First boot and SSH

Give the Pi about 60 seconds to boot on first power-on.

Find the Pi on your network. Try:
```bash
ping stationmaster.local
```

If that doesn't resolve, check your router's DHCP client list for the Pi's IP address.

SSH in:
```bash
ssh cmpe8803@stationmaster.local
# or: ssh cmpe8803@<pi-ip-address>
```

Verify you're on 64-bit ARM:
```bash
uname -m
# Should output: aarch64
```

Run OS updates (takes a few minutes):
```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

SSH back in after the reboot.

---

### Step 2 — Assign a static IP

Your Pi needs a stable IP address so your TV and phone clients can always reach Jellyfin.

**In your router's admin UI:**
1. Find the DHCP client list and locate the Pi by its hostname (`stationmaster`) or MAC address.
2. Assign a DHCP reservation — e.g. `192.168.1.50`.
3. Save and optionally reboot the router.

From now on the Pi will always get the same IP.

> **Why not set a static IP on the Pi itself?** Router-side DHCP reservation is cleaner — it survives OS reinstalls and you only configure it in one place.

Verify NAS reachability from the Pi before running the installer:
```bash
sudo apt install -y smbclient
smbclient -L //WDMYCLOUD -N
# Should list shares including "Public"
```

If the hostname doesn't resolve, find the NAS IP from your router's DHCP list and use that IP in `.env`'s `NAS_UNC_PATH` instead (e.g. `//192.168.1.10/Public`).

---

### Step 3 — Clone and configure

```bash
# Install git if not present
sudo apt install -y git python3-pip

# Clone the repo
git clone https://github.com/your-org/stationmaster-pi.git
cd stationmaster-pi

# Install Python deps
pip3 install -r requirements.txt --break-system-packages

# Create your config file
cp .env.example .env
nano .env
```

**Required edits in `.env`:**

| Variable | What to set |
|---|---|
| `NAS_UNC_PATH` | UNC path to your share root, e.g. `//WDMYCLOUD/Public` |
| `NAS_USER` | NAS user with read access (e.g. `media`) |
| `NAS_PASS` | That user's password |
| `JELLYFIN_ADMIN_USER` | Admin username for Jellyfin (default: `cmpe8803`) |
| `TZ` | Your timezone, e.g. `America/Chicago` |

Leave `JELLYFIN_ADMIN_PASS` blank — the installer generates a strong password and saves it back to `.env`.

Everything else has sensible defaults for the `//WDMYCLOUD/Public/Videos/` layout.

---

### Step 4 — Run the installer

```bash
python3 bootstrap.py
```

What happens, in order:

| Phase | What | Time |
|---|---|---|
| 0 | Preflight checks — architecture, .env, NAS connectivity | ~5s |
| 1 | `apt` packages: cifs-utils, ffmpeg, ufw, etc. Firewall rules. | ~2 min |
| 2 | NAS mount: credentials file, fstab entry, mount now | ~10s |
| 3 | Jellyfin install from official apt repo, systemd enable | ~2–5 min |
| 4 | Jellyfin first-run wizard (admin user, generates password) | ~10s |
| 5 | Add Jellyfin libraries (Kids Movies, Movies, TV Shows, Fitness, Stand Up) | ~10s |
| 6 | Create view-only `media` user for TV/phone clients | ~5s |
| 7 | Download ErsatzTV linux-arm64 from GitHub releases, install to `/opt/ersatztv/` | ~2–5 min |
| 8 | ErsatzTV first run — waits for SQLite DB to be created | ~30s |
| 9 | Inject library paths into ErsatzTV DB | ~5s |
| 10 | Wait for ErsatzTV media scan to stabilise | **5–30 min** (library dependent) |
| 11 | Build channels via `full_setup.py` | ~30s |
| 12 | Register M3U tuner + XMLTV guide in Jellyfin | ~10s |
| 13 | Install systemd services (ErsatzTV + startup_sync) | ~10s |

> **Phase 10 is the long one.** ErsatzTV scans your NAS and indexes all media. For a large library (~3,500 items) this is 5–15 minutes. For smaller libraries it's faster. The installer tells you the current MediaItem count and waits for it to stabilise before proceeding.

If you need to stop and resume:
```bash
# Resume from library config onward (skips apt, NAS, Jellyfin install, wizard)
python3 bootstrap.py --resume
```

---

### Step 5 — Add the XMLTV guide in Jellyfin (30 seconds, manual)

The XMLTV guide POST sometimes returns 404 via the API on certain Jellyfin builds. If Live TV channels appear but the guide is empty, add it through the UI:

1. Open `http://localhost:8096` (or `http://<pi-ip>:8096` from another device)
2. Log in as `cmpe8803` with the password from `.env`
3. **Dashboard → Live TV → TV Guide Data Providers** → click `+` → **XMLTV**
4. **File or URL:** `http://localhost:8409/iptv/xmltv.xml`
5. Check **Enable for all tuner devices**
6. **Save**, then **Refresh Guide**

Within 60s, **Live TV → Guide** should show all channels with real program info.

---

### Step 6 — Verify it works

```bash
python3 tools/diagnose.py
```

Expected output (all green):
```
stationmaster-pi diagnose
  ETV : http://localhost:8409
  JF  : http://localhost:8096

  [+] ENV     .env loaded
  [+] NAS     /mnt/nas mounted, accessible
  [+] FFMPEG  ffmpeg present
  [+] ETV     process running (systemd: active)
  [+] ETVAPI  API up, N channels in M3U
  [+] ETVDB   healthy: 5 libraries, N media items, N channels
  [+] JFAPI   Jellyfin up
  [+] JFCFG   tuner registered
  [+] JFEPG   guide provider registered
  [+] STREAM  streaming at ~Nxx KB/s

Summary:  N ok  0 warn  0 fail
```

**From a browser on your laptop/phone:**
- Jellyfin: `http://<pi-ip>:8096` — Live TV → Guide → click a channel → it plays
- Jellyfin app on phone/TV: Add Server → `http://<pi-ip>:8096` → log in as `media` / `media123`

---

## Day-2 operations

| Situation | Command |
|---|---|
| Added new media to the NAS | `python3 tools/full_cleanup.py --apply` |
| Something is broken | `python3 tools/diagnose.py` |
| Everything is broken | `python3 tools/factory_reset.py --yes --jf-user cmpe8803 --jf-pass <pass>` |
| Channels disappeared from JF | `python3 tools/jf_cleanup_dups.py --apply --user cmpe8803 --password <pass>` |
| Restart ErsatzTV | `sudo systemctl restart ersatztv` |
| View ErsatzTV live log | `journalctl -u ersatztv -f` |
| View startup sync log | `journalctl -u stationmaster-sync` |
| Reboot the Pi | `sudo reboot` (everything restarts automatically) |

---

## Configuration reference

All settings live in `.env`. Key variables:

| Variable | Default | Notes |
|---|---|---|
| `NAS_UNC_PATH` | `//WDMYCLOUD/Public` | SMB share root |
| `NAS_MOUNT_POINT` | `/mnt/nas` | Linux mount point |
| `NAS_USER` | `media` | NAS credentials user |
| `NAS_PASS` | `M3dia123!` | NAS credentials password |
| `NAS_MOVIES_KIDS_DIR` | `Videos/Movies/Kids` | Kids movies subdir under mount |
| `NAS_MOVIES_ADULT_DIR` | `Videos/Movies/Adult` | Adult movies subdir under mount |
| `NAS_SHOWS_DIR` | `Videos/TV Shows` | TV shows subdir |
| `NAS_FITNESS_DIR` | `Videos/Fitness` | Fitness videos subdir |
| `NAS_STANDUP_DIR` | `Videos/Stand Up Comedy` | Stand-up comedy subdir |
| `JELLYFIN_ADMIN_USER` | `cmpe8803` | Jellyfin admin username |
| `JELLYFIN_ADMIN_PASS` | *(generated)* | Written by bootstrap; don't set manually |
| `JELLYFIN_MEDIA_USER` | `media` | View-only TV client user |
| `JELLYFIN_MEDIA_PASS` | `media123` | Easy to type on a TV remote |
| `ETV_HOST` | `http://localhost:8409` | ErsatzTV base URL |
| `ETV_EXE_PATH` | `/opt/ersatztv/ErsatzTV` | Path to ErsatzTV binary |
| `TZ` | `America/Chicago` | Timezone for EPG |
| `AUTO_SHOWS_MIN` | *(unset)* | Per-show channels for shows with N+ episodes |
| `AUTO_MOVIES_MIN` | *(unset)* | Per-franchise channels for franchises with N+ films |

---

## Re-running and partial flows

```bash
python3 bootstrap.py --skip-apt        # packages already installed
python3 bootstrap.py --skip-nas        # NAS already mounted
python3 bootstrap.py --skip-jellyfin   # Jellyfin already installed
python3 bootstrap.py --skip-wizard     # Jellyfin already initialized
python3 bootstrap.py --skip-libs       # libraries already configured
python3 bootstrap.py --skip-etv        # ErsatzTV already installed
python3 bootstrap.py --skip-channels   # channels already built
python3 bootstrap.py --skip-services   # systemd already set up
python3 bootstrap.py --resume          # jump to library config (skips everything before)
```

---

## Troubleshooting

**Always start with:**
```bash
python3 tools/diagnose.py
```

### `[X] NAS  /mnt/nas not mounted`

```bash
sudo mount /mnt/nas
# If that fails:
sudo umount /mnt/nas 2>/dev/null; sudo mount /mnt/nas
```

Check the NAS is powered on: `ping wdmycloud` or `ping <nas-ip>`.

### `[X] ETV  process not running`

```bash
sudo systemctl start ersatztv
journalctl -u ersatztv -n 50   # check for errors
```

### `[X] ETVDB  no libraries configured`

```bash
python3 tools/factory_reset.py --yes --jf-user cmpe8803 --jf-pass <password-from-env>
```

### `[!] JFCFG  duplicate ETV tuners`

```bash
python3 tools/jf_cleanup_dups.py --apply --user cmpe8803 --password <password-from-env>
```

### `[X] STREAM  only N bytes in 5s`

ErsatzTV is producing no output. Most often a bad source file or stale playout state:
```bash
python3 tools/fix_playout_crash.py
sudo systemctl restart ersatztv
```

### Live TV guide shows wrong programs

Stale Jellyfin channel cache. Full reset:
```bash
python3 tools/factory_reset.py --yes --jf-user cmpe8803 --jf-pass <password-from-env>
```

### Pi can't reach Jellyfin from phone/TV

Check firewall:
```bash
sudo ufw status
# Should show: 8096/tcp ALLOW, 8409/tcp ALLOW
```

If rules are missing:
```bash
sudo ufw allow 8096/tcp
sudo ufw allow 8409/tcp
```

### ErsatzTV didn't start after reboot

Check systemd:
```bash
systemctl status ersatztv
journalctl -u ersatztv -n 50
```

If the NAS wasn't mounted when ErsatzTV started, the media paths resolve as empty. Fix:
```bash
sudo mount /mnt/nas && sudo systemctl restart ersatztv
```

---

## How channels and the EPG work

*(Same as the Windows version — platform-agnostic.)*

### Data flow inside ErsatzTV

```
Channel #N "Animaniacs"
   └── ProgramSchedule  (in-order OR shuffle)
         └── Collection "Animaniacs Collection"
               └── CollectionItems → MediaItems (the episodes)
   └── Playout (concrete timeline: episode X plays 9:00–9:30pm, etc.)
         └── PlayoutItems (one row per scheduled slot)
```

`full_setup.py` writes the Channel, ProgramSchedule, and Collection. ErsatzTV's background service computes the Playout — a concrete sequence of `(start_time, end_time, media_item_id)` rows.

- **TV shows (`ORDER_IN_ORDER`):** walks episodes by `(Season, Episode)` ascending. Loops.
- **Generic channels (`ORDER_SHUFFLE`):** random pick from the Collection.

### How that reaches Jellyfin

ErsatzTV exposes two HTTP endpoints:

| Endpoint | Drives | Contents |
|---|---|---|
| `/iptv/channels.m3u` | JF channel list | One `#EXTINF` line per channel + stream URL |
| `/iptv/xmltv.xml` | JF EPG guide | `<channel>` + `<programme>` entries |

The channel `id` ties them together. As long as the `tvg-id` in the M3U matches the `<channel id>` in the XMLTV, the guide is correct.

---

## Repo layout

```
stationmaster-pi/
├── bootstrap.py              # 13-phase one-command installer (Linux/Pi)
├── full_setup.py             # ErsatzTV channel builder + JF wire-up
├── tools/
│   ├── diagnose.py           # health check — START HERE when broken
│   ├── factory_reset.py      # clean-slate rebuild
│   ├── full_cleanup.py       # 7-stage maintenance pipeline
│   ├── jf_cleanup_dups.py    # prune duplicate JF tuners/providers
│   ├── fix_playout_crash.py  # recover from UNIQUE constraint crash loops
│   ├── startup_sync.sh       # self-healer (runs via systemd on boot)
│   ├── install_services.sh   # systemd service installer
│   ├── reorg_fitness.py      # bin fitness videos by program
│   ├── reorg_standup.py      # bin standup videos by comedian
│   ├── reorg_movies.py       # bin movies into <bin>/<Title>/<file>
│   └── ...                   # (other tools from Windows version, ported)
├── systemd/
│   └── ersatztv.service      # ErsatzTV systemd unit
├── .env.example
├── requirements.txt
└── README.md
```

---

## Tested versions

| Component | Version |
|---|---|
| ErsatzTV Legacy | v26.5.1 linux-arm64 |
| Jellyfin | Latest from official apt repo |
| Python | 3.10+ (tested 3.11) |
| Raspberry Pi OS | Lite 64-bit (Debian Bookworm) |
| Hardware | Raspberry Pi 5 |

---

## License

MIT.
