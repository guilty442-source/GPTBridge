package search

import (
	"html"
	"net/url"
	"strings"
)

const (
	maxQueryLen   = 400
	maxTitleLen   = 200
	maxSnippetLen = 300
)

// cleanText strips tags, decodes entities, collapses whitespace and bounds
// length so upstream markup cannot leak into evidence/citations.
func cleanText(s string, max int) string {
	var b strings.Builder
	inTag := false
	for _, r := range s {
		switch {
		case r == '<':
			inTag = true
		case r == '>':
			inTag = false
		case !inTag:
			b.WriteRune(r)
		}
	}
	out := html.UnescapeString(b.String())
	out = strings.Join(strings.Fields(out), " ")
	if len(out) > max {
		out = strings.TrimSpace(out[:max])
	}
	return out
}

// normalizeURL lowercases scheme+host, strips www. and common tracking
// parameters so cross-adapter duplicates collapse to one key.
func normalizeURL(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	u.Scheme = strings.ToLower(u.Scheme)
	u.Host = strings.ToLower(u.Host)
	u.Host = strings.TrimPrefix(u.Host, "www.")
	q := u.Query()
	for k := range q {
		lk := strings.ToLower(k)
		if strings.HasPrefix(lk, "utm_") || lk == "fbclid" || lk == "gclid" {
			q.Del(k)
		}
	}
	u.RawQuery = q.Encode()
	u.Fragment = ""
	u.Path = strings.TrimSuffix(u.Path, "/")
	return u.String()
}

func domainOf(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	return strings.TrimPrefix(strings.ToLower(u.Hostname()), "www.")
}

// validResultURL restricts output to fetchable web URLs; anything else
// (javascript:, data:, empty host) is dropped fail-closed.
func validResultURL(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Hostname() == "" {
		return false
	}
	return u.Scheme == "http" || u.Scheme == "https"
}

// domainMatch reports whether host sits under any listed domain.
func domainMatch(host string, domains []string) bool {
	host = strings.ToLower(strings.TrimPrefix(host, "www."))
	for _, d := range domains {
		d = strings.ToLower(strings.TrimSpace(strings.TrimPrefix(d, "www.")))
		if d == "" {
			continue
		}
		if host == d || strings.HasSuffix(host, "."+d) {
			return true
		}
	}
	return false
}

func sanitizeQuery(q string) string {
	q = strings.TrimSpace(strings.Join(strings.Fields(q), " "))
	if len(q) > maxQueryLen {
		q = q[:maxQueryLen]
	}
	return q
}
