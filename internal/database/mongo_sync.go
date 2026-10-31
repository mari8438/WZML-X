package database

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

// MongoReplicator replicates local SQLite mutations to MongoDB Atlas and hydrates state on boot.
type MongoReplicator struct {
	db        *DBManager
	client    *mongo.Client
	database  *mongo.Database
	isOffline bool
	closeOnce sync.Once
}

// NewMongoReplicator initializes a MongoDB Atlas connection using DATABASE_URL.
// If mongoURI is empty, it enters graceful offline mode (SQLite-only operations).
func NewMongoReplicator(db *DBManager, mongoURI string) (*MongoReplicator, error) {
	mongoURI = strings.TrimSpace(mongoURI)
	if mongoURI == "" {
		slog.Info("DATABASE_URL is not set; running in local SQLite-only mode (Mongo replication disabled)")
		return &MongoReplicator{
			db:        db,
			isOffline: true,
		}, nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()

	clientOpts := options.Client().
		ApplyURI(mongoURI).
		SetConnectTimeout(8 * time.Second).
		SetServerSelectionTimeout(8 * time.Second).
		SetMaxPoolSize(10).
		SetMinPoolSize(1)

	client, err := mongo.Connect(ctx, clientOpts)
	if err != nil {
		slog.Warn("failed to initiate mongo client; falling back to offline mode", "error", err)
		return &MongoReplicator{
			db:        db,
			isOffline: true,
		}, nil
	}

	if err := client.Ping(ctx, nil); err != nil {
		slog.Warn("mongo ping failed on boot; running in offline mode", "error", err)
		return &MongoReplicator{
			db:        db,
			isOffline: true,
		}, nil
	}

	slog.Info("successfully connected to MongoDB Atlas for state replication")
	return &MongoReplicator{
		db:        db,
		client:    client,
		database:  client.Database("starfallx"),
		isOffline: false,
	}, nil
}

// HydrateFromMongo syncs remote MongoDB Atlas documents to local SQLite if remote is newer.
func (r *MongoReplicator) HydrateFromMongo(parentCtx context.Context) error {
	if r.isOffline || r.client == nil {
		return nil
	}

	ctx, cancel := context.WithTimeout(parentCtx, 8*time.Second)
	defer cancel()

	// 1. Hydrate users
	usersColl := r.database.Collection("users")
	cursor, err := usersColl.Find(ctx, bson.M{})
	if err == nil {
		defer cursor.Close(ctx)
		for cursor.Next(ctx) {
			var remoteDoc struct {
				ID          string          `bson:"_id"`
				TelegramID  int64           `bson:"telegram_id"`
				Name        string          `bson:"name"`
				Email       string          `bson:"email"`
				AvatarURL   string          `bson:"avatar_url"`
				DumpChat    string          `bson:"telegram_dump_chat"`
				Theme       string          `bson:"anime_theme"`
				PosterID    int             `bson:"poster_template"`
				Settings    json.RawMessage `bson:"settings"`
				UpdatedAtNs int64           `bson:"updated_at_ns"`
			}
			if err := cursor.Decode(&remoteDoc); err == nil && remoteDoc.ID != "" {
				localUser, err := r.db.GetUser(ctx, remoteDoc.ID)
				if errors.Is(err, ErrNotFound) || localUser == nil {
					// Hydrate new user
					newUser := &models.User{
						ID:               remoteDoc.ID,
						TelegramID:       remoteDoc.TelegramID,
						Name:             remoteDoc.Name,
						Email:            remoteDoc.Email,
						AvatarURL:        remoteDoc.AvatarURL,
						TelegramDumpChat: remoteDoc.DumpChat,
						AnimeTheme:       remoteDoc.Theme,
						PosterTemplate:   remoteDoc.PosterID,
					}
					if len(remoteDoc.Settings) > 0 {
						_ = json.Unmarshal(remoteDoc.Settings, &newUser.Settings)
					}
					_ = r.db.UpsertUser(ctx, newUser)
				}
			}
		}
	}

	// 2. Hydrate bot_settings
	botSettingsColl := r.database.Collection("bot_settings")
	bCursor, err := botSettingsColl.Find(ctx, bson.M{})
	if err == nil {
		defer bCursor.Close(ctx)
		for bCursor.Next(ctx) {
			var bDoc struct {
				Key         string `bson:"_id"`
				ValueJSON   string `bson:"value_json"`
				UpdatedAtNs int64  `bson:"updated_at_ns"`
			}
			if err := bCursor.Decode(&bDoc); err == nil && bDoc.Key != "" {
				var val any
				if err := json.Unmarshal([]byte(bDoc.ValueJSON), &val); err == nil {
					_ = r.db.SetBotSetting(ctx, bDoc.Key, val)
				}
			}
		}
	}

	slog.Info("MongoDB state hydration completed")
	return nil
}

// StartReplication begins the background outbox replication worker.
func (r *MongoReplicator) StartReplication(ctx context.Context) {
	if r.isOffline || r.client == nil {
		return
	}

	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-r.db.NotifyOutboxChannel():
				r.flushOutbox(ctx)
			case <-ticker.C:
				r.flushOutbox(ctx)
			}
		}
	}()
}

func (r *MongoReplicator) flushOutbox(ctx context.Context) {
	if r.isOffline || r.client == nil {
		return
	}

	items, err := r.db.FetchOutboxBatch(ctx, 50)
	if err != nil || len(items) == 0 {
		return
	}

	var ackedIDs []int64

	// Group items by collection
	grouped := make(map[string][]OutboxItem)
	for _, item := range items {
		grouped[item.CollectionName] = append(grouped[item.CollectionName], item)
	}

	for collName, batch := range grouped {
		coll := r.database.Collection(collName)
		var writeModels []mongo.WriteModel

		for _, item := range batch {
			var doc bson.M
			if err := json.Unmarshal([]byte(item.PayloadJSON), &doc); err != nil {
				doc = bson.M{"raw": item.PayloadJSON}
			}
			doc["_id"] = item.DocID
			doc["updated_at_ns"] = item.UpdatedAtNs

			if item.Action == "delete" {
				model := mongo.NewDeleteOneModel().SetFilter(bson.M{
					"_id":           item.DocID,
					"updated_at_ns": bson.M{"$lte": item.UpdatedAtNs},
				})
				writeModels = append(writeModels, model)
			} else {
				model := mongo.NewReplaceOneModel().
					SetFilter(bson.M{
						"_id": item.DocID,
						"$or": []bson.M{
							{"updated_at_ns": bson.M{"$exists": false}},
							{"updated_at_ns": bson.M{"$lte": item.UpdatedAtNs}},
						},
					}).
					SetReplacement(doc).
					SetUpsert(true)
				writeModels = append(writeModels, model)
			}
		}

		if len(writeModels) > 0 {
			writeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			_, err := coll.BulkWrite(writeCtx, writeModels, options.BulkWrite().SetOrdered(false))
			cancel()
			if err != nil {
				slog.Debug("bulk write to mongo failed", "collection", collName, "error", err)
			} else {
				for _, item := range batch {
					ackedIDs = append(ackedIDs, item.ID)
				}
			}
		}
	}

	if len(ackedIDs) > 0 {
		_ = r.db.DeleteOutboxBatch(ctx, ackedIDs)
	}
}

// Close disconnects MongoDB client cleanly.
func (r *MongoReplicator) Close(ctx context.Context) error {
	var err error
	r.closeOnce.Do(func() {
		if r.client != nil {
			err = r.client.Disconnect(ctx)
		}
	})
	return err
}
