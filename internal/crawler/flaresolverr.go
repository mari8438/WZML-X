package crawler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

var (
	ErrSolverTimeout     = errors.New("flaresolverr: challenge solving timed out")
	ErrCloudflareBlocked = errors.New("flaresolverr: cloudflare challenge could not be bypassed")
)

// FlareSolverrClient manages interactions with a local or remote FlareSolverr daemon,
// strictly enforcing single-session serialization to avoid memory spikes on the 4 GB VPS.
type FlareSolverrClient struct {
	endpoint   string
	httpClient *http.Client
	sessionMu  sync.Mutex // Enforce at most 1 in-flight FlareSolverr browser session
}

// NewFlareSolverrClient initializes a FlareSolverr client.
func NewFlareSolverrClient(endpoint string) *FlareSolverrClient {
	endpoint = strings.TrimRight(endpoint, "/")
	if endpoint == "" {
		endpoint = "http://127.0.0.1:8191/v1"
	}
	return &FlareSolverrClient{
		endpoint: endpoint,
		httpClient: &http.Client{
			Timeout: 60 * time.Second,
		},
	}
}

// FetchClearedHTML acquires a browser session, retrieves the Cloudflare-cleared DOM,
// and ensures the headless browser tab is destroyed even if parent context cancels.
func (fc *FlareSolverrClient) FetchClearedHTML(parentCtx context.Context, targetURL string) (string, error) {
	fc.sessionMu.Lock()
	defer fc.sessionMu.Unlock()

	ctx, cancel := context.WithTimeout(parentCtx, 50*time.Second)
	defer cancel()

	// 1. Create isolated session
	createPayload := map[string]string{"cmd": "sessions.create"}
	sessionID, err := fc.executeCommand(ctx, createPayload)
	if err != nil {
		return "", fmt.Errorf("creating flaresolverr session: %w", err)
	}

	// 2. Ensure session destruction on exit with detached context
	defer func() {
		destroyCtx, dCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer dCancel()
		_, _ = fc.executeCommand(destroyCtx, map[string]string{
			"cmd":     "sessions.destroy",
			"session": sessionID,
		})
	}()

	// 3. Request page
	reqPayload := map[string]any{
		"cmd":        "request.get",
		"url":        targetURL,
		"session":    sessionID,
		"maxTimeout": 40000,
	}

	rawJSON, err := json.Marshal(reqPayload)
	if err != nil {
		return "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fc.endpoint, bytes.NewReader(rawJSON))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := fc.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("flaresolverr request failed: %w", err)
	}
	defer resp.Body.Close()

	var result struct {
		Status   string `json:"status"`
		Message  string `json:"message"`
		Solution struct {
			Response string `json:"response"`
			Status   int    `json:"status"`
		} `json:"solution"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decoding flaresolverr response: %w", err)
	}

	if result.Status != "ok" {
		return "", fmt.Errorf("%w: %s", ErrCloudflareBlocked, result.Message)
	}

	return result.Solution.Response, nil
}

func (fc *FlareSolverrClient) executeCommand(ctx context.Context, payload any) (string, error) {
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fc.endpoint, bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := fc.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var res struct {
		Status  string `json:"status"`
		Session string `json:"session"`
		Message string `json:"message"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		return "", err
	}
	if res.Status != "ok" {
		return "", errors.New(res.Message)
	}
	return res.Session, nil
}
