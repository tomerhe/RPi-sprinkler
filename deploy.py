#!/usr/bin/env python3
"""
Deploy script: backs up existing files on the Pi, then uploads the new ones.
"""
import paramiko
import os
import time

HOST = "192.168.68.116"
USER = "pi"
PASS = "t0mer!ko"
REMOTE_DIR = "/var/www/html/cgi-bin"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = [
    "app.py",
    "sprinkler.config",
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

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

# Install Flask + pigpio only if not already installed
print("Checking/installing Flask and pigpio...")
_, chk, _ = ssh.exec_command("python3 -c 'import flask, pigpio' 2>&1")
chk.channel.recv_exit_status()
if chk.read().decode().strip():
    run("pip3 install flask pigpio --break-system-packages --quiet", timeout=120)
    print("  installed")
else:
    print("  already installed, skipping")

# Fix ownership
run(f"sudo chown -R pi:pi {REMOTE_DIR}")

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

