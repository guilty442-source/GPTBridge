// ai-collab-host — governed Go backend for the ai-collaboration tool.
// Exit contract: 0 clean shutdown, 13 = fail-closed environment/DSN.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/dsn"
	"gptbridge.local/ai-collaboration-backend/internal/govenv"
	"gptbridge.local/ai-collaboration-backend/internal/host"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
	"gptbridge.local/ai-collaboration-backend/internal/service"
)

const exitDenied = 13

func main() {
	env, err := govenv.Load()
	if err != nil {
		fmt.Fprintln(os.Stderr, "PERMISSION_DENIED")
		os.Exit(exitDenied)
	}
	version := govenv.ManifestVersion(env.ToolRoot)

	// Data authority: PostgreSQL schema gptbridge_collab — fail closed.
	dsnValue, err := dsn.Resolve()
	if err != nil {
		fmt.Fprintln(os.Stderr, "DATA_AUTHORITY_UNAVAILABLE")
		os.Exit(exitDenied)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	repository, err := repo.Open(ctx, dsnValue, repo.Schema())
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, "DATA_AUTHORITY_UNAVAILABLE")
		os.Exit(exitDenied)
	}
	defer repository.Close()

	h := host.New(env, nil, version)
	browser := service.NewBrowserSession(&host.WSBrowserOps{H: h})
	svc := service.New(version, env.ToolRoot, repository, browser)
	h.AttachService(svc)

	if err := h.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "PERMISSION_DENIED")
		os.Exit(exitDenied)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	select {
	case <-sig:
	case <-h.ShutdownToken():
	}
	h.RequestShutdown()
	svc.Shutdown()
	time.Sleep(200 * time.Millisecond)
}
