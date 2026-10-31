// Package auth provides HTTP middleware guarding protected `/api/` endpoints
// using the 30-day `starfall_session` HTTP-Only JWT cookie or Telegram `/web` magic link.
package auth

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/starfall-org/starfall-go/internal/models"
)

type contextKey int

const (
	claimsContextKey contextKey = iota
	userContextKey
)

// UserLoader resolves a live User record by ID during middleware execution.
type UserLoader interface {
	GetUser(ctx context.Context, userID string) (*models.User, error)
}

// Middleware guards `/api/` routes by verifying the `starfall_session` JWT cookie
// (or `?token=` magic link) and injecting authenticated user context into downstream handlers.
type Middleware struct {
	jwtManager     *JWTManager
	userLoader     UserLoader
	logger         *slog.Logger
	fallbackUserID string
}

// NewMiddleware constructs an authentication Middleware instance.
func NewMiddleware(jwtManager *JWTManager, userLoader UserLoader, logger *slog.Logger) *Middleware {
	return &Middleware{
		jwtManager: jwtManager,
		userLoader: userLoader,
		logger:     logger,
	}
}

// SetFallbackUserID sets an optional owner fallback ID (e.g. `tg:8814651149`) used only when
// Google OAuth 2.0 is not yet configured, allowing immediate Web Studio use on private deployments.
func (mw *Middleware) SetFallbackUserID(userID string) {
	mw.fallbackUserID = strings.TrimSpace(userID)
}

// JWTManager returns the underlying JWTManager for minting magic links (`/web`).
func (mw *Middleware) JWTManager() *JWTManager {
	return mw.jwtManager
}

// RequireAuth wraps an `http.Handler`, rejecting unauthenticated requests with HTTP 401 JSON
// and hydrating `r.Context()` with `*SessionClaims` and `*models.User`.
func (mw *Middleware) RequireAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		claims, err := mw.jwtManager.ExtractFromRequest(r)
		if err != nil {
			// If fallback owner mode is active (when Google OAuth is unconfigured), hydrate fallback user.
			if mw.fallbackUserID != "" && mw.userLoader != nil {
				if fallbackUser, ferr := mw.userLoader.GetUser(r.Context(), mw.fallbackUserID); ferr == nil && fallbackUser != nil {
					_, _ = mw.jwtManager.SetSessionCookie(w, fallbackUser)
					ctx := context.WithValue(r.Context(), userContextKey, fallbackUser)
					next.ServeHTTP(w, r.WithContext(ctx))
					return
				}
			}
			writeJSONError(w, http.StatusUnauthorized, "authentication required: please sign in with Google or /web magic link")
			return
		}

		ctx := context.WithValue(r.Context(), claimsContextKey, claims)

		if mw.userLoader != nil {
			user, err := mw.userLoader.GetUser(ctx, claims.Subject)
			if err != nil || user == nil {
				mw.jwtManager.ClearSessionCookie(w)
				writeJSONError(w, http.StatusUnauthorized, "user session is no longer valid")
				return
			}
			// If logged in via `?token=` magic link, persist the 30-day HTTP-Only cookie automatically.
			if strings.TrimSpace(r.URL.Query().Get("token")) != "" {
				_, _ = mw.jwtManager.SetSessionCookie(w, user)
			}
			ctx = context.WithValue(ctx, userContextKey, user)
		}

		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// RequireAuthFunc is a convenience wrapper for guarding an `http.HandlerFunc`.
func (mw *Middleware) RequireAuthFunc(next http.HandlerFunc) http.Handler {
	return mw.RequireAuth(next)
}

// ClaimsFromContext retrieves the verified `*SessionClaims` injected by `RequireAuth`.
func ClaimsFromContext(ctx context.Context) (*SessionClaims, bool) {
	claims, ok := ctx.Value(claimsContextKey).(*SessionClaims)
	return claims, ok && claims != nil
}

// UserFromContext retrieves the authenticated `*models.User` injected by `RequireAuth`.
func UserFromContext(ctx context.Context) (*models.User, bool) {
	u, ok := ctx.Value(userContextKey).(*models.User)
	return u, ok && u != nil
}

// UserIDFromContext extracts the authenticated user's ID from `ctx`.
func UserIDFromContext(ctx context.Context) (string, bool) {
	if u, ok := UserFromContext(ctx); ok && strings.TrimSpace(u.ID) != "" {
		return u.ID, true
	}
	if claims, ok := ClaimsFromContext(ctx); ok && strings.TrimSpace(claims.Subject) != "" {
		return claims.Subject, true
	}
	return "", false
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"ok":    false,
		"error": message,
	})
}
