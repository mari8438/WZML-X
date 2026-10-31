//go:build !linux

package safety

// GetDiskSpace provides fallback disk metrics for non-Linux hosts.
func GetDiskSpace(path string) (totalBytes uint64, freeBytes uint64, err error) {
	// Fallback to generous default on non-Linux development machines (e.g. 100 GB total, 50 GB free)
	const defaultTotal = 100 * 1024 * 1024 * 1024
	const defaultFree = 50 * 1024 * 1024 * 1024
	return defaultTotal, defaultFree, nil
}
