package uploader

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
)

// UploadResult contains the public download link and file metadata returned by an uphoster.
type UploadResult struct {
	ServiceName string `json:"service_name"`
	DownloadURL string `json:"download_url"`
	FileID      string `json:"file_id,omitempty"`
	FileName    string `json:"file_name"`
	FileSize    int64  `json:"file_size"`
}

// ProgressFunc reports streaming upload progress.
type ProgressFunc func(bytesUploaded, totalBytes int64)

type progressReader struct {
	reader     io.Reader
	total      int64
	uploaded   int64
	onProgress ProgressFunc
}

func (pr *progressReader) Read(p []byte) (int, error) {
	n, err := pr.reader.Read(p)
	if n > 0 {
		pr.uploaded += int64(n)
		if pr.onProgress != nil {
			pr.onProgress(pr.uploaded, pr.total)
		}
	}
	return n, err
}

// NewDeterministicMultipartRequest creates an HTTP POST request with an EXACT Content-Length header
// by concatenating preamble, raw file handle, and epilogue using io.MultiReader.
// This prevents HTTP 411 Length Required errors on cyberlockers (GoFile, VikingFile, DevUploads).
func NewDeterministicMultipartRequest(
	ctx context.Context,
	uploadURL string,
	fileFieldName string,
	filePath string,
	extraFields map[string]string,
	onProgress ProgressFunc,
) (*http.Request, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("opening upload file: %w", err)
	}

	st, err := file.Stat()
	if err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("statting upload file: %w", err)
	}
	fileSize := st.Size()

	var preambleBuf bytes.Buffer
	mw := multipart.NewWriter(&preambleBuf)

	// Write extra text form fields
	for k, v := range extraFields {
		if err := mw.WriteField(k, v); err != nil {
			_ = file.Close()
			return nil, fmt.Errorf("writing field %s: %w", k, err)
		}
	}

	// Create file header boundary
	fileName := filepath.Base(filePath)
	_, err = mw.CreateFormFile(fileFieldName, fileName)
	if err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("creating form file boundary: %w", err)
	}

	// Capture boundary preamble up to the raw file bytes
	preamble := preambleBuf.Bytes()

	// Calculate epilogue boundary
	var epilogueBuf bytes.Buffer
	epilogueMw := multipart.NewWriter(&epilogueBuf)
	if err := epilogueMw.SetBoundary(mw.Boundary()); err != nil {
		_ = file.Close()
		return nil, err
	}
	_ = epilogueMw.Close()
	epilogue := []byte("\r\n" + epilogueBuf.String())

	totalLength := int64(len(preamble)) + fileSize + int64(len(epilogue))

	combined := io.MultiReader(
		bytes.NewReader(preamble),
		file,
		bytes.NewReader(epilogue),
	)

	var reqBody io.Reader = combined
	if onProgress != nil {
		reqBody = &progressReader{
			reader:     combined,
			total:      totalLength,
			onProgress: onProgress,
		}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, uploadURL, reqBody)
	if err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("creating upload request: %w", err)
	}

	req.ContentLength = totalLength
	req.Header.Set("Content-Type", mw.FormDataContentType())

	return req, nil
}
