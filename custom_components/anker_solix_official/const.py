"""Constants definition."""

DOMAIN = "anker_solix_official"

# Timing constants (seconds)
SCAN_INTERVAL = 5
CONNECTION_RETRY_DELAY = 10
MODBUS_RESPONSE_TIMEOUT = 5
WRITE_CONDITION_REVERT_DELAY = 0.1

# Connection constants
CONNECTION_CHECK_INTERVAL = 30

# Data reading constants
BATCH_READ_GAP_THRESHOLD = 5
MAX_REGISTERS_PER_READ = 100

# Logging constants
LOG_THROTTLE_INTERVAL = 60

# Error messages
ERROR_INVALID_IP = "invalid_ip"
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_CONNECTION_TIMEOUT = "connection_timeout"
ERROR_DEVICE_NOT_SUPPORTED = "device_not_supported"

