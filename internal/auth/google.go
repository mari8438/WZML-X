// Package auth implements the Google OAuth 2.0 web login redirect, callback state
// validation, user profile synchronization, and logout handlers.
package auth

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	googleoauth2 "google.golang.org/api/oauth2/v2"
	"google.golang.org/api/option"
)

const (
	// OAuthStateCookieName stores the transient CSRF state token during the OAuth2 redirect flow.
	OAuthStateCookieName = "starfall_oauth_state"

	// oauthStateTTL caps the lifetime of an in-flight OAuth2 login attempt at 10 minutes.
	oauthStateTTL = 10 * time.Minute

	// oauthExchangeTimeout bounds outbound Google API calls so slow networks never stall workers.
	oauthExchangeTimeout = 12 * time.Second
)

// UserUpserter defines the persistence interface required by GoogleAuthHandler to upsert
// authenticated Google users into the embedded SQLite store.
type UserUpserter interface {
	UpsertGoogleUser(ctx context.Context, id, email, name, avatarURL string) (*models.User, error)
}

// GoogleOAuthConfig holds Google Cloud Console OAuth 2.0 credentials and redirect parameters.
type GoogleOAuthConfig struct {
	ClientID          string
	ClientSecret      string
	RedirectURL       string
	PostLoginRedirect string
	StateSigningKey   []byte
	SecureCookie      bool
}

// GoogleAuthHandler serves `/auth/google/login`, `/auth/google/callback`, and `/auth/logout`.
type GoogleAuthHandler struct {
	oauthConfig       *oauth2.Config
	jwtManager        *JWTManager
	userStore         UserUpserter
	stateSigningKey   []byte
	secureCookie      bool
	postLoginRedirect string
	logger            *slog.Logger
}

// NewGoogleAuthHandler initializes the Google OAuth 2.0 flow handler.
func NewGoogleAuthHandler(
	cfg GoogleOAuthConfig,
	jwtManager *JWTManager,
	userStore UserUpserter,
	logger *slog.Logger,
) (*GoogleAuthHandler, error) {
	if strings.TrimSpace(cfg.ClientID) == "" || strings.TrimSpace(cfg.ClientSecret) == "" {
		return nil, errors.New("google oauth2 client_id and client_secret are required")
	}
	if strings.TrimSpace(cfg.RedirectURL) == "" {
		return nil, errors.New("google oauth2 redirect_url is required")
	}
	if jwtManager == nil {
		return nil, errors.New("jwtManager cannot be nil")
	}
	if userStore == nil {
		return nil, errors.New("userStore cannot be nil")
	}

	stateKey := cfg.StateSigningKey
	if len(stateKey) < 32 {
		stateKey = jwtManager.secretKey
	}

	postLogin := strings.TrimSpace(cfg.PostLoginRedirect)
	if postLogin == "" {
		postLogin = "/"
	}

	oauthCfg := &oauth2.Config{
		ClientID:     strings.TrimSpace(cfg.ClientID),
		ClientSecret: strings.TrimSpace(cfg.ClientSecret),
		RedirectURL:  strings.TrimSpace(cfg.RedirectURL),
		Scopes: []string{
			"openid",
			googleoauth2.UserinfoEmailScope,
			googleoauth2.UserinfoProfileScope,
		},
		Endpoint: google.Endpoint,
	}

	return &GoogleAuthHandler{
		oauthConfig:       oauthCfg,
		jwtManager:        jwtManager,
		userStore:         userStore,
		stateSigningKey:   stateKey,
		secureCookie:      cfg.SecureCookie,
		postLoginRedirect: postLogin,
		logger:            logger,
	}, nil
}

// RegisterRoutes mounts the OAuth 2.0 authentication endpoints onto `mux`.
func (h *GoogleAuthHandler) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /auth/google/login", h.HandleLogin)
	mux.HandleFunc("GET /auth/google/callback", h.HandleCallback)
	mux.HandleFunc("GET /auth/logout", h.HandleLogout)
}

// HandleLogin (`GET /auth/google/login`) generates an HMAC-signed cryptographic state token,
// stores it in a short-lived HTTP-Only transient cookie, and redirects the user to Google Accounts.
func (h *GoogleAuthHandler) HandleLogin(w http.ResponseWriter, r *http.Request) {
	state, err := h.generateSignedState()
	if err != nil {
		h.logger.Error("failed to generate oauth2 state", slog.String("error", err.Error()))
		http.Error(w, "internal authentication error", http.StatusInternalServerError)
		return
	}

	http.SetCookie(w, &http.Cookie{
		Name:     OAuthStateCookieName,
		Value:    state,
		Path:     "/auth/google",
		Expires:  time.Now().UTC().Add(oauthStateTTL),
		MaxAge:   int(oauthStateTTL.Seconds()),
		HttpOnly: true,
		Secure:   h.secureCookie,
		SameSite: http.SameSiteLaxMode,
	})

	authURL := h.oauthConfig.AuthCodeURL(
		state,
		oauth2.AccessTypeOnline,
		oauth2.SetAuthURLParam("prompt", "select_account"),
	)
	http.Redirect(w, r, authURL, http.StatusTemporaryRedirect)
}

