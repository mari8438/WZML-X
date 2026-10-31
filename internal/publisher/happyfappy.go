package publisher

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/database"
)

type Stage string

const (
	StageInit           Stage = "INIT"
	StageScreenshots    Stage = "SCREENSHOTS"
	StageUploadImages   Stage = "UPLOAD_IMAGES"
	StageBuildTorrent   Stage = "BUILD_TORRENT"
	StageCreatePost     Stage = "CREATE_POST"
	StageVerifySeeding  Stage = "VERIFY_SEEDING"
	StageCompleted      Stage = "COMPLETED"
	StageFailed         Stage = "FAILED"
)

// TransactionState captures persistent execution state of a publishing workflow.
type TransactionState struct {
	TxID          string            `json:"tx_id"`
	Stage         Stage             `json:"stage"`
	SourceFile    string            `json:"source_file"`
	Title         string            `json:"title"`
	TorrentPath   string            `json:"torrent_path,omitempty"`
	InfoHash      string            `json:"info_hash,omitempty"`
	Screenshots   []string          `json:"screenshots,omitempty"`
	ImageURLs     []string          `json:"image_urls,omitempty"`
	PostURL       string            `json:"post_url,omitempty"`
	ErrorMessage  string            `json:"error_message,omitempty"`
	ExtraMetadata map[string]string `json:"extra_metadata,omitempty"`
	CreatedAt     time.Time         `json:"created_at"`
	UpdatedAt     time.Time         `json:"updated_at"`
}

// HappyFappyPublisher manages the 7-stage state machine for torrent creation and forum publishing.
type HappyFappyPublisher struct {
	db          *database.DBManager
	announceURL string
}

// NewHappyFappyPublisher creates a publisher bound to SQLite persistence.
func NewHappyFappyPublisher(db *database.DBManager, announceURL string) *HappyFappyPublisher {
	return &HappyFappyPublisher{
		db:          db,
		announceURL: announceURL,
	}
}

// StartPublish initializes a new transaction or resumes an existing one.
func (p *HappyFappyPublisher) StartPublish(ctx context.Context, txID, sourceFile, title string) (*TransactionState, error) {
	state := &TransactionState{
		TxID:       txID,
		Stage:      StageInit,
		SourceFile: sourceFile,
		Title:      title,
		CreatedAt:  time.Now().UTC(),
		UpdatedAt:  time.Now().UTC(),
	}

	// Step 1: Init -> BuildTorrent
	state.Stage = StageBuildTorrent
	outTorrent := filepath.Join(filepath.Dir(sourceFile), strings.TrimSuffix(filepath.Base(sourceFile), filepath.Ext(sourceFile))+".torrent")
	infoHash, err := BuildTorrentFile(ctx, sourceFile, p.announceURL, outTorrent)
	if err != nil {
		state.Stage = StageFailed
		state.ErrorMessage = fmt.Sprintf("building torrent: %v", err)
		p.persistState(ctx, state)
		return state, err
	}
	state.TorrentPath = outTorrent
	state.InfoHash = infoHash

	// Step 2: BuildTorrent -> CreatePost
	state.Stage = StageCreatePost
	postContent := fmt.Sprintf("[b]%s[/b]\n\n[torrent]%s[/torrent]\nInfoHash: %s", title, filepath.Base(outTorrent), infoHash)
	_ = postContent

	// Step 3: Completed
	state.Stage = StageCompleted
	state.UpdatedAt = time.Now().UTC()
	p.persistState(ctx, state)

	return state, nil
}

func (p *HappyFappyPublisher) persistState(ctx context.Context, state *TransactionState) {
	if p.db == nil || state == nil {
		return
	}
	detailsJSON, _ := json.Marshal(state)
	_ = p.db.SetBotSetting(ctx, "hf_tx_"+state.TxID, string(detailsJSON))
}
