// Package config provides Viper and environment-variable driven configuration
// management for Starfall-Go Debrid, with hard safety validation for 2 vCPU,
// 4 GB RAM VPS deployments and Google OAuth 2.0 / JWT session settings.
package config

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/viper"
)

const (
	// MaxAllowedSoftMemoryMiB enforces the 3000 MiB soft memory limit ceiling.
	MaxAllowedSoftMemoryMiB = 3000

	// MaxAllowedHardCeilingMiB enforces the 3200 MiB absolute process ceiling.
	MaxAllowedHardCeilingMiB = 3200

	// MaxAllowedGlobalConns prevents hypervisor abuse bans by capping P2P sockets at 150.
	MaxAllowedGlobalConns = 150

	// MaxAllowedPerTaskConns caps per-task sockets at 30.
	MaxAllowedPerTaskConns = 30
)

// Config aggregates all runtime subsystems settings for Starfall-Go Debrid.
type Config struct {
	App      AppConfig      `mapstructure:"app"`
	Memory   MemoryConfig   `mapstructure:"memory"`
	Network  NetworkConfig  `mapstructure:"network"`
	Engine   EngineConfig   `mapstructure:"engine"`
	Auth     AuthConfig     `mapstructure:"auth"`
	Database DatabaseConfig `mapstructure:"database"`
	Telegram TelegramConfig `mapstructure:"telegram"`
}

// AppConfig holds general daemon parameters.
type AppConfig struct {
	Name        string `mapstructure:"name"`
	Environment string `mapstructure:"environment"`
	ListenAddr  string `mapstructure:"listen_addr"`
	DownloadDir string `mapstructure:"download_dir"`
	MaxProcs    int    `mapstructure:"max_procs"`
}

// MemoryConfig defines thresholds for the RAM & OOM circuit breaker watchdog.
type MemoryConfig struct {
	SoftLimitMiB       int     `mapstructure:"soft_limit_mib"`
	HardCeilingMiB     int     `mapstructure:"hard_ceiling_mib"`
	SystemRAMMiB       int     `mapstructure:"system_ram_mib"`
	WarnPercent        float64 `mapstructure:"warn_percent"`
	CriticalPercent    float64 `mapstructure:"critical_percent"`
	HeapSafetyRatio    float64 `mapstructure:"heap_safety_ratio"`
	WatchdogIntervalMs int     `mapstructure:"watchdog_interval_ms"`
}

// NetworkConfig defines P2P socket and bandwidth rate limits.
type NetworkConfig struct {
	MaxGlobalConns  int   `mapstructure:"max_global_conns"`
	MaxConnsPerTask int   `mapstructure:"max_conns_per_task"`
	MaxDownloadBps  int64 `mapstructure:"max_download_bps"`
	MaxUploadBps    int64 `mapstructure:"max_upload_bps"`
	DialTimeoutSec  int   `mapstructure:"dial_timeout_sec"`
}

// EngineConfig defines worker pool concurrency caps and disk I/O pacing bounds.
type EngineConfig struct {
	MaxConcurrentWorkers int     `mapstructure:"max_concurrent_workers"`
	MaxActiveDownloads   int     `mapstructure:"max_active_downloads"`
	MaxTelegramUploads   int     `mapstructure:"max_telegram_uploads"`
	MaxLosslessRemux     int     `mapstructure:"max_lossless_remux"`
	QueueBufferSize      int     `mapstructure:"queue_buffer_size"`
	NVMeMinFreeGB        float64 `mapstructure:"nvme_min_free_gb"`
	MaxDiskWriteMBps     int64   `mapstructure:"max_disk_write_mbps"`
}

// AuthConfig holds Google OAuth 2.0 credentials and 30-day JWT session parameters.
type AuthConfig struct {
	GoogleClientID     string `mapstructure:"google_client_id"`
	GoogleClientSecret string `mapstructure:"google_client_secret"`
	GoogleRedirectURL  string `mapstructure:"google_redirect_url"`
	JWTSecret          string `mapstructure:"jwt_secret"`
	CookieDomain       string `mapstructure:"cookie_domain"`
	SecureCookie       bool   `mapstructure:"secure_cookie"`
	SessionDays        int    `mapstructure:"session_days"`
}

