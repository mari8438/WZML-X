#!/usr/bin/env bash
# ==============================================================================
# Starfall-Go Debrid — Production VPS Kernel Hardening & 1-Click Bootstrap
# Target OS: Ubuntu 24.04 LTS / Debian 12 (2 vCPU, 4 GB RAM)
#
# Performs:
#   1. 4 GB NVMe Swapfile provisioning (/swapfile, mode 0600, /etc/fstab)
#   2. Sysctl kernel & TCP BBR optimization (/etc/sysctl.d/99-starfall.conf)
#   3. File descriptor ceiling elevation to 65535 (/etc/security/limits.conf)
#   4. Workspace directory initialization (./data, ./downloads)
# ==============================================================================
set -euo pipefail

SWAP_FILE="/swapfile"
SWAP_SIZE_MB=4096
SYSCTL_CONF="/etc/sysctl.d/99-starfall.conf"
LIMITS_CONF="/etc/security/limits.d/99-starfall.conf"

log_info()  { printf "\033[1;36m[STARFALL-SETUP]\033[0m %s\n" "$*"; }
log_ok()    { printf "\033[1;32m[STARFALL-OK]\033[0m    %s\n" "$*"; }
log_warn()  { printf "\033[1;33m[STARFALL-WARN]\033[0m  %s\n" "$*"; }
log_error() { printf "\033[1;31m[STARFALL-ERR]\033[0m   %s\n" "$*" >&2; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  log_error "This script must be run as root (e.g., sudo bash deploy/setup_vps.sh)"
  exit 1
fi

log_info "Starting Starfall-Go 2 vCPU / 4 GB VPS Kernel Hardening..."

# ------------------------------------------------------------------------------
# 1. Provision 4 GB NVMe Swapfile (Prevents Sudden OOM Kills)
# ------------------------------------------------------------------------------
if swapon --show | grep -q "${SWAP_FILE}"; then
  log_ok "Swapfile ${SWAP_FILE} is already active."
else
  if [[ ! -f "${SWAP_FILE}" ]]; then
    log_info "Creating ${SWAP_SIZE_MB} MiB NVMe swapfile at ${SWAP_FILE}..."
    if command -v fallocate >/dev/null 2>&1; then
      fallocate -l "${SWAP_SIZE_MB}M" "${SWAP_FILE}" || dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_SIZE_MB}" status=progress
    else
      dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_SIZE_MB}" status=progress
    fi
  fi

  chmod 600 "${SWAP_FILE}"
  mkswap "${SWAP_FILE}"
  swapon "${SWAP_FILE}"

  if ! grep -q "^${SWAP_FILE} " /etc/fstab; then
    echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
    log_ok "Persisted ${SWAP_FILE} in /etc/fstab."
  fi
  log_ok "4 GB NVMe swapfile activated."
fi

# ------------------------------------------------------------------------------
# 2. Apply Kernel Memory, VFS, & Network Tuning (/etc/sysctl.d/99-starfall.conf)
# ------------------------------------------------------------------------------
log_info "Writing kernel optimization profile to ${SYSCTL_CONF}..."
cat << 'SYSCTL_EOF' > "${SYSCTL_CONF}"
# Starfall-Go Debrid — 2 vCPU / 4 GB RAM VPS Kernel Tuning
# Keep physical RAM prioritized; avoid premature swap thrashing on NVMe
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.overcommit_memory = 1
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5

# System-wide file descriptor ceiling
fs.file-max = 100000

# High-throughput TCP socket backlog & buffer bounds (capped for 150 P2P conns)
net.core.somaxconn = 2048
net.ipv4.tcp_max_syn_backlog = 2048
net.core.netdev_max_backlog = 4096
net.ipv4.ip_local_port_range = 10240 65535
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
SYSCTL_EOF

# Enable BBR congestion control if supported by the running Linux kernel
if sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -q "bbr"; then
  cat << 'BBR_EOF' >> "${SYSCTL_CONF}"
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
BBR_EOF
fi

sysctl --system >/dev/null
log_ok "Kernel sysctl parameters applied (vm.swappiness=10, fs.file-max=100000)."

# ------------------------------------------------------------------------------
# 3. Elevate User & Process File Descriptor Limits to 65535
# ------------------------------------------------------------------------------
log_info "Configuring file descriptor limits (65535) in /etc/security/limits.conf..."
cat << 'LIMITS_EOF' > "${LIMITS_CONF}"
# Starfall-Go Debrid — Prevent socket exhaustion during 1Gbps MTProto uploads
*               soft    nofile          65535
*               hard    nofile          65535
root            soft    nofile          65535
root            hard    nofile          65535
LIMITS_EOF

if ! grep -q "65535" /etc/security/limits.conf 2>/dev/null; then
  cat << 'LIMITS_APPEND_EOF' >> /etc/security/limits.conf
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535
LIMITS_APPEND_EOF
fi
log_ok "File descriptor ceiling set to 65535."

# ------------------------------------------------------------------------------
# 4. Prepare Local Persistent Directories
# ------------------------------------------------------------------------------
mkdir -p ./data ./downloads ./downloads/thumbnails
chown -R 10001:10001 ./data ./downloads 2>/dev/null || true

log_ok "VPS hardening complete! Summary:"
free -h || true
echo "----------------------------------------------------------------------"
echo "Next Step: Run 'docker compose up -d --build' or 'bash scripts/verify.sh'"
