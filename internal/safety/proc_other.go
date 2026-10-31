//go:build !linux

package safety

import (
	"context"
	"os/exec"
	"time"
)

// NewSupervisedCommand returns an *exec.Cmd configured with process cancellation
// and pipe close delay for non-Linux OSes.
func NewSupervisedCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.WaitDelay = 5 * time.Second
	return cmd
}