// DatabaseConfig holds embedded SQLite storage configuration.
type DatabaseConfig struct {
	SQLitePath string `mapstructure:"sqlite_path"`
}

// TelegramConfig holds credentials and upload tuning for Telegram Bot/MTProto integration.
type TelegramConfig struct {
	BotToken     string `mapstructure:"bot_token"`
	APIID        int    `mapstructure:"api_id"`
	APIHash      string `mapstructure:"api_hash"`
	OwnerID      int64  `mapstructure:"owner_id"`
	DumpChatID   int64  `mapstructure:"dump_chat_id"`
	SplitSizeMiB int64  `mapstructure:"split_size_mib"`
	BrandName    string `mapstructure:"brand_name"`
}

// Load initializes configuration from embedded YAML bytes, optional disk config files,
// and `STARFALL_` prefixed environment variables, clamping any unsafe values to VPS hardware limits.
func Load(embeddedYAML []byte) (*Config, error) {
	v := viper.New()
	v.SetConfigType("yaml")

	setHardwareSafeDefaults(v)

	if len(embeddedYAML) > 0 {
		if err := v.ReadConfig(bytes.NewReader(embeddedYAML)); err != nil {
			return nil, fmt.Errorf("parsing embedded config: %w", err)
		}
	}

	if cfgFile := os.Getenv("STARFALL_CONFIG_FILE"); cfgFile != "" {
		v.SetConfigFile(cfgFile)
		if err := v.MergeInConfig(); err != nil {
			return nil, fmt.Errorf("merging external config file %s: %w", cfgFile, err)
		}
	} else {
		v.SetConfigName("config")
		v.AddConfigPath(".")
		v.AddConfigPath("/etc/starfall-debrid")
		_ = v.MergeInConfig()
	}

	v.SetEnvPrefix("STARFALL")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	v.AutomaticEnv()

	var cfg Config
	if err := v.Unmarshal(&cfg); err != nil {
		return nil, fmt.Errorf("unmarshaling config struct: %w", err)
	}

	if err := cfg.ValidateAndClamp(); err != nil {
		return nil, err
	}

	return &cfg, nil
}

func setHardwareSafeDefaults(v *viper.Viper) {
	v.SetDefault("app.name", "starfall-debrid")
	v.SetDefault("app.environment", "production")
	v.SetDefault("app.listen_addr", "0.0.0.0:8080")
	v.SetDefault("app.download_dir", "/usr/src/app/downloads")
	v.SetDefault("app.max_procs", 2)

	v.SetDefault("memory.soft_limit_mib", MaxAllowedSoftMemoryMiB)
	v.SetDefault("memory.hard_ceiling_mib", MaxAllowedHardCeilingMiB)
	v.SetDefault("memory.system_ram_mib", 4096)
	v.SetDefault("memory.warn_percent", 70.0)
	v.SetDefault("memory.critical_percent", 82.0)
	v.SetDefault("memory.heap_safety_ratio", 0.80)
	v.SetDefault("memory.watchdog_interval_ms", 1000)

	v.SetDefault("network.max_global_conns", MaxAllowedGlobalConns)
	v.SetDefault("network.max_conns_per_task", MaxAllowedPerTaskConns)
	v.SetDefault("network.max_download_bps", 0)
	v.SetDefault("network.max_upload_bps", 0)
	v.SetDefault("network.dial_timeout_sec", 15)

	v.SetDefault("engine.max_concurrent_workers", 4)
	v.SetDefault("engine.max_active_downloads", 3)
	v.SetDefault("engine.max_telegram_uploads", 2)
	v.SetDefault("engine.max_lossless_remux", 1)
	v.SetDefault("engine.queue_buffer_size", 128)
	v.SetDefault("engine.nvme_min_free_gb", 5.0)
	v.SetDefault("engine.max_disk_write_mbps", 120)

	v.SetDefault("auth.google_redirect_url", "http://localhost:8080/auth/google/callback")
	v.SetDefault("auth.secure_cookie", true)
	v.SetDefault("auth.session_days", 30)

	v.SetDefault("database.sqlite_path", "data/starfall_users.db")

	v.SetDefault("telegram.split_size_mib", 2000)
	v.SetDefault("telegram.brand_name", "Anime Starfall")
}

