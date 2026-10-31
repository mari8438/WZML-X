//go:build linux

package safety

import (
	"syscall"
)

// GetDiskSpace returns total and available disk bytes for the given filesystem path on Linux.
func GetDiskSpace(path string) (totalBytes uint64, freeBytes uint64, err error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		if errFallback := syscall.Statfs(".", &stat); errFallback != nil {
			return 0, 0, err
		}
	}
	total := stat.Blocks * uint64(stat.Bsize)
	free := stat.Bavail * uint64(stat.Bsize)
	return total, free, nil
}
