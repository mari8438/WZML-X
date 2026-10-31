// Package direct implements the 4-connection parallel chunked HTTP/HTTPS downloader
// with `Range: bytes` support, automatic resume, 512 KiB sync.Pool streaming directly
// to `/app/downloads/`, and 500ms real-time progress telemetry on the internal IPC bus.
package direct

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

const (
	// MaxParallelSegments caps concurrent HTTP connections per file at 4.
	MaxParallelSegments = 4

	// MinSegmentSizeBytes (2 MiB) prevents splitting tiny files into 4 sub-requests.
	MinSegmentSizeBytes int64 = 2 * 1024 * 1024

	// ProgressBroadcastInterval is the mandatory 500ms telemetry cadence.
	ProgressBroadcastInterval = 500 * time.Millisecond

	// MaxSegmentRetries specifies how many times a segment automatically resumes via Range.
	MaxSegmentRetries = 5
)

// RequestOptions allows upstream resolvers (e.g. GDrive, Terabox) to pass custom
// cookies, referers, or filename overrides into the parallel HTTP downloader.
type RequestOptions struct {
	Headers          map[string]string
	Cookies          []*http.Cookie
	FilenameOverride string
}

// HTTPDownloader performs parallel 4-segment HTTP/HTTPS downloads directly to disk
// using reusable 512 KiB buffers from `buffer.Pool`.
type HTTPDownloader struct {
	bufPool   *buffer.Pool
	watchdog  *safety.Watchdog
	limiter   *safety.RateLimiter
	bus       *ipc.Bus
	logger    *slog.Logger
	userAgent string
}

// NewHTTPDownloader constructs a parallel chunked HTTP downloader.
func NewHTTPDownloader(
	bufPool *buffer.Pool,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
	userAgent string,
) *HTTPDownloader {
	if bufPool == nil {
		bufPool = buffer.DefaultPool
	}
	return &HTTPDownloader{
		bufPool:   bufPool,
		watchdog:  watchdog,
		limiter:   limiter,
		bus:       bus,
		logger:    logger,
		userAgent: userAgent,
	}
}

// Download executes a standard direct HTTP/HTTPS download with default RequestOptions.
func (d *HTTPDownloader) Download(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	return d.DownloadWithOptions(ctx, task, rawURL, destDir, RequestOptions{})
}

// DownloadWithOptions probes the target URL via HEAD (with Range GET fallback),
// determines whether `Accept-Ranges: bytes` and `Content-Length` allow 4-way parallel
// segmentation, streams chunks directly to `/app/downloads/<filename>`, and emits
// real-time progress events every 500ms.
func (d *HTTPDownloader) DownloadWithOptions(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
	opts RequestOptions,
) (*DownloadResult, error) {
	if strings.TrimSpace(destDir) == "" {
		destDir = "/app/downloads"
	}
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return nil, fmt.Errorf("creating download directory %s: %w", destDir, err)
	}

	client := d.buildTaskHTTPClient(task.ID)

	// 1. Inspect Content-Length, Accept-Ranges, and filename via HEAD (with Range 0-0 fallback).
	meta, err := d.probeRemoteFile(ctx, client, rawURL, opts)
	if err != nil {
		return nil, err
	}

	if meta.ContentLength > 0 {
		if err := d.watchdog.CheckNVMeHeadroom(uint64(meta.ContentLength)); err != nil {
			return nil, err
		}
		task.UpdateSpeed(0, 0, 0, 0)
	}

	fileName := sanitizeFileName(opts.FilenameOverride)
	if fileName == "" {
		fileName = sanitizeFileName(meta.FileName)
	}
	if fileName == "" {
		fileName = fmt.Sprintf("starfall_%s.bin", task.ID)
	}
	if task.Name == "" {
		task.Name = fileName
	}

	finalPath := filepath.Join(destDir, fileName)
	task.OutputPath = finalPath

	outFile, err := os.OpenFile(finalPath, os.O_CREATE|os.O_RDWR|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, fmt.Errorf("opening output file %s: %w", finalPath, err)
	}
	defer outFile.Close()

	var bytesDownloaded atomic.Int64
	var activeConns atomic.Int32

	// 2. Start 500ms real-time progress reporter goroutine publishing to ipc.Bus.
	progressCtx, stopProgress := context.WithCancel(ctx)
	var progressWG sync.WaitGroup
	progressWG.Add(1)
	go func() {
		defer progressWG.Done()
		d.emitProgressLoop(progressCtx, task, &bytesDownloaded, meta.ContentLength, &activeConns)
	}()

	var dlErr error
	if meta.SupportsRanges && meta.ContentLength >= MinSegmentSizeBytes*2 {
		// Pre-size file on disk so 4 workers can write concurrently via WriteAt.
		if err := outFile.Truncate(meta.ContentLength); err != nil {
			stopProgress()
			progressWG.Wait()
			return nil, fmt.Errorf("pre-allocating file %s (%d bytes): %w", finalPath, meta.ContentLength, err)
		}
		dlErr = d.downloadParallelSegments(
			ctx,
			client,
			task,
			meta.FinalURL,
			opts,
			outFile,
			meta.ContentLength,
			&bytesDownloaded,
			&activeConns,
		)
	} else {
		dlErr = d.downloadSingleStream(
			ctx,
			client,
			task,
			meta.FinalURL,
			opts,
			outFile,
			&bytesDownloaded,
			&activeConns,
		)
	}

	stopProgress()
	progressWG.Wait()

	if dlErr != nil {
		_ = os.Remove(finalPath)
		return nil, dlErr
	}

	if err := outFile.Sync(); err != nil {
		return nil, fmt.Errorf("syncing downloaded file to disk: %w", err)
	}

	totalSaved := bytesDownloaded.Load()
	d.publishProgressSnapshot(task, totalSaved, meta.ContentLength, 0, 0)

	return &DownloadResult{
		FilePath:   finalPath,
		FileName:   fileName,
		BytesSaved: totalSaved,
		Source:     SourceDirectHTTP,
	}, nil
}

