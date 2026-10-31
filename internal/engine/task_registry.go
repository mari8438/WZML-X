// Package engine implements the core task registry and worker-pool dispatcher
// for Starfall-Go Debrid.
package engine

import (
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
)

var (
	// ErrTaskAlreadyExists is returned when registering a duplicate task ID.
	ErrTaskAlreadyExists = errors.New("task ID already registered")

	// ErrTaskNotFound is returned when querying an unknown task ID.
	ErrTaskNotFound = errors.New("task not found in registry")
)

// TaskRegistry maintains a lock-free / thread-safe sync.Map of active and recently
// completed tasks with atomic counters to avoid O(N) scans on hot paths.
type TaskRegistry struct {
	tasks       sync.Map // map[string]*models.Task
	activeCount atomic.Int32
	totalCount  atomic.Int32
}

// NewTaskRegistry creates an empty, thread-safe TaskRegistry.
func NewTaskRegistry() *TaskRegistry {
	return &TaskRegistry{}
}

// Register inserts a new Task into the registry if its ID is not already present.
func (r *TaskRegistry) Register(task *models.Task) error {
	if task == nil || task.ID == "" {
		return errors.New("invalid task or empty task ID")
	}
	if _, loaded := r.tasks.LoadOrStore(task.ID, task); loaded {
		return ErrTaskAlreadyExists
	}
	r.totalCount.Add(1)
	if !task.Status().IsTerminal() {
		r.activeCount.Add(1)
	}
	return nil
}

// Get retrieves a Task pointer by ID.
func (r *TaskRegistry) Get(taskID string) (*models.Task, error) {
	val, ok := r.tasks.Load(taskID)
	if !ok {
		return nil, ErrTaskNotFound
	}
	return val.(*models.Task), nil
}

// UpdateStatus transitions a registered task's status and maintains the active task counter.
func (r *TaskRegistry) UpdateStatus(taskID string, next models.TaskStatus, errMsg string) (*models.Task, error) {
	task, err := r.Get(taskID)
	if err != nil {
		return nil, err
	}
	prev := task.Status()
	task.SetStatus(next, errMsg)
	if !prev.IsTerminal() && next.IsTerminal() {
		r.activeCount.Add(-1)
	}
	return task, nil
}

// Remove deletes a task from the sync.Map and decrements counters accordingly.
func (r *TaskRegistry) Remove(taskID string) bool {
	val, loaded := r.tasks.LoadAndDelete(taskID)
	if !loaded {
		return false
	}
	task := val.(*models.Task)
	if !task.Status().IsTerminal() {
		r.activeCount.Add(-1)
	}
	r.totalCount.Add(-1)
	return true
}

// ActiveCount returns the number of non-terminal tasks currently in the registry.
func (r *TaskRegistry) ActiveCount() int {
	cnt := int(r.activeCount.Load())
	if cnt < 0 {
		return 0
	}
	return cnt
}

// ActiveCountForUser returns the number of active (non-terminal) tasks owned by `userID` (Google Sub ID).
func (r *TaskRegistry) ActiveCountForUser(userID string) int {
	count := 0
	r.tasks.Range(func(_, value any) bool {
		t := value.(*models.Task)
		if t.UserID == userID && !t.Status().IsTerminal() {
			count++
		}
		return true
	})
	return count
}

// ListSnapshots returns immutable snapshots of all tasks currently held in the registry.
func (r *TaskRegistry) ListSnapshots() []models.TaskSnapshot {
	out := make([]models.TaskSnapshot, 0, r.totalCount.Load())
	r.tasks.Range(func(_, value any) bool {
		t := value.(*models.Task)
		out = append(out, t.Snapshot())
		return true
	})
	return out
}

// Snapshot is an alias for ListSnapshots.
func (r *TaskRegistry) Snapshot() []models.TaskSnapshot {
	return r.ListSnapshots()
}

// PauseAllActive pauses all currently downloading tasks (used by Watchdog on CRITICAL RAM).
func (r *TaskRegistry) PauseAllActive() int {
	paused := 0
	r.tasks.Range(func(_, value any) bool {
		t := value.(*models.Task)
		if t.Status() == models.StatusDownloading {
			if t.Pause() {
				paused++
			}
		}
		return true
	})
	return paused
}

// ResumeAllPaused resumes all paused tasks when Watchdog returns to StateNormal.
func (r *TaskRegistry) ResumeAllPaused() int {
	resumed := 0
	r.tasks.Range(func(_, value any) bool {
		t := value.(*models.Task)
		if t.Status() == models.StatusPaused {
			if t.Resume() {
				resumed++
			}
		}
		return true
	})
	return resumed
}

// PruneTerminal evicts completed, failed, or canceled tasks older than `maxAge`
// so finished task metadata never accumulates on the heap.
func (r *TaskRegistry) PruneTerminal(maxAge time.Duration) int {
	cutoff := time.Now().UTC().Add(-maxAge)
	pruned := 0
	r.tasks.Range(func(key, value any) bool {
		t := value.(*models.Task)
		snap := t.Snapshot()
		if snap.Status.IsTerminal() && !snap.FinishedAt.IsZero() && snap.FinishedAt.Before(cutoff) {
			if r.Remove(key.(string)) {
				pruned++
			}
		}
		return true
	})
	return pruned
}
