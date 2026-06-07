# Memory

_Last updated: 2026-06-06_

## Memory

- Always use PowerShell for scripts on Rodolfo's dev machine — never bash or sh. (added 2026-05-20)
- Before handing any script or command to Rodolfo, test it in the sandbox first. If it can't be tested (Pi-specific hardware, ARM64 binaries), say so explicitly before giving it to him. No untested code handed over. (added 2026-05-20)
- Do research and dry runs before suggesting commands — back-and-forth trial and error on the Pi is unacceptable. (added 2026-05-20)
- Pi access: SSH/VS Code Remote to `cmpe8803@192.168.68.61` (hostname `gamecentral`, also runs RetroPie). Repo lives at `~/Software_repos/stationmaster-pi-main`. On Windows use the IP, not `.local` (no Bonjour). (added 2026-06-06)
- After the Pi reboots, verify with `python3 tools/diagnose.py` — expect 9/9. It's self-healing on boot (NAS auto-mounts, ErsatzTV auto-restarts). (added 2026-06-06)
- NAS = WD My Cloud, MAC `00:14:ee:0a:be:6e`, currently `192.168.68.50` (drifted from `.53`). `nas-resolve.service` now finds it by MAC at boot, writes `nas-stationmaster` into `/etc/hosts`, and fstab mounts `//nas-stationmaster/Public` at `/mnt/nas` — so DHCP drift no longer breaks the mount. Router DHCP reservation still recommended as backstop. (added 2026-06-06)
- Stack: ErsatzTV (IPTV, port 8409) + Jellyfin (port 8096). diagnose.py gotcha: read Jellyfin tuner/EPG config from `GET /System/Configuration/livetv` (TunerHosts/ListingProviders), NOT `GET /LiveTv/TunerHosts` or `/LiveTv/ListingProviders` — those are POST/DELETE-only and return 405. (added 2026-06-06)
- GitHub remote: `https://github.com/one-repo-to-rule-them-all/stationmaster-pi.git`. The Pi has no git configured — deploy to it via `scp` from Windows, not `git pull`. (added 2026-06-06)
- Open item from 2026-05-23: `fix_etv_libraries.py` MediaSourceId patch is still uncommitted in the workspace — decide whether it goes in. (added 2026-06-06)
