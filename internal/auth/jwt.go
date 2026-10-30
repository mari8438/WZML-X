// Package auth implements 100% web-first Google OAuth 2.0 authentication,
// 30-day HTTP-Only Secure JWT session management, and route protection middleware.
package auth

import (
	"crypto/rand"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/starfall-org/starfall-go/internal/models"
)

const (
	// SessionCookieName is the HTTP-Only cookie storing the signed 30-day JWT.
	SessionCookieName = "starfall_session"

	// DefaultSessionTTL is the mandatory 30-day session lifetime.
	DefaultSessionTTL = 30 * 24 * time.Hour

	// DefaultIssuer identifies tokens minted by the Starfall-Go Debrid authentication engine.
	DefaultIssuer = "starfall-debrid"
)

var (
	// ErrMissingSessionCookie indicates no `starfall_session` cookie or Bearer token was supplied.
	ErrMissingSessionCookie = errors.New("missing starfall_session authentication cookie")

	// ErrInvalidSessionToken indicates JWT signature verification failed or claims are malformed.
	ErrInvalidSessionToken = errors.New("invalid or expired starfall_session token")

	// ErrShortJWTSecret indicates the configured HMAC-SHA256 signing secret is < 32 bytes.
	ErrShortJWTSecret = errors.New("jwt signing secret must be at least 32 bytes")
)

// SessionClaims extends standard RFC 7519 JWT registered claims with Google user metadata.
type SessionClaims struct {
	Email     string `json:"email"`
	Name      string `json:"name"`
	AvatarURL string `json:"avatar_url,omitempty"`
	jwt.RegisteredClaims
}

// JWTConfig holds configuration parameters for the JWT session manager.
type JWTConfig struct {
	SecretKey    []byte
	Issuer       string
	CookieDomain string
	SecureCookie bool
	SessionTTL   time.Duration
}

// JWTManager mints, verifies, and binds 30-day signed JWTs to HTTP-Only cookies.
type JWTManager struct {
	secretKey    []byte
	issuer       string
	cookieDomain string
	secureCookie bool
	sessionTTL   time.Duration
}

// NewJWTManager constructs a JWTManager, enforcing at least a 256-bit (32-byte) signing key.
func NewJWTManager(cfg JWTConfig) (*JWTManager, error) {
	if len(cfg.SecretKey) < 32 {
		return nil, ErrShortJWTSecret
	}
	issuer := strings.TrimSpace(cfg.Issuer)
	if issuer == "" {
		issuer = DefaultIssuer
	}
	ttl := cfg.SessionTTL
	if ttl <= 0 {
		ttl = DefaultSessionTTL
	}
	return &JWTManager{
		secretKey:    append([]byte(nil), cfg.SecretKey...),
		issuer:       issuer,
		cookieDomain: strings.TrimSpace(cfg.CookieDomain),
		secureCookie: cfg.SecureCookie,
		sessionTTL:   ttl,
	}, nil
}

// MintToken generates a cryptographically signed HS256 JWT for `user` valid for 30 days.
func (m *JWTManager) MintToken(user *models.User) (string, time.Time, error) {
	if user == nil || strings.TrimSpace(user.ID) == "" {
		return "", time.Time{}, errors.New("cannot mint token for nil user or empty Google Sub ID")
	}

	now := time.Now().UTC()
	expiresAt := now.Add(m.sessionTTL)

	jtiBytes := make([]byte, 16)
	_, _ = rand.Read(jtiBytes)

	claims := SessionClaims{
		Email:     user.Email,
		Name:      user.Name,
		AvatarURL: user.AvatarURL,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    m.issuer,
			Subject:   user.ID,
			Audience:  jwt.ClaimStrings{m.issuer},
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now.Add(-30 * time.Second)), // 30s clock-skew leeway
			ExpiresAt: jwt.NewNumericDate(expiresAt),
			ID:        fmt.Sprintf("%x", jtiBytes),
		},
	}

	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := token.SignedString(m.secretKey)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("signing jwt token: %w", err)
	}
	return signed, expiresAt, nil
}

// VerifyToken parses and validates an HS256 JWT string, enforcing algorithm pinning,
// issuer match, expiration, and non-empty Google Sub ID (`sub`).
func (m *JWTManager) VerifyToken(rawToken string) (*SessionClaims, error) {
	rawToken = strings.TrimSpace(rawToken)
	if rawToken == "" {
		return nil, ErrInvalidSessionToken
	}

	claims := &SessionClaims{}
	parser := jwt.NewParser(
		jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}),
		jwt.WithIssuer(m.issuer),
		jwt.WithLeeway(30*time.Second),
		jwt.WithExpirationRequired(),
	)

	token, err := parser.ParseWithClaims(rawToken, claims, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return m.secretKey, nil
	})
	if err != nil || !token.Valid {
		return nil, fmt.Errorf("%w: %v", ErrInvalidSessionToken, err)
	}

	if strings.TrimSpace(claims.Subject) == "" {
		return nil, ErrInvalidSessionToken
	}
	return claims, nil
}

// SetSessionCookie mints a 30-day JWT for `user` and writes the HTTP-Only, Secure
// `starfall_session` cookie onto `w`.
func (m *JWTManager) SetSessionCookie(w http.ResponseWriter, user *models.User) (string, error) {
	signedToken, expiresAt, err := m.MintToken(user)
	if err != nil {
		return "", err
	}

	http.SetCookie(w, &http.Cookie{
		Name:     SessionCookieName,
		Value:    signedToken,
		Path:     "/",
		Domain:   m.cookieDomain,
		Expires:  expiresAt,
		MaxAge:   int(m.sessionTTL.Seconds()),
		HttpOnly: true,
		Secure:   m.secureCookie,
		SameSite: http.SameSiteLaxMode,
	})
	return signedToken, nil
}

// ClearSessionCookie immediately expires and overwrites the `starfall_session` cookie.
func (m *JWTManager) ClearSessionCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     SessionCookieName,
		Value:    "",
		Path:     "/",
		Domain:   m.cookieDomain,
		Expires:  time.Unix(0, 0).UTC(),
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   m.secureCookie,
		SameSite: http.SameSiteLaxMode,
	})
}

// ExtractFromRequest reads the JWT from the `starfall_session` cookie (preferred)
// or falls back to an `Authorization: Bearer <token>` header, and verifies its signature.
func (m *JWTManager) ExtractFromRequest(r *http.Request) (*SessionClaims, error) {
	if cookie, err := r.Cookie(SessionCookieName); err == nil && strings.TrimSpace(cookie.Value) != "" {
		return m.VerifyToken(cookie.Value)
	}

	authHeader := strings.TrimSpace(r.Header.Get("Authorization"))
	if strings.HasPrefix(strings.ToLower(authHeader), "bearer ") {
		rawToken := strings.TrimSpace(authHeader[7:])
		if rawToken != "" {
			return m.VerifyToken(rawToken)
		}
	}

	return nil, ErrMissingSessionCookie
}
