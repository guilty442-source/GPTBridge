package search

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// DuckDuckGoLite scrapes the public lite endpoint. Parsing is deliberately
// tolerant: if markup drifts the adapter returns empty results rather than
// garbage, and upstream failure never fails the whole query.
type DuckDuckGoLite struct {
	BaseURL string
}

func (*DuckDuckGoLite) Name() string { return "duckduckgo" }

// findAttr extracts the named attribute (single or double quoted) from a tag.
func findAttr(tag, name string) string {
	for _, quote := range []byte{'"', '\''} {
		marker := name + "=" + string(quote)
		i := strings.Index(tag, marker)
		if i < 0 {
			continue
		}
		rest := tag[i+len(marker):]
		j := strings.IndexByte(rest, quote)
		if j > 0 {
			return rest[:j]
		}
	}
	return ""
}

// ddgDecodeLink resolves the //duckduckgo.com/l/?uddg=... redirect wrapper
// to the real destination URL.
func ddgDecodeLink(href string) string {
	if strings.HasPrefix(href, "//") {
		href = "https:" + href
	}
	u, err := url.Parse(href)
	if err != nil {
		return ""
	}
	if target := u.Query().Get("uddg"); target != "" {
		return target
	}
	if strings.HasPrefix(href, "http") {
		return href
	}
	return ""
}

func parseDDGLite(body string, limit int) []Result {
	var links []struct{ href, text string }
	var snippets []string

	rest := body
	for {
		i := strings.Index(rest, "<a ")
		if i < 0 {
			break
		}
		rest = rest[i:]
		j := strings.Index(rest, "</a>")
		if j < 0 {
			break
		}
		seg := rest[:j+4]
		rest = rest[j+4:]
		tagEnd := strings.Index(seg, ">")
		if tagEnd < 0 {
			continue
		}
		tag := seg[:tagEnd+1]
		cls := findAttr(tag, "class")
		if !strings.Contains(cls, "result-link") {
			continue
		}
		href := findAttr(tag, "href")
		text := seg[tagEnd+1 : len(seg)-4]
		links = append(links, struct{ href, text string }{href, text})
	}

	rest = body
	for {
		i := strings.Index(rest, "result-snippet")
		if i < 0 {
			break
		}
		rest = rest[i:]
		gt := strings.Index(rest, ">")
		if gt < 0 {
			break
		}
		rest = rest[gt+1:]
		end := strings.Index(rest, "</td>")
		if end < 0 {
			break
		}
		snippets = append(snippets, rest[:end])
		rest = rest[end+5:]
	}

	results := make([]Result, 0, len(links))
	for i, l := range links {
		if len(results) >= limit {
			break
		}
		link := ddgDecodeLink(l.href)
		if link == "" || !validResultURL(link) {
			continue
		}
		snippet := ""
		if i < len(snippets) {
			snippet = snippets[i]
		}
		results = append(results, Result{
			Title:   cleanText(l.text, maxTitleLen),
			URL:     link,
			Domain:  domainOf(link),
			Snippet: cleanText(snippet, maxSnippetLen),
			Engine:  "duckduckgo",
			Rank:    len(results),
		})
	}
	return results
}

func (d *DuckDuckGoLite) Search(ctx context.Context, req Request, client *http.Client) ([]Result, error) {
	limit := req.MaxResults
	if limit <= 0 {
		limit = 10
	}
	base := d.BaseURL
	if base == "" {
		base = "https://lite.duckduckgo.com/lite/"
	}
	sep := "?"
	if strings.Contains(base, "?") {
		sep = "&"
	}
	u := base + sep + "q=" + url.QueryEscape(req.Query)
	hreq, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
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
		return nil, fmt.Errorf("duckduckgo status %d", resp.StatusCode)
	}
	body, err := readBody(resp.Body)
	if err != nil {
		return nil, err
	}
	return parseDDGLite(string(body), limit), nil
}
