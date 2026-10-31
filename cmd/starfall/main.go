// Package main bootstraps the Starfall-Go Debrid engine, wiring the 3000 MiB
// Memory Watchdog, 150/30 P2P RateLimiter, 4-Worker Dispatcher, Pure-Go SQLite
// User Store, and Google OAuth 2.0 + 30-Day HTTP-Only JWT Authentication System.
package main

import (
	"context"
	"embed"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"syscall"
	"time"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/config"
	"github.com/starfall-org/starfall-go/internal/downloader"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/safety"
	"github.com/starfall-org/starfall-go/internal/user"
	webhandlers "github.com/starfall-org/starfall-go/internal/web"
	webassets "github.com/starfall-org/starfall-go/web"
)

const (
	// SoftMemoryLimitBytes configures the Go runtime GC soft memory ceiling at 3000 MiB.
	SoftMemoryLimitBytes int64 = 3000 * 1024 * 1024

	// HardMemoryCeilingBytes represents the absolute 3200 MiB process safety boundary.
	HardMemoryCeilingBytes uint64 = 3200 * 1024 * 1024
)

// embeddedAssets bundles default YAML configs and web templates directly into the
// single static binary `starfall-debrid` using Go 1.23+ embed.FS.
//
//go:embed assets/*
var embeddedAssets embed.FS

