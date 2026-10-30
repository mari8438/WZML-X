// Package telegram implements the Adaptive 2GB/4GB Parallel Stream Chunk Uploader (`uploader.go`)
// using `github.com/gotd/td/tg` (`upload.saveBigFilePart`).
//
// Memory & Performance Profile on 2 vCPU / 4 GB RAM VPS:
//   - Reads files from disk via `os.File.ReadAt` in fixed 512 KiB (`524,288` byte) chunks
//     borrowed from `buffer.Pool`.
//   - Dispatches parts across 4 parallel worker goroutines (`4 * 512 KiB = 2 MiB` active heap,
//     staying well under the 35 MB RAM budget).
//   - Automatically splits >2GB files on Free/Bot sessions (or >4GB on Premium sessions)
//     via `Splitter.EnsureWithinLimit` and uploads Part 1, Part 2 sequentially with 0% transcoding.
package telegram

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gotd/td/tg"
	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

const (
	// MTProtoPartSize512KB is the exact 512 KiB (524,288 bytes) part size required by
	// Telegram's `upload.saveBigFilePart` (divides 1 MiB evenly and <= 512 KiB max).
	MTProtoPartSize512KB = 512 * 1024

	// ParallelUploadWorkers sets the number of concurrent goroutines uploading 512 KiB parts.
	// 4 workers * 512 KiB = 2 MiB active chunk memory (< 35 MB RAM requirement).
	ParallelUploadWorkers = 4

	// MaxPartUploadRetries is the maximum retry count per 512 KiB chunk on FLOOD_WAIT or transient error.
	MaxPartUploadRetries = 5
)

var floodWaitRegex = regexp.MustCompile(`FLOOD_WAIT_([0-9]+)`)

// UploadReceipt summarizes a completed silent delivery into the user's TelegramDumpChat.
type UploadReceipt struct {
	TaskID        string           `json:"task_id"`
	PartsUploaded int              `json:"parts_uploaded"`
	MessageIDs    []int            `json:"message_ids"`
	TotalBytes    int64            `json:"total_bytes"`
	Banner        CompletionBanner `json:"banner"`
}

// Uploader orchestrates adaptive 2GB/4GB splitting, 4-worker 512 KiB chunk streaming,
// 16:9 poster generation, and silent delivery to the user's `TelegramDumpChat`.
type Uploader struct {
	client   *Client
	splitter *Splitter
	poster   *PosterGenerator
	notifier *Notifier
	bufPool  *buffer.Pool
	limiter  *safety.RateLimiter
	logger   *slog.Logger
}

// NewUploader constructs an Adaptive 2GB/4GB Parallel Stream Chunk Uploader.
func NewUploader(
	client *Client,
	splitter *Splitter,
	poster *PosterGenerator,
	notifier *Notifier,
	bufPool *buffer.Pool,
	limiter *safety.RateLimiter,
	logger *slog.Logger,
) *Uploader {
	if bufPool == nil {
		bufPool = buffer.DefaultPool
	}
	return &Uploader{
		client:   client,
		splitter: splitter,
		poster:   poster,
		notifier: notifier,
		bufPool:  bufPool,
		limiter:  limiter,
		logger:   logger,
	}
}

