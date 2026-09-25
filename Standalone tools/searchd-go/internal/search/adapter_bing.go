package search

import (
	"context"
	"encoding/xml"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// BingRSS uses the public RSS view of Bing search — credential-free XML.
type BingRSS struct {
	BaseURL string
}

func (*BingRSS) Name() string { return "bing" }

type rssEnvelope struct {
	Channel struct {
		Items []struct {
			Title       string `xml:"title"`
			Link        string `xml:"link"`
			Description string `xml:"description"`
			PubDate     string `xml:"pubDate"`
		} `xml:"item"`
	} `xml:"channel"`
}

func (b *BingRSS) Search(ctx context.Context, req Request, client *http.Client) ([]Result, error) {
	count := req.MaxResults
	if count <= 0 {
		count = 10
	}
	base := b.BaseURL
	if base == "" {
		base = "https://www.bing.com/search"
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	u := *parsed
	q := u.Query()
	q.Set("q", req.Query)
	q.Set("format", "rss")
	q.Set("count", fmt.Sprint(count))
	if req.Language != "" {
		q.Set("setlang", strings.ToLower(req.Language))
	}
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
		return nil, fmt.Errorf("bing status %d", resp.StatusCode)
	}
	body, err := readBody(resp.Body)
	if err != nil {
		return nil, err
	}
	var env rssEnvelope
	if err := xml.Unmarshal(body, &env); err != nil {
		return nil, fmt.Errorf("bing malformed rss: %w", err)
	}
	items := env.Channel.Items
	if len(items) > count {
		items = items[:count]
	}
	results := make([]Result, 0, len(items))
	for i, it := range items {
		link := strings.TrimSpace(it.Link)
		if link == "" || !validResultURL(link) {
			continue
		}
		results = append(results, Result{
			Title:     cleanText(it.Title, maxTitleLen),
			URL:       link,
			Domain:    domainOf(link),
			Snippet:   cleanText(it.Description, maxSnippetLen),
			Published: strings.TrimSpace(it.PubDate),
			Engine:    "bing",
			Rank:      i,
		})
	}
	return results, nil
}