func main() {
	debug.SetMemoryLimit(SoftMemoryLimitBytes)
	debug.SetGCPercent(75)
	runtime.GOMAXPROCS(2)

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
	slog.SetDefault(logger)

	defaultCfgBytes, _ := embeddedAssets.ReadFile("assets/default_config.yaml")
	cfg, err := config.Load(defaultCfgBytes)
	if err != nil {
		logger.Error("failed to load configuration", slog.String("error", err.Error()))
		os.Exit(1)
	}

	if cfg.App.MaxProcs > 0 {
		runtime.GOMAXPROCS(cfg.App.MaxProcs)
	}

	rootCtx, stopSignals := signal.NotifyContext(
		context.Background(),
		os.Interrupt,
		syscall.SIGTERM,
		syscall.SIGQUIT,
	)
	defer stopSignals()

	// 1. Initialize Embedded Pure-Go SQLite User Store & User Service.
	userStore, err := user.NewStore(cfg.Database.SQLitePath)
	if err != nil {
		logger.Error("failed to initialize sqlite user store", slog.String("error", err.Error()))
		os.Exit(1)
	}
	defer userStore.Close()
	userService := user.NewService(userStore, logger)

	// 2. Initialize 30-Day HTTP-Only JWT Session Manager & Auth Middleware.
	jwtManager, err := auth.NewJWTManager(auth.JWTConfig{
		SecretKey:    []byte(cfg.Auth.JWTSecret),
		Issuer:       cfg.App.Name,
		CookieDomain: cfg.Auth.CookieDomain,
		SecureCookie: cfg.Auth.SecureCookie,
		SessionTTL:   time.Duration(cfg.Auth.SessionDays) * 24 * time.Hour,
	})
	if err != nil {
		logger.Error("failed to initialize jwt manager", slog.String("error", err.Error()))
		os.Exit(1)
	}
	authMiddleware := auth.NewMiddleware(jwtManager, userService, logger)

	// 3. Initialize Internal IPC Event Bus, Task Registry, RateLimiter, Watchdog, and Dispatcher.
	bus := ipc.NewBus(logger, 256)
	defer bus.Close()

	eventSubID, eventCh := bus.SubscribeAll(128)
	defer bus.Unsubscribe(eventSubID)
	go monitorEngineEvents(rootCtx, logger, eventCh)

	registry := engine.NewTaskRegistry()

	rateLimiter := safety.NewRateLimiter(safety.RateLimitConfig{
		MaxGlobalConnections:  cfg.Network.MaxGlobalConns,
		MaxPerTaskConnections: cfg.Network.MaxConnsPerTask,
		GlobalDownloadBps:     cfg.Network.MaxDownloadBps,
		GlobalUploadBps:       cfg.Network.MaxUploadBps,
		DialTimeout:           time.Duration(cfg.Network.DialTimeoutSec) * time.Second,
	}, logger)

	watchdog := safety.NewWatchdog(safety.WatchdogConfig{
		SoftLimitBytes:    uint64(SoftMemoryLimitBytes),
		HardCeilingBytes:  HardMemoryCeilingBytes,
		TotalSystemRAM:    uint64(cfg.Memory.SystemRAMMiB) * 1024 * 1024,
		WarnThresholdPct:  cfg.Memory.WarnPercent,
		CritThresholdPct:  cfg.Memory.CriticalPercent,
		HeapSafetyRatio:   cfg.Memory.HeapSafetyRatio,
		TickInterval:      time.Duration(cfg.Memory.WatchdogIntervalMs) * time.Millisecond,
		BaseDiskWriteBps:  cfg.Engine.MaxDiskWriteMBps * 1024 * 1024,
		MinNVMeFreeBytes:  uint64(cfg.Engine.NVMeMinFreeGB * 1024 * 1024 * 1024),
		DownloadDirectory: cfg.App.DownloadDir,
	}, logger)

	dispatcher := engine.NewDispatcher(
		engine.DispatcherConfig{
			MaxConcurrentWorkers: cfg.Engine.MaxConcurrentWorkers,
			MaxActiveDownloads:   cfg.Engine.MaxActiveDownloads,
			MaxTelegramUploads:   cfg.Engine.MaxTelegramUploads,
			MaxLosslessRemux:     cfg.Engine.MaxLosslessRemux,
			QueueBufferSize:      cfg.Engine.QueueBufferSize,
			DownloadDir:          cfg.App.DownloadDir,
		},
		registry,
		watchdog,
		rateLimiter,
		bus,
		logger,
	)

	watchdog.SetCallbacks(safety.WatchdogCallbacks{
		OnEnterNormal: func(snap safety.MemorySnapshot) {
			rateLimiter.SetThrottleFactor(1.0)
			dispatcher.ResumeActiveDownloads("memory pressure returned to NORMAL")
		},
		OnEnterWarn: func(snap safety.MemorySnapshot) {
			rateLimiter.SetThrottleFactor(0.60)
		},
		OnEnterCritical: func(snap safety.MemorySnapshot) {
			rateLimiter.SetThrottleFactor(0.15)
			dispatcher.PauseActiveDownloads("CRITICAL memory threshold reached")
		},
	})

	watchdog.Start(rootCtx)
	dispatcher.Start(rootCtx)

	// 4. Initialize Unified Direct Leech Manager (GDrive, Mega, Terabox, DDL, yt-dlp).
	dlManager := downloader.NewManager(
		downloader.ManagerConfig{
			DownloadDir:         cfg.App.DownloadDir,
			RequireTelegramDump: false,
		},
		dispatcher,
		registry,
		watchdog,
		rateLimiter,
		bus,
		logger,
	)

	// 5. Start HTTP Server with Web Studio, Video Tools, Owner Admin Deck, and Google OAuth 2.0.
	httpServer := startHTTPServer(
		cfg,
		jwtManager,
		authMiddleware,
		userService,
		watchdog,
		rateLimiter,
		registry,
		dispatcher,
		dlManager,
		bus,
		logger,
	)

	<-rootCtx.Done()
	logger.Info("shutdown signal received; draining workers and closing resources")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_ = httpServer.Shutdown(shutdownCtx)
	dispatcher.Shutdown(shutdownCtx)
	watchdog.Stop()
}

func monitorEngineEvents(ctx context.Context, logger *slog.Logger, ch <-chan ipc.Event) {
	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-ch:
			if !ok {
				return
			}
			if evt.Type != ipc.EventTaskProgress {
				logger.Info("engine lifecycle event",
					slog.String("event", string(evt.Type)),
					slog.String("task_id", evt.TaskID),
					slog.String("user_id", evt.UserID),
					slog.String("message", evt.Message),
				)
			}
		}
	}
}

