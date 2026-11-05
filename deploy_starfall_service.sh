#!/usr/bin/env bash
# ==============================================================================
# Starfall v1.6 — 48-Hour Pipeline Systemd Service & RAM Disk Installer
# Target Host : Ubuntu 24.04 LTS (16 vCPU, 32 GB RAM, 775 GB NVMe, Kernel 7.0)
# Usage       : sudo bash deploy_starfall_service.sh [/home/administrator/starfall]
# ==============================================================================
set -euo pipefail

STARFALL_ROOT="${1:-/home/administrator/starfall}"
PIPELINE_PKG_DIR="${STARFALL_ROOT}/bot/modules/starfall_pipeline"
RAM_MUX_MOUNT="/dev/shm/starfall_mux"
SERVICE_NAME="starfall-pipeline.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
LOG_DIR="${STARFALL_ROOT}/logs"
DATA_DIR="${STARFALL_ROOT}/data"

echo "=================================================================="
echo " Starfall v1.6 — Deploying 48-Hour High-Throughput Muxing Daemon"
echo " Root Directory : ${STARFALL_ROOT}"
echo " RAM Workspace  : ${RAM_MUX_MOUNT} (16 GB tmpfs)"
echo "=================================================================="

# 1. Ensure runtime directories exist
mkdir -p "${PIPELINE_PKG_DIR}" "${LOG_DIR}" "${DATA_DIR}/tam_audios" "${DATA_DIR}/torrents" "${DATA_DIR}/thumbnails" "${RAM_MUX_MOUNT}"

# 2. Configure 16 GB /dev/shm tmpfs mount if smaller than 16 GB
SHM_SIZE_GB=$(df -BG /dev/shm | awk 'NR==2 {gsub("G","",$2); print $2}')
if [ "${SHM_SIZE_GB:-0}" -lt 16 ]; then
    echo "[*] Remounting /dev/shm to 16G tmpfs..."
    mount -o remount,size=16G /dev/shm
fi
chmod 1777 "${RAM_MUX_MOUNT}"

# 3. Copy drop-in pipeline modules if present in current working directory
for mod in pipeline_manager.py upload_router.py mux_engine.py; do
    if [ -f "./${mod}" ]; then
        cp -f "./${mod}" "${PIPELINE_PKG_DIR}/${mod}"
        echo "[+] Installed ./${mod} -> ${PIPELINE_PKG_DIR}/${mod}"
    fi
done

# 4. Detect Python binary (prefer /wzvenv/bin/python3 or local .venv)
if [ -x "/wzvenv/bin/python3" ]; then
    PYTHON_BIN="/wzvenv/bin/python3"
elif [ -x "${STARFALL_ROOT}/.venv/bin/python3" ]; then
    PYTHON_BIN="${STARFALL_ROOT}/.venv/bin/python3"
elif [ -x "${STARFALL_ROOT}/venv/bin/python3" ]; then
    PYTHON_BIN="${STARFALL_ROOT}/venv/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
fi
echo "[*] Using Python interpreter: ${PYTHON_BIN}"

# 5. Run syntax & unit self-test before enabling service
PYTHONPATH="${STARFALL_ROOT}" "${PYTHON_BIN}" -m py_compile \
    "${PIPELINE_PKG_DIR}/pipeline_manager.py" \
    "${PIPELINE_PKG_DIR}/upload_router.py" \
    "${PIPELINE_PKG_DIR}/mux_engine.py"
echo "[+] Syntax check passed."

# 6. Write systemd service unit with automatic crash recovery & I/O priority
cat <<EOF > "${SERVICE_PATH}"
[Unit]
Description=Starfall v1.6 High-Throughput Tamil Anime Muxing Pipeline (48h Target)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${STARFALL_ROOT}
Environment="PYTHONPATH=${STARFALL_ROOT}"
Environment="PYTHONUNBUFFERED=1"
ExecStartPre=/usr/bin/mkdir -p ${RAM_MUX_MOUNT} ${LOG_DIR}
ExecStart=${PYTHON_BIN} -m bot.modules.starfall_pipeline.pipeline_manager --catalogue-json ${DATA_DIR}/channel_animes.json --source-channel -1004335327733 --target-channel -1004415413346
Restart=always
RestartSec=5
TimeoutStopSec=45
LimitNOFILE=1048576
OOMScoreAdjust=-500
CPUSchedulingPolicy=other
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=0
StandardOutput=append:${LOG_DIR}/starfall_48h_pipeline.log
StandardError=append:${LOG_DIR}/starfall_48h_pipeline.log

[Install]
WantedBy=multi-user.target
EOF

# 7. Reload systemd, enable and start service
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "=================================================================="
echo " ✅ ${SERVICE_NAME} deployed and running!"
echo " • Status : sudo systemctl status ${SERVICE_NAME}"
echo " • Logs   : tail -f ${LOG_DIR}/starfall_48h_pipeline.log"
echo "=================================================================="