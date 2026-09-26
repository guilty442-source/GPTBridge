// searchd — 星澄 governed metasearch service (go-service layer).
//
// Loopback-only HTTP JSON service implementing the versioned
// xingcheng-searchd/v1 contract (see CONTRACT.md). It fans governed
// queries out to a fixed set of credential-free upstream adapters,
// deduplicates and deterministically re-ranks results, and returns
// bounded metadata only — it never fetches result pages and can never
// reach a host outside the compiled-in upstream allowlist.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"runtime/debug"
	"strings"
	"time"

	"xingcheng/searchd/internal/search"
)

const version = "1.0.0"

// loopbackOnly enforces the same destination governance as the Python
// side: the service may only listen on loopback addresses.
func loopbackOnly(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("invalid listen address: %w", err)
	}
	if host == "" {
		return errors.New("listen host must be explicit loopback")
	}
	ip := net.ParseIP(strings.ToLower(host))
	switch {
	case host == "localhost":
		return nil
	case ip != nil && ip.IsLoopback():
		return nil
	default:
		return fmt.Errorf("refusing to listen on non-loopback host %q", host)
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8091", "loopback listen address")
	upstreamTimeout := flag.Duration("upstream-timeout", 8*time.Second, "per-adapter upstream timeout")
	maxInflight := flag.Int("max-inflight", 16, "max concurrent /v1/search requests")
	flag.Parse()

	if err := loopbackOnly(*listen); err != nil {
		log.Fatalf("searchd: %v", err)
	}

	// Soft memory limit: GC leans harder before RSS grows unbounded — the
	// service is a small loopback endpoint and should never balloon.
	debug.SetMemoryLimit(256 << 20) // 256 MiB

	engine := search.NewEngine(*upstreamTimeout, nil)

	// Concurrency gate: bounded in-flight searches — each fans out to one
	// goroutine per adapter, so unbounded callers could spawn unbounded
	// upstream work; queue instead of exceeding the budget.
	inflight := make(chan struct{}, *maxInflight)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": true, "service": "searchd", "version": version,
			"contract": search.ContractVersion,
		})
	})
	mux.HandleFunc("/v1/search", func(w http.ResponseWriter, r *http.Request) {
		select {
		case inflight <- struct{}{}:
			defer func() { <-inflight }()
		case <-r.Context().Done():
			writeJSON(w, http.StatusServiceUnavailable, search.Response{
				OK: false, Contract: search.ContractVersion, Engine: "searchd",
				Error: "OVERLOADED",
			})
			return
		}
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, search.Response{
				OK: false, Contract: search.ContractVersion, Engine: "searchd",
				Error: "METHOD_NOT_ALLOWED",
			})
			return
		}
		var req search.Request
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
		if err := dec.Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, search.Response{
				OK: false, Contract: search.ContractVersion, Engine: "searchd",
				Error: "INVALID_JSON",
			})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		writeJSON(w, http.StatusOK, engine.Search(ctx, req))
	})

	srv := &http.Server{
		Addr:              *listen,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("searchd %s listening on http://%s (contract %s)", version, *listen, search.ContractVersion)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("searchd: %v", err)
	}
}