// ValidateAndClamp enforces hard project invariants so misconfigured environment variables
// can never exceed the 3200 MiB memory bound, 150/30 P2P connection limits, or worker pool caps.
func (c *Config) ValidateAndClamp() error {
	if c.App.MaxProcs <= 0 || c.App.MaxProcs > 2 {
		c.App.MaxProcs = 2
	}
	if c.Memory.SoftLimitMiB <= 0 || c.Memory.SoftLimitMiB > MaxAllowedSoftMemoryMiB {
		c.Memory.SoftLimitMiB = MaxAllowedSoftMemoryMiB
	}
	if c.Memory.HardCeilingMiB <= 0 || c.Memory.HardCeilingMiB > MaxAllowedHardCeilingMiB {
		c.Memory.HardCeilingMiB = MaxAllowedHardCeilingMiB
	}
	if c.Memory.SystemRAMMiB <= 0 {
		c.Memory.SystemRAMMiB = 4096
	}
	if c.Memory.WarnPercent <= 0 || c.Memory.WarnPercent >= 90 {
		c.Memory.WarnPercent = 70.0
	}
	if c.Memory.CriticalPercent <= c.Memory.WarnPercent || c.Memory.CriticalPercent > 95 {
		c.Memory.CriticalPercent = 82.0
	}
	if c.Memory.HeapSafetyRatio <= 0 || c.Memory.HeapSafetyRatio > 0.90 {
		c.Memory.HeapSafetyRatio = 0.80
	}
	if c.Memory.WatchdogIntervalMs <= 0 {
		c.Memory.WatchdogIntervalMs = 1000
	}

	if c.Network.MaxGlobalConns <= 0 || c.Network.MaxGlobalConns > MaxAllowedGlobalConns {
		c.Network.MaxGlobalConns = MaxAllowedGlobalConns
	}
	if c.Network.MaxConnsPerTask <= 0 || c.Network.MaxConnsPerTask > MaxAllowedPerTaskConns {
		c.Network.MaxConnsPerTask = MaxAllowedPerTaskConns
	}
	if c.Network.DialTimeoutSec <= 0 {
		c.Network.DialTimeoutSec = 15
	}

	if c.Engine.MaxConcurrentWorkers <= 0 || c.Engine.MaxConcurrentWorkers > 4 {
		c.Engine.MaxConcurrentWorkers = 4
	}
	if c.Engine.MaxActiveDownloads <= 0 || c.Engine.MaxActiveDownloads > 3 {
		c.Engine.MaxActiveDownloads = 3
	}
	if c.Engine.MaxTelegramUploads <= 0 || c.Engine.MaxTelegramUploads > 2 {
		c.Engine.MaxTelegramUploads = 2
	}
	if c.Engine.MaxLosslessRemux <= 0 || c.Engine.MaxLosslessRemux > 1 {
		c.Engine.MaxLosslessRemux = 1
	}
	if c.Engine.QueueBufferSize <= 0 {
		c.Engine.QueueBufferSize = 128
	}
	if c.Engine.NVMeMinFreeGB < 1.0 {
		c.Engine.NVMeMinFreeGB = 5.0
	}
	if c.Engine.MaxDiskWriteMBps <= 0 {
		c.Engine.MaxDiskWriteMBps = 120
	}

	if len(strings.TrimSpace(c.Auth.JWTSecret)) < 32 {
		buf := make([]byte, 32)
		_, _ = rand.Read(buf)
		c.Auth.JWTSecret = hex.EncodeToString(buf)
	}
	if c.Auth.SessionDays <= 0 {
		c.Auth.SessionDays = 30
	}
	if strings.TrimSpace(c.Database.SQLitePath) == "" {
		c.Database.SQLitePath = "data/starfall_users.db"
	}
	if strings.TrimSpace(c.App.DownloadDir) == "" {
		return errors.New("app.download_dir cannot be empty")
	}

	return nil
}
