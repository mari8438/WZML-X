import os
import sys

# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIO_VAULT_DIR = os.path.join(DATA_DIR, "tam_audios")
RAM_MUX_DIR = "/dev/shm/starfall_mux" if os.path.exists("/dev/shm") else os.path.join(DATA_DIR, "tmp_mux")
DB_PATH = os.path.join(DATA_DIR, "starfall_state.db")

# Target Telegram channel for Tamil audio harvesting
DEFAULT_TAMIL_CHANNEL_ID = -1004335327733

# Disk quota governance (GB)
MAX_NVME_PIPELINE_QUOTA_GB = 75.0
SAFETY_BUFFER_GB = 10.0

# Concurrency budgets
HARVESTER_MAX_WORKERS = 5
ACOUSTIC_SYNC_MAX_WORKERS = 6
MUX_MAX_CONCURRENT = 4

# Acoustic delay parameters (4-Anchor GCC-PHAT)
SAMPLE_RATE = 8000
WINDOW_SEC = 75.0
MAX_LAG_SEC = 25.0
ANCHOR_POSITIONS = (0.20, 0.40, 0.60, 0.80)
MIN_CONFIDENCE_PSR = 10.0

# Telegram split limit (MiB)
TG_SPLIT_THRESHOLD_MIB = 1980
TG_MAX_USER_LIMIT_MIB = 3950

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_VAULT_DIR, exist_ok=True)
os.makedirs(RAM_MUX_DIR, exist_ok=True)

# Credentials from root config or environment
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    import config as root_cfg
    TELEGRAM_API = getattr(root_cfg, "TELEGRAM_API", None)
    TELEGRAM_HASH = getattr(root_cfg, "TELEGRAM_HASH", None)
    BOT_TOKEN = getattr(root_cfg, "BOT_TOKEN", None)
    USER_SESSION_STRING = getattr(root_cfg, "USER_SESSION_STRING", None)
    HELPER_TOKENS = getattr(root_cfg, "HELPER_TOKENS", "")
except Exception:
    TELEGRAM_API = int(os.getenv("TELEGRAM_API", "0"))
    TELEGRAM_HASH = os.getenv("TELEGRAM_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")
    HELPER_TOKENS = os.getenv("HELPER_TOKENS", "")
