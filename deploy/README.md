# Sherlock — NUC Deployment Guide

## Hardware Requirements
- Intel NUC (any generation, x86_64)
- 32 GB RAM (confirmed: GEEKOM A8 Max config)
- 500 GB SSD minimum (NVMe preferred)
- Gigabit Ethernet (wired, not WiFi)
- USB port for installation

---

## Step 1 — Prepare the Ventoy USB Drive

You already have Ventoy on the USB. Do the following on your Mac:

### 1a. Download Ubuntu Server 24.04 LTS
Download from: `https://ubuntu.com/download/server`
File: `ubuntu-24.04.4-live-server-amd64.iso`

### 1b. Copy ISO to Ventoy USB
Open Finder → locate your Ventoy USB drive → drag the `.iso` into the root of the drive.
That's it — Ventoy handles the rest.

### 1c. Copy the Autoinstall files (optional — for automated install)
If you want Ubuntu to install without prompts, create this folder structure on the Ventoy USB:

```
[Ventoy USB]/
  ventoy/
    ventoy.json            ← Ventoy config (see below)
  ubuntu-24.04-...-amd64.iso
  sherlock-autoinstall/
    user-data              ← copy from deploy/autoinstall/user-data
    meta-data              ← copy from deploy/autoinstall/meta-data
```

Add to `ventoy/ventoy.json`:
```json
{
  "auto_install": [
    {
      "image": "/ubuntu-24.04.x-live-server-amd64.iso",
      "template": ["/sherlock-autoinstall/user-data"]
    }
  ]
}
```

**Autoinstall defaults:**
- Username: `sherlock`
- Password: `Sherlock2024!`  ← **CHANGE THIS AFTER FIRST LOGIN**
- Hostname: `sherlock`
- Disk: Largest available drive, LVM

If you skip autoinstall, just do the Ubuntu Server manual install and create a user called `sherlock`.

---

## Step 2 — Install Ubuntu on the NUC

1. Plug USB into NUC
2. Power on → press **F10** (or F2/Del depending on NUC model) for boot menu
3. Select your Ventoy USB
4. Select the Ubuntu ISO
5. Choose **"Try or Install Ubuntu Server"**
6. If using autoinstall: Ubuntu installs automatically and reboots
7. If manual: Follow the installer — accept defaults, create user `sherlock`

After Ubuntu installs and the NUC reboots, remove the USB drive.

---

## Step 3 — Connect and Run the Sherlock Installer

SSH into the NUC from your Mac (get the IP from your router, or check the NUC screen):

```bash
ssh sherlock@<NUC-IP-ADDRESS>
# Password: Sherlock2024! (or whatever you set)
```

Then run the installer — it handles everything automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/Tnijem/SherlockAi/main/deploy/sherlock-install.sh | bash
```

**Or** if you've copied the repo to the USB:
```bash
chmod +x ~/Sherlock/deploy/sherlock-install.sh
~/Sherlock/deploy/sherlock-install.sh
```

The installer will:
- Install Docker CE, Ollama, Python, Nginx, Tesseract, LibreOffice, ffmpeg
- Clone the Sherlock repo from GitHub
- Set up the Python virtual environment
- Configure systemd services (auto-start on boot)
- Configure Nginx with self-signed TLS
- Set up the firewall
- Pull the AI models (gemma3:4b + mxbai-embed-large) — **~4 GB download**
- Walk you through creating the admin account
- Start everything up

**Total install time: ~15-20 minutes** (most of that is model download)

---

## Step 4 — Access Sherlock

Once installed, open a browser on any computer on the LAN:

```
https://<NUC-IP-ADDRESS>
```

Your browser will warn about the self-signed certificate — click **Advanced → Proceed**.

You can also add `sherlock` to your `/etc/hosts` or router DNS:
```
192.168.x.x    sherlock sherlock.local
```
Then access via `https://sherlock.local`

---

## Post-Install — Add Case Documents

To point Sherlock at your NAS document folders, either:

**Option A — Setup Wizard** (recommended for first run):
Visit `https://<NUC-IP>/` → it will run the wizard if no admin exists yet.

**Option B — Edit sherlock.conf**:
```bash
nano ~/Sherlock/sherlock.conf
# Add your NAS mount path:
NAS_PATHS=/mnt/nas/cases,/mnt/nas/archive
```
Then restart: `sudo systemctl restart sherlock-web`

**Mount NAS shares** (add to `/etc/fstab` for auto-mount):
```
//nas-server/cases  /mnt/nas/cases  cifs  credentials=/etc/samba/sherlock-creds,ro,uid=sherlock  0  0
```

---

## Service Management

```bash
# Status
sudo systemctl status sherlock-web
sudo systemctl status sherlock-docker
sudo journalctl -u sherlock-web -f        # live logs

# Restart
sudo systemctl restart sherlock-web
sudo systemctl restart sherlock-docker

# Update Sherlock to latest version
~/Sherlock/deploy/sherlock-update.sh

# Reindex all documents
cd ~/Sherlock/web && ~/Sherlock/venv/bin/python run_indexer.py

# Add/reset users
cd ~/Sherlock/web && ~/Sherlock/venv/bin/python create_admin.py
```

---

## NUC BIOS Settings (recommended)

Enter BIOS with **F2** at boot:
- **Boot → Boot Priority**: Set USB first, then NVMe (change back after install)
- **Power → Secondary Power Settings → After Power Failure**: Set to **"Power On"** — NUC auto-restarts after a power outage
- **Security → Secure Boot**: Disable (required for some Linux drivers)
- **Boot → Fast Boot**: Disable (avoids USB boot issues)

---

## Firewall Reference

Ports open by default after install:

| Port | Purpose | Open To |
|------|---------|---------|
| 22 | SSH | LAN |
| 80 | HTTP (redirects to HTTPS) | LAN |
| 443 | Sherlock HTTPS | LAN |

Internal ports (3000, 8000, 8888, 11434) are blocked externally — only accessible from localhost.

---

## Troubleshooting

**Web app not starting:**
```bash
sudo journalctl -u sherlock-web -n 50
```

**ChromaDB/SearXNG not running:**
```bash
cd ~/Sherlock && docker compose ps
docker compose logs chroma
```

**Ollama not responding:**
```bash
sudo systemctl status ollama
sudo journalctl -u ollama -n 30
ollama list    # check models are present
```

**Model missing:**
```bash
ollama pull gemma3:4b
ollama pull mxbai-embed-large
```