type remoteProbeMeta struct {
	FinalURL       string
	ContentLength  int64
	SupportsRanges bool
	FileName       string
}

// probeRemoteFile issues a HEAD request (falling back to a 1-byte Range GET if HEAD is rejected)
// to determine exact Content-Length, `Accept-Ranges: bytes` support, and Content-Disposition filename.
func (d *HTTPDownloader) probeRemoteFile(
	ctx context.Context,
	client *http.Client,
	rawURL string,
	opts RequestOptions,
) (*remoteProbeMeta, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, rawURL, nil)
	if err != nil {
		return nil, err
	}
	d.applyHeaders(req, opts)

	resp, err := client.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}

	// If HEAD failed or returned 403/405/501, probe with GET Range: bytes=0-0.
	if err != nil || resp.StatusCode >= 400 || resp.ContentLength <= 0 {
		getReq, gErr := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if gErr != nil {
			return nil, gErr
		}
		d.applyHeaders(getReq, opts)
		getReq.Header.Set("Range", "bytes=0-0")

		getResp, gErr := client.Do(getReq)
		if gErr != nil {
			return nil, fmt.Errorf("probing remote URL %s: %w", rawURL, gErr)
		}
		defer getResp.Body.Close()

		if getResp.StatusCode >= 400 {
			return nil, fmt.Errorf("remote server returned HTTP %d", getResp.StatusCode)
		}

		meta := &remoteProbeMeta{
			FinalURL:       getResp.Request.URL.String(),
			ContentLength:  parseTotalFromContentRange(getResp.Header.Get("Content-Range"), getResp.ContentLength),
			SupportsRanges: getResp.StatusCode == http.StatusPartialContent || strings.Contains(strings.ToLower(getResp.Header.Get("Accept-Ranges")), "bytes"),
			FileName:       extractFileName(getResp),
		}
		return meta, nil
	}

	return &remoteProbeMeta{
		FinalURL:       resp.Request.URL.String(),
		ContentLength:  resp.ContentLength,
		SupportsRanges: strings.Contains(strings.ToLower(resp.Header.Get("Accept-Ranges")), "bytes"),
		FileName:       extractFileName(resp),
	}, nil
}

// downloadParallelSegments divides the file into 4 balanced byte ranges and downloads
// them concurrently into `outFile` via `os.File.WriteAt`.
func (d *HTTPDownloader) downloadParallelSegments(
	ctx context.Context,
	client *http.Client,
	task *models.Task,
	targetURL string,
	opts RequestOptions,
	outFile *os.File,
	totalBytes int64,
	bytesDownloaded *atomic.Int64,
	activeConns *atomic.Int32,
) error {
	segCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	segmentSize := totalBytes / MaxParallelSegments
	var wg sync.WaitGroup
	errCh := make(chan error, MaxParallelSegments)

	for i := 0; i < MaxParallelSegments; i++ {
		startOffset := int64(i) * segmentSize
		endOffset := startOffset + segmentSize - 1
		if i == MaxParallelSegments-1 {
			endOffset = totalBytes - 1
		}

		segIdx := i
		wg.Add(1)
		go func(start, end int64) {
			defer wg.Done()
			if err := d.downloadSegmentWithResume(
				segCtx,
				client,
				task,
				segIdx,
				targetURL,
				opts,
				outFile,
				start,
				end,
				bytesDownloaded,
				activeConns,
			); err != nil {
				select {
				case errCh <- err:
				default:
				}
				cancel()
			}
		}(startOffset, endOffset)
	}

	wg.Wait()
	close(errCh)

	if err, ok := <-errCh; ok && err != nil {
		return err
	}
	return nil
}

