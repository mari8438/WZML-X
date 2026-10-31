package database

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
)

func TestDualPoolConcurrencyStress(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_db_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	dbPath := filepath.Join(tempDir, "test_dual_pool.db")
	mgr, err := NewDBManager(dbPath)
	if err != nil {
		t.Fatalf("failed to create DBManager: %v", err)
	}
	defer mgr.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	const numWorkers = 50
	var wg sync.WaitGroup
	wg.Add(numWorkers)

	errChan := make(chan error, numWorkers)

	for i := 0; i < numWorkers; i++ {
		workerID := i
		go func() {
			defer wg.Done()
			user := &models.User{
				ID:         fmt.Sprintf("user_%d", workerID),
				TelegramID: int64(100000 + workerID),
				Name:       fmt.Sprintf("Stress User %d", workerID),
				Email:      fmt.Sprintf("user%d@starfall.local", workerID),
				Settings:   models.DefaultUserSettings(),
			}

			// Write operation
			if err := mgr.UpsertUser(ctx, user); err != nil {
				errChan <- fmt.Errorf("worker %d upsert user failed: %w", workerID, err)
				return
			}

			// Another write operation (BotSetting)
			settingKey := fmt.Sprintf("setting_%d", workerID%5)
			if err := mgr.SetBotSetting(ctx, settingKey, map[string]int{"count": workerID}); err != nil {
				errChan <- fmt.Errorf("worker %d set bot setting failed: %w", workerID, err)
				return
			}

			// Read operation
			fetched, err := mgr.GetUser(ctx, user.ID)
			if err != nil {
				errChan <- fmt.Errorf("worker %d get user failed: %w", workerID, err)
				return
			}
			if fetched == nil || fetched.Name != user.Name {
				errChan <- fmt.Errorf("worker %d user mismatch: got %v", workerID, fetched)
				return
			}

			// Seen items write & read
			if err := mgr.MarkItemSeen(ctx, "tamilmv", fmt.Sprintf("item_%d", workerID), "Movie Title", "https://url"); err != nil {
				errChan <- fmt.Errorf("worker %d mark seen failed: %w", workerID, err)
				return
			}
			seen, err := mgr.IsItemSeen(ctx, "tamilmv", fmt.Sprintf("item_%d", workerID))
			if err != nil || !seen {
				errChan <- fmt.Errorf("worker %d expected item seen, err=%v, seen=%v", workerID, err, seen)
				return
			}
		}()
	}

	wg.Wait()
	close(errChan)

	for err := range errChan {
		t.Fatalf("concurrency stress error (SQLITE_BUSY detected?): %v", err)
	}

	// Verify all 50 users exist
	users, err := mgr.ListUsers(ctx)
	if err != nil {
		t.Fatalf("failed to list users: %v", err)
	}
	if len(users) != numWorkers {
		t.Fatalf("expected %d users, got %d", numWorkers, len(users))
	}
}

func TestSyncOutboxQueue(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_db_outbox_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	dbPath := filepath.Join(tempDir, "test_outbox.db")
	mgr, err := NewDBManager(dbPath)
	if err != nil {
		t.Fatalf("failed to create DBManager: %v", err)
	}
	defer mgr.Close()

	ctx := context.Background()

	// Insert 3 users which queue 3 outbox items
	for i := 1; i <= 3; i++ {
		u := &models.User{
			ID:   fmt.Sprintf("ob_user_%d", i),
			Name: fmt.Sprintf("Outbox User %d", i),
		}
		if err := mgr.UpsertUser(ctx, u); err != nil {
			t.Fatalf("failed to upsert user: %v", err)
		}
	}

	items, err := mgr.FetchOutboxBatch(ctx, 10)
	if err != nil {
		t.Fatalf("failed to fetch outbox batch: %v", err)
	}
	if len(items) != 3 {
		t.Fatalf("expected 3 outbox items, got %d", len(items))
	}

	// Ack first 2
	ackedIDs := []int64{items[0].ID, items[1].ID}
	if err := mgr.DeleteOutboxBatch(ctx, ackedIDs); err != nil {
		t.Fatalf("failed to delete outbox batch: %v", err)
	}

	remaining, err := mgr.FetchOutboxBatch(ctx, 10)
	if err != nil {
		t.Fatalf("failed to fetch remaining outbox: %v", err)
	}
	if len(remaining) != 1 || remaining[0].DocID != "ob_user_3" {
		t.Fatalf("expected remaining item to be ob_user_3, got %v", remaining)
	}
}

func TestTNPSCPYQFTS(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_db_fts_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	dbPath := filepath.Join(tempDir, "test_fts.db")
	mgr, err := NewDBManager(dbPath)
	if err != nil {
		t.Fatalf("failed to create DBManager: %v", err)
	}
	defer mgr.Close()

	ctx := context.Background()

	record1 := &PYQRecord{
		QuestionID:  "q101",
		Year:        "2023",
		ExamName:    "Group 4",
		Subject:     "Tamil Heritage",
		Question:    "Who authored the famous classical work Thirukkural in Tamil literature?",
		OptionsJSON: []string{"Thiruvalluvar", "Avvaiyar", "Kambar", "Bharathiyar"},
		Answer:      "Thiruvalluvar",
		Explanation: "Thiruvalluvar is celebrated as the venerable poet and philosopher.",
	}
	record2 := &PYQRecord{
		QuestionID:  "q102",
		Year:        "2022",
		ExamName:    "Group 2",
		Subject:     "Indian Constitution",
		Question:    "Which Article of the Indian Constitution pertains to the abolition of Untouchability?",
		OptionsJSON: []string{"Article 14", "Article 17", "Article 19", "Article 21"},
		Answer:      "Article 17",
		Explanation: "Article 17 of the Constitution explicitly abolishes Untouchability.",
	}

	if err := mgr.IndexPYQ(ctx, record1); err != nil {
		t.Fatalf("failed to index pyq 1: %v", err)
	}
	if err := mgr.IndexPYQ(ctx, record2); err != nil {
		t.Fatalf("failed to index pyq 2: %v", err)
	}

	// Search for "Thirukkural"
	results, err := mgr.SearchPYQ(ctx, "Thirukkural", 5)
	if err != nil {
		t.Fatalf("failed to search pyq: %v", err)
	}
	if len(results) != 1 || results[0].QuestionID != "q101" {
		t.Fatalf("expected q101 for Thirukkural query, got %v", results)
	}

	// Search for "Untouchability"
	results2, err := mgr.SearchPYQ(ctx, "Untouchability", 5)
	if err != nil {
		t.Fatalf("failed to search pyq: %v", err)
	}
	if len(results2) != 1 || results2[0].QuestionID != "q102" {
		t.Fatalf("expected q102 for Untouchability query, got %v", results2)
	}
}