// UploadTask silently delivers `task.OutputPath` into the user's `task.TelegramDumpChat`:
//  1. Inspects the active MTProto session ceiling (2,000 MB Free vs 4,000 MB Premium).
//  2. Automatically splits oversized files via `mkvmerge --split size:1950M` (0% CPU transcoding).
//  3. Generates or attaches the 16:9 thumbnail and formatted caption (Templates 1..11).
//  4. Streams each file part in 512 KiB chunks across 4 parallel goroutines (`upload.saveBigFilePart`).
//  5. Emits `EventTaskCompleted` so Web Studio flashes the completion banner.
func (u *Uploader) UploadTask(ctx context.Context, task *models.Task, customThumbPath string) (*UploadReceipt, error) {
	if task == nil || strings.TrimSpace(task.OutputPath) == "" {
		return nil, errors.New("task or task output path is empty")
	}
	if strings.TrimSpace(task.TelegramDumpChat) == "" {
		return nil, ErrInvalidDumpChatID
	}

	peer, _, err := u.client.ResolveDumpChannel(ctx, task.TelegramDumpChat)
	if err != nil {
		return nil, err
	}

	ceilingBytes := u.client.UploadCeilingBytes()
	splitSizeMB := u.client.SafeSplitSizeMB()

	splitRes, err := u.splitter.EnsureWithinLimit(ctx, task.OutputPath, ceilingBytes, splitSizeMB)
	if err != nil {
		return nil, err
	}
	defer splitRes.Cleanup()

	// Prepare 16:9 thumbnail bytes once and upload per part.
	thumbBytes, thumbName, thumbErr := u.poster.ResolveOrRenderThumbnail(
		task.ID,
		task.PosterTemplate,
		customThumbPath,
	)
	if thumbErr != nil {
		u.logger.Warn("proceeding without custom thumbnail", slog.String("error", thumbErr.Error()))
	}

	var messageIDs []int
	var cumulativeBytes int64
	var grandTotalBytes int64
	for _, pt := range splitRes.Parts {
		grandTotalBytes += pt.SizeBytes
	}

	for _, part := range splitRes.Parts {
		caption := u.poster.FormatCaption(PosterCardInput{
			TemplateID:        task.PosterTemplate,
			Title:              firstNonEmpty(task.Metadata.Title, task.Name),
			Year:              task.Metadata.Year,
			Season:            task.Metadata.Season,
			Episode:           task.Metadata.Episode,
			Resolution:        task.Metadata.Resolution,
			VideoCodec:        task.Metadata.VideoCodec,
			AudioLanguages:    task.Metadata.AudioLanguages,
			SubtitleLanguages: task.Metadata.SubLanguages,
			FileSizeBytes:     part.SizeBytes,
			PartNumber:        part.PartNumber,
			TotalParts:        part.TotalParts,
		})

		msgID, err := u.uploadSinglePartFile(
			ctx,
			task,
			peer,
			part,
			cumulativeBytes,
			grandTotalBytes,
			thumbBytes,
			thumbName,
			caption,
		)
		if err != nil {
			return nil, fmt.Errorf("uploading part %d/%d (%s): %w",
				part.PartNumber, part.TotalParts, part.FileName, err)
		}

		cumulativeBytes += part.SizeBytes
		messageIDs = append(messageIDs, msgID)
	}

	lastMsgID := 0
	if len(messageIDs) > 0 {
		lastMsgID = messageIDs[len(messageIDs)-1]
	}

	banner := u.notifier.EmitTaskCompleted(task, lastMsgID, len(splitRes.Parts), grandTotalBytes)
	return &UploadReceipt{
		TaskID:        task.ID,
		PartsUploaded: len(splitRes.Parts),
		MessageIDs:    messageIDs,
		TotalBytes:    grandTotalBytes,
		Banner:        banner,
	}, nil
}

