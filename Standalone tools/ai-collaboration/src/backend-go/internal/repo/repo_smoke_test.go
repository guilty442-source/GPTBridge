package repo

import (
	"context"
	"os"
	"testing"
	"time"
)

// TestDSNSmoke verifies the governed DSN resolves and the schema
// bootstraps against the live PostgreSQL authority. Skipped unless
// GPTBRIDGE_POSTGRES_DSN is present.
func TestDSNSmoke(t *testing.T) {
	if os.Getenv("GPTBRIDGE_POSTGRES_DSN") == "" {
		t.Skip("GPTBRIDGE_POSTGRES_DSN unset")
	}
	dsn := os.Getenv("GPTBRIDGE_POSTGRES_DSN")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	r, err := Open(ctx, dsn, Schema())
	if err != nil {
		t.Fatalf("repo.Open: %v", err)
	}
	defer r.Close()
	agents, err := r.ListAgents(ctx)
	if err != nil {
		t.Fatalf("ListAgents: %v", err)
	}
	t.Logf("agents=%d db=%s", len(agents), r.DBPath())
}
