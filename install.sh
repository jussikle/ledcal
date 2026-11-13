#!/usr/bin/env bash
# ledcal installer for Raspberry Pi OS (Bookworm or newer).
# Run from the ledcal folder as your normal user:   bash install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"
MATRIX_SRC="$HOME/rpi-rgb-led-matrix"
cd "$DIR"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev git build-essential libjpeg-dev zlib1g-dev

say "Creating Python environment in $VENV"
# --system-site-packages lets us reuse an rgbmatrix you installed for an earlier project
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt cython setuptools wheel

if "$VENV/bin/python" -c "import rgbmatrix" 2>/dev/null; then
    say "LED matrix library already installed - reusing it"
else
    say "Building the LED matrix library (takes a few minutes)"
    [ -d "$MATRIX_SRC" ] || git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix "$MATRIX_SRC"
    (
        cd "$MATRIX_SRC"
        source "$VENV/bin/activate"
        if ! { make build-python PYTHON="$VENV/bin/python" && make install-python PYTHON="$VENV/bin/python"; }; then
            echo "make-based install failed, trying pip..."
            pip install .
        fi
    )
    "$VENV/bin/python" -c "import rgbmatrix" && echo "rgbmatrix OK"
fi

if lsmod | grep -q '^snd_bcm2835'; then
    say "The Pi's onboard sound conflicts with the LED matrix timing"
    read -r -p "Turn off onboard audio (recommended; needs a reboot)? [Y/n] " ans
    if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then
        echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf >/dev/null
        for f in /boot/firmware/config.txt /boot/config.txt; do
            [ -f "$f" ] && sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$f"
        done
        NEED_REBOOT=1
    else
        echo "OK - set disable_hardware_pulsing = true in config.toml instead."
    fi
fi

if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    chmod 600 config.toml
    say "Created config.toml - add your Apple ID and app-specific password to it"
fi

say "Installing the background service"
sed "s|__DIR__|$DIR|g" ledcal.service | sudo tee /etc/systemd/system/ledcal.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ledcal.service >/dev/null

cat <<EOF

All set. Next steps:
  1. nano config.toml                         (add your iCloud details)
  2. sudo $VENV/bin/python ledcal.py --demo   (quick test with fake data, Ctrl+C to stop)
  3. sudo systemctl start ledcal              (run for real; starts on every boot)
     journalctl -u ledcal -f                  (watch the log)
EOF
[ "${NEED_REBOOT:-0}" = 1 ] && echo -e "\nReboot first so the audio change takes effect:  sudo reboot"
exit 0
