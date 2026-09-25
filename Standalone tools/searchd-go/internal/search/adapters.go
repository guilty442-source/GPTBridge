package search

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// maxUpstreamBody bounds every upstream response (fail-closed on overflow).
const maxUpstreamBody = 1 << 20 // 1 MiB

// Adapter is one upstream search source. Adapters may only emit metadata
// (title/url/snippet); they never fetch arbitrary result pages.
type Adapter interface {
	Name() string
	// Search issues the upstream request using the allowlisted client.
	Search(ctx context.Context, req Request, client *http.Client) ([]Result, error)
}

// upstreamAllowlist is the complete set of hosts the service may reach.
// Adapters hardcode their endpoints inside this set; user input can never
// extend it, so the service cannot be used as an arbitrary proxy (SSRF
// fail-closed at the transport layer, not just per adapter).
var upstreamAllowlist = map[string]bool{
	"lite.duckduckgo.com":  true,
	"html.duckduckgo.com":  true,
	"api.duckduckgo.com":   true,
	"www.bing.com":         true,
	"bing.com":             true,
}

func hostAllowed(host string) bool {
	host = strings.ToLower(host)
	if upstreamAllowlist[host] {
		return true
	}
	return strings.HasSuffix(host, ".wikipedia.org")
}

type allowlistTransport struct{ next http.RoundTripper }

func (t allowlistTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if !hostAllowed(r.URL.Hostname()) {
		return nil, fmt.Errorf("upstream host %q not in allowlist", r.URL.Hostname())
	}
	return t.next.RoundTrip(r)
}

// NewClient builds the upstream HTTP client: allowlist guard at transport
// level, redirect targets re-checked, per-request timeout.
func NewClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout:   timeout,
		Transport: allowlistTransport{next: http.DefaultTransport},
		CheckRedirect: func(r *http.Request, via []*http.Request) error {
			if !hostAllowed(r.URL.Hostname()) {
				return errors.New("redirect to non-allowlisted host rejected")
			}
			if len(via) >= 5 {
				return errors.New("too many redirects")
			}
			return nil
		},
	}
}

// defaultAdapters returns the credential-free upstream set. Order is the
// deterministic adapter ordering used for fan-out and status reporting.
func defaultAdapters() []Adapter {
	return []Adapter{
		&Wikipedia{},
		&BingRSS{},
		&DuckDuckGoLite{},
	}
}

func readBody(r io.Reader) ([]byte, error) {
	return io.ReadAll(io.LimitReader(r, maxUpstreamBody+1))
}
