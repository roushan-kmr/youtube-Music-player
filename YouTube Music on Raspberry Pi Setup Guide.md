# YouTube Music on Raspberry Pi: Setup Guide

*A phone-controlled YouTube Music player for the Pi (ytm.py): search, playlists, downloads and local songs, played through mpv.*

## What you get

- A phone page at `http://<pi-ip>:8001` to search songs, albums and playlists, or open any public playlist by link.
- Queue, favourites, saved playlists and Auto Play after a reboot.
- Downloads as Opus into `~/YTMusic/DownloadedSongs`, plus playback of local files.
- System volume control (same method as radio.py) with a peak limiter to avoid distortion.
- No Google login or cookies needed.

## Requirements

- A Raspberry Pi (built and tested on a Pi 5) running Raspberry Pi OS Bookworm or newer, desktop version, which uses PipeWire for audio.
- Internet access and working audio output (speakers, DAC, headphones or HDMI).
- SSH access, or a keyboard and screen, and the `ytm.py` file.

## 1. Update the system

```bash
sudo apt update && sudo apt full-upgrade -y
```

## 2. Install packages

```bash
sudo apt install -y mpv ffmpeg python3-pip nodejs
```

- **mpv** plays the audio.
- **ffmpeg** lets downloads become tagged `.opus` files. Without it they are saved as `.webm` (Opus inside), which still plays.
- **nodejs** is the JavaScript runtime yt-dlp may ask for. If yt-dlp still complains about a JavaScript runtime, install Deno instead.

## 3. Install the Python packages

```bash
pip install -U flask ytmusicapi yt-dlp --break-system-packages
echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc && source ~/.bashrc
yt-dlp --version
```

The last command should print a version number. If it says command not found, open a new terminal and try again.

## 4. Copy the app to the Pi

```bash
mkdir -p ~/YTMusic
```

From your PC, send the file (change `pi` and the address to match your Pi):

```bash
scp ytm.py pi@<pi-ip>:~/YTMusic/
```

Find the Pi's address by running `hostname -I` on the Pi.

## 5. Check audio and yt-dlp

```bash
wpctl status
wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 75%
mpv --no-video "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

If you hear the track, audio and yt-dlp both work. Press `q` to quit mpv. Keep the system volume around 70 to 80% so there is headroom and no distortion.

## 6. First run

```bash
cd ~/YTMusic && python3 ytm.py
```

On your phone (same Wi-Fi), open `http://<pi-ip>:8001`. Press `Ctrl+C` on the Pi to stop it.

## 7. Start automatically on boot

```bash
(crontab -l 2>/dev/null | grep -v ytm.py; echo "@reboot sleep 40 && python3 /home/pi/YTMusic/ytm.py > /home/pi/YTMusic/ytm.log 2>&1") | crontab -
```

- Change `/home/pi` if your username is different.
- The 40-second delay gives the audio service time to start.
- The `grep -v` removes any older ytm.py line, so running this twice will not create duplicates.
- Check with `crontab -l`, then reboot once to confirm the page comes up and plays.
- To start it now without rebooting: `nohup python3 ~/YTMusic/ytm.py > ~/YTMusic/ytm.log 2>&1 &`

## Optional settings

Set these in front of `python3` in the command or cron line, for example `YTM_NORMALIZE=1 python3 ...`.

| Variable | Purpose |
| --- | --- |
| `MPV_AUDIO_DEVICE` | Force an audio device, for example `pipewire`. List names with `mpv --audio-device=help`. |
| `YTM_NORMALIZE=1` | Even out loudness between tracks. |
| `YTM_PORT` | Use a different port than 8001. |
| `YTM_DIR` | Use a different data folder than `~/YTMusic`. |

## Using the app

- **Search:** songs, videos, albums or playlists. Tap a result to play it, and the rest of the list follows. **＋** adds to the queue, **♥** saves a favourite.
- **Playlists:** paste a public playlist or song link, then Play all, Shuffle, Save or **⬇ All** to download everything.
- **Local:** songs in `DownloadedSongs`, with filter, Play, Shuffle and delete. Download progress shows at the top.
- **Favs:** your favourite tracks.
- **Queue:** jump to any track and switch Auto Play on or off.
- **Player bar:** previous, play/pause, next, seek, and volume − / + with mute. Volume controls the Pi's system volume.
- **⬇** on any track downloads the best available audio, one song at a time.

## Files and folders

| Path | What it is |
| --- | --- |
| `~/YTMusic/ytm.py` | The app |
| `~/YTMusic/DownloadedSongs/` | Downloaded and local songs. You can copy your own music here too. |
| `~/YTMusic/favourites.json` | Favourite tracks |
| `~/YTMusic/saved_playlists.json` | Saved playlists |
| `~/YTMusic/state.json` | Last queue and Auto Play setting |
| `~/YTMusic/mpv.log` | mpv messages, useful when tracks skip |
| `~/YTMusic/ytm.log` | App messages |

## Maintenance

```bash
# update the tools (about once a week)
pip install -U yt-dlp ytmusicapi --break-system-packages

# restart the app
pkill -f ytm.py
nohup python3 ~/YTMusic/ytm.py > ~/YTMusic/ytm.log 2>&1 &

# look at recent mpv messages
tail -40 ~/YTMusic/mpv.log
```

Keeping yt-dlp and ytmusicapi current fixes most breakage, since YouTube changes often.

## Troubleshooting

| Problem | Likely cause | Fix |
| --- | --- | --- |
| Tracks skip instantly | No audio output. `mpv.log` shows `Host is down`. | Make sure the audio service is running (`wpctl status`), set `MPV_AUDIO_DEVICE`, and raise the cron delay to 60 seconds. |
| "Sign in to confirm you're not a bot" or HTTP 429 | YouTube is throttling your IP | Wait a few hours, update yt-dlp, try another connection such as a phone hotspot, and stay off VPNs. |
| Search shows an error | ytmusicapi is outdated | Update it with pip (see Maintenance). |
| Warning about a JavaScript runtime | No runtime installed | `sudo apt install nodejs`, or install Deno. |
| Page does not load | App not running, or different network | Run `pgrep -af ytm.py`, check `ytm.log`, and use the same Wi-Fi as the Pi. |
| A download fails | See the message on the Local tab | Update yt-dlp and check free space with `df -h`. |
| Downloads are `.webm` | ffmpeg is not installed | `sudo apt install ffmpeg`. Only new downloads change. |

## Good practice

- Run without cookies. They are only worth adding if you get persistent blocks, and then only from a throwaway Google account, never your main one. Exported cookies also expire quickly and can cause "page needs to be reloaded" errors.
- Keep the load light: one stream at a time, no bulk downloading, and tools kept up to date.
- yt-dlp and ytmusicapi are unofficial tools. Use this for personal listening at home.
- The page has no password. Use it on your home network only and do not expose port 8001 to the internet.
