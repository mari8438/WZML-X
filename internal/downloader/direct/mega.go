// Package direct implements a native Mega.nz decrypted chunk downloader in Go.
// It resolves Mega API file handles, derives the 128-bit AES-CTR key and 64-bit IV
// from the URL key, decrypts file attributes, and streams decrypted 512 KiB chunks
// directly to `/app/downloads/` with zero full-file RAM buffering.
package direct

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

var (
	// Matches modern `https://mega.nz/file/<handle>#<key>` URLs.
	megaModernRegex = regexp.MustCompile(`/file/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)`)
	// Matches legacy `https://mega.nz/#!<handle>!<key>` URLs.
	megaLegacyRegex = regexp.MustCompile(`#!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)`)

	// ErrInvalidMegaURL indicates the Mega link is missing a file handle or decryption key.
	ErrInvalidMegaURL = errors.New("invalid mega.nz URL: missing file handle or decryption key")
)

// MegaDownloader resolves Mega.nz links and streams AES-CTR decrypted chunks directly to disk.
type MegaDownloader struct {
	bufPool   *buffer.Pool
	watchdog  *safety.Watchdog
	limiter   *safety.RateLimiter
	bus       *ipc.Bus
	logger    *slog.Logger
	userAgent string
}

// NewMegaDownloader constructs a native Mega.nz decrypted stream downloader.
func NewMegaDownloader(
	bufPool *buffer.Pool,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
	userAgent string,
) *MegaDownloader {
	if bufPool == nil {
		bufPool = buffer.DefaultPool
	}
	return &MegaDownloader{
		bufPool:   bufPool,
		watchdog:  watchdog,
		limiter:   limiter,
		bus:       bus,
		logger:    logger,
		userAgent: userAgent,
	}
}

// ParseMegaURL extracts the public file handle and 256-bit base64url key from a Mega.nz URL,
// deriving the 128-bit AES key and 128-bit CTR counter IV.
func ParseMegaURL(rawURL string) (handle string, aesKey []byte, iv []byte, err error) {
	if m := megaModernRegex.FindStringSubmatch(rawURL); len(m) == 3 {
		handle = m[1]
		aesKey, iv, err = deriveMegaKeyAndIV(m[2])
		return handle, aesKey, iv, err
	}
	if m := megaLegacyRegex.FindStringSubmatch(rawURL); len(m) == 3 {
		handle = m[1]
		aesKey, iv, err = deriveMegaKeyAndIV(m[2])
		return handle, aesKey, iv, err
	}
	return "", nil, nil, ErrInvalidMegaURL
}

func deriveMegaKeyAndIV(b64Key string) ([]byte, []byte, error) {
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(b64Key, "="))
	if err != nil || len(raw) < 32 {
		return nil, nil, fmt.Errorf("%w: key decode failed", ErrInvalidMegaURL)
	}

	var k [8]uint32
	for i := 0; i < 8; i++ {
		k[i] = binary.BigEndian.Uint32(raw[i*4 : (i+1)*4])
	}

	// Mega unmasks the 128-bit file AES key by XORing the first four 32-bit words with the last four.
	aesKey := make([]byte, 16)
	binary.BigEndian.PutUint32(aesKey[0:4], k[0]^k[4])
	binary.BigEndian.PutUint32(aesKey[4:8], k[1]^k[5])
	binary.BigEndian.PutUint32(aesKey[8:12], k[2]^k[6])
	binary.BigEndian.PutUint32(aesKey[12:16], k[3]^k[7])

	// Words 4 and 5 form the upper 64 bits of the 128-bit AES-CTR nonce/counter.
	iv := make([]byte, 16)
	binary.BigEndian.PutUint32(iv[0:4], k[4])
	binary.BigEndian.PutUint32(iv[4:8], k[5])

	return aesKey, iv, nil
}

type megaAPIResponse struct {
	Size       int64  `json:"s"`
	Attributes string `json:"at"`
	DownloadG  string `json:"g"`
}

type megaFileAttributes struct {
	Name string `json:"n"`
}

