// Package telegram implements the Adaptive 2GB/4GB Parallel Stream Chunk Uploader (`uploader.go`)
// supporting Helper Bot Pool rotation (<2000 MB), Premium User Session routing (>2000 MB..4000 MB),
// Forum Topic (`-100xxxx|thread_id`) delivery, standalone 16:9 Poster Photo posts with
// `<blockquote expandable>` HTML entities, `AS_DOCUMENT`, and `LEECH_FONT` file captions.
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
	// MTProtoPartSize512KB is the exact 512 KiB (524,288 bytes) part size required by `upload.saveBigFilePart`.
	MTProtoPartSize512KB = 512 * 1024

	// ParallelUploadWorkers sets concurrent goroutines uploading 512 KiB parts (4 * 512 KiB = 2 MiB active heap).
	ParallelUploadWorkers = 4

	// MaxPartUploadRetries is the maximum retry count per 512 KiB chunk on FLOOD_WAIT or transient error.
	MaxPartUploadRetries = 5
)

var floodWaitRegex = regexp.MustCompile(`FLOOD_WAIT_([0-9]+)`)

// UploadReceipt summarizes a completed delivery into the user's TelegramDumpChat.
type UploadReceipt struct {
	TaskID        string           `json:"task_id"`
	PartsUploaded int              `json:"parts_uploaded"`
	MessageIDs    []int            `json:"message_ids"`
	TotalBytes    int64            `json:"total_bytes"`
	Banner        CompletionBanner `json:"banner"`
}

