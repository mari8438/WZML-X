package telegram

import (
	"sync"
	"time"
)

// IsOwner reports whether userID matches the primary bot owner ID.
func IsOwner(userID, ownerID int64) bool {
	return userID > 0 && ownerID > 0 && userID == ownerID
}

// IsSudo reports whether userID is in the list of authorized administrators.
func IsSudo(userID int64, sudoUsers []int64) bool {
	if userID <= 0 {
		return false
	}
	for _, id := range sudoUsers {
		if id == userID {
			return true
		}
	}
	return false
}

// IsAuthChat reports whether chatID is in the authorized group/channel list.
func IsAuthChat(chatID int64, authChats []int64) bool {
	if chatID == 0 {
		return false
	}
	for _, id := range authChats {
		if id == chatID {
			return true
		}
	}
	return false
}

// IsAuthorized verifies whether a user or chat has permission to execute a command.
func IsAuthorized(userID, chatID, ownerID int64, sudoUsers, authChats []int64) bool {
	if IsOwner(userID, ownerID) {
		return true
	}
	if IsSudo(userID, sudoUsers) {
		return true
	}
	if IsAuthChat(chatID, authChats) {
		return true
	}
	return false
}

// CommandRateLimiter implements a sliding-window rate limiter per Telegram user
// to prevent denial-of-service spam while allowing bursts.
type CommandRateLimiter struct {
	mu          sync.Mutex
	history     map[int64][]time.Time
	maxRequests int
	window      time.Duration
}

// NewCommandRateLimiter creates a rate limiter allowing up to maxRequests within window per user.
func NewCommandRateLimiter(maxRequests int, window time.Duration) *CommandRateLimiter {
	if maxRequests <= 0 {
		maxRequests = 5
	}
	if window <= 0 {
		window = 10 * time.Second
	}
	return &CommandRateLimiter{
		history:     make(map[int64][]time.Time),
		maxRequests: maxRequests,
		window:      window,
	}
}

// Allow reports whether the user is permitted to run a command.
// Owner always bypasses rate limiting.
func (l *CommandRateLimiter) Allow(userID, ownerID int64) bool {
	if IsOwner(userID, ownerID) {
		return true
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	now := time.Now()
	cutoff := now.Add(-l.window)

	timestamps := l.history[userID]
	valid := make([]time.Time, 0, len(timestamps))
	for _, t := range timestamps {
		if t.After(cutoff) {
			valid = append(valid, t)
		}
	}

	if len(valid) >= l.maxRequests {
		l.history[userID] = valid
		return false
	}

	valid = append(valid, now)
	l.history[userID] = valid
	return true
}