// uploadSinglePartFile streams `part.FilePath` in 512 KiB chunks over 4 parallel goroutines
// via `upload.saveBigFilePart` and posts the media silently to `peer`.
func (u *Uploader) uploadSinglePartFile(
	ctx context.Context,
	task *models.Task,
	peer *tg.InputPeerChannel,
	part SplitPart,
	baseUploadedBytes int64,
	grandTotalBytes int64,
	thumbBytes []byte,
	thumbName string,
	caption string,
) (int, error) {
	api, err := u.client.API()
	if err != nil {
		return 0, err
	}

	file, err := os.Open(part.FilePath)
	if err != nil {
		return 0, err
	}
	defer file.Close()

	totalParts := int((part.SizeBytes + MTProtoPartSize512KB - 1) / MTProtoPartSize512KB)
	if totalParts <= 0 {
		totalParts = 1
	}

	fileID := randomInt64()
	partIndices := make(chan int, totalParts)
	for idx := 0; idx < totalParts; idx++ {
		partIndices <- idx
	}
	close(partIndices)

	uploadCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	var partBytesUploaded atomic.Int64
	var wg sync.WaitGroup
	errCh := make(chan error, ParallelUploadWorkers)

	// Progress reporter every 500ms.
	progDone := make(chan struct{})
	go func() {
		defer close(progDone)
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		lastBytes := int64(0)
		lastTick := time.Now()
		for {
			select {
			case <-uploadCtx.Done():
				return
			case now := <-ticker.C:
				curr := partBytesUploaded.Load()
				dt := now.Sub(lastTick).Seconds()
				var bps int64
				if dt > 0 && curr >= lastBytes {
					bps = int64(float64(curr-lastBytes) / dt)
				}
				lastBytes = curr
				lastTick = now
				u.notifier.EmitUploadProgress(
					task,
					part.PartNumber,
					part.TotalParts,
					baseUploadedBytes+curr,
					grandTotalBytes,
					bps,
				)
			}
		}
	}()

	// Spawn 4 parallel chunk upload goroutines (total RAM < 4 * 512 KiB = 2 MiB).
	for w := 0; w < ParallelUploadWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			bufPtr := u.bufPool.Get()
			defer u.bufPool.Put(bufPtr)
			buf := *bufPtr

			for partIdx := range partIndices {
				if err := task.WaitIfPaused(uploadCtx); err != nil {
					select {
					case errCh <- err:
					default:
					}
					cancel()
					return
				}

				offset := int64(partIdx) * MTProtoPartSize512KB
				nr, rErr := file.ReadAt(buf, offset)
				if rErr != nil && !errors.Is(rErr, io.EOF) {
					select {
					case errCh <- rErr:
					default:
					}
					cancel()
					return
				}
				if nr == 0 {
					continue
				}

				if u.limiter != nil {
					if lErr := u.limiter.WaitUpload(uploadCtx, nr); lErr != nil {
						select {
						case errCh <- lErr:
						default:
						}
						cancel()
						return
					}
				}

				// Copy only the slice slice header referencing `buf[:nr]` for synchronous RPC call.
				chunkBytes := buf[:nr]
				if err := u.saveBigPartWithRetry(uploadCtx, api, fileID, partIdx, totalParts, chunkBytes); err != nil {
					select {
					case errCh <- err:
					default:
					}
					cancel()
					return
				}

				partBytesUploaded.Add(int64(nr))
			}
		}()
	}

	wg.Wait()
	cancel()
	<-progDone
	close(errCh)

	if err, ok := <-errCh; ok && err != nil {
		return 0, err
	}

	inputFile := &tg.InputFileBig{
		ID:    fileID,
		Parts: totalParts,
		Name:  part.FileName,
	}

	// Upload optional 16:9 JPEG thumbnail (< 256 KiB).
	var thumbInput tg.InputFileClass
	if len(thumbBytes) > 0 {
		if uploadedThumb, tErr := u.uploadSmallThumbnail(ctx, api, thumbBytes, thumbName); tErr == nil {
			thumbInput = uploadedThumb
		}
	}

	mediaDoc := &tg.InputMediaUploadedDocument{
		File:     inputFile,
		MimeType: detectVideoMimeType(part.FileName),
		Attributes: []tg.DocumentAttributeClass{
			&tg.DocumentAttributeFilename{FileName: part.FileName},
			&tg.DocumentAttributeVideo{
				SupportsStreaming: true,
				W:                 1920,
				H:                 1080,
			},
		},
	}
	if thumbInput != nil {
		mediaDoc.Thumb = thumbInput
		mediaDoc.SetFlags()
	}

	updatesClass, err := api.MessagesSendMedia(ctx, &tg.MessagesSendMediaRequest{
		Silent:   true, // 100% Silent Operation (Zero Notification Spam)
		Peer:     peer,
		Media:    mediaDoc,
		Message:  caption,
		RandomID: randomInt64(),
	})
	if err != nil {
		return 0, fmt.Errorf("sending media message to dump channel: %w", err)
	}

	return extractSentMessageID(updatesClass), nil
}

