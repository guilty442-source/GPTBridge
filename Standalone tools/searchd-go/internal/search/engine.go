package search

import (
	"context"
	"net/http"
	"sort"
	"sync"
	"time"
)

// rrfK is the reciprocal-rank-fusion constant; deterministic merge order.
const rrfK = 60.0

// Engine fans a query out to every adapter concurrently, then deduplicates
// and re-ranks deterministically.
type Engine struct {
	adapters []Adapter
	client   *http.Client
	timeout  time.Duration
}

func NewEngine(timeout time.Duration, adapters []Adapter) *Engine {
	if adapters == nil {
		adapters = defaultAdapters()
	}
	return &Engine{
		adapters: adapters,
		client:   NewClient(timeout),
		timeout:  timeout,
	}
}

type adapterOutcome struct {
	name    string
	results []Result
	err     error
	latency time.Duration
}

// Search executes one governed query. Individual adapter failure degrades
// to partial results; it never fails the whole request.
func (e *Engine) Search(ctx context.Context, req Request) Response {
	req.Query = sanitizeQuery(req.Query)
	resp := Response{
		OK:        true,
		Contract:  ContractVersion,
		Engine:    "searchd",
		RequestID: req.RequestID,
		Query:     req.Query,
		Results:   []Result{},
		Adapters:  []AdapterStatus{},
	}
	if req.Query == "" {
		resp.OK = false
		resp.Error = "QUERY_REQUIRED"
		return resp
	}
	maxResults := req.MaxResults
	if maxResults <= 0 {
		maxResults = 10
	}
	if maxResults > 20 {
		maxResults = 20
	}

	outcomes := make([]adapterOutcome, len(e.adapters))
	var wg sync.WaitGroup
	for i, a := range e.adapters {
		wg.Add(1)
		go func(i int, a Adapter) {
			defer wg.Done()
			start := time.Now()
			cctx, cancel := context.WithTimeout(ctx, e.timeout)
			defer cancel()
			res, err := a.Search(cctx, req, e.client)
			outcomes[i] = adapterOutcome{a.Name(), res, err, time.Since(start)}
		}(i, a)
	}
	wg.Wait()

	// Reciprocal-rank fusion merge with URL dedup.
	type merged struct {
		Result
		score float64
	}
	byURL := map[string]*merged{}
	order := []string{}
	for _, o := range outcomes {
		status := AdapterStatus{Name: o.name, OK: o.err == nil, Count: len(o.results), LatencyMs: o.latency.Milliseconds()}
		if o.err != nil {
			status.Error = o.err.Error()
		}
		resp.Adapters = append(resp.Adapters, status)
		for _, r := range o.results {
			if !validResultURL(r.URL) {
				continue
			}
			key := normalizeURL(r.URL)
			if key == "" {
				continue
			}
			if len(req.Sites) > 0 && !domainMatch(domainOf(r.URL), req.Sites) {
				continue
			}
			if m, ok := byURL[key]; ok {
				m.score += 1.0 / (rrfK + float64(r.Rank) + 1.0)
				if len(r.Snippet) > len(m.Snippet) {
					m.Snippet = r.Snippet
				}
				if m.Title == "" {
					m.Title = r.Title
				}
				continue
			}
			m := &merged{Result: r, score: 1.0 / (rrfK + float64(r.Rank) + 1.0)}
			m.Domain = domainOf(r.URL)
			byURL[key] = m
			order = append(order, key)
		}
	}

	ranked := make([]*merged, 0, len(order))
	for _, k := range order {
		ranked = append(ranked, byURL[k])
	}
	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].score != ranked[j].score {
			return ranked[i].score > ranked[j].score
		}
		return ranked[i].URL < ranked[j].URL
	})
	for i, m := range ranked {
		if i >= maxResults {
			break
		}
		m.Rank = i
		m.Score = m.score
		resp.Results = append(resp.Results, m.Result)
	}
	return resp
}