// downloadSegmentWithResume downloads byte range [start..end] and automatically resumes
// from the exact last written byte offset if a transient connection drop occurs.
func (d *HTTPDownloader) downloadSegmentWithResume(
	ctx context.Context,
	client *http.Client,
	task *models.Task,
	segIdx int,
	targetURL string,
	opts RequestOptions,
	outFile *os.File,
	start int64,
	end int64,
	bytesDownloaded *atomic.Int64,
	activeConns *atomic.Int32,
) error {
	cursor := start

	for attempt := 0; attempt <= MaxSegmentRetries; attempt++ {
		if cursor > end {
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}

		if attempt > 0 {
			backoff := time.Duration(attempt*500) * time.Millisecond
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
		if err != nil {
			return err
		}
		d.applyHeaders(req, opts)
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", cursor, end))

		activeConns.Add(1)
		resp, err := client.Do(req)
		if err != nil {
			activeConns.Add(-1)
			if attempt == MaxSegmentRetries {
				return fmt.Errorf("segment %d failed after %d retries: %w", segIdx, MaxSegmentRetries, err)
			}
			continue
		}

		if resp.StatusCode != http.StatusPartialContent && resp.StatusCode != http.StatusOK {
			_ = resp.Body.Close()
			activeConns.Add(-1)
			if resp.StatusCode >= 500 && attempt < MaxSegmentRetries {
				continue
			}
			return fmt.Errorf("segment %d unexpected HTTP status %d", segIdx, resp.StatusCode)
		}

		// Borrow a fixed 512 KiB buffer from sync.Pool.
		bufPtr := d.bufPool.Get()
		buf := *bufPtr

		var segErr error
		for cursor <= end {
			if pErr := task.WaitIfPaused(ctx); pErr != nil {
				segErr = pErr
				break
			}

			// Respect Watchdog memory state: use up to 512 KiB in NORMAL, smaller slice in WARN/CRITICAL.
			readCap := len(buf)
			if d.watchdog != nil && d.watchdog.State() != safety.StateNormal {
				if wdCap := d.watchdog.RecommendedBufferSize(); wdCap > 0 && wdCap < readCap {
					readCap = wdCap
				}
			}

			remaining := end - cursor + 1
			if int64(readCap) > remaining {
				readCap = int(remaining)
			}

			nr, rErr := resp.Body.Read(buf[:readCap])
			if nr > 0 {
				if d.limiter != nil {
					if lErr := d.limiter.WaitDownload(ctx, nr); lErr != nil {
						segErr = lErr
						break
					}
				}
				if d.watchdog != nil {
					if wErr := d.watchdog.WaitDiskWrite(ctx, nr); wErr != nil {
						segErr = wErr
						break
					}
				}

				nw, wErr := outFile.WriteAt(buf[:nr], cursor)
				cursor += int64(nw)
				bytesDownloaded.Add(int64(nw))
				if wErr != nil {
					segErr = wErr
					break
				}
				if nw != nr {
					segErr = io.ErrShortWrite
					break
				}
			}

			if rErr != nil {
				if errors.Is(rErr, io.EOF) {
					break
				}
				segErr = rErr
				break
			}
		}

		d.bufPool.Put(bufPtr)
		_ = resp.Body.Close()
		activeConns.Add(-1)

		if segErr == nil && cursor > end {
			return nil
		}
		if errors.Is(segErr, context.Canceled) || errors.Is(segErr, context.DeadlineExceeded) {
			return segErr
		}
		if attempt == MaxSegmentRetries {
			return fmt.Errorf("segment %d incomplete at offset %d/%d: %w", segIdx, cursor, end, segErr)
		}
	}

	return nil
}

// downloadSingleStream streams a non-Range HTTP response directly to disk using a 512 KiB pool buffer.
func (d *HTTPDownloader) downloadSingleStream(
	ctx context.Context,
	client *http.Client,
	task *models.Task,
	targetURL string,
	opts RequestOptions,
	outFile *os.File,
	bytesDownloaded *atomic.Int64,
	activeConns *atomic.Int32,
) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return err
	}
	d.applyHeaders(req, opts)

	activeConns.Add(1)
	defer activeConns.Add(-1)

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("HTTP download returned status %d", resp.StatusCode)
	}

	bufPtr := d.bufPool.Get()
	defer d.bufPool.Put(bufPtr)
	buf := *bufPtr

	for {
		if err := task.WaitIfPaused(ctx); err != nil {
			return err
		}

		nr, rErr := resp.Body.Read(buf)
		if nr > 0 {
			if d.limiter != nil {
				if err := d.limiter.WaitDownload(ctx, nr); err != nil {
					return err
				}
			}
			if d.watchdog != nil {
				if err := d.watchdog.WaitDiskWrite(ctx, nr); err != nil {
					return err
				}
			}

			nw, wErr := outFile.Write(buf[:nr])
			bytesDownloaded.Add(int64(nw))
			if wErr != nil {
				return wErr
			}
			if nw != nr {
				return io.ErrShortWrite
			}
		}
		if rErr != nil {
			if errors.Is(rErr, io.EOF) {
				return nil
			}
			return rErr
		}
	}
}

