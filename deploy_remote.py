"""
Remote Deployment Script for Nifty Trading Engine
Deploys codebase to an Ubuntu 24.04 LTS EC2 Instance (Mumbai ap-south-1)
and configures it as an autonomous systemd service.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEPLOY_FILES = [
    "main.py",
    "risk_guard.py",
    "regime_filter.py",
    "execution_engine.py",
    "audit_logger.py",
    "requirements.txt",
    ".env",
    "nifty-trading-engine.service",
]

REMOTE_DIR = "/home/ubuntu/nifty-trading-engine"


def run_ssh_command(host: str, key_path: str, command: str) -> int:
    ssh_cmd = [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        f"ubuntu@{host}",
        command,
    ]
    print(f"[SSH EXEC] {' '.join(ssh_cmd[:5])} '{command}'")
    return subprocess.run(ssh_cmd).returncode


def run_scp(host: str, key_path: str, local_file: Path, remote_dest: str) -> int:
    scp_cmd = [
        "scp",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        str(local_file),
        f"ubuntu@{host}:{remote_dest}",
    ]
    print(f"[SCP] {local_file.name} -> ubuntu@{host}:{remote_dest}")
    return subprocess.run(scp_cmd).returncode


def deploy(host: str, key_path: str) -> None:
    print(f"\n=======================================================")
    print(f"Deploying Nifty Trading Engine to Ubuntu Host: {host}")
    print(f"=======================================================\n")

    if not Path(key_path).exists():
        print(f"Error: SSH private key '{key_path}' does not exist!")
        sys.exit(1)

    # 1. Create target directory
    print("[1/5] Creating remote project workspace...")
    run_ssh_command(host, key_path, f"mkdir -p {REMOTE_DIR}")

    # 2. Upload source files
    print("[2/5] Shipping application modules and configuration...")
    for filename in DEPLOY_FILES:
        filepath = Path(filename)
        if filepath.exists():
            code = run_scp(host, key_path, filepath, f"{REMOTE_DIR}/{filename}")
            if code != 0:
                print(f"Warning: Failed to copy {filename}")
        else:
            print(f"Skipping missing file: {filename}")

    # 3. Setup virtualenv and dependencies
    print("[3/5] Provisioning Python virtualenv and installing dependencies...")
    setup_cmd = (
        f"cd {REMOTE_DIR} && "
        f"sudo apt update -y && sudo apt install -y python3-venv python3-pip && "
        f"python3 -m venv venv && "
        f"venv/bin/pip install --upgrade pip && "
        f"venv/bin/pip install -r requirements.txt"
    )
    run_ssh_command(host, key_path, setup_cmd)

    # 4. Install and enable systemd service
    print("[4/5] Installing and starting systemd service...")
    systemd_cmd = (
        f"sudo cp {REMOTE_DIR}/nifty-trading-engine.service /etc/systemd/system/ && "
        f"sudo systemctl daemon-reload && "
        f"sudo systemctl enable nifty-trading-engine && "
        f"sudo systemctl restart nifty-trading-engine"
    )
    run_ssh_command(host, key_path, systemd_cmd)

    # 5. Check status
    print("[5/5] Checking service status...")
    run_ssh_command(host, key_path, "sudo systemctl status nifty-trading-engine --no-pager")

    print("\n Deployment complete! To follow live logs run:")
    print(f"  ssh -i {key_path} ubuntu@{host} 'journalctl -u nifty-trading-engine -f'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy Nifty Trading Engine to Ubuntu EC2")
    parser.add_argument("--host", required=True, help="Public IPv4 or DNS of the EC2 instance")
    parser.add_argument("--key", default="algo-key.pem", help="Path to EC2 SSH private key (.pem)")
    args = parser.parse_args()

    deploy(args.host, args.key)