func startHTTPServer(
	cfg *config.Config,
	jwtManager *auth.JWTManager,
	authMiddleware *auth.Middleware,
	userService *user.Service,
	wd *safety.Watchdog,
	rl *safety.RateLimiter,
	reg *engine.TaskRegistry,
	disp *engine.Dispatcher,
	dlManager *downloader.Manager,
	bus *ipc.Bus,
	logger *slog.Logger,
) *http.Server {
	mux := http.NewServeMux()
	mux.Handle("GET /assets/", http.FileServer(http.FS(embeddedAssets)))

	// Register protected /api/user/profile, /api/user/telegram-dump, and /api/user/preferences routes.
	userService.RegisterRoutes(mux, authMiddleware)

	// Register Google OAuth 2.0 routes (/auth/google/login, /auth/google/callback, /auth/logout).
	if cfg.Auth.GoogleClientID != "" && cfg.Auth.GoogleClientSecret != "" {
		googleHandler, err := auth.NewGoogleAuthHandler(
			auth.GoogleOAuthConfig{
				ClientID:          cfg.Auth.GoogleClientID,
				ClientSecret:      cfg.Auth.GoogleClientSecret,
				RedirectURL:       cfg.Auth.GoogleRedirectURL,
				PostLoginRedirect: "/",
				SecureCookie:      cfg.Auth.SecureCookie,
			},
			jwtManager,
			userService,
			logger,
		)
		if err == nil {
			googleHandler.RegisterRoutes(mux)
		}
	} else {
		mux.HandleFunc("GET /auth/logout", func(w http.ResponseWriter, r *http.Request) {
			jwtManager.ClearSessionCookie(w)
			http.Redirect(w, r, "/", http.StatusSeeOther)
		})
	}

	// Register Cyberpunk Anime Web Studio Dashboard & Leech API routes
	dashboardHandler := webhandlers.NewDashboardHandler(
		webassets.Assets,
		dlManager,
		reg,
		userService,
		jwtManager,
		cfg.App.DownloadDir,
		logger,
	)
	dashboardHandler.RegisterRoutes(mux, authMiddleware)

	// Register Lossless Video Tools Studio routes (probe, frame grabber, mkvmerge remux)
	videoHandler := webhandlers.NewVideoStudioHandler(
		webassets.Assets,
		reg,
		disp,
		wd,
		cfg.App.DownloadDir,
		logger,
	)
	videoHandler.RegisterRoutes(mux, authMiddleware)

	// Register Owner Bot Settings UI & Admin routes
	adminHandler := webhandlers.NewAdminHandler(
		webassets.Assets,
		fmt.Sprintf("%d", cfg.Telegram.OwnerID),
		cfg,
		wd,
		rl,
		disp,
		reg,
		bus,
		logger,
	)
	adminHandler.RegisterRoutes(mux, authMiddleware)

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		snap := wd.Snapshot()
		stats := disp.Stats()
		w.Header().Set("Content-Type", "application/json")
		if snap.State == safety.StateCritical {
			w.WriteHeader(http.StatusServiceUnavailable)
		} else {
			w.WriteHeader(http.StatusOK)
		}
		_, _ = fmt.Fprintf(w,
			`{"status":%q,"ram_pct":%.2f,"heap_mib":%d,"global_conns":%d,"active_tasks":%d,"dl_active":%d,"ul_active":%d,"remux_active":%d}`,
			snap.State.String(),
			snap.UsagePercent,
			snap.HeapAllocBytes/(1024*1024),
			rl.ActiveGlobalConnections(),
			reg.ActiveCount(),
			stats.ActiveDownloads,
			stats.ActiveUploads,
			stats.ActiveRemuxes,
		)
	})

	srv := &http.Server{
		Addr:              cfg.App.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       30 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("HTTP server failed", slog.String("error", err.Error()))
		}
	}()

	return srv
}
