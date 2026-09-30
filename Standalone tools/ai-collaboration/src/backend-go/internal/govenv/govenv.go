// Package govenv implements the governed environment contract for the
// ai-collaboration Go host — parity with GovernedEnvironment.cs /
// GovernedToolRuntime: every failure is fail-closed PERMISSION_DENIED.
// The host never reads or forwards GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP
// beyond inheriting it into the transport-proxy sidecar (E4 boundary).
package govenv

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// ErrPermissionDenied mirrors PermissionDeniedException — the only
// failure shape the governed boundary is allowed to surface.
var ErrPermissionDenied = errors.New("PERMISSION_DENIED")

var (
	tokenPattern  = regexp.MustCompile(`^[a-f0-9]{64}$`)
	toolIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{1,63}$`)
)

// Environment is the resolved governed environment for this process.
type Environment struct {
	ToolID             string
	ProjectRoot        string
	ToolRoot           string
	ToolDataRoot       string
	SessionToken       string
	Port               int
	ShutdownToken      string
	SidecarExecutable  string // optional: deferred transport when empty
	WorkspaceInstance_ string
}

// WorkspaceInstanceID returns sha256(normalized project root)[:24] —
// parity with ipcSession.ts / workspace_instance_id().
func (e *Environment) WorkspaceInstanceID() string {
	if e.WorkspaceInstance_ != "" {
		return e.WorkspaceInstance_
	}
	normalized := strings.ReplaceAll(e.ProjectRoot, "\\", "/")
	normalized = strings.ToLower(normalized) // normcase on Windows
	sum := sha256.Sum256([]byte(normalized))
	e.WorkspaceInstance_ = hex.EncodeToString(sum[:])[:24]
	return e.WorkspaceInstance_
}

// Load resolves and validates the governed environment. Any deviation
// fails closed with ErrPermissionDenied.
func Load() (*Environment, error) {
	rawRoot := strings.TrimSpace(os.Getenv("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"))
	if rawRoot == "" {
		return nil, ErrPermissionDenied
	}
	root, err := filepath.Abs(rawRoot)
	if err != nil || !dirExists(root) {
		return nil, ErrPermissionDenied
	}

	toolID := strings.TrimSpace(os.Getenv("GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID"))
	if toolID == "" {
		toolID = strings.TrimSpace(os.Getenv("GPTBRIDGE_TOOL_ID"))
	}
	if !toolIDPattern.MatchString(toolID) || toolID == "main-system" {
		return nil, ErrPermissionDenied
	}

	rawToolDir := strings.TrimSpace(os.Getenv("GPTBRIDGE_TOOL_DIR"))
	toolRoot := filepath.Join(root, toolID)
	if rawToolDir != "" {
		toolRoot, err = filepath.Abs(rawToolDir)
		if err != nil {
			return nil, ErrPermissionDenied
		}
	}
	// tool_root must be inside the project root.
	rel, err := filepath.Rel(root, toolRoot)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
		return nil, ErrPermissionDenied
	}
	// manifest.json must exist and id must match.
	manifestPath := filepath.Join(toolRoot, "manifest.json")
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		return nil, ErrPermissionDenied
	}
	var manifest struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil || manifest.ID != toolID {
		return nil, ErrPermissionDenied
	}

	token := strings.ToLower(strings.TrimSpace(os.Getenv("GPTBRIDGE_IPC_SESSION_TOKEN")))
	if !tokenPattern.MatchString(token) {
		return nil, ErrPermissionDenied
	}
	port, err := strconv.Atoi(strings.TrimSpace(os.Getenv("GPTBRIDGE_IPC_PORT")))
	if err != nil || port < 1024 || port > 65535 {
		return nil, ErrPermissionDenied
	}
	shutdownToken := strings.TrimSpace(os.Getenv("GPTBRIDGE_SHUTDOWN_TOKEN"))
	dataRoot := strings.TrimSpace(os.Getenv("GPTBRIDGE_TOOL_DATA_ROOT"))
	if dataRoot == "" {
		dataRoot = filepath.Join(toolRoot, "runtime")
	}

	// Sidecar (star-governed-transport-proxy/v1) is optional: when the
	// entry is absent the governed claim channel is deferred and the
	// WS command surface remains the only request path.
	var sidecar string
	if rawEntry := strings.TrimSpace(os.Getenv("GPTBRIDGE_TOOLHOST_PROXY_ENTRY")); rawEntry != "" {
		candidate := rawEntry
		if !filepath.IsAbs(candidate) {
			candidate = filepath.Join(root, candidate)
		}
		candidate, err = filepath.Abs(candidate)
		if err != nil {
			return nil, ErrPermissionDenied
		}
		srel, serr := filepath.Rel(root, candidate)
		if serr != nil || srel == ".." || strings.HasPrefix(srel, ".."+string(filepath.Separator)) ||
			filepath.IsAbs(srel) || !strings.EqualFold(filepath.Ext(candidate), ".exe") ||
			!fileExists(candidate) {
			return nil, ErrPermissionDenied
		}
		sidecar = candidate
	}

	env := &Environment{
		ToolID:            toolID,
		ProjectRoot:       root,
		ToolRoot:          toolRoot,
		ToolDataRoot:      dataRoot,
		SessionToken:      token,
		Port:              port,
		ShutdownToken:     shutdownToken,
		SidecarExecutable: sidecar,
	}
	env.WorkspaceInstanceID()
	return env, nil
}

func dirExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

// ManifestVersion reads the tool manifest version with fallback 1.0.0 —
// parity with component_version("ai-collaboration").
func ManifestVersion(toolRoot string) string {
	raw, err := os.ReadFile(filepath.Join(toolRoot, "manifest.json"))
	if err != nil {
		return "1.0.0"
	}
	var m struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal(raw, &m); err != nil || strings.TrimSpace(m.Version) == "" {
		return "1.0.0"
	}
	return strings.TrimSpace(m.Version)
}

var _ = fmt.Sprintf