// HandleCallback (`GET /auth/google/callback`) validates the transient state cookie,
// exchanges the authorization code for a Google token, retrieves the user's Google profile
// via `google.golang.org/api/oauth2/v2`, upserts the user in SQLite, sets the 30-day
// HTTP-Only `starfall_session` JWT cookie, and redirects to the web application.
func (h *GoogleAuthHandler) HandleCallback(w http.ResponseWriter, r *http.Request) {
	// Always clear the single-use OAuth state cookie immediately upon callback.
	h.clearStateCookie(w)

	if oauthErr := strings.TrimSpace(r.URL.Query().Get("error")); oauthErr != "" {
		h.logger.Warn("google oauth2 callback returned provider error", slog.String("oauth_error", oauthErr))
		http.Error(w, "Google authentication was canceled or denied", http.StatusUnauthorized)
		return
	}

	queryState := strings.TrimSpace(r.URL.Query().Get("state"))
	stateCookie, err := r.Cookie(OAuthStateCookieName)
	if err != nil || queryState == "" || !h.verifySignedState(queryState, stateCookie.Value) {
		h.logger.Warn("oauth2 state mismatch or missing cookie")
		http.Error(w, "invalid or expired OAuth2 state token", http.StatusBadRequest)
		return
	}

	code := strings.TrimSpace(r.URL.Query().Get("code"))
	if code == "" {
		http.Error(w, "missing OAuth2 authorization code", http.StatusBadRequest)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), oauthExchangeTimeout)
	defer cancel()

	// 1. Exchange authorization code for Google OAuth2 access token.
	token, err := h.oauthConfig.Exchange(ctx, code)
	if err != nil {
		h.logger.Error("google oauth2 code exchange failed", slog.String("error", err.Error()))
		http.Error(w, "failed to exchange authorization code with Google", http.StatusBadGateway)
		return
	}

	// 2. Fetch verified Google profile (Sub ID, Email, Name, Avatar) via google.golang.org/api/oauth2/v2.
	oauth2Service, err := googleoauth2.NewService(
		ctx,
		option.WithTokenSource(h.oauthConfig.TokenSource(ctx, token)),
	)
	if err != nil {
		h.logger.Error("failed to initialize google oauth2 v2 service", slog.String("error", err.Error()))
		http.Error(w, "failed to initialize Google profile client", http.StatusInternalServerError)
		return
	}

	userInfo, err := oauth2Service.Userinfo.V2.Me.Get().Context(ctx).Do()
	if err != nil {
		h.logger.Error("failed to fetch google userinfo profile", slog.String("error", err.Error()))
		http.Error(w, "failed to fetch user profile from Google", http.StatusBadGateway)
		return
	}

	if strings.TrimSpace(userInfo.Id) == "" || strings.TrimSpace(userInfo.Email) == "" {
		h.logger.Error("google userinfo missing required id or email fields")
		http.Error(w, "Google account did not provide a valid ID or email address", http.StatusForbidden)
		return
	}
	if userInfo.VerifiedEmail != nil && !*userInfo.VerifiedEmail {
		h.logger.Warn("rejected unverified google email", slog.String("email", userInfo.Email))
		http.Error(w, "Google email address must be verified", http.StatusForbidden)
		return
	}

	displayName := strings.TrimSpace(userInfo.Name)
	if displayName == "" {
		displayName = strings.Split(userInfo.Email, "@")[0]
	}

	// 3. Upsert user in the pure-Go embedded SQLite store.
	user, err := h.userStore.UpsertGoogleUser(
		ctx,
		userInfo.Id,
		userInfo.Email,
		displayName,
		userInfo.Picture,
	)
	if err != nil {
		h.logger.Error("failed to upsert google user in sqlite store",
			slog.String("google_sub", userInfo.Id),
			slog.String("error", err.Error()),
		)
		http.Error(w, "failed to persist user account", http.StatusInternalServerError)
		return
	}

	// 4. Issue 30-day HTTP-Only, Secure JWT session cookie (`starfall_session`).
	if _, err := h.jwtManager.SetSessionCookie(w, user); err != nil {
		h.logger.Error("failed to mint session cookie", slog.String("error", err.Error()))
		http.Error(w, "failed to create user session", http.StatusInternalServerError)
		return
	}

	h.logger.Info("user authenticated via Google OAuth2",
		slog.String("user_id", user.ID),
		slog.String("email", user.Email),
		slog.Bool("telegram_linked", user.HasTelegramDumpLinked()),
	)

	http.Redirect(w, r, h.postLoginRedirect, http.StatusSeeOther)
}

// HandleLogout (`GET /auth/logout`) clears the `starfall_session` cookie and redirects to `/`.
func (h *GoogleAuthHandler) HandleLogout(w http.ResponseWriter, r *http.Request) {
	h.jwtManager.ClearSessionCookie(w)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

func (h *GoogleAuthHandler) generateSignedState() (string, error) {
	nonce := make([]byte, 24)
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	rawNonce := base64.RawURLEncoding.EncodeToString(nonce)

	mac := hmac.New(sha256.New, h.stateSigningKey)
	mac.Write([]byte(rawNonce))
	sig := hex.EncodeToString(mac.Sum(nil)[:16])

	return rawNonce + "." + sig, nil
}

func (h *GoogleAuthHandler) verifySignedState(queryState, cookieState string) bool {
	if subtle.ConstantTimeCompare([]byte(queryState), []byte(cookieState)) != 1 {
		return false
	}
	parts := strings.Split(queryState, ".")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return false
	}

	mac := hmac.New(sha256.New, h.stateSigningKey)
	mac.Write([]byte(parts[0]))
	expectedSig := hex.EncodeToString(mac.Sum(nil)[:16])

	return subtle.ConstantTimeCompare([]byte(parts[1]), []byte(expectedSig)) == 1
}

func (h *GoogleAuthHandler) clearStateCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     OAuthStateCookieName,
		Value:    "",
		Path:     "/auth/google",
		Expires:  time.Unix(0, 0).UTC(),
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   h.secureCookie,
		SameSite: http.SameSiteLaxMode,
	})
}
