//go:build linux

package safety

import (
	"context"
	"os/exec"
	"syscall"
	"time"
)

// NewSupervisedCommand returns an *exec.Cmd configured with process group isolation,
// kernel-level parent death signal (SIGKILL), process-group termination on cancellation,
// and bounded pipe close delay to prevent zombie processes.
func NewSupervisedCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid:   true,            // Isolate child + descendants into dedicated process group
		Pdeathsig: syscall.SIGKILL, // Kernel kills child immediately if starfall-debrid dies
	}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		// Negative PID signals the ENTIRE process group (e.g., yt-dlp + spawned ffmpeg)
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	}
	// WaitDelay allows grace period for child to exit after Cancel/ctx done,
	// then forcefully sends SIGKILL to child and closes all standard pipes.
	cmd.WaitDelay = 5 * time.Second
	return cmd
}
