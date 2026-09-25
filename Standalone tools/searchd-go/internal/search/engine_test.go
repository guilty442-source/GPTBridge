package search

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"
)

type fakeAdapter struct {
	name    string
	results []Result
	err     error
	delay   time.Duration
}

func (f *fakeAdapter) Name() string { return f.name }

func (f *fakeAdapter) Search(ctx context.Context, _ Request, _ *http.Client) ([]Result, error) {
	if f.delay > 0 {
		select {
		case <-time.After(f.delay):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return f.results, f.err
}

func testEngine(timeout time.Duration, adapters ...Adapter) *Engine {
	return &Engine{adapters: adapters, client: NewClient(timeout), timeout: timeout}
}

func TestMissingQueryRejected(t *testing.T) {
	e := testEngine(time.Second)
	resp := e.Search(context.Background(), Request{})
	if resp.OK || resp.Error != "QUERY_REQUIRED" {
		t.Fatalf("expected QUERY_REQUIRED, got %+v", resp)
	}
}

func TestMergeDedupeAndDeterministicRank(t *testing.T) {
	a := &fakeAdapter{name: "a", results: []Result{
		{Title: "One", URL: "https://www.Example.com/page?utm_source=x", Domain: "example.com", Rank: 0},
		{Title: "Two", URL: "https://b.example.com/other", Domain: "b.example.com", Rank: 1},
	}}
	b := &fakeAdapter{name: "b", results: []Result{
		{Title: "OneDup", URL: "https://example.com/page/", Domain: "example.com", Rank: 0},
		{Title: "Three", URL: "https://c.example.com/", Domain: "c.example.com", Rank: 1},
	}}
	e := testEngine(time.Second, a, b)

	first := e.Search(context.Background(), Request{Query: "q", MaxResults: 10})
	second := e.Search(context.Background(), Request{Query: "q", MaxResults: 10})
	if len(first.Results) != 3 {
		t.Fatalf("dedupe failed, got %d results", len(first.Results))
	}
	// Duplicated URL appears in two adapters -> highest fused score.
	if first.Results[0].Domain != "example.com" {
		t.Fatalf("expected example.com first, got %s", first.Results[0].URL)
	}
	for i := range first.Results {
		if first.Results[i].URL != second.Results[i].URL || first.Results[i].Score != second.Results[i].Score {
			t.Fatal("ranking not deterministic")
		}
		if first.Results[i].Rank != i {
			t.Fatal("rank not sequential")
		}
	}
	if len(first.Adapters) != 2 || !first.Adapters[0].OK || !first.Adapters[1].OK {
		t.Fatalf("adapter status missing: %+v", first.Adapters)
	}
}

func TestUpstreamFailureIsolation(t *testing.T) {
	dead := &fakeAdapter{name: "dead", err: errors.New("boom")}
	live := &fakeAdapter{name: "live", results: []Result{
		{Title: "x", URL: "https://x.example.com/", Domain: "x.example.com"},
	}}
	e := testEngine(time.Second, dead, live)
	resp := e.Search(context.Background(), Request{Query: "q"})
	if !resp.OK || len(resp.Results) != 1 {
		t.Fatalf("expected partial success, got %+v", resp)
	}
	if resp.Adapters[0].OK || resp.Adapters[0].Error == "" {
		t.Fatal("failed adapter not reported")
	}
}

func TestAdapterTimeoutIsolates(t *testing.T) {
	slow := &fakeAdapter{name: "slow", delay: 500 * time.Millisecond, results: []Result{
		{Title: "slow", URL: "https://slow.example.com/"},
	}}
	fast := &fakeAdapter{name: "fast", results: []Result{
		{Title: "fast", URL: "https://fast.example.com/"},
	}}
	e := testEngine(50*time.Millisecond, slow, fast)
	resp := e.Search(context.Background(), Request{Query: "q"})
	if len(resp.Results) != 1 || resp.Results[0].Domain != "fast.example.com" {
		t.Fatalf("timeout isolation failed: %+v", resp.Results)
	}
}

func TestMaxResultsBound(t *testing.T) {
	var rs []Result
	for i := 0; i < 30; i++ {
		rs = append(rs, Result{Title: "t", URL: "https://e.com/" + string(rune('a'+i))})
	}
	e := testEngine(time.Second, &fakeAdapter{name: "a", results: rs})
	resp := e.Search(context.Background(), Request{Query: "q", MaxResults: 100})
	if len(resp.Results) > 20 {
		t.Fatalf("result bound broken: %d", len(resp.Results))
	}
}

func TestSitesRestriction(t *testing.T) {
	a := &fakeAdapter{name: "a", results: []Result{
		{Title: "keep", URL: "https://zh.wikipedia.org/wiki/x"},
		{Title: "drop", URL: "https://other.com/x"},
	}}
	e := testEngine(time.Second, a)
	resp := e.Search(context.Background(), Request{Query: "q", Sites: []string{"wikipedia.org"}})
	if len(resp.Results) != 1 || resp.Results[0].Domain != "zh.wikipedia.org" {
		t.Fatalf("sites filter failed: %+v", resp.Results)
	}
}

func TestAllowlistTransportRejects(t *testing.T) {
	c := NewClient(time.Second)
	for _, host := range []string{"evil.com", "127.0.0.1", "metadata.google.internal"} {
		_, err := c.Get("http://" + host + "/")
		if err == nil {
			t.Fatalf("host %s was not blocked", host)
		}
	}
}

func TestEmptyResultsOK(t *testing.T) {
	e := testEngine(time.Second, &fakeAdapter{name: "a"})
	resp := e.Search(context.Background(), Request{Query: "q"})
	if !resp.OK || len(resp.Results) != 0 {
		t.Fatalf("empty result should be OK=true, got %+v", resp)
	}
}
