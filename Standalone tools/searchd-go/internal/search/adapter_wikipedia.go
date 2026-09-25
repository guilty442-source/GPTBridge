package search

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// Wikipedia uses the MediaWiki opensearch API — clean JSON, credential-free.
// Language picks the wiki host (zh-TW/zh -> zh, en -> en, ja -> ja).
type Wikipedia struct {
	// BaseURL overrides the production endpoint; tests only — the
	// transport allowlist still guards every outbound host.
	BaseURL string
}

func (*Wikipedia) Name() string { return "wikipedia" }

func wikiHost(language string) string {
	l := strings.ToLower(language)
	switch {
	case strings.HasPrefix(l, "zh"):
		return "zh.wikipedia.org"
	case strings.HasPrefix(l, "ja"):
		return "ja.wikipedia.org"
	case strings.HasPrefix(l, "en"):
		return "en.wikipedia.org"
	default:
		return "zh.wikipedia.org"
	}
}

func (w *Wikipedia) Search(ctx context.Context, req Request, client *http.Client) ([]Result, error) {
	limit := req.MaxResults
	if limit <= 0 {
		limit = 10
	}
	base := w.BaseURL
	if base == "" {
		base = "https://" + wikiHost(req.Language) + "/w/api.php"
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	u := *parsed
	q := u.Query()
	q.Set("action", "opensearch")
	q.Set("format", "json")
	q.Set("limit", fmt.Sprint(limit))
	q.Set("namespace", "0")
	q.Set("redirects", "resolve")
	q.Set("search", req.Query)
	u.RawQuery = q.Encode()

	hreq, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	hreq.Header.Set("User-Agent", "XingCheng-Searchd/1.0")
	resp, err := client.Do(hreq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("wikipedia status %d", resp.StatusCode)
	}
	body, err := readBody(resp.Body)
	if err != nil {
		return nil, err
	}
	// opensearch reply: [term, [titles], [descriptions], [urls]]
	var raw []json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil || len(raw) < 4 {
		return nil, fmt.Errorf("wikipedia malformed response")
	}
	var titles, descs, urls []string
	if err := json.Unmarshal(raw[1], &titles); err != nil {
		return nil, err
	}
	_ = json.Unmarshal(raw[2], &descs)
	_ = json.Unmarshal(raw[3], &urls)

	results := make([]Result, 0, len(titles))
	for i, title := range titles {
		var link, desc string
		if i < len(urls) {
			link = urls[i]
		}
		if i < len(descs) {
			desc = descs[i]
		}
		if link == "" || !validResultURL(link) {
			continue
		}
		results = append(results, Result{
			Title:   cleanText(title, maxTitleLen),
			URL:     link,
			Domain:  domainOf(link),
			Snippet: cleanText(desc, maxSnippetLen),
			Engine:  "wikipedia",
			Rank:    i,
		})
	}
	return results, nil
}
