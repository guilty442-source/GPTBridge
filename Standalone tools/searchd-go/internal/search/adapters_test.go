package search

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Adapter tests run the real Search() path against httptest fixtures
// (BaseURL injection); the production endpoints stay compiled-in.

func TestWikipediaAdapterEndToEnd(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("action") != "opensearch" {
			t.Errorf("unexpected query: %s", r.URL.RawQuery)
		}
		w.Write([]byte(`["term",["T1","T2"],["desc one","desc two"],["https://zh.wikipedia.org/wiki/A","https://zh.wikipedia.org/wiki/B"]]`))
	}))
	defer srv.Close()

	rs, err := (&Wikipedia{BaseURL: srv.URL}).Search(
		context.Background(), Request{Query: "x", Language: "zh-TW", MaxResults: 5}, srv.Client())
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if len(rs) != 2 || rs[0].Title != "T1" || rs[0].Snippet != "desc one" || rs[1].Domain != "zh.wikipedia.org" {
		t.Fatalf("bad normalization: %+v", rs)
	}
}

func TestBingAdapterEndToEnd(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`<?xml version="1.0"?><rss version="2.0"><channel>
			<item><title>T1</title><link>https://a.com/x</link><description>d &lt;b&gt;1&lt;/b&gt;</description><pubDate>Mon, 01 Jan 2024</pubDate></item>
			<item><title>T2</title><link>javascript:alert(1)</link><description>bad</description></item>
		</channel></rss>`))
	}))
	defer srv.Close()

	rs, err := (&BingRSS{BaseURL: srv.URL}).Search(
		context.Background(), Request{Query: "x", MaxResults: 5}, srv.Client())
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if len(rs) != 1 {
		t.Fatalf("expected javascript: URL dropped, got %d", len(rs))
	}
	if rs[0].Published == "" || rs[0].Snippet != "d 1" {
		t.Fatalf("bad fields: %+v", rs[0])
	}
}

func TestWikiHostSelection(t *testing.T) {
	cases := map[string]string{
		"zh-TW": "zh.wikipedia.org",
		"en-US": "en.wikipedia.org",
		"ja":    "ja.wikipedia.org",
		"de":    "zh.wikipedia.org",
	}
	for lang, want := range cases {
		if got := wikiHost(lang); got != want {
			t.Errorf("wikiHost(%q)=%q want %q", lang, got, want)
		}
	}
}

func TestDDGLiteParse(t *testing.T) {
	body := `<html><body><table>
	<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1&rut=abc" class='result-link'>Example &amp; Page</a></td></tr>
	<tr><td class="result-snippet">A snippet &lt;here&gt;</td></tr>
	<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsecond.org%2F&rut=def" class="result-link">Second</a></td></tr>
	<tr><td class="result-snippet">Second snippet</td></tr>
	</table></body></html>`
	rs := parseDDGLite(body, 10)
	if len(rs) != 2 {
		t.Fatalf("expected 2 results, got %d", len(rs))
	}
	if rs[0].URL != "https://example.com/page?a=1" {
		t.Fatalf("uddg decode failed: %s", rs[0].URL)
	}
	if rs[0].Title != "Example & Page" {
		t.Fatalf("title unescape failed: %q", rs[0].Title)
	}
	if rs[0].Snippet != "A snippet <here>" {
		t.Fatalf("snippet clean failed: %q", rs[0].Snippet)
	}
	if rs[1].Domain != "second.org" {
		t.Fatalf("domain extract failed: %q", rs[1].Domain)
	}
}

func TestDDGLiteDriftedMarkupFailsEmpty(t *testing.T) {
	if rs := parseDDGLite("<html>totally different markup</html>", 10); len(rs) != 0 {
		t.Fatal("drifted markup should yield zero results, not garbage")
	}
}

func TestDDGLiteAdapterEndToEnd(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("q") == "" {
			t.Error("missing q param")
		}
		w.Write([]byte(`<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F" class="result-link">X</a><td class="result-snippet">s</td>`))
	}))
	defer srv.Close()

	rs, err := (&DuckDuckGoLite{BaseURL: srv.URL}).Search(
		context.Background(), Request{Query: "x"}, srv.Client())
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if len(rs) != 1 || rs[0].URL != "https://x.com/" {
		t.Fatalf("bad result: %+v", rs)
	}
}

func TestUpstreamStatusError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer srv.Close()
	if _, err := (&BingRSS{BaseURL: srv.URL}).Search(
		context.Background(), Request{Query: "x"}, srv.Client()); err == nil ||
		!strings.Contains(err.Error(), "502") {
		t.Fatalf("expected status error, got %v", err)
	}
}

func TestOversizedBodyRejected(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Write(make([]byte, maxUpstreamBody+10))
	}))
	defer srv.Close()
	rs, err := (&BingRSS{BaseURL: srv.URL}).Search(
		context.Background(), Request{Query: "x"}, srv.Client())
	// capped body is not valid RSS -> must surface as error, never OOM/accept
	if err == nil && len(rs) > 0 {
		t.Fatal("oversized body should not produce results")
	}
	_ = time.Second
}