// Uploader orchestrates adaptive 2GB/4GB splitting, Helper Pool / Premium session selection,
// 4-worker 512 KiB chunk streaming, 16:9 poster generation, and forum-aware delivery.
type Uploader struct {
	client        *Client
	premiumClient *Client
	helperPool    *HelperPool
	splitter      *Splitter
	poster        *PosterGenerator
	notifier      *Notifier
	bufPool       *buffer.Pool
	limiter       *safety.RateLimiter
	logger        *slog.Logger
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
	if logger == nil {
		logger = slog.Default()
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

// SetPremiumClient attaches an optional Telegram Premium User session for >2000 MB..4000 MB unsplit uploads.
func (u *Uploader) SetPremiumClient(cli *Client) {
	u.premiumClient = cli
}

// SetHelperPool attaches an optional multi-bot HelperPool for parallel <2000 MB uploads.
func (u *Uploader) SetHelperPool(pool *HelperPool) {
	u.helperPool = pool
}

// Poster returns the attached PosterGenerator.
func (u *Uploader) Poster() *PosterGenerator {
	return u.poster
}

// IsReady reports whether at least one MTProto session is connected for upload.
func (u *Uploader) IsReady() bool {
	if u == nil {
		return false
	}
	return (u.client != nil && u.client.IsConnected()) ||
		(u.premiumClient != nil && u.premiumClient.IsConnected()) ||
		(u.helperPool != nil && u.helperPool.ConnectedCount() > 0)
}

// UploadTask retains compatibility for single-file task uploads with default settings.
func (u *Uploader) UploadTask(ctx context.Context, task *models.Task, customThumbPath string) (*UploadReceipt, error) {
	if task == nil {
		return nil, errors.New("nil task")
	}
	settings := models.DefaultUserSettings()
	settings.PostTemplateID = task.PosterTemplate
	settings.LeechDumpChat = task.TelegramDumpChat
	return u.UploadDeliverable(ctx, task, task.OutputPath, task.GetMetadata(), settings, customThumbPath, true)
}

// UploadDeliverable executes the full StarfallX upload flow for a processed deliverable file:
//  1. Selects Premium User Session (>2GB..4GB) or Helper/Primary Bot Session (<=2GB).
//  2. Enforces `LEECH_SPLIT_SIZE` & session ceiling via `Splitter.EnsureWithinLimit`.
//  3. Optionally sends standalone 1280x720 Poster Photo + `<blockquote expandable>` HTML caption.
//  4. Uploads all split parts in 512 KiB chunks across 4 parallel workers with `LEECH_FONT` HTML captions.
func (u *Uploader) UploadDeliverable(
	ctx context.Context,
	task *models.Task,
	filePath string,
	md models.MediaMetadata,
	settings models.UserSettings,
	customThumbPath string,
	emitFinalBanner bool,
) (*UploadReceipt, error) {
	if task == nil || strings.TrimSpace(filePath) == "" {
		return nil, errors.New("task or file path is empty")
	}
	targetChat := strings.TrimSpace(task.TelegramDumpChat)
	if targetChat == "" {
		targetChat = strings.TrimSpace(settings.LeechDumpChat)
	}
	if targetChat == "" {
		return nil, ErrInvalidDumpChatID
	}

	fi, err := os.Stat(filePath)
	if err != nil {
		return nil, fmt.Errorf("stat deliverable %s: %w", filepath.Base(filePath), err)
	}

	activeCli := u.selectClientForFile(fi.Size(), settings)
	if activeCli == nil || !activeCli.IsConnected() {
		return nil, ErrClientNotConnected
	}

	peer, _, threadID, err := activeCli.ResolveDumpTarget(ctx, targetChat)
	if err != nil {
		// Fallback to primary client if helper hasn't seen channel access hash yet
		if activeCli != u.client && u.client != nil && u.client.IsConnected() {
			activeCli = u.client
			peer, _, threadID, err = activeCli.ResolveDumpTarget(ctx, targetChat)
		}
		if err != nil {
			return nil, err
		}
	}

	// Determine effective ceiling and split size
	ceilingBytes := activeCli.UploadCeilingBytes()
	splitSizeMB := activeCli.SafeSplitSizeMB()
	if settings.LeechSplitSize > 0 && settings.LeechSplitSize < ceilingBytes {
		ceilingBytes = settings.LeechSplitSize
		userSplitMB := int(settings.LeechSplitSize/(1024*1024)) - 20
		if userSplitMB >= 100 && userSplitMB < splitSizeMB {
			splitSizeMB = userSplitMB
		}
	}

	splitRes, err := u.splitter.EnsureWithinLimit(ctx, filePath, ceilingBytes, splitSizeMB)
	if err != nil {
		return nil, err
	}
	defer splitRes.Cleanup()

	// Enrich metadata & render 1280x720 Poster Card (`AUTO_POSTER_ENABLED`)
	var posterBytes []byte
	if u.poster != nil && settings.AutoPosterEnabled {
		md = u.poster.EnrichMetadata(ctx, md)
		task.SetMetadata(md)
		tplID := settings.PostTemplateID
		if tplID < models.MinPosterTemplate || tplID > models.MaxPosterTemplate {
			tplID = task.PosterTemplate
		}
		if pb, perr := u.poster.RenderPosterCard(ctx, md, settings, tplID); perr == nil {
			posterBytes = pb
		}
	}

	// Send standalone 1280x720 Poster Photo + `<blockquote expandable>` HTML caption once per batch!
	if u.poster != nil && settings.AutoPosterEnabled && settings.AutoPosterSendMessage && len(posterBytes) > 0 {
		batchKey := task.BatchKey
		if batchKey == "" {
			batchKey = task.ID
		}
		if u.poster.ShouldSendStandalonePoster(batchKey) {
			posterCaptionHTML := u.poster.FormatExpandableCaption(md, settings)
			if _, perr := u.sendStandalonePosterPhoto(ctx, activeCli, peer, threadID, posterBytes, posterCaptionHTML); perr != nil {
				u.logger.Warn("standalone poster message failed; continuing with file upload", slog.String("error", perr.Error()))
			}
		}
	}

	// Resolve 16:9 thumbnail for the video/document upload
	var thumbBytes []byte
	var thumbName string
	if u.poster != nil {
		tb, tn, terr := u.poster.ResolveThumbnailForDelivery(
			ctx,
			task.ID,
			filePath,
			md.DurationSec,
			posterBytes,
			md,
			settings,
			customThumbPath,
		)
		if terr == nil {
			thumbBytes = tb
			thumbName = tn
		}
	}

	var messageIDs []int
	var cumulativeBytes int64
	var grandTotalBytes int64
	for _, pt := range splitRes.Parts {
		grandTotalBytes += pt.SizeBytes
	}

	for _, part := range splitRes.Parts {
		fileCapHTML := part.FileName
		if u.poster != nil {
			fileCapHTML = u.poster.FormatFileUploadCaption(part.FileName, settings, part.PartNumber, part.TotalParts)
		}

		msgID, err := u.uploadSinglePartFile(
			ctx,
			activeCli,
			task,
			peer,
			threadID,
			part,
			md,
			settings.AsDocument,
			cumulativeBytes,
			grandTotalBytes,
			thumbBytes,
			thumbName,
			fileCapHTML,
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

	if settings.LeechCompleteMsg && lastMsgID > 0 {
		doneText := fmt.Sprintf("<b>✅ Leech Completed</b>\n<b>File:</b> <code>%s</code>\n<b>Size:</b> <code>%s</code>",
			partBaseEscape(filepath.Base(filePath)), formatUploadBytes(grandTotalBytes))
		_ = u.sendHTMLText(ctx, activeCli, peer, threadID, doneText)
	}

	var banner CompletionBanner
	if emitFinalBanner && u.notifier != nil {
		banner = u.notifier.EmitTaskCompleted(task, lastMsgID, len(splitRes.Parts), grandTotalBytes)
	}

	return &UploadReceipt{
		TaskID:        task.ID,
		PartsUploaded: len(splitRes.Parts),
		MessageIDs:    messageIDs,
		TotalBytes:    grandTotalBytes,
		Banner:        banner,
	}, nil
}

func (u *Uploader) selectClientForFile(sizeBytes int64, settings models.UserSettings) *Client {
	// Route >2000 MB files to Telegram Premium user session when available (`USER_TRANSMISSION` / Hybrid)
	if sizeBytes > FreeUploadCeilingBytes && u.premiumClient != nil && u.premiumClient.IsConnected() {
		if u.premiumClient.UploadCeilingBytes() >= sizeBytes || settings.UserTransmission || settings.HybridLeech {
			return u.premiumClient
		}
	}
	if u.helperPool != nil {
		if picked := u.helperPool.PickClient(u.client); picked != nil && picked.IsConnected() {
			return picked
		}
	}
	if u.client != nil && u.client.IsConnected() {
		return u.client
	}
	if u.premiumClient != nil && u.premiumClient.IsConnected() {
		return u.premiumClient
	}
	return u.client
}

func (u *Uploader) sendStandalonePosterPhoto(
	ctx context.Context,
	cli *Client,
	peer *tg.InputPeerChannel,
	threadID int,
	posterBytes []byte,
	captionHTML string,
) (int, error) {
	api, err := cli.API()
	if err != nil {
		return 0, err
	}
	uploadedPhoto, err := u.uploadSmallThumbnail(ctx, api, posterBytes, "starfall_poster.jpg")
	if err != nil {
		return 0, err
	}
	plainText, entities := ParseHTMLMessage(captionHTML)
	req := &tg.MessagesSendMediaRequest{
		Silent:   true,
		Peer:     peer,
		Media:    &tg.InputMediaUploadedPhoto{File: uploadedPhoto},
		Message:  plainText,
		Entities: entities,
		RandomID: randomInt64(),
	}
	if threadID > 0 {
		req.ReplyTo = &tg.InputReplyToMessage{ReplyToMsgID: threadID}
	}
	req.SetFlags()

	upd, err := api.MessagesSendMedia(ctx, req)
	if err != nil {
		return 0, err
	}
	return extractSentMessageID(upd), nil
}

func (u *Uploader) sendHTMLText(
	ctx context.Context,
	cli *Client,
	peer *tg.InputPeerChannel,
	threadID int,
	htmlText string,
) error {
	api, err := cli.API()
	if err != nil {
		return err
	}
	plain, entities := ParseHTMLMessage(htmlText)
	req := &tg.MessagesSendMessageRequest{
		Silent:   true,
		Peer:     peer,
		Message:  plain,
		Entities: entities,
		RandomID: randomInt64(),
	}
	if threadID > 0 {
		req.ReplyTo = &tg.InputReplyToMessage{ReplyToMsgID: threadID}
	}
	req.SetFlags()
	_, err = api.MessagesSendMessage(ctx, req)
	return err
}

func (u *Uploader) uploadSinglePartFile(
	ctx context.Context,
	cli *Client,
	task *models.Task,
	peer *tg.InputPeerChannel,
	threadID int,
	part SplitPart,
	md models.MediaMetadata,
	asDocument bool,
	baseUploadedBytes int64,
	grandTotalBytes int64,
	thumbBytes []byte,
	thumbName string,
	captionHTML string,
) (int, error) {
	api, err := cli.API()
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
				if u.notifier != nil {
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
		}
	}()

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

				chunkBytes := buf[:nr]
				if err := u.saveBigPartWithRetry(uploadCtx, cli, api, fileID, partIdx, totalParts, chunkBytes); err != nil {
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

	var thumbInput tg.InputFileClass
	if len(thumbBytes) > 0 {
		if uploadedThumb, tErr := u.uploadSmallThumbnail(ctx, api, thumbBytes, thumbName); tErr == nil {
			thumbInput = uploadedThumb
		}
	}

	attrs := []tg.DocumentAttributeClass{
		&tg.DocumentAttributeFilename{FileName: part.FileName},
	}
	if !asDocument && isStreamableVideoExt(part.FileName) {
		vidW, vidH := 1920, 1080
		switch md.Resolution {
		case "2160p":
			vidW, vidH = 3840, 2160
		case "720p":
			vidW, vidH = 1280, 720
		case "480p":
			vidW, vidH = 854, 480
		}
		attrs = append(attrs, &tg.DocumentAttributeVideo{
			SupportsStreaming: true,
			Duration:          md.DurationSec,
			W:                 vidW,
			H:                 vidH,
		})
	}

	mediaDoc := &tg.InputMediaUploadedDocument{
		ForceFile: asDocument,
		File:      inputFile,
		MimeType:  detectMimeType(part.FileName),
		Attributes: attrs,
	}
	if thumbInput != nil {
		mediaDoc.Thumb = thumbInput
	}
	mediaDoc.SetFlags()

	plainCaption, entities := ParseHTMLMessage(captionHTML)
	sendReq := &tg.MessagesSendMediaRequest{
		Silent:   true,
		Peer:     peer,
		Media:    mediaDoc,
		Message:  plainCaption,
		Entities: entities,
		RandomID: randomInt64(),
	}
	if threadID > 0 {
		sendReq.ReplyTo = &tg.InputReplyToMessage{ReplyToMsgID: threadID}
	}
	sendReq.SetFlags()

	updatesClass, err := api.MessagesSendMedia(ctx, sendReq)
	if err != nil {
		return 0, fmt.Errorf("sending media message to dump channel: %w", err)
	}

	return extractSentMessageID(updatesClass), nil
}

func (u *Uploader) saveBigPartWithRetry(
	ctx context.Context,
	cli *Client,
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
					if u.helperPool != nil {
						u.helperPool.MarkFloodWait(cli, sec)
					}
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
	if strings.TrimSpace(thumbName) == "" {
		thumbName = "thumb.jpg"
	}
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

func isStreamableVideoExt(fileName string) bool {
	switch strings.ToLower(filepath.Ext(fileName)) {
	case ".mp4", ".mkv", ".webm", ".mov", ".m4v":
		return true
	default:
		return false
	}
}

func detectMimeType(fileName string) string {
	switch strings.ToLower(filepath.Ext(fileName)) {
	case ".mp4", ".m4v":
		return "video/mp4"
	case ".webm":
		return "video/webm"
	case ".mkv":
		return "video/x-matroska"
	case ".zip":
		return "application/zip"
	case ".7z":
		return "application/x-7z-compressed"
	case ".rar":
		return "application/vnd.rar"
	case ".srt":
		return "application/x-subrip"
	default:
		return "application/octet-stream"
	}
}

func partBaseEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;")
	return r.Replace(s)
}

func formatUploadBytes(b int64) string {
	const unit = 1024
	if b < unit {
		return fmt.Sprintf("%d B", b)
	}
	div, exp := int64(unit), 0
	for n := b / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.2f %ciB", float64(b)/float64(div), "KMGTPE"[exp])
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