// emitProgressLoop publishes real-time progress telemetry (`BytesDownloaded`, `SpeedMBps`,
// `ETA`, `Percent`) to the internal IPC Event Bus every 500ms.
func (d *HTTPDownloader) emitProgressLoop(
	ctx context.Context,
	task *models.Task,
	bytesDownloaded *atomic.Int64,
	totalBytes int64,
	activeConns *atomic.Int32,
) {
	ticker := time.NewTicker(ProgressBroadcastInterval)
	defer ticker.Stop()

	lastBytes := bytesDownloaded.Load()
	lastTime := time.Now()

	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			current := bytesDownloaded.Load()
			elapsed := now.Sub(lastTime).Seconds()
			var instantBps int64
			if elapsed > 0 && current >= lastBytes {
				instantBps = int64(float64(current-lastBytes) / elapsed)
			}
			lastBytes = current
			lastTime = now

			d.publishProgressSnapshot(task, current, totalBytes, instantBps, int(activeConns.Load()))
		}
	}
}

func (d *HTTPDownloader) publishProgressSnapshot(
	task *models.Task,
	completedBytes int64,
	totalBytes int64,
	instantBps int64,
	conns int,
) {
	stats := task.UpdateSpeed(completedBytes, instantBps, 0, conns)
	speedMBps := float64(stats.DownloadSpeedBps) / (1024.0 * 1024.0)

	if d.bus != nil {
		d.bus.Publish(ipc.Event{
			Type:      ipc.EventTaskProgress,
			TaskID:    task.ID,
			UserID:    task.UserID,
			Progress:  stats.ProgressPercent,
			SpeedBps:  stats.DownloadSpeedBps,
			Timestamp: time.Now().UTC(),
			Metadata: map[string]any{
				"bytes_downloaded": completedBytes,
				"total_bytes":      totalBytes,
				"speed_mbps":       speedMBps,
				"eta_seconds":      stats.ETASeconds,
				"percent":          stats.ProgressPercent,
				"connections":      conns,
			},
		})
	}
}

func (d *HTTPDownloader) buildTaskHTTPClient(taskID string) *http.Client {
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConns:          16,
		MaxIdleConnsPerHost:   MaxParallelSegments,
		MaxConnsPerHost:       MaxParallelSegments,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		ReadBufferSize:        64 * 1024,
	}
	if d.limiter != nil {
		transport.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
			return d.limiter.DialContext(ctx, taskID, network, addr)
		}
	}
	return &http.Client{
		Transport: transport,
	}
}

func (d *HTTPDownloader) applyHeaders(req *http.Request, opts RequestOptions) {
	req.Header.Set("User-Agent", d.userAgent)
	req.Header.Set("Accept", "*/*")
	for k, v := range opts.Headers {
		req.Header.Set(k, v)
	}
	for _, c := range opts.Cookies {
		req.AddCookie(c)
	}
}

func parseTotalFromContentRange(contentRange string, fallback int64) int64 {
	// Format: "bytes 0-0/104857600"
	if idx := strings.LastIndex(contentRange, "/"); idx != -1 && idx+1 < len(contentRange) {
		rawTotal := strings.TrimSpace(contentRange[idx+1:])
		if rawTotal != "*" {
			var total int64
			if _, err := fmt.Sscanf(rawTotal, "%d", &total); err == nil && total > 0 {
				return total
			}
		}
	}
	return fallback
}

func extractFileName(resp *http.Response) string {
	if cd := resp.Header.Get("Content-Disposition"); cd != "" {
		if _, params, err := mime.ParseMediaType(cd); err == nil {
			if fn := params["filename*"]; fn != "" {
				return fn
			}
			if fn := params["filename"]; fn != "" {
				return fn
			}
		}
	}
	if resp.Request != nil && resp.Request.URL != nil {
		base := path.Base(resp.Request.URL.Path)
		if decoded, err := url.PathUnescape(base); err == nil && decoded != "" && decoded != "/" && decoded != "." {
			return decoded
		}
	}
	return ""
}

func sanitizeFileName(name string) string {
	name = strings.TrimSpace(name)
	if name == "" {
		return ""
	}
	name = filepath.Base(name)
	replacer := strings.NewReplacer(
		"/", "_",
		"\\", "_",
		"\x00", "",
		":", "_",
	)
	cleaned := strings.TrimSpace(replacer.Replace(name))
	if cleaned == "." || cleaned == ".." {
		return ""
	}
	return cleaned
}
