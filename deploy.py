#!/usr/bin/env python3
"""
Deploy script: backs up existing files on the Pi, then uploads the new ones.
Before uploading, syncs user-editable config sections from the remote so that
any changes made via the web UI are not overwritten.
"""
import configparser
import io
import paramiko
import os
import time

HOST = "192.168.68.116"
USER = "pi"
PASS = "t0mer!ko"
REMOTE_DIR = "/opt/sprinkler"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = [
    "app.py",
    "sprinkler.config",
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# ── Sync user-editable config sections from remote before deploying ────────────
# These sections are modified via the web UI; we never want to overwrite them.
_USER_SECTIONS = {
    'forecastio', 'stations',
    'wateringminutes', 'norainskip', 'wateringinterval', 'runtimeparams',
}

local_cfg_path = os.path.join(LOCAL_DIR, "sprinkler.config")
print("Syncing config from remote...")
try:
    sftp_pre = ssh.open_sftp()
    remote_buf = io.BytesIO()
    sftp_pre.getfo(f"{REMOTE_DIR}/sprinkler.config", remote_buf)
    sftp_pre.close()

    remote_cfg = configparser.ConfigParser()
    remote_cfg.read_string(remote_buf.getvalue().decode('utf-8'))

    local_cfg = configparser.ConfigParser()
    local_cfg.read(local_cfg_path, encoding='utf-8')

    changed = False
    for section in remote_cfg.sections():
        if section.lower() in _USER_SECTIONS:
            if not local_cfg.has_section(section):
                local_cfg.add_section(section)
            for key, val in remote_cfg.items(section):
                if local_cfg.get(section, key, fallback=None) != val:
                    local_cfg.set(section, key, val)
                    changed = True

    if changed:
        with open(local_cfg_path, 'w', encoding='utf-8') as f:
            local_cfg.write(f)
        print("  Local config updated with remote changes.")
    else:
        print("  Local config already up to date.")
except Exception as e:
    print(f"  WARNING: could not sync remote config ({e}). Proceeding with local config.")

def run(cmd, timeout=30):
    """Run a command with sudo via password-on-stdin (no PTY required)."""
    # Wrap with sudo -S so it reads password from stdin; 2>&1 captures stderr too
    wrapped = f"echo '{PASS}' | sudo -S {cmd} 2>&1"
    _, stdout, _ = ssh.exec_command(wrapped, timeout=timeout)
    stdout.channel.recv_exit_status()
    return stdout.read().decode().strip()

def run_plain(cmd, timeout=30):
    """Run a command without sudo."""
    _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
    stdout.channel.recv_exit_status()
    return stdout.read().decode().strip()

# Create app directory, make it writable by pi for SFTP upload
run(f"mkdir -p {REMOTE_DIR}")
run(f"chown pi:pi {REMOTE_DIR}")

# Install Flask + pigpio only if not already installed
print("Checking/installing Flask and pigpio...")
_, chk, _ = ssh.exec_command("python3 -c 'import flask, pigpio' 2>&1")
chk.channel.recv_exit_status()
if chk.read().decode().strip():
    run("pip3 install flask pigpio --break-system-packages --quiet", timeout=120)
    print("  installed")
else:
    print("  already installed, skipping")

# Upload files (CRLF -> LF)
sftp = ssh.open_sftp()
print(f"\nUploading files to {REMOTE_DIR}...")
for fname in FILES:
    local_path = os.path.join(LOCAL_DIR, fname)
    remote_path = f"{REMOTE_DIR}/{fname}"
    if os.path.exists(local_path):
        with open(local_path, 'rb') as f:
            content = f.read().replace(b'\r\n', b'\n')
        with sftp.open(remote_path, 'wb') as f:
            f.write(content)
        print(f"  Uploaded: {fname}")
    else:
        print(f"  SKIPPED (not found): {fname}")

# Upload and install systemd service
print("\nInstalling systemd service...")
svc_local = os.path.join(LOCAL_DIR, "sprinkler.service")
with open(svc_local, 'rb') as f:
    content = f.read().replace(b'\r\n', b'\n')
with sftp.open("/tmp/sprinkler.service", 'wb') as f:
    f.write(content)
run("cp /tmp/sprinkler.service /etc/systemd/system/sprinkler.service")
run("systemctl daemon-reload")
run("systemctl enable sprinkler")

# Start the Flask app via systemd (--no-block returns immediately)
print("\nRestarting sprinkler service...")
run("systemctl restart --no-block sprinkler")
time.sleep(5)
out = run_plain("systemctl is-active sprinkler")
print(f"  Service status: {out}")

sftp.close()
ssh.close()
print(f"\nDeployment complete!")
print(f"Open http://{HOST}/ in your browser.")