// Download queries the Mega.nz API for the encrypted CDN stream URL, decrypts the filename,
// and streams AES-CTR decrypted 512 KiB chunks directly to `destDir`.
func (m *MegaDownloader) Download(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	if strings.TrimSpace(destDir) == "" {
		destDir = "/app/downloads"
	}
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return nil, err
	}

	handle, aesKey, iv, err := ParseMegaURL(rawURL)
	if err != nil {
		return nil, err
	}

	meta, err := m.fetchMegaNodeMeta(ctx, handle)
	if err != nil {
		return nil, err
	}
	if meta.DownloadG == "" {
		return nil, errors.New("mega API returned empty download URL (file may be removed or rate-limited)")
	}

	if m.watchdog != nil && meta.Size > 0 {
		if err := m.watchdog.CheckNVMeHeadroom(uint64(meta.Size)); err != nil {
			return nil, err
		}
	}

	fileName := decryptMegaAttributes(meta.Attributes, aesKey)
	fileName = sanitizeFileName(fileName)
	if fileName == "" {
		fileName = fmt.Sprintf("mega_%s.bin", handle)
	}
	task.SetName(fileName)
	if meta.Size > 0 {
		task.SetTotalBytes(meta.Size)
	}

	finalPath := filepath.Join(destDir, fileName)
	task.SetOutputPath(finalPath)

	outFile, err := os.OpenFile(finalPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, err
	}
	defer outFile.Close()

	block, err := aes.NewCipher(aesKey)
	if err != nil {
		return nil, err
	}
	streamCipher := cipher.NewCTR(block, iv)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, meta.DownloadG, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", m.userAgent)

	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		_ = os.Remove(finalPath)
		return nil, fmt.Errorf("connecting to mega storage CDN: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		_ = os.Remove(finalPath)
		return nil, fmt.Errorf("mega CDN returned HTTP %d", resp.StatusCode)
	}

	bufPtr := m.bufPool.Get()
	defer m.bufPool.Put(bufPtr)
	buf := *bufPtr

	var written int64
	lastTick := time.Now()
	var bytesSinceTick int64

	for {
		if err := task.WaitIfPaused(ctx); err != nil {
			_ = os.Remove(finalPath)
			return nil, err
		}

		nr, rErr := resp.Body.Read(buf)
		if nr > 0 {
			if m.limiter != nil {
				if err := m.limiter.WaitDownload(ctx, nr); err != nil {
					_ = os.Remove(finalPath)
					return nil, err
				}
			}
			if m.watchdog != nil {
				if err := m.watchdog.WaitDiskWrite(ctx, nr); err != nil {
					_ = os.Remove(finalPath)
					return nil, err
				}
			}

			// Decrypt in-place inside the reusable 512 KiB buffer before writing to disk.
			streamCipher.XORKeyStream(buf[:nr], buf[:nr])

			nw, wErr := outFile.Write(buf[:nr])
			written += int64(nw)
			bytesSinceTick += int64(nw)
			if wErr != nil {
				_ = os.Remove(finalPath)
				return nil, wErr
			}
			if nw != nr {
				_ = os.Remove(finalPath)
				return nil, io.ErrShortWrite
			}

			if elapsed := time.Since(lastTick); elapsed >= ProgressBroadcastInterval {
				bps := int64(float64(bytesSinceTick) / elapsed.Seconds())
				stats := task.UpdateSpeed(written, bps, 0, 1)
				if m.bus != nil {
					m.bus.Publish(ipc.Event{
						Type:      ipc.EventTaskProgress,
						TaskID:    task.ID,
						UserID:    task.UserID,
						Progress:  stats.ProgressPercent,
						SpeedBps:  stats.DownloadSpeedBps,
						Timestamp: time.Now().UTC(),
						Metadata: map[string]any{
							"bytes_downloaded": written,
							"total_bytes":      meta.Size,
							"speed_mbps":       float64(stats.DownloadSpeedBps) / (1024.0 * 1024.0),
							"eta_seconds":      stats.ETASeconds,
							"percent":          stats.ProgressPercent,
						},
					})
				}
				lastTick = time.Now()
				bytesSinceTick = 0
			}
		}

		if rErr != nil {
			if errors.Is(rErr, io.EOF) {
				break
			}
			_ = os.Remove(finalPath)
			return nil, rErr
		}
	}

	_ = outFile.Sync()
	return &DownloadResult{
		FilePath:   finalPath,
		FileName:   fileName,
		BytesSaved: written,
		Source:     SourceMega,
	}, nil
}

func (m *MegaDownloader) fetchMegaNodeMeta(ctx context.Context, handle string) (*megaAPIResponse, error) {
	payload := fmt.Sprintf(`[{"a":"g","g":1,"p":%q}]`, handle)
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		"https://g.api.mega.co.nz/cs",
		strings.NewReader(payload),
	)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", m.userAgent)

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("querying mega API for handle %s: %w", handle, err)
	}
	defer resp.Body.Close()

	var results []megaAPIResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64*1024)).Decode(&results); err != nil || len(results) == 0 {
		return nil, fmt.Errorf("decoding mega API response: %w", err)
	}
	return &results[0], nil
}

func decryptMegaAttributes(b64Attr string, aesKey []byte) string {
	encBytes, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(b64Attr, "="))
	if err != nil || len(encBytes) == 0 || len(encBytes)%aes.BlockSize != 0 {
		return ""
	}
	block, err := aes.NewCipher(aesKey)
	if err != nil {
		return ""
	}
	zeroIV := make([]byte, aes.BlockSize)
	cbc := cipher.NewCBCDecrypter(block, zeroIV)
	plain := make([]byte, len(encBytes))
	cbc.CryptBlocks(plain, encBytes)

	plain = bytes.TrimRight(plain, "\x00")
	if !bytes.HasPrefix(plain, []byte("MEGA{")) {
		return ""
	}
	var attr megaFileAttributes
	if err := json.Unmarshal(plain[4:], &attr); err != nil {
		return ""
	}
	return attr.Name
}
