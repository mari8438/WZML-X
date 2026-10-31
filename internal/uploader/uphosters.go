package uploader

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

var (
	ErrUploadFailed = errors.New("uphoster: file upload rejected by remote server")
)

// Uphoster defines the interface for cloud cyberlocker file uploads.
type Uphoster interface {
	Upload(ctx context.Context, filePath string, onProgress ProgressFunc) (*UploadResult, error)
}

// GoFileClient uploads to gofile.io.
type GoFileClient struct {
	client *http.Client
	token  string
}

// NewGoFileClient initializes a GoFile uphoster client.
func NewGoFileClient(token string) *GoFileClient {
	return &GoFileClient{
		client: &http.Client{Timeout: 30 * time.Minute},
		token:  token,
	}
}

// Upload streams filePath to GoFile using exact Content-Length framing.
func (g *GoFileClient) Upload(ctx context.Context, filePath string, onProgress ProgressFunc) (*UploadResult, error) {
	// 1. Fetch available server
	serverReq, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.gofile.io/servers", nil)
	if err != nil {
		return nil, err
	}
	sResp, err := g.client.Do(serverReq)
	if err != nil {
		return nil, fmt.Errorf("getting gofile server: %w", err)
	}
	defer sResp.Body.Close()

	var sData struct {
		Status string `json:"status"`
		Data   struct {
			Servers []struct {
				Name string `json:"name"`
			} `json:"servers"`
		} `json:"data"`
	}
	_ = json.NewDecoder(sResp.Body).Decode(&sData)
	serverName := "store1"
	if len(sData.Data.Servers) > 0 {
		serverName = sData.Data.Servers[0].Name
	}

	uploadURL := fmt.Sprintf("https://%s.gofile.io/contents/uploadfile", serverName)
	extra := map[string]string{}
	if g.token != "" {
		extra["token"] = g.token
	}

	req, err := NewDeterministicMultipartRequest(ctx, uploadURL, "file", filePath, extra, onProgress)
	if err != nil {
		return nil, err
	}

	resp, err := g.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("gofile upload request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%w: status %d", ErrUploadFailed, resp.StatusCode)
	}

	var resData struct {
		Status string `json:"status"`
		Data   struct {
			DownloadPage string `json:"downloadPage"`
			FileID       string `json:"fileId"`
			FileName     string `json:"fileName"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&resData); err != nil {
		return nil, fmt.Errorf("decoding gofile response: %w", err)
	}

	fi, _ := os.Stat(filePath)
	var sz int64
	if fi != nil {
		sz = fi.Size()
	}

	return &UploadResult{
		ServiceName: "GoFile",
		DownloadURL: resData.Data.DownloadPage,
		FileID:      resData.Data.FileID,
		FileName:    filepath.Base(filePath),
		FileSize:    sz,
	}, nil
}

// PixeldrainClient uploads to pixeldrain.com via PUT streaming.
type PixeldrainClient struct {
	client *http.Client
	apiKey string
}

// NewPixeldrainClient initializes a Pixeldrain uphoster client.
func NewPixeldrainClient(apiKey string) *PixeldrainClient {
	return &PixeldrainClient{
		client: &http.Client{Timeout: 30 * time.Minute},
		apiKey: apiKey,
	}
}

// Upload streams filePath to Pixeldrain with exact Content-Length.
func (p *PixeldrainClient) Upload(ctx context.Context, filePath string, onProgress ProgressFunc) (*UploadResult, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	st, err := file.Stat()
	if err != nil {
		return nil, err
	}

	fileName := filepath.Base(filePath)
	uploadURL := fmt.Sprintf("https://pixeldrain.com/api/file/%s", fileName)

	var bodyReader io.Reader = file
	if onProgress != nil {
		bodyReader = &progressReader{
			reader:     file,
			total:      st.Size(),
			onProgress: onProgress,
		}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPut, uploadURL, bodyReader)
	if err != nil {
		return nil, err
	}
	req.ContentLength = st.Size()
	if p.apiKey != "" {
		req.SetBasicAuth("", p.apiKey)
	}

	resp, err := p.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("pixeldrain upload request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("%w: status %d", ErrUploadFailed, resp.StatusCode)
	}

	var pData struct {
		ID      string `json:"id"`
		Success bool   `json:"success"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&pData); err != nil {
		return nil, fmt.Errorf("decoding pixeldrain response: %w", err)
	}

	return &UploadResult{
		ServiceName: "Pixeldrain",
		DownloadURL: fmt.Sprintf("https://pixeldrain.com/u/%s", pData.ID),
		FileID:      pData.ID,
		FileName:    fileName,
		FileSize:    st.Size(),
	}, nil
}

// PixhostImageClient uploads screenshots to pixhost.to.
type PixhostImageClient struct {
	client *http.Client
}

// NewPixhostImageClient creates a Pixhost client.
func NewPixhostImageClient() *PixhostImageClient {
	return &PixhostImageClient{
		client: &http.Client{Timeout: 30 * time.Second},
	}
}

// UploadImage uploads an image to Pixhost and returns the direct viewer URL.
func (px *PixhostImageClient) UploadImage(ctx context.Context, imgPath string) (string, error) {
	req, err := NewDeterministicMultipartRequest(
		ctx,
		"https://api.pixhost.to/images",
		"img",
		imgPath,
		map[string]string{"content_type": "0", "max_th_size": "420"},
		nil,
	)
	if err != nil {
		return "", err
	}

	resp, err := px.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("pixhost request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("pixhost returned status %d", resp.StatusCode)
	}

	var data struct {
		ShowURL string `json:"show_url"`
		THURL   string `json:"th_url"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return "", err
	}
	if data.ShowURL != "" {
		return data.ShowURL, nil
	}
	return data.THURL, nil
}
