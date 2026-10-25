# How to Upload and Deploy Docker Images via GitHub Container Registry (GHCR)

This guide walks through generating a GitHub Personal Access Token (PAT), pushing your built Docker image to the GitHub Container Registry (`ghcr.io`), pulling it securely onto a fresh machine (such as another Raspberry Pi), and troubleshooting common hardware audio or playback issues using your username: **`roushan-kmr`**.

---

## Part 1: Generating a GitHub Personal Access Token (PAT)

To push images to GitHub (or pull **private** images), you need a Personal Access Token:

1. Log in to your GitHub account on your web browser.
2. Go to **Settings** (click your profile icon in the upper right -> **Settings**).
3. Scroll down on the left sidebar and click **Developer settings**.
4. Select **Personal access tokens** -> **Tokens (classic)**.
5. Click **Generate new token** -> **Generate new token (classic)**.
6. Set a **Note** describing its purpose (e.g., `Raspberry Pi Docker Access`).
7. Set an Expiration period (e.g., `90 days` or `No expiration`).
8. Select the required scopes:
   * **`write:packages`** (Upload packages to GitHub Container Registry)
   * **`read:packages`** (Download packages from GitHub Container Registry)
   * **`delete:packages`** (Optional: allows deleting uploaded images)
9. Click **Generate token** at the bottom.
10. **Copy and safely store your token.** You won't be able to see it again!

---

## Part 2: Uploading (Pushing) the Docker Image from Source Machine

### 1. Log in to GitHub Container Registry
Run the following command in your terminal (replace `YOUR_GITHUB_PAT` with your token):
```bash
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u roushan-kmr --password-stdin
```

### 2. Tag Your Local Image
Tag your local image to point to GitHub's container registry:
```bash
docker tag ytm-docker-yt-music:latest ghcr.io/roushan-kmr/ytm-pi:latest
```

### 3. Push the Image to GitHub
Upload the image to `ghcr.io`:
```bash
docker push ghcr.io/roushan-kmr/ytm-pi:latest
```

---

## Part 3: Deploying on a Fresh Machine / Raspberry Pi

### Step 1: Install Docker & Docker Compose
On the target machine, run the official installation script:
```bash
# 1. Install Docker
curl -sSL https://get.docker.com | sh

# 2. Allow your user to run Docker commands without sudo
sudo usermod -aG docker $USER

# 3. Apply group changes immediately
newgrp docker
```

### Step 2: Log in to GHCR on the New Machine
If your package is **Private**, log in using your GitHub token:
```bash
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u roushan-kmr --password-stdin
```
*(If you made the package Public on GitHub, you can skip this step).*

### Step 3: Create the Project Directory and `docker-compose.yml`
1. Create a working folder on the target machine:
   ```bash
   mkdir -p ~/ytm-docker && cd ~/ytm-docker
   ```

2. Create a `docker-compose.yml` file with this exact content:
   ```yaml
   services:
     yt-music:
       image: ghcr.io/roushan-kmr/ytm-pi:latest
       container_name: ytm-pi
       restart: unless-stopped
       ports:
         - "8001:8001"
       volumes:
         - ./YTMusic:/app/YTMusic
       devices:
         - /dev/snd:/dev/snd
       group_add:
         - audio
       environment:
         - YTM_PORT=8001
         - YTM_DIR=/app/YTMusic
         - MPV_AUDIO_DEVICE=alsa/default
   ```

### Step 4: Launch the Container
Run Docker Compose to pull the image from GitHub and start the app:
```bash
docker compose up -d
```
### Step 4: For Machine already running older image, first download the latest image

```bash
docker pull ghcr.io/roushan-kmr/ytm-pi:latest
docker compose up -d
```

---

## Part 4: Troubleshooting & Hardware Audio Fixes

If you experience playback hangs, freezing during song switches, or no sound output on a new Raspberry Pi, use these troubleshooting steps:

### 1. Hardware HAT DAC Routing (e.g., JustBoom, HiFiBerry)
If your Pi uses an onboard HAT DAC instead of the standard headphone jack:
* Boot the app and open the web UI.
* Go to the **Audio Settings / Output Device** section from the queue/settings menu.
* Switch the output device from generic `alsa/default` to your specific hardware DAC (e.g., **JustBoom DAC**).
* Restart the container to lock in the hardware stream route:
  ```bash
  docker compose restart
  ```

### 2. Fixing Stuck `mpv` Processes
If changing tracks freezes because a previous audio stream process didn't close cleanly:
1. Check for hanging `mpv` instances inside the container:
   ```bash
   docker exec -it ytm-pi ps aux
   ```
2. Force-kill the hung process ID if necessary:
   ```bash
   docker exec -it ytm-pi kill -9 <PID_NUMBER>
   ```
3. Restart the container:
   ```bash
   docker compose restart
   ```

### 3. Updating `yt-dlp` Inside the Container
If YouTube signature changes cause playback to stall, update `yt-dlp` manually inside the running container:
```bash
docker exec -it ytm-pi pip install --no-cache-dir --upgrade yt-dlp
```
