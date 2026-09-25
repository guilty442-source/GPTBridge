package search

// xingcheng-searchd/v1 contract types. The versioned wire contract is the
// only boundary between this go-service and the governed Python caller;
// neither side may import the other's internals (DependencyPolicy:
// go-service -> channel-api only).

const ContractVersion = "xingcheng-searchd/v1"

// Request is the inbound governed search request posted to /v1/search.
type Request struct {
	RequestID  string   `json:"request_id,omitempty"`
	Query      string   `json:"query"`
	Language   string   `json:"language,omitempty"`
	MaxResults int      `json:"max_results,omitempty"`
	TimeRange  string   `json:"time_range,omitempty"`
	Sites      []string `json:"sites,omitempty"`
	SafeSearch *bool    `json:"safesearch,omitempty"`
}

// Result is one normalized, citation-compatible metadata record. Snippets
// are bounded; full page contents are never returned.
type Result struct {
	Title     string  `json:"title"`
	URL       string  `json:"url"`
	Domain    string  `json:"domain"`
	Snippet   string  `json:"snippet"`
	Published string  `json:"published,omitempty"`
	Engine    string  `json:"engine"`
	Rank      int     `json:"rank"`
	Score     float64 `json:"score"`
}

// AdapterStatus reports per-upstream health for audit/degradation reasons.
type AdapterStatus struct {
	Name      string `json:"name"`
	OK        bool   `json:"ok"`
	Count     int    `json:"count"`
	LatencyMs int64  `json:"latency_ms"`
	Error     string `json:"error,omitempty"`
}

// Response is the /v1/search reply envelope.
type Response struct {
	OK        bool            `json:"ok"`
	Contract  string          `json:"contract"`
	Engine    string          `json:"engine"`
	RequestID string          `json:"request_id,omitempty"`
	Query     string          `json:"query"`
	Results   []Result        `json:"results"`
	Adapters  []AdapterStatus `json:"adapters"`
	Error     string          `json:"error,omitempty"`
}