// saveBigPartWithRetry sends a single 512 KiB chunk via `upload.saveBigFilePart`,
// automatically honoring Telegram `FLOOD_WAIT_X` backoff responses.
func (u *Uploader) saveBigPartWithRetry(
	ctx context.Context,
	api *tg.Client,
	fileID int64,
	partIdx int,
	totalParts int,
	chunk []byte,
) error {
	for attempt := 0; attempt <= MaxPartUploadRetries; attempt++ {
		ok, err := api.UploadSaveBigFilePart(ctx, &tg.UploadSaveBigFilePartRequest{
			FileID:         fileID,
			FilePart:       partIdx,
			FileTotalParts: totalParts,
			Bytes:          chunk,
		})
		if err == nil && ok {
			return nil
		}

		waitDur := time.Duration((attempt+1)*750) * time.Millisecond
		if err != nil {
			if m := floodWaitRegex.FindStringSubmatch(err.Error()); len(m) == 2 {
				if sec, pErr := strconv.Atoi(m[1]); pErr == nil && sec > 0 {
					waitDur = time.Duration(sec+1) * time.Second
				}
			}
		}
		if attempt == MaxPartUploadRetries {
			return fmt.Errorf("upload.saveBigFilePart failed on part %d/%d: %w", partIdx, totalParts, err)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(waitDur):
		}
	}
	return nil
}

func (u *Uploader) uploadSmallThumbnail(
	ctx context.Context,
	api *tg.Client,
	thumbBytes []byte,
	thumbName string,
) (tg.InputFileClass, error) {
	thumbID := randomInt64()
	totalParts := (len(thumbBytes) + MTProtoPartSize512KB - 1) / MTProtoPartSize512KB
	for i := 0; i < totalParts; i++ {
		start := i * MTProtoPartSize512KB
		end := start + MTProtoPartSize512KB
		if end > len(thumbBytes) {
			end = len(thumbBytes)
		}
		_, err := api.UploadSaveFilePart(ctx, &tg.UploadSaveFilePartRequest{
			FileID:   thumbID,
			FilePart: i,
			Bytes:    thumbBytes[start:end],
		})
		if err != nil {
			return nil, err
		}
	}
	return &tg.InputFile{
		ID:          thumbID,
		Parts:       totalParts,
		Name:        thumbName,
		MD5Checksum: "",
	}, nil
}

func extractSentMessageID(u tg.UpdatesClass) int {
	if updates, ok := u.(*tg.Updates); ok {
		for _, up := range updates.Updates {
			if msgUp, ok := up.(*tg.UpdateNewChannelMessage); ok {
				if msg, ok := msgUp.Message.(*tg.Message); ok {
					return msg.ID
				}
			}
			if idUp, ok := up.(*tg.UpdateMessageID); ok {
				return idUp.ID
			}
		}
	}
	return 0
}

func detectVideoMimeType(fileName string) string {
	switch strings.ToLower(filepath.Ext(fileName)) {
	case ".mp4":
		return "video/mp4"
	case ".webm":
		return "video/webm"
	default:
		return "video/x-matroska"
	}
}

func firstNonEmpty(a, b string) string {
	if strings.TrimSpace(a) != "" {
		return strings.TrimSpace(a)
	}
	return strings.TrimSpace(b)
}

func randomInt64() int64 {
	var b [8]byte
	_, _ = rand.Read(b[:])
	val := int64(binary.LittleEndian.Uint64(b[:]) & 0x7fffffffffffffff)
	if val == 0 {
		return time.Now().UnixNano()
	}
	return val
}
